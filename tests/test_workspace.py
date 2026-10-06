"""Git worktree workspaces and the isolation rule."""

import time
from pathlib import Path

import pytest

from conftest import git, make_repo
from core.workspace import (
    AttemptWorkspace,
    GitWorktrees,
    IsolationError,
    RepositoryError,
    check_isolated,
    control_plane_root,
)

ATTEMPT = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def protected(tmp_path):
    projects = tmp_path / "projects"
    state = tmp_path / "state"
    projects.mkdir()
    state.mkdir()
    return (projects, state, control_plane_root())


@pytest.fixture
def worktrees(tmp_path, protected):
    return GitWorktrees(tmp_path / "worktrees", protected)


# --- isolation --------------------------------------------------------------


def test_a_separate_directory_is_accepted(tmp_path, protected):
    assert check_isolated(tmp_path / "elsewhere", protected) == (tmp_path / "elsewhere").resolve()


@pytest.mark.parametrize("relative", ["projects", "projects/alpha", "state/x", "state"])
def test_a_path_equal_to_or_inside_a_protected_path_is_refused(tmp_path, protected, relative):
    with pytest.raises(IsolationError):
        check_isolated(tmp_path / relative, protected)


def test_a_path_containing_a_protected_path_is_refused(tmp_path, protected):
    with pytest.raises(IsolationError):
        check_isolated(tmp_path, protected)


def test_the_control_plane_itself_is_refused(protected):
    with pytest.raises(IsolationError):
        check_isolated(control_plane_root(), protected)
    with pytest.raises(IsolationError):
        check_isolated(control_plane_root() / "core", protected)


def test_symlinks_are_resolved_before_checking(tmp_path, protected):
    link = tmp_path / "innocent-looking"
    link.symlink_to(tmp_path / "state")

    with pytest.raises(IsolationError):
        check_isolated(link, protected)


def test_a_worktrees_root_overlapping_protected_paths_is_refused(tmp_path, protected):
    with pytest.raises(IsolationError):
        GitWorktrees(tmp_path / "state" / "worktrees", protected)


def test_a_repository_inside_the_projects_root_is_refused(tmp_path, worktrees):
    repo = make_repo(tmp_path / "projects" / "inner-repo")

    with pytest.raises(IsolationError):
        worktrees.check_repository(repo, "main")


# --- the repository ---------------------------------------------------------


def test_check_repository_returns_the_base_commit(git_repo, worktrees):
    repo, base = worktrees.check_repository(git_repo, "main")

    assert repo == git_repo.resolve()
    assert base == git(git_repo, "rev-parse", "main")


def test_a_missing_branch_is_refused(git_repo, worktrees):
    with pytest.raises(RepositoryError, match="nope"):
        worktrees.check_repository(git_repo, "nope")


def test_a_directory_that_is_not_a_repository_is_refused(tmp_path, worktrees):
    (tmp_path / "plain").mkdir()

    with pytest.raises(RepositoryError, match="not a git repository"):
        worktrees.check_repository(tmp_path / "plain", "main")


def test_a_subdirectory_of_a_repository_is_refused(git_repo, worktrees):
    with pytest.raises(RepositoryError, match="top"):
        worktrees.check_repository(git_repo / "tests", "main")


def test_a_missing_repository_is_refused(tmp_path, worktrees):
    with pytest.raises(RepositoryError, match="does not exist"):
        worktrees.check_repository(tmp_path / "gone", "main")


# --- one attempt ------------------------------------------------------------


def make_attempt(git_repo, worktrees):
    _, base = worktrees.check_repository(git_repo, "main")
    path = worktrees.create(git_repo, worktrees.path_for("alpha", ATTEMPT), ATTEMPT, base)
    return path, base


