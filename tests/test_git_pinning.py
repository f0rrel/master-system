"""Control-plane git is pinned to the git directories the control plane created.

A worker runs as the owner and can write its worktree's admin directory
(``<common>/worktrees/<id>/``) and the worktree's ``.git`` file. Pointing either at a
git directory it built itself used to make the snapshot's ``git add -A`` read that
directory's config, where a ``clean`` filter runs worker-chosen code as the owner.
Control-plane git therefore gets ``GIT_DIR``, ``GIT_COMMON_DIR`` and ``GIT_WORK_TREE``
explicitly, from the common directory recorded when the worktree was created.
"""

import shutil
from pathlib import Path

import pytest

from conftest import git
from core.history import EventType
from core.workspace import (WorkspaceError, control_git_env, GitWorktrees,
                            control_git_argv)
from test_attempt_workspace import payload, run_once
from test_tamper_check import Does, common_dir, edit_app, tampered
from test_workspace import protected, worktrees  # noqa: F401  (fixtures)


def forged_common_dir(tmp_path, repo, marker) -> Path:
    """A copy of the repository's git dir whose config defines a clean filter."""
    fake = tmp_path / "forged-git"
    shutil.copytree(common_dir(repo), fake, ignore=shutil.ignore_patterns("worktrees"))
    with open(fake / "config", "a") as config:
        config.write(f'[filter "evil"]\n\tclean = "touch {marker}; cat"\n')
    return fake


def admin_dir(worktree: Path) -> Path:
    return Path(git(worktree, "rev-parse", "--absolute-git-dir"))


def attempt(worktrees, repo, project="p", attempt_id="a1"):
    base = git(repo, "rev-parse", "main")
    path = worktrees.path_for(project, attempt_id)
    worktrees.create(repo, path, attempt_id, base)
    return path, base


# --- the demonstrated attack: a redirected commondir -------------------------------------


def test_a_redirected_commondir_does_not_run_a_filter_during_the_snapshot(tmp_path):
    marker = tmp_path / "filter-ran"
    holder = {}

    def redirect(path):
        edit_app(path)
        fake = forged_common_dir(tmp_path, holder["repo"], marker)
        (admin_dir(path) / "commondir").write_text(str(fake) + "\n")
        (path / ".gitattributes").write_text("*.txt filter=evil\n")
        (path / "notes.txt").write_text("hello\n")

    class Redirects(Does):
        def execute(self, task, context, *, workspace):
            holder["repo"] = Path(
                git(workspace.path, "rev-parse", "--path-format=absolute",
                    "--git-common-dir")).parent
            return super().execute(task, context, workspace=workspace)

    result, history, repo, _, _ = run_once(tmp_path, Redirects(redirect))

    assert not marker.exists()
    assert "notes.txt" in payload(history, EventType.ATTEMPT_FINISHED)["files_changed"]


def test_a_redirected_dot_git_file_does_not_run_a_filter_during_the_snapshot(tmp_path):
    """The worktree's own ``.git`` file names its admin directory; it can be forged too."""
    marker = tmp_path / "filter-ran"
    holder = {}

    def redirect(path):
        edit_app(path)
        fake = forged_common_dir(tmp_path, holder["repo"], marker)
        (path / ".git").write_text(f"gitdir: {fake}\n")
        (path / ".gitattributes").write_text("*.txt filter=evil\n")
        (path / "notes.txt").write_text("hello\n")

    class Redirects(Does):
        def execute(self, task, context, *, workspace):
            holder["repo"] = Path(
                git(workspace.path, "rev-parse", "--path-format=absolute",
                    "--git-common-dir")).parent
            return super().execute(task, context, workspace=workspace)

    result, history, repo, _, _ = run_once(tmp_path, Redirects(redirect))

    assert not marker.exists()
    # The attempt still ends with a recorded outcome rather than an exception.
    assert payload(history, EventType.ATTEMPT_FINISHED)["outcome"]


def test_the_snapshot_still_commits_the_workers_changes_after_a_redirect(tmp_path):
    holder = {}

    def redirect(path):
        edit_app(path)
        fake = forged_common_dir(tmp_path, holder["repo"], tmp_path / "unused")
        (admin_dir(path) / "commondir").write_text(str(fake) + "\n")

    class Redirects(Does):
        def execute(self, task, context, *, workspace):
            holder["repo"] = Path(
                git(workspace.path, "rev-parse", "--path-format=absolute",
                    "--git-common-dir")).parent
            return super().execute(task, context, workspace=workspace)

    result, history, repo, _, _ = run_once(tmp_path, Redirects(redirect))

    finished = payload(history, EventType.ATTEMPT_FINISHED)
    assert "app.py" in finished["files_changed"]
    assert finished["result_sha"]


# --- the helpers themselves --------------------------------------------------------------


def test_control_git_env_pins_the_git_directories_of_a_created_worktree(
        git_repo, worktrees):
    path, _ = attempt(worktrees, git_repo)

    env = control_git_env(cwd=path)

    assert Path(env["GIT_WORK_TREE"]) == path.resolve()
    assert Path(env["GIT_COMMON_DIR"]) == common_dir(git_repo).resolve()
    assert Path(env["GIT_DIR"]) == admin_dir(path).resolve()
    assert Path(env["GIT_DIR"]).parent == Path(env["GIT_COMMON_DIR"]) / "worktrees"


