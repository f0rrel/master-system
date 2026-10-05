"""What history says about execution, in the forms orchestration needs.

Three consumers read execution history, and all of them go through the same
"latest attempt" logic here so they cannot disagree:

* the completion gate, which decides whether an autonomous ``completed`` may
  apply without a human;
* the attempt limit, which bounds how often one session may run one task;
* the evidence Master is shown before its next decision.

Only neutral facts leave this module: the attempt's outcome as the
orchestrator observed it (``finished``, ``timed_out``, ``error``,
``interrupted`` or ``unfinished``), what changed (files, insertions,
deletions), whether the attempt was made against the task's current spec, and
the verifier's verdict, summary and findings. The worker's own status is shown
only as ``worker_reported_status``, a claim. Worker output, worker identity
and the execution reason stay in history as opaque provenance and are never
shown to Master or branched on.

Evidence is bound to what it verified: every attempt records the
``spec_hash`` of the task (title and acceptance) it was run against, and a
pass for a different spec does not authorise completion.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

from core.history import EventType, HistoryStore

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "RECENT_DECISIONS",
    "AttemptSummary",
    "HistoryEvidence",
    "attempts_for_task",
    "spec_hash",
]

#: Attempts one session may make at one task before a human must look.
DEFAULT_MAX_ATTEMPTS = 3
#: How many of the session's own recent decisions Master is reminded of.
RECENT_DECISIONS = 5

_ATTEMPT_TYPES = (
    EventType.ATTEMPT_STARTED,
    EventType.ATTEMPT_FINISHED,
    EventType.VERIFICATION,
    EventType.ATTEMPT_INTERRUPTED,
)


def spec_hash(task: Optional[Mapping]) -> str:
    """sha256 of the canonical JSON of the task's spec: title, description, acceptance.

    ``description`` is included only when the task has one, so adding the field
    did not change the hash of tasks without it.
    """
    task = task or {}
    spec = {"title": task.get("title"), "acceptance": task.get("acceptance")}
    if task.get("description") is not None:
        spec["description"] = task.get("description")
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_NOT_A_COMPLETION = object()


@dataclass(frozen=True)
class AttemptSummary:
    """One execution attempt, reduced to the facts decisions may use."""

    attempt_id: str
    session_id: Optional[str]
    run_id: str
    #: Decided by the orchestrator: ``finished``, ``timed_out``, ``error``
    #: (the worker or its workspace raised), ``interrupted`` (found
    #: unfinished during recovery) or ``unfinished`` (no outcome yet).
    outcome: str
    #: The worker's own ExecutionResult status. A claim, never a fact.
    worker_reported_status: Optional[str] = None
    #: The spec the attempt was run against; None for attempts recorded
    #: before spec hashes existed.
    spec_hash: Optional[str] = None
    result_sha: Optional[str] = None
    changes: dict = field(default_factory=dict)
    verified: bool = False
    verdict: Optional[str] = None
    summary: Optional[str] = None
    findings: tuple = ()

    def to_context(self, current_spec_hash: Optional[str] = None) -> dict:
        verification = None
        if self.verified:
            verification = {
                "verdict": self.verdict if self.verdict is not None else "error",
                "summary": self.summary,
                "findings": list(self.findings),
            }
        return {
            "outcome": self.outcome,
            "worker_reported_status": self.worker_reported_status,
            "changes": dict(self.changes),
            "spec_current": (
                self.spec_hash is not None and self.spec_hash == current_spec_hash
            ),
            "verification": verification,
        }


def attempts_for_task(
    history: HistoryStore, project_id: str, task_id: str, **scope
) -> list:
    """Every attempt at task_id, oldest first, optionally limited to a scope."""
    attempts: dict = {}
    for event in history.events(
        project_id=project_id, task_id=task_id, types=_ATTEMPT_TYPES, **scope
    ):
        if event.type is EventType.ATTEMPT_STARTED:
            attempts[event.attempt_id] = {
                "attempt_id": event.attempt_id,
                "session_id": event.session_id,
                "run_id": event.run_id,
                "outcome": "unfinished",
                "spec_hash": event.payload.get("spec_hash"),
            }
            continue
        record = attempts.get(event.attempt_id)
        if record is None:
            continue
        payload = event.payload
        if event.type is EventType.ATTEMPT_FINISHED:
            if "outcome" in payload:
                record["outcome"] = payload["outcome"]
                record["worker_reported_status"] = payload.get("worker_reported_status")
            else:  # recorded before Milestone 1: "status" was the worker's claim
                status = payload.get("status")
                record["outcome"] = "error" if status in (None, "error") else "finished"
                record["worker_reported_status"] = None if status == "error" else status
            record["result_sha"] = payload.get("result_sha")
            diffstat = payload.get("diffstat")
            if isinstance(diffstat, Mapping):
                record["changes"] = {
                    key: diffstat.get(key) for key in ("files", "insertions", "deletions")
                }
        elif event.type is EventType.ATTEMPT_INTERRUPTED:
            if record["outcome"] == "unfinished":
                record["outcome"] = "interrupted"
        elif event.type is EventType.VERIFICATION:
            record["verified"] = True
            record["verdict"] = payload.get("verdict")
            record["summary"] = payload.get("summary")
            record["findings"] = tuple(payload.get("findings") or ())
    return [AttemptSummary(**record) for record in attempts.values()]


class HistoryEvidence:
    """Evidence about execution for one loop, read from history.

    The scope of "this session" is the session when there is one. A loop run
    without a session scopes to its current run, which :class:`AutonomousLoop`
    sets through :attr:`run_id`. With neither, the scope is all of history.
    """

    def __init__(
        self,
        history: HistoryStore,
        *,
        session_id: Optional[str] = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._history = history
        self._session_id = session_id
        self.max_attempts = max_attempts
        self.run_id: Optional[str] = None

    def _scope(self) -> dict:
        if self._session_id is not None:
            return {"session_id": self._session_id}
        if self.run_id is not None:
            return {"run_id": self.run_id}
        return {}

    # --- attempt limit ---------------------------------------------------

    def attempts_in_scope(self, project_id: str, task_id: str) -> int:
        """Attempts that started, whatever their outcome. Errors and
        interruptions count: the worker actually started."""
        return len(attempts_for_task(self._history, project_id, task_id, **self._scope()))

    def attempt_limit_reached(self, project_id: str, task_id: str) -> bool:
        return self.attempts_in_scope(project_id, task_id) >= self.max_attempts

    # --- completion gate -------------------------------------------------

    def completion_gate(self, operation, current_task: Optional[Mapping] = None) -> Optional[str]:
        return self.completion_check(operation, current_task)[0]

    def completion_check(self, operation, current_task: Optional[Mapping] = None):
        """``(reason, attempt_id)``: the gate's verdict and the attempt it judged.

        ``attempt_id`` is the task's latest attempt when the operation completes
        a task (None if there is none, or for any other operation), so every
        autonomous completion traces to the evidence that authorised it.
        """
        reason = self._completion_reason(operation, current_task)
        if reason is _NOT_A_COMPLETION:
            return None, None
        arguments = operation.arguments
        attempts = attempts_for_task(self._history, arguments.get("project_id"),
                                     arguments.get("task_id"))
        return reason, (attempts[-1].attempt_id if attempts else None)

    def _completion_reason(self, operation, current_task: Optional[Mapping] = None):
        """Why an autonomous ``completed`` must wait for a human, or None.

        Applies to any operation that would set a task's status to
        ``completed``. It may apply autonomously only when the task's latest
        attempt -- across all of history, since a pass from an earlier session
        is still evidence -- finished, was run against the task's spec as it
        will be *after* the operation (so retitling and completing in one
        operation does not pass), and was verified as ``pass``. Every other
        status change is left to the approval policy alone.

        ``current_task`` is the task record as it is now, or None if it does
        not exist yet.
        """
        arguments = operation.arguments
        if arguments.get("status") != "completed":
            return _NOT_A_COMPLETION
        if operation.operation not in ("update_task", "create_task"):
            return _NOT_A_COMPLETION

        task_id = arguments.get("task_id")
        project_id = arguments.get("project_id")
        attempts = attempts_for_task(self._history, project_id, task_id)
        if not attempts:
            return "no_attempt"

        latest = attempts[-1]
        if latest.outcome == "interrupted":
            return "latest_attempt_interrupted"
        if latest.outcome == "unfinished":
            return "latest_attempt_unfinished"
        if latest.outcome == "error":
            return "latest_attempt_errored"
        if latest.outcome == "timed_out":
            return "latest_attempt_timed_out"
        projected = dict(current_task or {})
        if "title" in arguments:
            projected["title"] = arguments["title"]
        if latest.spec_hash is None or latest.spec_hash != spec_hash(projected):
            return "spec_changed"
        if not latest.verified:
            return "not_verified"
        if latest.verdict is None:
            return "verification_errored"
        if latest.verdict != "pass":
            return f"verification_{latest.verdict}"
        return None

    # --- context for Master ---------------------------------------------

    def for_project(self, project_id: str, focus_task_ids: Iterable[str],
                    task_records: Optional[Mapping[str, Mapping]] = None) -> Mapping:
        """The context keys Master sees about execution in project_id.

        Returns ``execution_evidence`` and ``recent_decisions``. Evidence covers
        the given tasks (the ones in progress) and any task this
        session has attempted. Neutral facts only.
        """
        scope = self._scope()
        attempted_here = {
            event.task_id
            for event in self._history.events(
                project_id=project_id, types=[EventType.ATTEMPT_STARTED], **scope
            )
        }
        task_ids = sorted(set(focus_task_ids) | attempted_here)

        tasks = {}
        for task_id in task_ids:
            every = attempts_for_task(self._history, project_id, task_id)
            here = attempts_for_task(self._history, project_id, task_id, **scope)
            tasks[task_id] = {
                "attempts_this_session": len(here),
                "attempts_total": len(every),
                "latest_attempt": (
                    every[-1].to_context(
                        spec_hash((task_records or {}).get(task_id))
                        if task_records is not None and task_id in task_records
                        else None
                    )
                    if every else None
                ),
            }

        recent = []
        for event in self._history.events(
            project_id=project_id,
            types=[EventType.DECISION],
            last=RECENT_DECISIONS,
            **scope,
        ):
            payload = event.payload
            operation = payload.get("operation") or {}
            recent.append(
                {
                    "decision": payload.get("decision"),
                    "operation": operation.get("name"),
                    "task_id": event.task_id,
                    "reason": payload.get("reason"),
                }
            )

        return {
            "execution_evidence": {"attempt_limit": self.max_attempts, "tasks": tasks},
            "recent_decisions": recent,
        }
