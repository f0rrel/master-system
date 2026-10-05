"""Attempt workspaces: one git worktree per attempt, owned by the orchestrator.

The orchestrator, never a worker, decides where an attempt runs. Each attempt
gets ``git worktree add <root>/<project_id>/<attempt_id> -b attempt/<attempt_id>
<base_sha>`` in the project's repository. After the worker returns, the
orchestrator commits whatever is in the worktree itself, so ``base_sha`` and
``result_sha`` are facts observed by the orchestrator, not claims. Worktrees and
their branches are kept.

Isolation: a repository, a worktree and the worktrees root must not be, contain
or sit inside a protected path -- the projects root, the state directory and
the control plane's own source directory. This isolates files; it is not a
sandbox. A worker still runs as the owner's user.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from core.worker_process import DEFAULT_GRACE_S, ProcessOutcome, run_process

__all__ = [
    "AttemptWorkspace",
    "GitWorktrees",
    "IsolationError",
    "MAX_LISTED_PATHS",
    "RepositoryError",
    "WorkspaceError",
    "check_isolated",
    "control_plane_root",
]

#: How many paths an event lists before it only counts the rest.
MAX_LISTED_PATHS = 200
_GIT_TIMEOUT_S = 120
#: Snapshot commits are the orchestrator's: a fixed identity (set through the
#: environment, which outranks any configured or inherited one), no hooks from
#: the managed repository, and no dependence on the owner's signing setup.
_COMMIT_CONFIG = ("-c", "commit.gpgsign=false")
_COMMIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "master-system",
    "GIT_AUTHOR_EMAIL": "master-system@localhost",
    "GIT_COMMITTER_NAME": "master-system",
    "GIT_COMMITTER_EMAIL": "master-system@localhost",
}


class WorkspaceError(RuntimeError):
    """An attempt workspace could not be prepared or observed."""


class IsolationError(WorkspaceError):
    """A path overlaps something no workspace may reach."""


class RepositoryError(WorkspaceError):
    """The project's repository or base branch is unusable."""


def control_plane_root() -> Path:
    """The directory holding this control plane's code (it contains ``core/``)."""
    return Path(__file__).resolve().parent.parent


def check_isolated(path, protected: Iterable, what: str = "workspace") -> Path:
    """Return path resolved, or raise if it equals, contains or is inside a
    protected path. Symlinks are resolved first."""
    target = Path(path).expanduser().resolve()
    for guarded in protected:
        guarded = Path(guarded).expanduser().resolve()
        if target == guarded or guarded in target.parents or target in guarded.parents:
            raise IsolationError(
                f"{what} {target} overlaps protected path {guarded}"
            )
    return target


def _git(args: Sequence[str], cwd, check: bool = True,
         extra_env=None) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", **(extra_env or {}))
    try:
        done = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True,
            errors="replace", env=env, timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkspaceError(f"git {' '.join(args)} failed: {error}") from error
    if check and done.returncode != 0:
        raise WorkspaceError(
            f"git {' '.join(args)} failed ({done.returncode}): {done.stderr.strip()}"
        )
    return done


def _numstat(output: str) -> dict:
    files = insertions = deletions = 0
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        # Binary files report "-": they count as a changed file with no lines.
        insertions += int(parts[0]) if parts[0].isdigit() else 0
        deletions += int(parts[1]) if parts[1].isdigit() else 0
    return {"files": files, "insertions": insertions, "deletions": deletions}


