"""Shared project memory: lessons workers propose and the owner approves.

After an attempt passes verification, the orchestrator reads up to three
``LESSON: ...`` lines from the end of the worker's reply and adds them to the
project's **pending** list, tagged with the task type. Nothing pending is ever
used. The owner approves or rejects pending lessons in a batch
(``ms lessons <project>``); approved lessons for a task's type (or for ``all``)
are added to future worker prompts, newest first, within a budget.

Lessons are project content, so they live with the project's private
definition: ``<projects root>/<project>/lessons.yaml``::

    pending:  [{id, type, text, task_id, attempt_id, proposed_at}]
    approved: [{..., decided_at}]
    rejected: [{..., decided_at}]
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import yaml

from core.project_state import write_yaml_atomically

__all__ = ["LessonStore", "extract_lessons", "lesson_evidence", "MAX_LESSONS_PER_ATTEMPT",
           "MAX_LESSON_CHARS", "NEVER_PASSED_AFTER", "PROMPT_BUDGET"]

FILENAME = "lessons.yaml"
MAX_LESSONS_PER_ATTEMPT = 3
MAX_LESSON_CHARS = 300
#: Characters of approved lessons in one worker prompt. Fixed: not configurable.
PROMPT_BUDGET = 2000
#: Attempts without a pass after which a lesson is marked "never in a passing attempt".
NEVER_PASSED_AFTER = 5
_LINE = re.compile(r"^\s*[-*]?\s*LESSON:\s*(.+?)\s*$", re.IGNORECASE)


def extract_lessons(reply: Optional[str]) -> list:
    """``LESSON:`` lines from a worker's reply: at most 3, bounded, without duplicates."""
    found = []
    for line in str(reply or "").splitlines():
        match = _LINE.match(line)
        if not match:
            continue
        text = " ".join(match.group(1).split())[:MAX_LESSON_CHARS]
        if text and _key(text) not in {_key(t) for t in found}:
            found.append(text)
    return found[-MAX_LESSONS_PER_ATTEMPT:]


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LessonStore:
    def __init__(self, project_path):
        self.path = Path(project_path) / FILENAME

    def _load(self) -> dict:
        try:
            data = yaml.safe_load(self.path.read_text()) or {}
        except FileNotFoundError:
            data = {}
        return {key: list(data.get(key) or []) for key in ("pending", "approved", "rejected")}

    def _save(self, data: dict) -> None:
        if not self.path.exists():
            self.path.touch(mode=0o644)
        write_yaml_atomically(self.path, data)

    def pending(self) -> list:
        return self._load()["pending"]

    def approved(self) -> list:
        return self._load()["approved"]

    def propose(self, task_type: str, texts: Iterable[str], task_id: str,
                attempt_id: str) -> list:
        """Add lessons to the pending list; returns the records added."""
        data = self._load()
        known = {_key(r["text"]) for key in data for r in data[key]}
        numbers = [int(r["id"][2:]) for key in data for r in data[key]
                   if str(r.get("id", "")).startswith("L-") and r["id"][2:].isdigit()]
        next_number = max(numbers, default=0) + 1
        added = []
        for text in list(texts)[:MAX_LESSONS_PER_ATTEMPT]:
            text = " ".join(str(text).split())[:MAX_LESSON_CHARS]
            if not text or _key(text) in known:
                continue
            known.add(_key(text))
            record = {"id": f"L-{next_number}", "type": task_type, "text": text,
                      "task_id": task_id, "attempt_id": attempt_id, "proposed_at": _now()}
            next_number += 1
            data["pending"].append(record)
            added.append(record)
        if added:
            self._save(data)
        return added

    def decide(self, approve: Iterable[str] = (), reject: Iterable[str] = ()) -> dict:
        """Move pending lessons (by id) to approved or rejected. Returns what moved.

        ``reject`` may also name approved lessons: the owner retires them.
        """
        approve, reject = set(approve), set(reject)
        if approve & reject:
            raise ValueError(f"both approved and rejected: {sorted(approve & reject)}")
        data = self._load()
        ids = {r["id"] for r in data["pending"]}
        retire = reject & {r["id"] for r in data["approved"]}
        unknown = (approve | reject) - ids - retire
        if unknown:
            raise ValueError(f"not pending: {sorted(unknown)}")
        moved = {"approved": [], "rejected": []}
        for record in [r for r in data["approved"] if r["id"] in retire]:
            record = {**record, "decided_at": _now()}
            data["rejected"].append(record)
            moved["rejected"].append(record)
        data["approved"] = [r for r in data["approved"] if r["id"] not in retire]
        keep = []
        for record in data["pending"]:
            target = ("approved" if record["id"] in approve
                      else "rejected" if record["id"] in reject else None)
            if target is None:
                keep.append(record)
                continue
            record = {**record, "decided_at": _now()}
            data[target].append(record)
            moved[target].append(record)
        data["pending"] = keep
        self._save(data)
        return moved

    def select(self, task_type: str, budget: int = PROMPT_BUDGET) -> dict:
        """Approved lessons for this type (or ``all``), newest first, within ``budget``.

        ``included`` are the records the prompt carries; ``dropped`` are the
        approved ones of this type that the budget left out.
        """
        included, dropped, used = [], [], 0
        for record in reversed(self.approved()):
            if record.get("type") not in (task_type, "all"):
                continue
            if dropped or used + len(record["text"]) > budget:
                dropped.append(record)
                continue
            included.append(record)
            used += len(record["text"])
        return {"included": included, "dropped": dropped}

    def for_prompt(self, task_type: str, budget: int = PROMPT_BUDGET) -> list:
        """The texts of :meth:`select`'s included lessons."""
        return [record["text"] for record in self.select(task_type, budget)["included"]]

    def fit(self, budget: int = PROMPT_BUDGET) -> dict:
        """Approved lesson id → the task types whose prompt leaves it out (empty: it fits)."""
        from core.project_state import TASK_TYPES

        left_out = {record["id"]: [] for record in self.approved()}
        for task_type in TASK_TYPES:
            for record in self.select(task_type, budget)["dropped"]:
                left_out[record["id"]].append(task_type)
        return left_out


def lesson_evidence(history, project_id: str) -> dict:
    """Lesson id → ``{attempts, passed}`` from history alone.

    Counts finished attempts whose ``attempt_started`` names the lesson in
    ``lessons_used_ids``, and those of them with a ``pass`` verification.
    Attempts a provider limit stopped never ran the lessons and do not count;
    older attempts without ``lessons_used_ids`` do not count either.
    """
    from core.history import EventType

    used, finished, passed = {}, set(), set()
    for event in history.events(project_id=project_id, types=[
            EventType.ATTEMPT_STARTED, EventType.ATTEMPT_FINISHED, EventType.VERIFICATION]):
        if event.type is EventType.ATTEMPT_STARTED:
            ids = event.payload.get("lessons_used_ids")
            if isinstance(ids, list):
                used[event.attempt_id] = ids
        elif event.type is EventType.ATTEMPT_FINISHED:
            if event.payload.get("outcome") != "limited":
                finished.add(event.attempt_id)
        elif event.payload.get("verdict") == "pass":
            passed.add(event.attempt_id)
    evidence = {}
    for attempt_id, ids in used.items():
        if attempt_id not in finished:
            continue
        for lesson_id in ids:
            counts = evidence.setdefault(lesson_id, {"attempts": 0, "passed": 0})
            counts["attempts"] += 1
            counts["passed"] += attempt_id in passed
    return evidence
