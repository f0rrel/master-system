"""The repository is checked and restored around every worker run.

A worker shares refs and the common git directory with the dedicated clone. What it
changes there is put back before the snapshot; anything beyond its own branch, the
stash, remote-tracking refs and its git identity fails the attempt as tampering.
"""

import subprocess
from pathlib import Path

from conftest import git
from core.execution import ExecutionResult
from core.history import EventType
from test_attempt_workspace import Recorder, payload, run_once


def common_dir(path) -> Path:
    return Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir"))


class Does:
    """A worker that runs a function in its worktree."""

    def __init__(self, action):
        self.action = action

    def execute(self, task, context, *, workspace):
        self.action(workspace.path)
        return ExecutionResult(status="success", reason="done")


def edit_app(path):
    (path / "app.py").write_text("def greet(name):\n    return 'Hello ' + name\n")


def tampered(history):
    verification = payload(history, EventType.VERIFICATION)
    return [f for f in verification["findings"] if f["kind"] == "repository_tampered"]


def test_moving_the_base_branch_from_the_worktree_is_undone_and_fails(tmp_path):
    def move_main(path):
        edit_app(path)
        git(path, "commit", "-qam", "sneak")
        git(path, "update-ref", "refs/heads/main", "HEAD")

    verifier = Recorder()
    result, history, repo, verifier, _ = run_once(tmp_path, Does(move_main), verifier)

    started = payload(history, EventType.ATTEMPT_STARTED)
    verification = payload(history, EventType.VERIFICATION)
    assert git(repo, "rev-parse", "main") == started["base_sha"]
    assert verification["verdict"] == "fail"
    assert tampered(history)[0]["changed"] == ["refs/heads/main"]
    assert verifier.calls == []  # acceptance commands never ran
    assert payload(history, EventType.ATTEMPT_FINISHED)["repository_tampered"] == [
        "refs/heads/main"]


def test_a_tag_or_a_new_branch_is_removed_and_fails(tmp_path):
    def add_refs(path):
        edit_app(path)
        git(path, "tag", "v9.9")
        git(path, "branch", "elsewhere")

    result, history, repo, _, _ = run_once(tmp_path, Does(add_refs))

    assert git(repo, "tag", "-l") == ""
    assert "elsewhere" not in git(repo, "branch", "--list")
    assert tampered(history)[0]["changed"] == ["refs/heads/elsewhere", "refs/tags/v9.9"]


def test_a_new_hook_is_removed_and_fails(tmp_path):
    def plant_hook(path):
        edit_app(path)
        hook = common_dir(path) / "hooks" / "post-commit"
        hook.write_text("#!/bin/sh\necho owned\n")
        hook.chmod(0o755)

    result, history, repo, _, _ = run_once(tmp_path, Does(plant_hook))

    assert not (common_dir(repo) / "hooks" / "post-commit").exists()
    assert payload(history, EventType.VERIFICATION)["verdict"] == "fail"
    assert tampered(history)[0]["changed"] == ["hooks/post-commit"]


def test_a_filter_driver_is_removed_before_the_snapshot_so_it_never_runs(tmp_path):
    marker = tmp_path / "filter-ran"

    def add_filter(path):
        edit_app(path)
        (path / ".gitattributes").write_text("*.txt filter=evil\n")
        (path / "notes.txt").write_text("hello\n")
        git(path, "config", "filter.evil.clean", f"touch {marker}; cat")

    result, history, repo, _, _ = run_once(tmp_path, Does(add_filter))

    assert not marker.exists()
    assert "filter.evil" not in (common_dir(repo) / "config").read_text()
    assert payload(history, EventType.VERIFICATION)["verdict"] == "fail"
    assert tampered(history)[0]["changed"] == ["config: filter.evil.clean"]


def test_an_exclude_entry_is_removed_and_fails(tmp_path):
    def exclude(path):
        edit_app(path)
        with open(common_dir(path) / "info" / "exclude", "a") as handle:
            handle.write("conftest.py\n")

    result, history, repo, _, _ = run_once(tmp_path, Does(exclude))

    assert "conftest.py" not in (common_dir(repo) / "info" / "exclude").read_text()
    assert tampered(history)[0]["changed"] == ["info/exclude"]


def test_setting_the_git_identity_is_undone_without_a_failure(tmp_path):
    def identity(path):
        edit_app(path)
        git(path, "config", "user.email", "worker@example.invalid")
        git(path, "config", "user.name", "worker")

    result, history, repo, verifier, _ = run_once(tmp_path, Does(identity))

    assert "worker@example.invalid" not in (common_dir(repo) / "config").read_text()
    assert payload(history, EventType.VERIFICATION)["verdict"] == "pass"
    assert len(verifier.calls) == 1
    assert "repository_tampered" not in payload(history, EventType.ATTEMPT_FINISHED)


