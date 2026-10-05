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
4. fast-forwards only: if the base branch moved on, it refuses -- re-run the
   task from the new base instead;
5. records an ``integration`` event.

It never changes task status.
"""

from __future__ import annotations

import argparse
import sys
import uuid
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
from core.task_orchestrator import default_worktrees
from core.workspace import WorkspaceError, branch_tip, fast_forward, is_ancestor

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
    #: ``ff_merge``, ``update_ref``, or ``already_integrated`` (nothing done).
    method: str


def _attempt_records(history: HistoryStore, project_id: str, attempt_id: str):
    events = history.events(project_id=project_id, attempt_id=attempt_id)
    started = next((e for e in events if e.type is EventType.ATTEMPT_STARTED), None)
    finished = next((e for e in events if e.type is EventType.ATTEMPT_FINISHED), None)
    verifications = [e for e in events if e.type is EventType.VERIFICATION]
    return started, finished, (verifications[-1] if verifications else None)


def integrate(master: Master, history: HistoryStore, project_id: str, attempt_id: str,
              *, paths: Optional[RuntimePaths] = None) -> IntegrationResult:
    """Integrate one verified attempt. Raises IntegrationRefused or ProjectBusyError."""
    paths = paths if paths is not None else RuntimePaths.default()
    state = master.project_state(project_id)
    worktrees = default_worktrees(master, paths)

    with ProjectLock(state.project_path, holder=f"integrate {attempt_id}"):
        recover_project(history, project_id, store=FileSessionStore(paths.sessions_dir),
                        worktrees=worktrees)

        started, finished, verification = _attempt_records(history, project_id, attempt_id)
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
            if is_ancestor(repo, result_sha, tip):
                return IntegrationResult(attempt_id, base_branch, result_sha, tip,
                                         "already_integrated")
            if not is_ancestor(repo, tip, result_sha):
                raise IntegrationRefused(
                    "base_moved",
                    f"{base_branch!r} has moved on since the attempt's base; "
                    "re-run the task from the new base",
                )
            method = fast_forward(repo, base_branch, tip, result_sha)
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
                "result_sha": result_sha,
                "method": method,
                "actor": "human-cli",
            },
        )
        return IntegrationResult(attempt_id, base_branch, result_sha, tip, method)


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
                           args.project_id, args.attempt_id, paths=paths)
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
              f"{result.previous_sha[:12]} -> {result.result_sha[:12]} ({result.method})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