def test_a_worktree_is_created_on_its_own_branch_at_the_base(git_repo, worktrees, tmp_path):
    path, base = make_attempt(git_repo, worktrees)

    assert path == (tmp_path / "worktrees" / "alpha" / ATTEMPT).resolve()
    assert git(path, "rev-parse", "HEAD") == base
    assert git(path, "rev-parse", "--abbrev-ref", "HEAD") == f"attempt/{ATTEMPT}"
    # The owner's checkout is untouched.
    assert git(git_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_a_snapshot_commits_the_changes_and_describes_them(git_repo, worktrees):
    path, base = make_attempt(git_repo, worktrees)
    (path / "app.py").write_text("def greet(name):\n    return 'Hello ' + name\n")
    (path / "new.txt").write_text("a\nb\n")

    facts = worktrees.snapshot(path, base, ATTEMPT)

    assert facts["result_sha"] != base
    assert facts["result_sha"] == git(path, "rev-parse", "HEAD")
    assert sorted(facts["files_changed"]) == ["app.py", "new.txt"]
    assert facts["files_changed_count"] == 2
    assert facts["diffstat"] == {"files": 2, "insertions": 3, "deletions": 1}
    assert git(path, "status", "--porcelain") == ""
    assert git(path, "log", "-1", "--format=%an") == "master-system"


def test_a_snapshot_with_no_changes_keeps_the_base(git_repo, worktrees):
    path, base = make_attempt(git_repo, worktrees)

    facts = worktrees.snapshot(path, base, ATTEMPT)

    assert facts["result_sha"] == base
    assert facts["files_changed"] == [] and facts["diffstat"]["files"] == 0


def test_commits_the_worker_made_itself_are_included(git_repo, worktrees):
    path, base = make_attempt(git_repo, worktrees)
    (path / "one.txt").write_text("1\n")
    git(path, "add", "one.txt")
    git(path, "commit", "-q", "-m", "worker commit")
    (path / "two.txt").write_text("2\n")

    facts = worktrees.snapshot(path, base, ATTEMPT)

    assert sorted(facts["files_changed"]) == ["one.txt", "two.txt"]


def test_a_deleted_file_is_a_changed_file(git_repo, worktrees):
    path, base = make_attempt(git_repo, worktrees)
    (path / "tests" / "test_app.py").unlink()

    facts = worktrees.snapshot(path, base, ATTEMPT)

    assert facts["files_changed"] == ["tests/test_app.py"]


def test_observe_reports_uncommitted_work_without_changing_it(git_repo, worktrees):
    path, base = make_attempt(git_repo, worktrees)
    (path / "app.py").write_text("changed\n")
    (path / "stray.txt").write_text("x\n")

    facts = worktrees.observe(path, base)

    assert facts["worktree_present"] is True
    assert facts["untracked"] == 1
    assert sorted(facts["git_status"]) == [" M app.py", "?? stray.txt"]
    assert facts["diffstat"]["files"] == 1
    assert git(path, "rev-parse", "HEAD") == base
    assert (path / "stray.txt").exists()


def test_observe_reports_a_missing_worktree(tmp_path, worktrees):
    assert worktrees.observe(tmp_path / "gone", "abc") == {"worktree_present": False}


# --- what workers and verifiers are given ------------------------------------


def test_the_workspace_runs_processes_in_itself_and_records_them(tmp_path):
    spawned = []
    workspace = AttemptWorkspace(
        path=tmp_path, base_sha="b", log_dir=tmp_path / "logs", deadline=time.monotonic() + 30,
        deadline_at="later", on_spawn=lambda pid, pgid: spawned.append(pid),
    )

    outcome = workspace.run(["pwd"])

    assert outcome.stdout.strip() == str(tmp_path.resolve())
    assert workspace.processes == [outcome]
    assert spawned == [outcome.pid]


def test_a_per_call_timeout_never_extends_the_deadline(tmp_path):
    workspace = AttemptWorkspace(path=tmp_path, base_sha="b", log_dir=tmp_path / "logs",
                                 deadline=time.monotonic() + 1, deadline_at="soon",
                                 grace=1)

    outcome = workspace.run(["sleep", "30"], timeout=600)

    assert outcome.timed_out is True


# --- control-plane git ignores the repository's hooks ---------------------------------


def plant_hook(repo, name, marker):
    hook = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")) \
        / "hooks" / name
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)


def test_a_post_checkout_hook_in_the_common_git_dir_does_not_run_on_create(
        git_repo, worktrees, tmp_path):
    marker = tmp_path / "hook-ran"
    plant_hook(git_repo, "post-checkout", marker)
    base = git(git_repo, "rev-parse", "main")

    worktrees.create(git_repo, worktrees.path_for("p", ATTEMPT), ATTEMPT, base)

    assert not marker.exists()


def test_host_git_does_not_run_the_repositorys_hooks(git_repo, tmp_path):
    from core.host import git as host_git

    marker = tmp_path / "hook-ran"
    plant_hook(git_repo, "post-checkout", marker)

    host_git(["checkout", "-q", "-b", "other"], git_repo)

    assert not marker.exists()
