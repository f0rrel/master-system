"""What a retry learns from the attempts before it, and when retrying stops.

Before this module a retry was blind: the worker got the same prompt as the failed
attempt, so a task that failed because it touched a protected path failed again the
same way, up to the failure cap. Everything here is deterministic: no model call
decides whether to retry or what the worker is told.

* :func:`task_failures` reads the task's failed attempts since the owner's last
  action on it, against its *current* spec (a failure under old acceptance criteria
  is not evidence about the current ones). Each failure gets a **fingerprint**: a
  stable hash of what went wrong (the findings, not the timings), with paths, numbers
  and hex ids normalised out, so the same test failing at different speeds is the
  same failure.
* :func:`repeated_failure` says whether the last ``limit`` failures were identical
  on the same worker profile. The service then stops retrying and tells the owner
  (``core.daemon.work_for``); a different worker profile (an escalation) starts a
  new streak, because it is a genuinely different attempt.
* :func:`retry_note` is the extra instruction the next attempt's prompt gets: what
  failed, the sanitized evidence, and, when the same failure repeated, that the
  approach must change. It never touches the task's acceptance criteria: those stay
  the human-written, frozen spec the verifier checks.

Failure details are redacted (``core.redaction``) and bounded.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Mapping, Optional

from core.evidence import spec_hash
from core.history import EventType
from core.redaction import redact_output

__all__ = [
    "DEFAULT_IDENTICAL_FAILURES",
    "AttemptFailure",
    "describe_failure",
    "identical_streak",
    "repeated_failure",
    "retry_limits",
    "retry_note",
    "task_failures",
]

#: Identical failures in a row (same worker profile) before automatic retries stop.
DEFAULT_IDENTICAL_FAILURES = 2
#: How much of the failure evidence goes into the next prompt.
NOTE_DETAIL_CHARS = 2500
_EXCERPT_LINES = 15
_FINGERPRINT_LINES = 12

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_HEX = re.compile(r"\b[0-9a-f]{8,}\b")
_DIGITS = re.compile(r"\d+")
_SPACE = re.compile(r"\s+")
#: Outcomes that are infrastructure, not the task's failure (as in core.daemon).
_NOT_THE_TASKS_FAULT = ("limited", "stalled")


@dataclass(frozen=True)
class AttemptFailure:
    attempt_id: str
    #: The worker profile that made the attempt (None for a single default worker).
    profile: Optional[str]
    #: verification_failed | verification_unavailable | timed_out | worker_error | no_result
    kind: str
    fingerprint: str
    summary: str
    #: Sanitized, bounded lines of evidence (findings, output excerpts).
    details: tuple = ()


def _normalise(text) -> str:
    """The stable part of program output: no colours, ids, numbers or whitespace runs."""
    text = _ANSI.sub("", str(text or "")).lower()
    text = _HEX.sub("<hex>", text)
    text = _DIGITS.sub("#", text)
    lines = [_SPACE.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join([line for line in lines if line][-_FINGERPRINT_LINES:])


def _finding_signature(finding) -> list:
    if not isinstance(finding, Mapping):
        return ["malformed", _normalise(finding)]
    kind = str(finding.get("kind") or "unknown")
    if kind in ("protected_path", "outside_allowed_paths"):
        return [kind, str(finding.get("path"))]
    if kind == "command_failed":
        return [kind, str(finding.get("command")), finding.get("exit_code"),
                bool(finding.get("timed_out")), _normalise(finding.get("output_excerpt"))]
    if kind == "repository_tampered":
        changed = finding.get("changed")
        return [kind, sorted(str(c) for c in changed) if isinstance(changed, list) else []]
    scalars = {k: v for k, v in finding.items()
               if isinstance(v, (str, int, bool)) and len(str(v)) <= 200}
    return [kind, _normalise(json.dumps(scalars, sort_keys=True))]


def _hash(signature) -> str:
    raw = json.dumps(signature, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _excerpt(text, lines: int = _EXCERPT_LINES) -> str:
    kept = [line.rstrip() for line in str(text or "").splitlines() if line.strip()]
    return "\n".join(kept[-lines:])


def _finding_details(finding) -> str:
    if not isinstance(finding, Mapping):
        return "a malformed finding was recorded"
    kind = str(finding.get("kind") or "unknown")
    if kind == "protected_path":
        return f"you changed a protected path: {finding.get('path')}"
    if kind == "outside_allowed_paths":
        return f"you changed a path outside the allowed ones: {finding.get('path')}"
    if kind == "repository_tampered":
        return f"the repository was changed outside your branch: {finding.get('changed')}"
    if kind == "command_failed":
        head = f"acceptance command failed (exit {finding.get('exit_code')}"
        head += ", timed out)" if finding.get("timed_out") else ")"
        excerpt = _excerpt(finding.get("output_excerpt"))
        return f"{head}: {finding.get('command')}" + (f"\n{excerpt}" if excerpt else "")
    return f"{kind}: {_normalise(finding)}"


def _classify(started, finished, verification) -> Optional[tuple]:
    """``(kind, summary, signature, details)`` for a failed attempt, else None."""
    outcome = finished.get("outcome")
    if verification is not None and verification.get("verdict") == "pass":
        return None
    if outcome in _NOT_THE_TASKS_FAULT or outcome is None:
        return None
    if outcome == "timed_out":
        return ("timed_out", "the attempt ran past its deadline", ["timed_out"], ())
    if outcome == "error":
        error_type = str(finished.get("error_type") or "error")
        message = redact_output(str(finished.get("message") or ""))[:300]
        return ("worker_error", f"the attempt raised {error_type}",
                ["worker_error", error_type], (message,) if message else ())
    if verification is None:
        reason = str(finished.get("reason") or "")
        return ("no_result", "the attempt produced nothing to verify"
                + (f" ({reason})" if reason else ""),
                ["no_result", finished.get("worker_reported_status"), _normalise(reason)], ())
    verdict = verification.get("verdict")
    findings = verification.get("findings")
    findings = findings if isinstance(findings, list) else []
    summary = str(verification.get("summary") or "")
    if verdict == "fail":
        signature = sorted(_finding_signature(f) for f in findings) or [["no_findings", summary]]
        details = tuple(_finding_details(f) for f in findings) or (summary,)
        return ("verification_failed", summary or "verification failed", signature, details)
    return ("verification_unavailable", summary or "verification could not run",
            ["verification_unavailable", str(verdict), _normalise(summary)], ())


def task_failures(history, project_id: str, task_id: str,
                  task: Optional[Mapping] = None) -> list:
    """The task's failed attempts since the owner's last action on it, oldest first.

    With ``task`` (its current record), only attempts made against its current spec
    count: after the acceptance criteria change, earlier failures say nothing about
    the new ones. Attempts the service itself ended (worker limits, stalls) are not
    failures of the task.
    """
    attempts: dict = {}
    current = spec_hash(task) if task is not None else None
    for event in history.events(project_id=project_id, task_id=task_id,
                                types=[EventType.HUMAN_ACTION, EventType.ATTEMPT_STARTED,
                                       EventType.ATTEMPT_FINISHED, EventType.VERIFICATION]):
        if event.type is EventType.HUMAN_ACTION:
            attempts = {}
            continue
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        if event.type is EventType.ATTEMPT_STARTED:
            attempts[event.attempt_id] = {"started": payload, "finished": None,
                                          "verification": None, "passed": False}
        elif event.attempt_id in attempts:
            record = attempts[event.attempt_id]
            if event.type is EventType.ATTEMPT_FINISHED:
                record["finished"] = payload
            elif payload.get("verdict") == "pass":
                record["passed"] = True
            elif "rebased_from" not in payload:
                record["verification"] = payload
    failures = []
    for attempt_id, record in attempts.items():
        started, finished = record["started"], record["finished"]
        if finished is None or record["passed"]:
            continue
        if current is not None and started.get("spec_hash") != current:
            continue
        classified = _classify(started, finished, record["verification"])
        if classified is None:
            continue
        kind, summary, signature, details = classified
        failures.append(AttemptFailure(
            attempt_id=attempt_id, profile=started.get("worker_profile"), kind=kind,
            fingerprint=_hash([kind, signature]),
            summary=redact_output(summary)[:300],
            details=tuple(redact_output(d) for d in details)))
    return failures


def identical_streak(failures) -> int:
    """How many of the latest failures share the last one's fingerprint and worker profile."""
    failures = list(failures)
    if not failures:
        return 0
    last = failures[-1]
    streak = 0
    for failure in reversed(failures):
        if failure.fingerprint != last.fingerprint or failure.profile != last.profile:
            break
        streak += 1
    return streak


