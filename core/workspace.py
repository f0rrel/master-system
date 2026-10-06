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
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Sequence

from core.worker_process import DEFAULT_GRACE_S, ProcessOutcome, run_process

__all__ = [
    "AttemptWorkspace",
    "branch_tip",
    "checkout_of",
    "accept_tip",
    "fast_forward",
    "foreign_tip",
    "system_tip",
    "has_tracked_changes",
    "record_repository",
    "restore_repository",
    "is_ancestor",
    "GitWorktrees",
    "IsolationError",
    "MAX_LISTED_PATHS",
    "RepositoryError",
    "WorkspaceError",
    "check_isolated",
    "control_git_argv",
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


def control_git_argv(*args: str) -> list:
    """argv for a git command the control plane runs itself, never a worker's.

    Repository hooks and the fsmonitor hook are switched off: a worker can write
    the shared git directory, and the control plane must not run its code.
    """
    return ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args]


def _git(args: Sequence[str], cwd, check: bool = True,
         extra_env=None, input_text=None) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", **(extra_env or {}))
    try:
        done = subprocess.run(
            control_git_argv(*args), cwd=str(cwd), capture_output=True, text=True,
            errors="replace", env=env, timeout=_GIT_TIMEOUT_S, input=input_text,
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

    def create(self, repository, path, attempt_id: str, base_sha: str,
               branch: Optional[str] = None) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(
            ["worktree", "add", str(path), "-b", branch or self.branch_for(attempt_id),
             base_sha],
            repository,
        )
        return path

    def free_path(self, project_id: str, name: str) -> Path:
        """``<root>/<project>/<name>``, or ``<name>-2``, ``-3``... if taken."""
        candidate, counter = self.root / project_id / name, 1
        while candidate.exists():
            counter += 1
            candidate = self.root / project_id / f"{name}-{counter}"
        return check_isolated(candidate, self._protected)

    @staticmethod
    def cherry_pick(path, base_sha: str, result_sha: str) -> Optional[str]:
        """Replay base..result onto the worktree's HEAD; the new HEAD, or None on
        conflict (the cherry-pick is aborted and the worktree left clean)."""
        if base_sha == result_sha:
            return _git(["rev-parse", "HEAD"], path).stdout.strip()
        done = _git([*_COMMIT_CONFIG, "cherry-pick", "--allow-empty", "--keep-redundant-commits",
                     f"{base_sha}..{result_sha}"], path, check=False,
                    extra_env=_COMMIT_IDENTITY)
        if done.returncode != 0:
            _git(["cherry-pick", "--abort"], path, check=False)
            return None
        return _git(["rev-parse", "HEAD"], path).stdout.strip()

    def snapshot(self, path, base_sha: str, attempt_id: str,
                 subject: Optional[str] = None) -> dict:
        """Commit everything in the worktree and describe base..result.

        The commit message is ``subject`` (for example ``<task id>: <task
        title>``) with the attempt id in the body. The verified SHA is the
        one integrated, so this message is what lands on the base branch.
        """
        _git(["add", "-A"], path)
        staged = _git(["diff", "--cached", "--quiet"], path, check=False)
        if staged.returncode == 1:
            message = f"{subject}\n\nAttempt {attempt_id}." if subject else f"attempt {attempt_id}"
            _git([*_COMMIT_CONFIG, "commit", "--no-verify", "-q", "-m", message],
                 path, extra_env=_COMMIT_IDENTITY)
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


# --- the shared repository around a worker run ------------------------------

#: Git directory contents a worker can use to change what the control plane runs.
_GUARDED_DIRS = ("hooks", "info")
#: Config keys a worker may set; restored without counting as tampering.
_ALLOWED_CONFIG_KEYS = frozenset({"user.name", "user.email"})


def _guarded_files(common: Path) -> dict:
    files, names = {}, ["config"]
    for name in _GUARDED_DIRS:
        top = common / name
        if top.is_symlink():
            names.append(name)
        elif top.is_dir():
            names += [str(p.relative_to(common)) for p in top.rglob("*")]
    for rel in names:
        path = common / rel
        if path.is_symlink():
            files[rel] = ("link", os.readlink(path))
        elif path.is_file():
            files[rel] = (path.stat().st_mode & 0o7777, path.read_bytes())
    return files


def record_repository(repository, worktree) -> dict:
    """Every ref, the common git dir's config, hooks/ and info/, and the worktree's HEAD."""
    common = Path(_git(["rev-parse", "--path-format=absolute", "--git-common-dir"],
                       repository).stdout.strip())
    listing = _git(["for-each-ref", "--format=%(objectname) %(refname)"], repository).stdout
    head = _git(["symbolic-ref", "-q", "HEAD"], worktree, check=False).stdout.strip()
    return {"common_dir": common, "head": head, "files": _guarded_files(common),
            "refs": dict(reversed(line.split(" ", 1)) for line in listing.splitlines())}


def _config_keys(text: bytes) -> dict:
    listing = _git(["config", "-f", "-", "--list", "-z"], ".",
                   input_text=text.decode(errors="replace")).stdout
    keys: dict = {}
    for entry in filter(None, listing.split("\0")):
        key, _, value = entry.partition("\n")
        keys.setdefault(key, []).append(value)
    return keys


def restore_repository(repository, worktree, recorded: dict, own_branch: str):
    """Put refs and guarded files back as recorded.

    Returns ``(tampered, found)``: what counts as tampering, and the tip each
    tampered ref had before it was restored (so a commit can be recovered).

    The attempt's own branch is the attempt's work and stays. The stash,
    remote-tracking refs and the git identity are restored silently. Anything
    else changed is restored and listed. Detection and restoration, not a sandbox.
    """
    tampered, found = [], {}
    now = record_repository(repository, worktree)
    before, after = recorded["refs"], now["refs"]
    for ref in sorted(set(before) | set(after)):
        old, new = before.get(ref), after.get(ref)
        if old == new or ref == f"refs/heads/{own_branch}":
            continue
        if ref != "refs/stash" and not ref.startswith("refs/remotes/"):
            tampered.append(ref)
            if new is not None:
                found[ref] = new
        if old is None:
            _git(["update-ref", "-d", ref, new], repository)
        else:
            _git(["update-ref", ref, old, new or ""], repository)
    if now["head"] != recorded["head"]:
        tampered.append("HEAD")
        _git(["symbolic-ref", "HEAD", recorded["head"]], worktree)
    common = recorded["common_dir"]
    for rel in sorted(set(recorded["files"]) | set(now["files"])):
        old, new = recorded["files"].get(rel), now["files"].get(rel)
        if old == new:
            continue
        if rel == "config" and old and new and old[0] != "link" and new[0] != "link":
            was, keys = _config_keys(old[1]), _config_keys(new[1])
            tampered += [f"config: {key}" for key in sorted(set(was) | set(keys))
                         if was.get(key) != keys.get(key) and key not in _ALLOWED_CONFIG_KEYS]
        else:
            tampered.append(rel)
        path = common / rel
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        if old is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            if old[0] == "link":
                path.symlink_to(old[1])
            else:
                path.write_bytes(old[1])
                path.chmod(old[0])
    return tampered, found


# --- integration (used only by the human integrate command) ---------------


def branch_tip(repository, branch: str) -> Optional[str]:
    done = _git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"],
                repository, check=False)
    return done.stdout.strip() if done.returncode == 0 else None