def test_control_git_env_without_a_worktree_has_no_git_dir_variables(git_repo, tmp_path):
    for env in (control_git_env(), control_git_env(cwd=git_repo), control_git_env(cwd=tmp_path)):
        assert not {"GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE"} & set(env)


def test_a_pinned_directory_comes_from_creation_not_from_the_dot_git_file(
        git_repo, worktrees, tmp_path):
    path, _ = attempt(worktrees, git_repo)
    real = admin_dir(path).resolve()
    (path / ".git").write_text(f"gitdir: {tmp_path / 'elsewhere'}\n")

    with pytest.raises(WorkspaceError):
        control_git_env(cwd=path)

    (path / ".git").write_text(f"gitdir: {real}\n")
    assert Path(control_git_env(cwd=path)["GIT_DIR"]) == real


def test_an_admin_directory_that_belongs_to_another_worktree_is_refused(
        git_repo, worktrees):
    first, _ = attempt(worktrees, git_repo, attempt_id="a1")
    second, _ = attempt(worktrees, git_repo, attempt_id="a2")
    (second / ".git").write_text(f"gitdir: {admin_dir(first)}\n")

    with pytest.raises(WorkspaceError):
        control_git_env(cwd=second)


def test_a_forged_admin_directory_inside_the_real_common_dir_cannot_move_the_common_dir(
        git_repo, worktrees, tmp_path):
    """Even an admin directory the worker made under ``<common>/worktrees`` is read
    with the real common directory, because ``GIT_COMMON_DIR`` is set explicitly."""
    marker = tmp_path / "filter-ran"
    path, base = attempt(worktrees, git_repo)
    fake = forged_common_dir(tmp_path, git_repo, marker)
    (admin_dir(path) / "commondir").write_text(str(fake) + "\n")
    (path / ".gitattributes").write_text("*.txt filter=evil\n")
    (path / "notes.txt").write_text("x\n")

    facts = worktrees.snapshot(path, base, "a1")

    assert not marker.exists()
    assert "notes.txt" in facts["files_changed"]


def test_every_worktree_level_operation_is_pinned(git_repo, worktrees, tmp_path):
    marker = tmp_path / "filter-ran"
    path, base = attempt(worktrees, git_repo)
    fake = forged_common_dir(tmp_path, git_repo, marker)
    (admin_dir(path) / "commondir").write_text(str(fake) + "\n")
    (path / ".gitattributes").write_text("*.txt filter=evil\n")
    (path / "notes.txt").write_text("x\n")

    worktrees.observe(path, base)
    from core.workspace import has_tracked_changes
    has_tracked_changes(path)

    assert not marker.exists()


def test_observe_after_a_restart_is_anchored_on_the_recorded_repository(
        git_repo, worktrees, tmp_path):
    """A recovering process has not created the worktree; it is given the repository
    the attempt recorded."""
    from core import workspace as module

    marker = tmp_path / "filter-ran"
    path, base = attempt(worktrees, git_repo)
    fake = forged_common_dir(tmp_path, git_repo, marker)
    (admin_dir(path) / "commondir").write_text(str(fake) + "\n")
    (path / ".gitattributes").write_text("*.txt filter=evil\n")
    (path / "notes.txt").write_text("x\n")
    module._COMMON_DIRS.clear()  # a new process knows nothing

    facts = worktrees.observe(path, base, repository=git_repo)

    assert not marker.exists()
    assert facts["worktree_present"] is True and facts["untracked"] >= 1


def test_observe_without_an_anchor_does_not_run_git_on_unverified_metadata(
        git_repo, worktrees, tmp_path):
    from core import workspace as module

    marker = tmp_path / "filter-ran"
    path, base = attempt(worktrees, git_repo)
    fake = forged_common_dir(tmp_path, git_repo, marker)
    (admin_dir(path) / "commondir").write_text(str(fake) + "\n")
    (path / ".gitattributes").write_text("*.txt filter=evil\n")
    (path / "notes.txt").write_text("x\n")
    module._COMMON_DIRS.clear()

    facts = worktrees.observe(path, base)

    assert not marker.exists()
    assert facts["worktree_present"] is True
    assert "observe_error" in facts


# --- nothing that worked before stops working --------------------------------------------


def test_cherry_pick_and_commit_work_in_a_pinned_worktree(git_repo, worktrees):
    path, base = attempt(worktrees, git_repo)
    (path / "new.txt").write_text("n\n")
    facts = worktrees.snapshot(path, base, "a1", subject="t: title")

    other, _ = attempt(worktrees, git_repo, attempt_id="a2")
    picked = worktrees.cherry_pick(other, base, facts["result_sha"])

    assert picked and picked != base
    assert git(other, "show", "--name-only", "--format=", "HEAD") == "new.txt"


def test_the_pin_does_not_leak_into_other_commands(git_repo, worktrees):
    attempt(worktrees, git_repo)
    top = git(git_repo, "rev-parse", "--show-toplevel")
    assert Path(top).resolve() == Path(git_repo).resolve()
    assert control_git_argv("status")[0] == "git"
