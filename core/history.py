"""Append-only history of what autonomous work actually did.

History is evidence, not authority. ProjectState says what is true about the
work; WorkSession says where the current run stands; history says what was
decided, what was attempted and what was observed, in order, and is never
rewritten. Nothing here is memory: an event records an observation, it does not
promote it into something the system believes.

Two rules give history its value for recovery:

* A side effect is preceded by a committed intent. ``attempt_started`` is
  appended before a worker runs, and a state operation is preceded by its
  ``decision``. An intent without an outcome is therefore visible after a
  crash, as an interrupted, uncertain step.
* History and ProjectState are separate stores and are not atomic together.
  If the process dies between a state mutation and its ``operation_result``,
  ProjectState is authoritative and the missing event is an audit gap, not
  something to repair.

Worker output is stored as opaque provenance. It may name a runtime or a
model; that is fine in a record, and it is never something orchestration
branches on or shows to Master.

This module holds the contract and an in-memory store. The durable store lives
in :mod:`core.sqlite_history`, the only module that knows SQLite exists.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable, Mapping, Optional, Protocol, runtime_checkable

__all__ = [
    "ATTEMPT_EVENT_TYPES",
    "EVENT_ID_PATTERN",
    "MAX_PAYLOAD_BYTES",
    "EventType",
    "HistoryEvent",
    "HistoryStore",
    "InMemoryHistoryStore",
    "InvalidEventError",
    "jsonable",
    "new_event_id",
    "prepare_event",
]


class InvalidEventError(ValueError):
    """Raised when an event cannot be recorded as given."""


class EventType(Enum):
    RUN_STARTED = "run_started"
    DECISION = "decision"
    OPERATION_RESULT = "operation_result"
    ATTEMPT_STARTED = "attempt_started"
    ATTEMPT_FINISHED = "attempt_finished"
    VERIFICATION = "verification"
    ATTEMPT_INTERRUPTED = "attempt_interrupted"
    RUN_STOPPED = "run_stopped"
    RUN_ERROR = "run_error"
    RUN_INTERRUPTED = "run_interrupted"
    #: A process started for an attempt (worker or verification): pid, pgid.
    ATTEMPT_PROCESS = "attempt_process"
    #: A human integrated a verified attempt's result into the base branch.
    INTEGRATION = "integration"
    #: A human edited a task's spec through run_cli (actor, action, spec hashes).
    HUMAN_ACTION = "human_action"
    #: Schema v4 (Milestone 3). The system could not integrate a verified
    #: attempt (rebase conflict, failed re-verification, ...): reason, detail.
    INTEGRATION_REFUSED = "integration_refused"
    #: A branch or site was published (pushed) to the project's remote.
    PUBLISHED = "published"
    #: One turn of the planner chat (usage, model; the draft's hash).
    PLANNER_TURN = "planner_turn"
    #: A release step: notes and PR opened, or tag and GitHub Release made.
    RELEASE = "release"


#: Events that belong to one execution attempt and must name it.
ATTEMPT_EVENT_TYPES = frozenset(
    {
        EventType.ATTEMPT_FINISHED,
        EventType.VERIFICATION,
        EventType.ATTEMPT_INTERRUPTED,
        EventType.ATTEMPT_PROCESS,
        EventType.INTEGRATION,
        EventType.INTEGRATION_REFUSED,
    }
)

#: The shape of a generated event id; a caller-provided attempt id must match it.
EVENT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def new_event_id() -> str:
    return uuid.uuid4().hex

#: Upper bound on one serialised payload. Worker artifacts can carry whole
#: files; history keeps a fingerprint of anything larger rather than the bytes.
MAX_PAYLOAD_BYTES = 256 * 1024
#: Strings longer than this are the first thing cut when a payload is too big.
_STRING_CAP = 16 * 1024
_HEAD_CHARS = 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def jsonable(value: object) -> object:
    """Return ``value`` as plain JSON data, rendering anything else with repr.

    Used for worker output, which is opaque and may hold paths, tuples or
    other objects. Never used for anything orchestration interprets.
    """
    return json.loads(json.dumps(value, default=repr))


def _fingerprint(text: str) -> dict:
    encoded = text.encode("utf-8")
    return {
        "truncated": True,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "original_bytes": len(encoded),
    }


def _cut_strings(value: object) -> object:
    if isinstance(value, str):
        if len(value) <= _STRING_CAP:
            return value
        marker = _fingerprint(value)
        marker["head"] = value[:_HEAD_CHARS]
        return marker
    if isinstance(value, dict):
        return {key: _cut_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_cut_strings(item) for item in value]
    return value


def _bounded_payload(payload: Mapping) -> str:
    """Serialise payload, shrinking it deterministically to fit the cap."""
    try:
        text = json.dumps(payload, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise InvalidEventError(f"payload is not JSON-serialisable: {error}") from error

    if len(text.encode("utf-8")) <= MAX_PAYLOAD_BYTES:
        return text

    cut = json.dumps(_cut_strings(json.loads(text)), sort_keys=True)
    if len(cut.encode("utf-8")) <= MAX_PAYLOAD_BYTES:
        return cut

    whole = _fingerprint(text)
    whole["keys"] = sorted(payload)
    return json.dumps(whole, sort_keys=True)


def _optional_id(value: object, name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidEventError(f"{name} must be a non-empty string or None")
    return value


def _required_id(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidEventError(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True)
class HistoryEvent:
    """One recorded event. The payload is held as JSON text so it cannot change."""

    seq: int
    event_id: str
    run_id: str
    session_id: Optional[str]
    project_id: str
    task_id: Optional[str]
    attempt_id: Optional[str]
    type: EventType
    payload_json: str
    created_at: str

    @property
    def payload(self) -> dict:
        """A fresh copy of the payload on every access."""
        return json.loads(self.payload_json)


def prepare_event(
    *,
    type: object,
    run_id: object,
    project_id: object,
    session_id: object = None,
    task_id: object = None,
    attempt_id: object = None,
    payload: object = None,
    event_id: object = None,
) -> dict:
    """Validate one append request and return the row a store should write.

    Shared by every store so that the rules cannot differ between them. An
    ``attempt_started`` event names itself as its attempt; the events that
    follow it must name that attempt and its task.

    ``event_id`` may be supplied only for ``attempt_started``: the
    orchestrator chooses the attempt id first so the event can record the
    worktree path derived from it before the worktree exists. Stores still
    refuse a duplicate id.
    """
    try:
        event_type = EventType(type)
    except ValueError as error:
        raise InvalidEventError(f"unknown event type {type!r}") from error

    if payload is None:
        payload = {}
    if not isinstance(payload, Mapping):
        raise InvalidEventError("payload must be a mapping")

    if event_id is not None:
        if event_type is not EventType.ATTEMPT_STARTED:
            raise InvalidEventError("only attempt_started may supply its event_id")
        if not isinstance(event_id, str) or not EVENT_ID_PATTERN.match(event_id):
            raise InvalidEventError("event_id must be 32 lowercase hex characters")
    else:
        event_id = new_event_id()
    task_id = _optional_id(task_id, "task_id")
    attempt_id = _optional_id(attempt_id, "attempt_id")

    if event_type is EventType.ATTEMPT_STARTED:
        if attempt_id is not None:
            raise InvalidEventError("attempt_started defines its own attempt_id")
        if task_id is None:
            raise InvalidEventError("attempt_started requires task_id")
        attempt_id = event_id
    elif event_type in ATTEMPT_EVENT_TYPES:
        if attempt_id is None or task_id is None:
            raise InvalidEventError(
                f"{event_type.value} requires attempt_id and task_id"
            )

    return {
        "event_id": event_id,
        "run_id": _required_id(run_id, "run_id"),
        "session_id": _optional_id(session_id, "session_id"),
        "project_id": _required_id(project_id, "project_id"),
        "task_id": task_id,
        "attempt_id": attempt_id,
        "type": event_type,
        "payload_json": _bounded_payload(payload),
        "created_at": _now(),
    }


def _types(types: Optional[Iterable]) -> Optional[frozenset]:
    if types is None:
        return None
    return frozenset(EventType(t) for t in types)


@runtime_checkable
class HistoryStore(Protocol):
    """Where history lives. Append and read; there is no update or delete."""

    def append(
        self,
        *,
        type,
        run_id: str,
        project_id: str,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
        payload: Optional[Mapping] = None,
        event_id: Optional[str] = None,
    ) -> HistoryEvent:
        """Durably record one event and return it."""
        ...

    def events(
        self,
        *,
        project_id: Optional[str] = None,
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
        types: Optional[Iterable] = None,
        last: Optional[int] = None,
    ) -> tuple:
        """Matching events in recording order; ``last`` keeps only the newest N."""
        ...


class InMemoryHistoryStore:
    """A HistoryStore that lasts as long as the process. Same rules, no disk."""

    def __init__(self):
        self._events: list[HistoryEvent] = []

    def append(self, **request) -> HistoryEvent:
        row = prepare_event(**request)
        if any(e.event_id == row["event_id"] for e in self._events):
            raise InvalidEventError(f"duplicate event_id {row['event_id']}")
        event = HistoryEvent(seq=len(self._events) + 1, **row)
        self._events.append(event)
        return event

    def events(
        self,
        *,
        project_id=None,
        session_id=None,
        run_id=None,
        task_id=None,
        attempt_id=None,
        types=None,
        last=None,
    ) -> tuple:
        wanted = _types(types)
        matches = [
            e
            for e in self._events
            if (project_id is None or e.project_id == project_id)
            and (session_id is None or e.session_id == session_id)
            and (run_id is None or e.run_id == run_id)
            and (task_id is None or e.task_id == task_id)
            and (attempt_id is None or e.attempt_id == attempt_id)
            and (wanted is None or e.type in wanted)
        ]
        if last is not None:
            matches = matches[-last:] if last > 0 else []
        return tuple(matches)
