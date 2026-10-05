"""Human commands for attempts: ``python -m core.attempts integrate``.

Integration merges a verified attempt's committed result into the project's
base branch. It is the one way an attempt's work reaches the real branch, and
only a human runs it: the matching operation, ``integrate_attempt``, is
CRITICAL, so a model can request it but never perform it.

``integrate <project_id> <attempt_id>``, holding the project lock (after
closing out anything dead processes left open):

1. reads the attempt's own records, written by the orchestrator;
2. requires that the attempt finished, verified ``pass``, was run against the
   task's current spec, and targets the repository and base branch that
   ``project.yaml`` still names;
3. does nothing if the result is already on the base branch (idempotent by SHA);
4. fast-forwards the base branch to the result. If the base moved on since the
   attempt started, it refuses -- unless ``--rebase`` is given: then the
   attempt's commits are cherry-picked onto the current base in a fresh
   worktree (a conflict refuses), the task's acceptance is **run again on the
   new commit** and recorded as a new ``verification`` event bound to that
   SHA, and the base is fast-forwarded to exactly that verified SHA -- or
   refused if it did not pass;
5. records an ``integration`` event.

It never changes task status.
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.evidence import spec_hash
from core.history import EventType, HistoryStore
from core.master import EXPECTED_ERRORS, Master
from core.paths import RuntimePaths
from core.recovery import recover_project
from core.run_lock import ProjectBusyError, ProjectLock
from core.session_store import FileSessionStore
from core.acceptance_verifier import AcceptanceVerifier
from core.history import jsonable
from core.task_orchestrator import (
    DEFAULT_VERIFICATION_TIMEOUT_S,
    process_records,
    default_worker_env,
    default_worktrees,
)
from core.workspace import (
    AttemptWorkspace,
    WorkspaceError,
    branch_tip,
    fast_forward,
    is_ancestor,
)

__all__ = ["IntegrationRefused", "IntegrationResult", "integrate", "main"]


class IntegrationRefused(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class IntegrationResult:
    attempt_id: str
    base_branch: str
    result_sha: str
    previous_sha: Optional[str]
    #: ``ff_merge``, ``update_ref``, ``rebased_ff_merge``, ``rebased_update_ref``,
    #: or ``already_integrated`` (nothing done).
    method: str
    #: The attempt's own result when it was rebased onto a moved base.
    rebased_from: Optional[str] = None


def _attempt_records(history: HistoryStore, project_id: str, attempt_id: str):
    events = history.events(project_id=project_id, attempt_id=attempt_id)
    started = next((e for e in events if e.type is EventType.ATTEMPT_STARTED), None)
    finished = next((e for e in events if e.type is EventType.ATTEMPT_FINISHED), None)
    # The attempt's own verification, not a re-verification of a rebased result.
    verifications = [e for e in events if e.type is EventType.VERIFICATION
                     and "rebased_from" not in e.payload]
    integrations = [e for e in events if e.type is EventType.INTEGRATION]
    return started, finished, (verifications[-1] if verifications else None), integrations


def _rebase_and_verify(history, worktrees, repo, project_id, attempt_id, task, started,
                       result_sha, tip, lock, paths, verifier, worker_env, timeout_s):
    """Replay the attempt onto tip, re-run acceptance there, record the verdict."""
    path = worktrees.free_path(project_id, f"{attempt_id}-rebase")
    worktrees.create(repo, path, attempt_id, tip, branch=f"attempt/{path.name}")
    new_sha = worktrees.cherry_pick(path, started.payload["base_sha"], result_sha)
    if new_sha is None:
        raise IntegrationRefused(
            "rebase_conflict",
            f"attempt {attempt_id} does not apply cleanly onto the current base; "
            "re-run the task from the new base",
        )
    deadline_at = (datetime.now(timezone.utc) + timedelta(seconds=timeout_s)).isoformat(
        timespec="seconds")
    workspace = AttemptWorkspace(
        path=path, base_sha=tip, result_sha=new_sha,
        deadline=time.monotonic() + timeout_s, deadline_at=deadline_at,
        log_dir=paths.state_dir / "logs" / project_id / attempt_id, phase="reintegration",
        lock_fd=lock.fileno(), env=worker_env,
    )
    evidence = {"base_sha": tip, "result_sha": new_sha, "rebased_from": result_sha}
    verdict = verifier.verify(task, {"project_id": project_id, "task_id": started.task_id},
                              evidence, workspace=workspace)
    history.append(
        type=EventType.VERIFICATION,
        run_id=uuid.uuid4().hex,
        project_id=project_id,
        task_id=started.task_id,
        attempt_id=attempt_id,
        payload={
            "verdict": verdict.verdict,
            "summary": verdict.summary,
            "findings": jsonable(list(verdict.findings)),
            "evidence": jsonable(verdict.evidence),
            "base_sha": tip,
            "result_sha": new_sha,
            "rebased_from": result_sha,
            "worktree": str(path),
            "actor": "human-cli",
            "deadline_at": deadline_at,
            "processes": process_records(workspace),
        },
    )
    if verdict.verdict != "pass":
        raise IntegrationRefused(
            "reverification_failed",
            f"the rebased result {new_sha[:12]} did not pass acceptance "
            f"({verdict.verdict}): {verdict.summary}",
        )
    return new_sha


def integrate(master: Master, history: HistoryStore, project_id: str, attempt_id: str,
              *, paths: Optional[RuntimePaths] = None, rebase: bool = False,
              verifier=None, worker_env=None,
              verification_timeout_s: float = DEFAULT_VERIFICATION_TIMEOUT_S,
              ) -> IntegrationResult:
    """Integrate one verified attempt. Raises IntegrationRefused or ProjectBusyError."""
    paths = paths if paths is not None else RuntimePaths.default()
    state = master.project_state(project_id)
    worktrees = default_worktrees(master, paths)

    with ProjectLock(state.project_path, holder=f"integrate {attempt_id}") as lock:
        recover_project(history, project_id, store=FileSessionStore(paths.sessions_dir),
                        worktrees=worktrees)

        started, finished, verification, integrations = _attempt_records(
            history, project_id, attempt_id)
        if started is None:
            raise IntegrationRefused("unknown_attempt",
                                     f"no attempt {attempt_id} in project {project_id!r}")
        attempt = started.payload
        done = finished.payload if finished is not None else {}
        if done.get("outcome") != "finished" or not done.get("result_sha"):
            raise IntegrationRefused(
                "not_finished",
                f"attempt {attempt_id} did not finish with a committed result "
                f"(outcome: {done.get('outcome') or 'none recorded'})",
            )
        verdict = verification.payload.get("verdict") if verification else None
        if verdict != "pass":
            raise IntegrationRefused("not_verified",
                                     f"attempt {attempt_id} was not verified as pass "
                                     f"(verdict: {verdict})")
        task = state.get_task(started.task_id)
        if task is None or attempt.get("spec_hash") != spec_hash(task):
            raise IntegrationRefused(
                "spec_changed",
                f"task {started.task_id!r} changed since attempt {attempt_id} ran",
            )
        project = state.project()
        repository, base_branch = project.get("repository"), project.get("base_branch")
        if (
            not repository
            or Path(repository).expanduser().resolve() != Path(attempt.get("repository", "")).resolve()
            or base_branch != attempt.get("base_branch")
        ):
            raise IntegrationRefused(
                "repository_changed",
                "project.yaml no longer names the repository and base branch the "
                "attempt was made against",
            )

        try:
            repo, _ = worktrees.check_repository(Path(repository).expanduser(), base_branch)
            result_sha = done["result_sha"]
            tip = branch_tip(repo, base_branch)
            for sha in [result_sha, *(e.payload.get("result_sha") for e in integrations)]:
                if sha and is_ancestor(repo, sha, tip):
                    return IntegrationResult(attempt_id, base_branch, sha, tip,
                                             "already_integrated")
            target, rebased_from = result_sha, None
            if not is_ancestor(repo, tip, result_sha):
                if not rebase:
                    raise IntegrationRefused(
                        "base_moved",
                        f"{base_branch!r} has moved on since the attempt's base; "
                        "integrate with --rebase, or re-run the task from the new base",
                    )
                target = _rebase_and_verify(
                    history, worktrees, repo, project_id, attempt_id, task, started,
                    result_sha, tip, lock, paths, verifier or AcceptanceVerifier(),
                    worker_env if worker_env is not None else default_worker_env(),
                    verification_timeout_s)
                rebased_from = result_sha
            method = fast_forward(repo, base_branch, tip, target)
            if rebased_from:
                method = f"rebased_{method}"
        except WorkspaceError as error:
            raise IntegrationRefused("git_refused", str(error)) from error

        history.append(
            type=EventType.INTEGRATION,
            run_id=uuid.uuid4().hex,
            project_id=project_id,
            task_id=started.task_id,
            attempt_id=attempt_id,
            payload={
                "repository": str(repo),
                "base_branch": base_branch,
                "previous_sha": tip,
                "result_sha": target,
                "rebased_from": rebased_from,
                "method": method,
                "actor": "human-cli",
            },
        )
        return IntegrationResult(attempt_id, base_branch, target, tip, method, rebased_from)


def build_parser():
    parser = argparse.ArgumentParser(prog="python -m core.attempts",
                                     description="Human commands for task attempts.")
    parser.add_argument("--root", default=None, metavar="PATH",
                        help="Projects root (default: the repository's projects/).")
    parser.add_argument("--state-dir", default=None, metavar="PATH",
                        help="State directory holding history.sqlite and sessions/.")
    commands = parser.add_subparsers(dest="command", metavar="<command>", required=True)
    integrate_cmd = commands.add_parser(
        "integrate", help="Fast-forward the base branch to a verified attempt's result.")
    integrate_cmd.add_argument("project_id")
    integrate_cmd.add_argument("attempt_id")
    integrate_cmd.add_argument(
        "--rebase", action="store_true",
        help="If the base moved, replay the attempt onto it and re-run its acceptance.")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    paths = RuntimePaths.default()
    if args.state_dir:
        paths = RuntimePaths(state_dir=Path(args.state_dir),
                             worktrees_root=paths.worktrees_root)
    from core.sqlite_history import SQLiteHistoryStore

    try:
        result = integrate(Master(args.root), SQLiteHistoryStore(paths.history_path),
                           args.project_id, args.attempt_id, paths=paths,
                           rebase=args.rebase)
    except IntegrationRefused as error:
        print(f"error: refused ({error.reason}): {error}", file=sys.stderr)
        return 1
    except (ProjectBusyError, *EXPECTED_ERRORS) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if result.method == "already_integrated":
        print(f"already integrated: {result.result_sha} is on {result.base_branch}")
    else:
        print(f"integrated {result.attempt_id}: {result.base_branch} "
              f"{result.previous_sha[:12]} -> {result.result_sha[:12]} ({result.method})"
              + (f", rebased from {result.rebased_from[:12]} and re-verified"
                 if result.rebased_from else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
