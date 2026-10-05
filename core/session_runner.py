"""Run bounded autonomous work as a durable session.

This is the seam between the in-memory loop and the on-disk session. It does
not change what the loop does: it constructs the existing
:class:`core.autonomous_loop.AutonomousLoop` with injected collaborators, runs
it once, and translates the resulting
:class:`core.autonomous_loop.LoopResult` into durable session fields.

Per-step durability lives in history, not in the session. The loop appends
every decision, operation result and attempt to an append-only
:class:`core.history.HistoryStore` as it happens; the session file itself is
saved only when a run starts and when it ends. So after a crash the session
says "running", and history says exactly how far the run got.

Recovery
--------
Every run holds the project's :class:`core.run_lock.ProjectLock`. A session
found in RUNNING while that lock can be acquired was left by a process that no
longer exists. Before running again, the runner marks every run of that
session with no recorded end as ``run_interrupted``, and every attempt with no
recorded outcome as ``attempt_interrupted``, adds the interrupted runs' steps
from history, and stops the session with ``interrupted``. Nothing is replayed:
the worker may or may not have changed the workspace, so the interrupted
attempt is shown to Master as evidence and Master decides what to do.

If the lock is held, the session is still running somewhere and resuming it is
refused without touching anything.

An exception during a run -- a worker or verifier that raised -- is recorded by
the loop as ``run_error``; the runner stops the session with ``error`` and
re-raises. A session is never left claiming to run after its process has
handled the failure.

Why nothing is stored about the worker
--------------------------------------
Dependencies are constructor arguments here, exactly as in the loop. A session
never records which provider, model, runtime or backend was used, so the same
session can legitimately be resumed with a different one -- which is the point
of keeping the runtime replaceable.
"""

from __future__ import annotations

from typing import Optional

from core.autonomous_loop import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_REQUEST,
    STOP_APPROVAL,
    AutonomousLoop,
    LoopResult,
)
from core.evidence import DEFAULT_MAX_ATTEMPTS
from core.execution import ExecutionBackend
from core.history import EventType, HistoryStore
from core.master import Master
from core.paths import RuntimePaths
from core.provider import ReasoningProvider
from core.run_lock import ProjectLock
from core.session_store import FileSessionStore
from core.verification import VerificationBackend
from core.work_session import SessionStatus, WorkSession, status_for_stop_reason


#: Session-level stop reasons, set by the runner rather than the loop.
STOP_ERROR = "error"
STOP_INTERRUPTED = "interrupted"

_RUN_ENDS = (EventType.RUN_STOPPED, EventType.RUN_ERROR, EventType.RUN_INTERRUPTED)
_ATTEMPT_ENDS = (EventType.ATTEMPT_FINISHED, EventType.ATTEMPT_INTERRUPTED)


