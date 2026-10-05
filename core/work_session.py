"""The durable session boundary for autonomous work.

A :class:`WorkSession` is the minimum a new Python process needs in order to
continue work that an earlier process started. It is deliberately *not* a
second source of truth. It holds no task list, no milestone list and no
project data: Master remains the only writer of those, and a resumed session
re-reads them through Master exactly as a first run would. What the session
adds is the one thing project state cannot express -- what this particular
run of autonomous work was trying to achieve, how far it got, and why it
stopped.

Why a session exists at all
---------------------------
A bounded :class:`core.autonomous_loop.AutonomousLoop` keeps its progress in
memory. It records the last execution and verification result on the reasoning
engine so the *next turn* can see it, and that is correct for one process. It
is not enough to resume: when the process exits, the only record is whatever
the model said in a message, and reconstructing state from chat history is
both lossy and untrustworthy. The session is the durable note that says what
happened, in structured fields, so a fresh process does not have to guess.

What is deliberately absent
--------------------------
* No provider, model, runtime or tool name. Those are injected dependencies,
  chosen by the composition root, and recording them would make a session
  unusable the moment the choice changes. The loop's own stop reasons are kept
  because they describe the work, not the worker.
* No task or project state, and no copy of the executed task.
* No cycle timing. ``steps_completed`` counts; it does not measure. Timing
  belongs to whatever durable runtime eventually exists, not to a model that
  only counts.
* No event log. The session is current state plus a small amount of history
  (created/started/updated), not a replayable sequence. What each run decided
  and attempted is recorded step by step in :mod:`core.history`.

Lifecycle
---------
Six states, and the mapping from the loop's stop reasons is in
:func:`status_for_stop_reason`. Two states in the obvious set are left out:
``paused`` would be a second spelling of ``stopped`` while V1 has no pause
operation, and nothing sets ``completed`` or ``failed`` automatically, because
deciding that a human objective has been met is a judgement this system has
not been given the authority to make.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Mapping, Optional, Protocol, runtime_checkable

__all__ = [
    "SESSION_ID_PATTERN",
    "WorkSession",
    "InvalidSessionError",
    "SessionError",
    "SessionStatus",
    "SessionStore",
    "status_for_stop_reason",
]


class SessionError(Exception):
    """Base error for the work-session layer."""


class InvalidSessionError(SessionError, ValueError):
    """Raised when session data cannot be read as a valid WorkSession."""


class SessionStatus(Enum):
    """Where a session is in its life.

    ``CREATED`` -> ``RUNNING`` is the normal beginning. ``STOPPED`` is the
    interesting one: it means a bounded run ended for any ordinary reason and
    the session can be resumed, which is the state most sessions spend their
    life in. ``NEEDS_HUMAN`` is the state that is genuinely stuck on someone
    else. ``COMPLETED`` and ``FAILED`` are only ever set deliberately.
    """

    CREATED = "created"
    RUNNING = "running"
    NEEDS_HUMAN = "needs_human"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"


#: Only reachable transitions. A session that cannot legally move is a bug, so
#: it is refused rather than silently allowed: an out-of-order write would
#: otherwise make a resumed session claim to be running while it is not.
ALLOWED_TRANSITIONS: Mapping[SessionStatus, frozenset] = {
    SessionStatus.CREATED: frozenset(
        {SessionStatus.RUNNING, SessionStatus.STOPPED, SessionStatus.FAILED}
    ),
    SessionStatus.RUNNING: frozenset(
        {
            SessionStatus.STOPPED,
            SessionStatus.NEEDS_HUMAN,
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
        }
    ),
    SessionStatus.NEEDS_HUMAN: frozenset(
        {SessionStatus.RUNNING, SessionStatus.STOPPED, SessionStatus.FAILED}
    ),
    SessionStatus.STOPPED: frozenset(
        {SessionStatus.RUNNING, SessionStatus.COMPLETED, SessionStatus.FAILED}
    ),
    SessionStatus.COMPLETED: frozenset(),
    SessionStatus.FAILED: frozenset({SessionStatus.RUNNING, SessionStatus.STOPPED}),
}

#: Conservative on purpose: readable, sortable, and cannot be mistaken for a
#: path. Session ids come from humans and from filenames, so anything that
#: could escape a directory is refused before it is used as one.
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidSessionError(f"{field_name} must be a non-empty string")
    return value


def _optional_text(value: object, field_name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidSessionError(f"{field_name} must be a non-empty string or null")
    return value


def _validate_session_id(session_id: object) -> str:
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.match(session_id):
        raise InvalidSessionError(
            "session_id must start alphanumeric and contain only letters, "
            "digits, dot, underscore or hyphen (max 64 chars)"
        )
    return session_id


def _validate_timestamp(value: object, field_name: str) -> str:
    _require_text(value, field_name)
    try:
        datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise InvalidSessionError(
            f"{field_name} must be an ISO 8601 timestamp, got {value!r}"
        ) from error
    return value


def _validate_progress(value: object) -> Optional[dict]:
    """Validate last_progress, keeping only provider-neutral observations.

    The execution *reason* and the backend *artifacts* are deliberately
    dropped: they are written by whichever worker ran, so a backend whose
    reason string names its own runtime would otherwise leak that name into
    the durable session and tie the record to today's tooling.
    """
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise InvalidSessionError("last_progress must be a mapping or null")
    allowed = ("task_id", "execution_status", "verification_verdict", "summary")
    unexpected = sorted(set(value) - set(allowed))
    if unexpected:
        raise InvalidSessionError(
            f"last_progress has unsupported keys {unexpected}; allowed: {list(allowed)}"
        )
    progress = {}
    for key in allowed:
        if key in value:
            progress[key] = _optional_text(value[key], f"last_progress.{key}")
    return progress or None


def _validate_pending(value: object) -> Optional[dict]:
    """Validate pending_approval, which may only describe enums we already have.

    ``reason`` is optional and says why a human is needed: ``policy`` for an
    operation the approval policy gates, or a completion-gate reason such as
    ``verification_fail``.
    """
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise InvalidSessionError("pending_approval must be a mapping or null")
    required = ("operation", "approval_state", "decision")
    allowed = required + ("reason",)
    unexpected = sorted(set(value) - set(allowed))
    if unexpected:
        raise InvalidSessionError(
            f"pending_approval has unsupported keys {unexpected}; allowed: {list(allowed)}"
        )
    pending = {key: _require_text(value.get(key), f"pending_approval.{key}") for key in required}
    if "reason" in value:
        pending["reason"] = _require_text(value["reason"], "pending_approval.reason")
    return pending


@dataclass(frozen=True)
class WorkSession:
    """Durable record of one bounded run of autonomous work.

    Immutable. Every change produces a new session via :meth:`evolve`, which
    is what makes "save the session at a safe boundary" an explicit step rather
    than a mutation that can be forgotten.
    """

    session_id: str
    project_id: str
    objective: str
    status: SessionStatus
    created_at: str
    updated_at: str
    started_at: Optional[str] = None
    #: How many bounded loop steps this session has completed in total. A
    #: count, not a duration: no timing belongs here yet.
    steps_completed: int = 0
    #: The last task this session acted on, by id only -- never a copy of the
    #: task. What is *currently* in flight is deliberately absent: Master owns
    #: that, and a resumed session re-reads it from Master rather than trusting
    #: a field that could disagree with the truth.
    last_task_id: Optional[str] = None
    #: The latest meaningful outcome, as observations only.
    last_progress: Optional[dict] = None
    #: Set when a decision is waiting on a human.
    pending_approval: Optional[dict] = None
    #: The loop's own words for why it stopped. Provider-neutral by construction.
    last_stop_reason: Optional[str] = None

    def __post_init__(self):
        _validate_session_id(self.session_id)
        _require_text(self.project_id, "project_id")
        _require_text(self.objective, "objective")
        _validate_timestamp(self.created_at, "created_at")
        _validate_timestamp(self.updated_at, "updated_at")
        if self.started_at is not None:
            _validate_timestamp(self.started_at, "started_at")
        if not isinstance(self.status, SessionStatus):
            raise InvalidSessionError(
                f"status must be a SessionStatus, got {self.status!r}"
            )
        if isinstance(self.steps_completed, bool) or not isinstance(
            self.steps_completed, int
        ):
            raise InvalidSessionError("steps_completed must be an integer")
        if self.steps_completed < 0:
            raise InvalidSessionError("steps_completed must not be negative")
        _optional_text(self.last_task_id, "last_task_id")
        _optional_text(self.last_stop_reason, "last_stop_reason")
        _validate_progress(self.last_progress)
        _validate_pending(self.pending_approval)
        if self.started_at and self.updated_at < self.started_at:
            raise InvalidSessionError("updated_at cannot precede started_at")

    @classmethod
    def create(cls, session_id: str, project_id: str, objective: str) -> "WorkSession":
        """Start a new session in CREATED. Nothing has run yet."""
        stamp = _now()
        return cls(
            session_id=_validate_session_id(session_id),
            project_id=_require_text(project_id, "project_id"),
            objective=_require_text(objective, "objective"),
            status=SessionStatus.CREATED,
            created_at=stamp,
            updated_at=stamp,
        )

    def evolve(self, **changes) -> "WorkSession":
        """Return a copy with ``changes`` applied, stamping ``updated_at``.

        Refuses a lifecycle change that is not allowed from the current state,
        so an out-of-order write fails loudly instead of producing a session
        that lies about whether it is running.
        """
        if "status" in changes:
            wanted = changes["status"]
            if not isinstance(wanted, SessionStatus):
                raise InvalidSessionError(
                    f"status must be a SessionStatus, got {wanted!r}"
                )
            if wanted is not self.status and wanted not in ALLOWED_TRANSITIONS[
                self.status
            ]:
                allowed = sorted(s.value for s in ALLOWED_TRANSITIONS[self.status])
                raise InvalidSessionError(
                    f"cannot move session from {self.status.value} to "
                    f"{wanted.value}; allowed: {allowed}"
                )
        changes.setdefault("updated_at", _now())
        if changes.get("started_at") is None and self.started_at is None:
            if changes.get("status") is SessionStatus.RUNNING:
                changes["started_at"] = changes["updated_at"]
        return replace(self, **changes)

    def can_resume(self) -> bool:
        """Whether resuming this session is a meaningful thing to do.

        A completed session has nothing left to continue. A RUNNING session is
        still a candidate: whether its process is alive is decided by the
        project lock, not by this field, and
        :class:`core.session_runner.SessionRunner` refuses while the lock is
        held and recovers the session when it is not.
        """
        return self.status is not SessionStatus.COMPLETED

    # --- serialisation ---------------------------------------------------

    def to_dict(self) -> dict:
        """Plain data for persistence. Round-trips through :meth:`from_dict`."""
        return {
            "session_id": self.session_id,
            "project_id": self.project_id,
            "objective": self.objective,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "steps_completed": self.steps_completed,
            "last_task_id": self.last_task_id,
            "last_progress": self.last_progress,
            "pending_approval": self.pending_approval,
            "last_stop_reason": self.last_stop_reason,
        }

    @classmethod
    def from_dict(cls, data: object) -> "WorkSession":
        """Rebuild a session from persisted data, validating as it goes.

        Unknown keys are refused rather than ignored. A future version that
        adds a field should not have an older reader silently drop it, because
        that is how a resumed session quietly loses the thing it was for.
        """
        if not isinstance(data, Mapping):
            raise InvalidSessionError(
                f"session data must be a mapping, got {type(data).__name__}"
            )
        known = {
            "session_id",
            "project_id",
            "objective",
            "status",
            "created_at",
            "started_at",
            "updated_at",
            "steps_completed",
            "last_task_id",
            "last_progress",
            "pending_approval",
            "last_stop_reason",
        }
        unexpected = sorted(set(data) - known)
        if unexpected:
            raise InvalidSessionError(f"unknown session fields {unexpected}")
        missing = sorted(
            key
            for key in ("session_id", "project_id", "objective", "status",
                        "created_at", "updated_at")
            if key not in data
        )
        if missing:
            raise InvalidSessionError(f"session data is missing {missing}")

        raw_status = data["status"]
        try:
            status = SessionStatus(raw_status)
        except ValueError as error:
            raise InvalidSessionError(
                f"unknown session status {raw_status!r}; "
                f"valid: {sorted(s.value for s in SessionStatus)}"
            ) from error

        return cls(
            session_id=data["session_id"],
            project_id=data["project_id"],
            objective=data["objective"],
            status=status,
            created_at=data["created_at"],
            started_at=data.get("started_at"),
            updated_at=data["updated_at"],
            steps_completed=data.get("steps_completed", 0),
            last_task_id=data.get("last_task_id"),
            last_progress=data.get("last_progress"),
            pending_approval=data.get("pending_approval"),
            last_stop_reason=data.get("last_stop_reason"),
        )


def status_for_stop_reason(stop_reason: object) -> SessionStatus:
    """Map a loop stop reason onto a session status.

    Only two outcomes are distinguished. A run that stopped waiting on a human
    -- an approval, or a task that used up its attempts -- is ``NEEDS_HUMAN``,
    because resuming it without that human's answer would just repeat the same
    stop. Everything else -- no work left, step limit reached, Master
    declined, a refused operation, an unusable reply -- is ``STOPPED``: the
    run ended cleanly and the session is resumable.

    ``running``/``created`` are not produced here, and neither are ``completed``
    or ``failed``: this function reports what happened to a bounded run, and
    deciding that an objective is met is not a thing a stop reason can say.
    """
    from core.autonomous_loop import STOP_APPROVAL, STOP_ATTEMPT_LIMIT

    if stop_reason in (STOP_APPROVAL, STOP_ATTEMPT_LIMIT):
        return SessionStatus.NEEDS_HUMAN
    return SessionStatus.STOPPED


@runtime_checkable
class SessionStore(Protocol):
    """Where sessions live.

    An interface, not a filesystem. The orchestration layer depends on this so
    that a different backing store later is a new implementation here and
    nothing else; exactly as ``ExecutionBackend`` lets the worker change
    without the runner changing.
    """

    def create(self, session: WorkSession) -> WorkSession:
        """Store a new session. Fails if the id is already taken."""
        ...

    def load(self, session_id: str) -> WorkSession:
        """Return a stored session by id."""
        ...

    def save(self, session: WorkSession) -> WorkSession:
        """Replace a stored session with ``session``."""
        ...

    def list_sessions(self, project_id: Optional[str] = None) -> tuple:
        """Return stored sessions, newest first, optionally filtered by project."""
        ...