class GitWorktrees:
    """Creates, snapshots and observes attempt worktrees under one root."""

    def __init__(self, root, protected: Iterable):
        self._protected = tuple(Path(p) for p in protected)
        self.root = check_isolated(root, self._protected, "worktrees root")

    @property
    def protected(self) -> tuple:
        return self._protected

    # --- before an attempt ----------------------------------------------

    def check_repository(self, repository, base_branch: str):
        """Return ``(repository, base_sha)`` or raise. Reads only."""
        repo = check_isolated(repository, self._protected, "repository")
        if not repo.is_dir():
            raise RepositoryError(f"repository {repo} does not exist")
        top = _git(["rev-parse", "--show-toplevel"], repo, check=False)
        if top.returncode != 0:
            raise RepositoryError(f"{repo} is not a git repository")
        if Path(top.stdout.strip()).resolve() != repo:
            raise RepositoryError(
                f"repository must be the top of a git checkout, got {repo} "
                f"inside {top.stdout.strip()}"
            )
        base = _git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{base_branch}^{{commit}}"],
            repo, check=False,
        )
        if base.returncode != 0:
            raise RepositoryError(f"branch {base_branch!r} does not exist in {repo}")
        return repo, base.stdout.strip()

    def path_for(self, project_id: str, attempt_id: str) -> Path:
        path = self.root / project_id / attempt_id
        return check_isolated(path, self._protected)

    @staticmethod
    def branch_for(attempt_id: str) -> str:
        return f"attempt/{attempt_id}"

    # --- the attempt ------------------------------------------------------

    def create(self, repository, path, attempt_id: str, base_sha: str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(
            ["worktree", "add", str(path), "-b", self.branch_for(attempt_id), base_sha],
            repository,
        )
        return path

    def snapshot(self, path, base_sha: str, attempt_id: str) -> dict:
        """Commit everything in the worktree and describe base..result."""
        _git(["add", "-A"], path)
        staged = _git(["diff", "--cached", "--quiet"], path, check=False)
        if staged.returncode == 1:
            _git([*_COMMIT_CONFIG, "commit", "--no-verify", "-q", "-m",
                  f"attempt {attempt_id}"], path, extra_env=_COMMIT_IDENTITY)
        elif staged.returncode != 0:
            raise WorkspaceError(f"git diff --cached failed: {staged.stderr.strip()}")
        result_sha = _git(["rev-parse", "HEAD"], path).stdout.strip()
        return {"result_sha": result_sha, **self.changes(path, base_sha, result_sha)}

    @staticmethod
    def changes(path, base_sha: str, result_sha: str) -> dict:
        names = _git(
            ["diff", "--name-only", "--no-renames", base_sha, result_sha], path
        ).stdout.splitlines()
        stat = _git(["diff", "--numstat", "--no-renames", base_sha, result_sha], path)
        return {
            "files_changed": names[:MAX_LISTED_PATHS],
            "files_changed_count": len(names),
            "diffstat": _numstat(stat.stdout),
        }

    # --- after a crash ----------------------------------------------------

    def observe(self, path, base_sha: Optional[str]) -> dict:
        """What an interrupted attempt left in its worktree. Changes nothing."""
        path = Path(path)
        if not path.is_dir():
            return {"worktree_present": False}
        facts: dict = {"worktree_present": True}
        status = _git(["status", "--porcelain"], path, check=False)
        if status.returncode != 0:
            facts["observe_error"] = status.stderr.strip()
            return facts
        lines = status.stdout.splitlines()
        facts["git_status"] = lines[:MAX_LISTED_PATHS]
        facts["git_status_count"] = len(lines)
        facts["untracked"] = sum(1 for line in lines if line.startswith("??"))
        if base_sha:
            diff = _git(["diff", "--numstat", "--no-renames", base_sha], path, check=False)
            if diff.returncode == 0:
                facts["diffstat"] = _numstat(diff.stdout)
        return facts


@dataclass(frozen=True)
class AttemptWorkspace:
    """What a worker or verifier is given: where to work, and how to run things.

    ``run`` is the only way for them to start a process: it goes through
    :func:`core.worker_process.run_process`, so the lock fd, the deadline and
    the process group apply to every process. Each outcome is kept in
    ``processes`` for the orchestrator to record.
    """

    path: Path
    base_sha: str
    deadline: float
    deadline_at: str
    result_sha: Optional[str] = None
    lock_fd: Optional[int] = None
    on_spawn: Optional[Callable[[int, int], None]] = None
    grace: float = DEFAULT_GRACE_S
    clock: Callable[[], float] = time.monotonic
    processes: list = field(default_factory=list, compare=False)

    def remaining(self) -> float:
        return max(0.0, self.deadline - self.clock())

    def expired(self) -> bool:
        return self.clock() >= self.deadline

    def run(self, argv: Sequence[str], *, timeout: Optional[float] = None,
            env=None) -> ProcessOutcome:
        deadline = self.deadline
        if timeout is not None:
            deadline = min(deadline, self.clock() + timeout)
        outcome = run_process(
            argv, cwd=self.path, deadline=deadline, lock_fd=self.lock_fd,
            grace=self.grace, env=env, on_spawn=self.on_spawn, clock=self.clock,
        )
        self.processes.append(outcome)
        return outcome