def is_ancestor(repository, ancestor: str, descendant: str) -> bool:
    done = _git(["merge-base", "--is-ancestor", ancestor, descendant], repository,
                check=False)
    if done.returncode not in (0, 1):
        raise WorkspaceError(f"git merge-base failed: {done.stderr.strip()}")
    return done.returncode == 0


def checkout_of(repository, branch: str) -> Optional[Path]:
    """The worktree that has ``branch`` checked out, if any."""
    listing = _git(["worktree", "list", "--porcelain"], repository).stdout
    path = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):])
        elif line == f"branch refs/heads/{branch}" and path is not None:
            return path
    return None


def has_tracked_changes(path) -> bool:
    status = _git(["status", "--porcelain", "--untracked-files=no"], path)
    return bool(status.stdout.strip())


def _system_ref(branch: str) -> str:
    return f"refs/ms-system/heads/{branch}"


def system_tip(repository, branch: str) -> Optional[str]:
    """The tip the system last set on ``branch`` (None before its first write)."""
    done = _git(["rev-parse", "--verify", "--quiet", _system_ref(branch)], repository,
                check=False)
    return done.stdout.strip() if done.returncode == 0 else None


def accept_tip(repository, branch: str) -> Optional[str]:
    """Record ``branch``'s current tip as the system's (the owner's decision); returns it."""
    tip = branch_tip(repository, branch)
    if tip:
        _git(["update-ref", _system_ref(branch), tip], repository)
    return tip


def foreign_tip(repository, branch: str) -> Optional[str]:
    """``branch``'s tip if the system did not set it, else None.

    Before the system has written the branch, the current tip is trusted and recorded.
    """
    recorded = system_tip(repository, branch)
    if recorded is None:
        accept_tip(repository, branch)
        return None
    tip = branch_tip(repository, branch)
    return tip if tip != recorded else None


def fast_forward(repository, branch: str, expected_tip: str, result_sha: str) -> str:
    """Advance ``branch`` from ``expected_tip`` to ``result_sha``; never merge.

    If the branch is checked out somewhere, that checkout is fast-forwarded
    (refused if it has uncommitted changes), so its files stay consistent.
    Otherwise the ref is moved atomically, only if it still points at
    ``expected_tip``. Returns the method used.

    The new tip is recorded as the system's (``refs/ms-system/heads/<branch>``)
    only when the branch moved from a tip the system set, so building on a
    change made elsewhere does not make it the system's.
    """
    recorded = system_tip(repository, branch)
    checkout = checkout_of(repository, branch)
    if checkout is not None:
        if has_tracked_changes(checkout):
            raise WorkspaceError(
                f"{checkout} has {branch!r} checked out with uncommitted changes"
            )
        if branch_tip(repository, branch) != expected_tip:
            raise WorkspaceError(f"{branch!r} moved while integrating")
        _git(["merge", "--ff-only", "-q", result_sha], checkout)
        method = "ff_merge"
    else:
        _git(["update-ref", f"refs/heads/{branch}", result_sha, expected_tip], repository)
        method = "update_ref"
    if recorded in (None, expected_tip):
        _git(["update-ref", _system_ref(branch), result_sha], repository)
    return method


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
    #: Where process output is written, under the state dir, never in the worktree.
    log_dir: Path
    #: ``worker`` or ``verification``; names the log files.
    phase: str = "worker"
    #: The allowlisted environment every process gets (core/worker_env.py).
    #: None only in unit tests that call a backend directly.
    env: Optional[Mapping[str, str]] = None
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
            argv, cwd=self.path, deadline=deadline, log_dir=self.log_dir,
            log_name=self.phase, lock_fd=self.lock_fd, grace=self.grace,
            env=env if env is not None else self.env,
            on_spawn=self.on_spawn, clock=self.clock,
        )
        self.processes.append(outcome)
        return outcome