def repeated_failure(history, project_id: str, task_id: str, limit: int,
                     task: Optional[Mapping] = None) -> Optional[AttemptFailure]:
    """The failure that repeated ``limit`` times in a row (a stop for retries), or None.

    ``limit`` below 2 disables the check: one failure is not a repeat.
    """
    if limit < 2:
        return None
    failures = task_failures(history, project_id, task_id, task)
    return failures[-1] if identical_streak(failures) >= limit else None


def retry_limits(config) -> dict:
    """How often a task may fail before the service stops retrying it, from the config.

    With worker tiers, the same failure twice is how a task earns its escalation to the
    next tier (core.worker_tiers), so identical failures only stop it above that."""
    from core.worker_tiers import FAILURES_PER_TIER

    ladder = len(config.worker.ladder)
    identical = config.run.max_identical_failures
    if identical and ladder:
        identical = max(identical, FAILURES_PER_TIER + 1)
    return {"max_failures": max(3, 2 * ladder), "max_identical": identical}


def describe_failure(failure: AttemptFailure, times: int) -> str:
    """One owner-facing line: what repeated, how often."""
    return f"failed the same way {times} times in a row ({failure.kind}): {failure.summary}"


def retry_note(history, project_id: str, task_id: str,
               task: Optional[Mapping] = None, failures=None) -> Optional[str]:
    """The instruction for the next attempt after failures, or None if none failed.

    Built from the orchestrator's own record (the verifier's findings and the
    attempt's outcome), never from a worker's claim of what went wrong.
    """
    if failures is None:
        failures = task_failures(history, project_id, task_id, task)
    if not failures:
        return None
    last, streak = failures[-1], identical_streak(failures)
    lines = [f"IMPORTANT: {len(failures)} earlier attempt(s) at this task failed. The most "
             f"recent one: {last.summary}."]
    if streak >= 2:
        lines.append(f"The last {streak} attempts failed in exactly the same way. Do not "
                     "repeat the same approach: find the root cause in the evidence below "
                     "and change what you do.")
    if last.kind == "timed_out":
        lines.append("It ran out of time. Work in smaller steps and write files early.")
    detail = "\n".join(f"- {d}" for d in last.details)[:NOTE_DETAIL_CHARS]
    if detail:
        lines.append("What the verifier found:\n" + detail)
    lines.append("The acceptance commands and path rules above are fixed; making them pass "
                 "by editing the checks, or by changing unrelated files, fails the task.")
    return "\n".join(lines)