class SessionRunner:
    """Start and resume durable bounded runs.

    Every collaborator is injected, including the store, so a caller can supply
    a different :class:`core.work_session.SessionStore` without this class
    knowing what persistence is in use.
    """

    def __init__(
        self,
        master: Master,
        provider: ReasoningProvider,
        execution_backend: ExecutionBackend,
        verification_backend: VerificationBackend,
        store=None,
        history: Optional[HistoryStore] = None,
        paths: Optional[RuntimePaths] = None,
        request: Optional[str] = None,
        max_steps: int = 20,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_attempts_per_task: int = DEFAULT_MAX_ATTEMPTS,
    ):
        self._master = master
        self._provider = provider
        self._execution_backend = execution_backend
        self._verification_backend = verification_backend
        paths = paths if paths is not None else RuntimePaths.default()
        self._paths = paths
        self._store = store if store is not None else FileSessionStore(paths.sessions_dir)
        if history is None:
            from core.sqlite_history import SQLiteHistoryStore

            history = SQLiteHistoryStore(paths.history_path)
        self._history = history
        self._request = request
        self._max_steps = max_steps
        self._max_retries = max_retries
        self._max_attempts_per_task = max_attempts_per_task

    @property
    def store(self):
        return self._store

    @property
    def history(self) -> HistoryStore:
        return self._history

    # --- lifecycle -------------------------------------------------------

    def start(self, project_id: str, objective: str, session_id: str) -> WorkSession:
        """Create a session and immediately run it once.

        The session is persisted before any work starts, so a crash during the
        first run still leaves a resumable record rather than nothing at all.
        """
        new = WorkSession.create(
            session_id=session_id, project_id=project_id, objective=objective
        )
        with self._lock(project_id, session_id):
            self._store.create(new)
            return self._run(session_id)

    def resume(self, session_id: str) -> WorkSession:
        """Load a persisted session, run one bounded pass, save, return it.

        Safe to call in a brand-new process: nothing is carried over from a
        previous one. Project and task state are re-read through Master,
        execution evidence is re-read from history, and the request is rebuilt
        from the session's own objective.

        Raises :class:`core.run_lock.ProjectBusyError`, changing nothing, while
        another process holds the project.
        """
        session = self._store.load(session_id)
        self._require_resumable(session)
        with self._lock(session.project_id, session_id):
            return self._run(session_id)

    # --- one locked run --------------------------------------------------

    def _lock(self, project_id: str, session_id: str) -> ProjectLock:
        project_path = self._master.project_state(project_id).project_path
        return ProjectLock(project_path, holder=f"session={session_id}")

    @staticmethod
    def _require_resumable(session: WorkSession) -> None:
        if not session.can_resume():
            raise ValueError(
                f"session {session.session_id!r} is {session.status.value} "
                "and cannot be resumed"
            )

    def _run(self, session_id: str) -> WorkSession:
        """Run once. The caller holds the project lock."""
        # Re-read under the lock: whatever was loaded before acquiring it may
        # already be out of date.
        session = self._store.load(session_id)
        self._require_resumable(session)

        if session.status is SessionStatus.RUNNING:
            # We hold the lock, so the process that set RUNNING is gone.
            session = self._recover(session)

        session = self._store.save(session.evolve(status=SessionStatus.RUNNING))

        loop = AutonomousLoop(
            self._master,
            self._provider,
            self._execution_backend,
            self._verification_backend,
            request=self._request or session.objective,
            max_steps=self._max_steps,
            max_retries=self._max_retries,
            history=self._history,
            session_id=session.session_id,
            max_attempts_per_task=self._max_attempts_per_task,
        )
        try:
            result = loop.run(session.project_id)
        except BaseException:
            # The loop has already recorded run_error. Leave the session in an
            # honest state, then let the failure reach the caller.
            self._store.save(
                session.evolve(
                    status=SessionStatus.STOPPED,
                    last_stop_reason=STOP_ERROR,
                    steps_completed=session.steps_completed
                    + self._steps_in_run(loop.run_id),
                )
            )
            raise

        return self._store.save(self._apply_result(session, result))

    # --- recovery --------------------------------------------------------

    def _steps_in_run(self, run_id: Optional[str]) -> int:
        if run_id is None:
            return 0
        return len(self._history.events(run_id=run_id, types=[EventType.DECISION]))

    def _recover(self, session: WorkSession) -> WorkSession:
        """Close out runs and attempts a dead process left open. No replay."""
        events = self._history.events(session_id=session.session_id)
        ended_runs = {e.run_id for e in events if e.type in _RUN_ENDS}
        open_runs = [
            e.run_id
            for e in events
            if e.type is EventType.RUN_STARTED and e.run_id not in ended_runs
        ]
        ended_attempts = {e.attempt_id for e in events if e.type in _ATTEMPT_ENDS}

        steps = 0
        for run_id in open_runs:
            for event in events:
                if (
                    event.run_id == run_id
                    and event.type is EventType.ATTEMPT_STARTED
                    and event.attempt_id not in ended_attempts
                ):
                    self._history.append(
                        type=EventType.ATTEMPT_INTERRUPTED,
                        run_id=run_id,
                        session_id=session.session_id,
                        project_id=event.project_id,
                        task_id=event.task_id,
                        attempt_id=event.attempt_id,
                        payload={
                            "reason": "the process ended before the attempt's "
                            "outcome was recorded"
                        },
                    )
            self._history.append(
                type=EventType.RUN_INTERRUPTED,
                run_id=run_id,
                session_id=session.session_id,
                project_id=session.project_id,
                payload={"reason": "the process ended before the run's end was recorded"},
            )
            steps += self._steps_in_run(run_id)

        return self._store.save(
            session.evolve(
                status=SessionStatus.STOPPED,
                last_stop_reason=STOP_INTERRUPTED,
                steps_completed=session.steps_completed + steps,
            )
        )

    # --- translating a run into durable state ----------------------------

    @staticmethod
    def _apply_result(session: WorkSession, result: LoopResult) -> WorkSession:
        """Fold one LoopResult into a new session.

        Only observations cross this line. Project and task state are untouched
        here: Master already applied whatever was approved, and re-recording it
        would create a second copy that can disagree with the truth.
        """
        changes = {
            "steps_completed": session.steps_completed + result.steps,
            "last_stop_reason": result.stop_reason,
            "status": status_for_stop_reason(result.stop_reason),
        }

        output = result.last_output
        if output:
            execution = output.get("execution") or {}
            verification = output.get("verification") or {}
            # execution.reason and execution.artifacts are deliberately left
            # out: they are written by whichever worker ran and often name it.
            changes["last_progress"] = {
                "task_id": output.get("task_id"),
                "execution_status": execution.get("status"),
                "verification_verdict": verification.get("verdict"),
                "summary": verification.get("summary"),
            }
            changes["last_task_id"] = output.get("task_id")

        decision = result.decision
        if (
            result.stop_reason == STOP_APPROVAL
            and decision is not None
            and decision.operation is not None
        ):
            changes["pending_approval"] = {
                "operation": decision.operation.operation,
                "approval_state": decision.operation.state.value,
                "decision": decision.decision.value,
                "reason": result.approval_reason or "policy",
            }
        elif result.stop_reason != STOP_APPROVAL:
            # Cleared once a run proceeds past the point that needed a human, so
            # a resumed session does not keep reporting a stale request.
            changes["pending_approval"] = None

        return session.evolve(**changes)