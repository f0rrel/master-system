"""Close out what dead processes left open in one project. Never replays.

Called by every history user right after it acquires the project lock
(``SessionRunner``, a loop that takes the lock itself, the integrate command).
Holding the lock proves that no run of this project is in progress, so
anything open in history belongs to a process that no longer exists:

* every attempt with ``attempt_started`` but no outcome gets
  ``attempt_interrupted``, carrying what its worktree holds now -- whether it
  exists, ``git status``, diff stats against its base and the number of
  untracked files -- observed read-only;
* every run with ``run_started`` but no end gets ``run_interrupted``;
* every session of the project still marked RUNNING is stopped with
  ``interrupted``, its interrupted runs' decisions added to its step count.

The worktrees are kept. Whether to try again is Master's or a human's decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from core.history import EventType, HistoryStore
from core.work_session import SessionStatus

__all__ = ["RecoveryReport", "STOP_INTERRUPTED", "recover_project"]

STOP_INTERRUPTED = "interrupted"

_RUN_ENDS = (EventType.RUN_STOPPED, EventType.RUN_ERROR, EventType.RUN_INTERRUPTED)
_ATTEMPT_ENDS = (EventType.ATTEMPT_FINISHED, EventType.ATTEMPT_INTERRUPTED)


@dataclass
class RecoveryReport:
    interrupted_runs: list = field(default_factory=list)
    interrupted_attempts: list = field(default_factory=list)
    stopped_sessions: list = field(default_factory=list)


def recover_project(history: HistoryStore, project_id: str, *, store=None,
                    worktrees=None) -> RecoveryReport:
    """Record every interruption left in project_id. The caller holds its lock."""
    events = history.events(project_id=project_id)
    report = RecoveryReport()

    ended_runs = {e.run_id for e in events if e.type in _RUN_ENDS}
    open_runs = {
        e.run_id: e for e in events
        if e.type is EventType.RUN_STARTED and e.run_id not in ended_runs
    }
    ended_attempts = {e.attempt_id for e in events if e.type in _ATTEMPT_ENDS}
    open_attempts = [
        e for e in events
        if e.type is EventType.ATTEMPT_STARTED and e.attempt_id not in ended_attempts
    ]
    decisions_by_run: dict = {}
    for event in events:
        if event.type is EventType.DECISION:
            decisions_by_run[event.run_id] = decisions_by_run.get(event.run_id, 0) + 1

    for started in open_attempts:
        facts = _observe(worktrees, started.payload)
        history.append(
            type=EventType.ATTEMPT_INTERRUPTED,
            run_id=started.run_id,
            session_id=started.session_id,
            project_id=project_id,
            task_id=started.task_id,
            attempt_id=started.attempt_id,
            payload={
                "reason": "the process ended before the attempt's outcome was recorded",
                **facts,
            },
        )
        report.interrupted_attempts.append(started.attempt_id)

    for run_id, started in open_runs.items():
        history.append(
            type=EventType.RUN_INTERRUPTED,
            run_id=run_id,
            session_id=started.session_id,
            project_id=project_id,
            payload={"reason": "the process ended before the run's end was recorded"},
        )
        report.interrupted_runs.append(run_id)

    if store is not None:
        for session in store.list_sessions(project_id):
            if session.status is not SessionStatus.RUNNING:
                continue
            steps = sum(
                decisions_by_run.get(run_id, 0)
                for run_id, started in open_runs.items()
                if started.session_id == session.session_id
            )
            store.save(
                session.evolve(
                    status=SessionStatus.STOPPED,
                    last_stop_reason=STOP_INTERRUPTED,
                    steps_completed=session.steps_completed + steps,
                )
            )
            report.stopped_sessions.append(session.session_id)

    return report


def _observe(worktrees, started_payload) -> dict:
    worktree: Optional[str] = started_payload.get("worktree")
    if worktree is None:
        return {"worktree_recorded": False}
    if worktrees is None:
        return {"worktree": worktree}
    try:
        return {"worktree": worktree,
                **worktrees.observe(worktree, started_payload.get("base_sha"))}
    except Exception as error:  # observation must never block recovery
        return {"worktree": worktree, "observe_error": f"{type(error).__name__}: {error}"}