def test_a_stash_is_dropped_without_a_failure(tmp_path):
    def stash(path):
        (path / "scratch.txt").write_text("later\n")
        git(path, "stash", "push", "-u", "-q")
        edit_app(path)

    result, history, repo, verifier, _ = run_once(tmp_path, Does(stash))

    done = subprocess.run(["git", "rev-parse", "--verify", "-q", "refs/stash"], cwd=repo)
    assert done.returncode != 0
    assert payload(history, EventType.VERIFICATION)["verdict"] == "pass"


def test_commits_on_the_attempts_own_branch_are_kept(tmp_path):
    def commit(path):
        edit_app(path)
        git(path, "commit", "-qam", "my own work")

    result, history, repo, verifier, _ = run_once(tmp_path, Does(commit))

    finished = payload(history, EventType.ATTEMPT_FINISHED)
    assert finished["files_changed"] == ["app.py"]
    assert payload(history, EventType.VERIFICATION)["verdict"] == "pass"
    assert "repository_tampered" not in finished


def test_tampering_without_any_file_change_is_a_failure_not_a_stall(tmp_path):
    def only_move_main(path):
        git(path, "commit", "-q", "--allow-empty", "-m", "nothing")
        git(path, "update-ref", "refs/heads/main", "HEAD")

    result, history, repo, _, _ = run_once(tmp_path, Does(only_move_main))

    assert payload(history, EventType.ATTEMPT_FINISHED)["outcome"] == "finished"
    assert payload(history, EventType.VERIFICATION)["verdict"] == "fail"


def test_a_tampered_attempt_counts_as_a_failed_attempt_and_is_reported(tmp_path):
    from core.daemon import failed_attempts_since_human
    from core.report import build_report, render_report

    def move_main(path):
        edit_app(path)
        git(path, "commit", "-qam", "sneak")
        git(path, "update-ref", "refs/heads/main", "HEAD")

    result, history, repo, _, _ = run_once(tmp_path, Does(move_main))

    assert failed_attempts_since_human(history, "alpha", "t1") == 1
    session_id = history.events(types=[EventType.ATTEMPT_STARTED])[-1].session_id
    text = render_report(build_report(history, session_id))
    assert "REPOSITORY TAMPERED" in text and "refs/heads/main" in text


def test_the_tip_of_a_restored_ref_is_recorded_so_the_commit_can_be_recovered(tmp_path):
    def move_main(path):
        edit_app(path)
        git(path, "commit", "-qam", "sneak")
        git(path, "update-ref", "refs/heads/main", "HEAD")

    result, history, repo, _, _ = run_once(tmp_path, Does(move_main))

    found = payload(history, EventType.ATTEMPT_FINISHED)["restored_refs"]
    assert git(repo, "log", "-1", "--format=%s", found["refs/heads/main"]) == "sneak"


# --- G1: control-plane git ignores the owner's global git config -------------------------


def test_a_filter_in_the_global_git_config_does_not_run_during_the_snapshot(
        tmp_path, monkeypatch):
    """A worker runs as the owner and can write ~/.gitconfig; a committed .gitattributes
    naming a filter defined there must not make the control plane run it."""
    marker = tmp_path / "global-filter-ran"
    global_config = tmp_path / "gitconfig"
    global_config.write_text(f'[filter "evil"]\n\tclean = "touch {marker}; cat"\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))

    def attributes(path):
        edit_app(path)
        (path / ".gitattributes").write_text("*.txt filter=evil\n")
        (path / "notes.txt").write_text("hello\n")

    result, history, repo, _, _ = run_once(tmp_path, Does(attributes))

    assert not marker.exists()
    assert "notes.txt" in payload(history, EventType.ATTEMPT_FINISHED)["files_changed"]


def test_control_plane_git_gets_a_minimal_environment_without_keys(monkeypatch):
    from core.workspace import control_git_env

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret")
    monkeypatch.setenv("GIT_DIR", "/elsewhere")
    env = control_git_env({"GIT_ASKPASS": "/askpass"})

    assert "DEEPSEEK_API_KEY" not in env and "GIT_DIR" not in env
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null" and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_ASKPASS"] == "/askpass"
    assert set(env) <= {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "GIT_TERMINAL_PROMPT",
                        "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_ASKPASS"}


def test_host_git_does_not_read_the_global_config(tmp_path, monkeypatch, git_repo):
    from core.host import git as host_git

    global_config = tmp_path / "gitconfig"
    global_config.write_text("[alias]\n\towned = status\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))

    with __import__("pytest").raises(RuntimeError):
        host_git(["owned"], git_repo)
