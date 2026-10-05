"""Run bounded autonomous work as a durable session.

This is the seam between the in-memory loop and the on-disk session. It does
not change what the loop does: it constructs the existing
:class:`core.autonomous_loop.AutonomousLoop` with injected collaborators, runs
it once, and translates the resulting
:class:`core.autonomous_loop.LoopResult` into durable session fields.

The loop is deliberately left alone. Per-step persistence would need a hook
inside ``AutonomousLoop.run()``, and adding one for this milestone would put a
durability concern into the module whose job is bounded decision sequencing.
So a session is saved at the boundaries that already exist in the problem:
created, started, and finished. A process that dies mid-run therefore resumes
from its last completed step rather than from the step it was in, which is the
honest V1 guarantee and is why the session records ``steps_completed`` as a
count rather than pretending to be a checkpoint log.

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
from core.execution import ExecutionBackend
from core.master import Master
from core.provider import ReasoningProvider
from core.session_store import FileSessionStore
from core.verification import VerificationBackend
from core.work_session import SessionStatus, WorkSession, status_for_stop_reason


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
        request: Optional[str] = None,
        max_steps: int = 20,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ):
        self._master = master
        self._provider = provider
        self._execution_backend = execution_backend
        self._verification_backend = verification_backend
        self._store = store if store is not None else FileSessionStore()
        self._request = request
        self._max_steps = max_steps
        self._max_retries = max_retries

    @property
    def store(self):
        return self._store

    # --- lifecycle -------------------------------------------------------

    def start(self, project_id: str, objective: str, session_id: str) -> WorkSession:
        """Create a session and immediately run it once.

        The session is persisted before any work starts, so a crash during the
        first run still leaves a resumable record rather than nothing at all.
        """
        session = self._store.create(
            WorkSession.create(
                session_id=session_id,
                project_id=project_id,
                objective=objective,
            )
        )
        return self.resume(session.session_id)

    def resume(self, session_id: str) -> WorkSession:
        """Load a persisted session, run one bounded pass, save, return it.

        Safe to call in a brand-new process: nothing is carried over from a
        previous one. Project and task state are re-read through Master, and
        the request is rebuilt from the session's own objective.
        """
        session = self._store.load(session_id)

        if not session.can_resume():
            raise ValueError(
                f"session {session_id!r} is {session.status.value} and cannot be resumed"
            )

        # Only claim to be running once we are about to be. A session left as
        # RUNNING by a crashed process is therefore never written here.
        session = self._store.save(session.evolve(status=SessionStatus.RUNNING))

        loop = AutonomousLoop(
            self._master,
            self._provider,
            self._execution_backend,
            self._verification_backend,
            request=self._request or session.objective,
            max_steps=self._max_steps,
            max_retries=self._max_retries,
        )
        result = loop.run(session.project_id)

        return self._store.save(self._apply_result(session, result))

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
        if decision is not None and getattr(decision, "pending_approval", False):
            changes["pending_approval"] = {
                "operation": decision.operation.operation,
                "approval_state": decision.operation.state.value,
                "decision": decision.decision.value,
            }
        elif result.stop_reason != STOP_APPROVAL:
            # Cleared once a run proceeds past the point that needed a human, so
            # a resumed session does not keep reporting a stale request.
            changes["pending_approval"] = None

        return session.evolve(**changes)