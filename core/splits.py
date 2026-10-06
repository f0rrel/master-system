"""Automatic recovery for tasks too big for a worker: retry incrementally, then split.

When a worker is cut off (it spent its whole output on thinking and wrote nothing:
a ``stalled`` attempt with ``finish_reason: length``):

1. the next attempt gets a recovery instruction: create the file with its first part,
   then add the rest in further edits (``recovery_note``);
2. after a second cut-off since the last human action, the service asks the planner to
   draft a **split** of the task (a draft with ``replaces``), runs the draft's checks,
   and puts it in "Needs you" (and in Telegram, with Approve / Reject buttons, plus
   Escalate when worker tiers are configured). The task waits; it is not blocked.

The owner decides with ``ms split <project> <task> approve|reject|escalate`` or the
buttons. ``ms split <project> <task> draft "<guidance>"`` asks for a split by hand.
Pending splits live in ``<state dir>/splits/<project>/<task>.json``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from core.history import EventType

__all__ = ["SplitStore", "cut_offs_since_human", "recovery_note", "split_request",
           "SplitStep", "CUT_OFFS_BEFORE_SPLIT"]

CUT_OFFS_BEFORE_SPLIT = 2


def _cut_off(payload) -> bool:
    stall = payload.get("stall") or {}
    return payload.get("outcome") == "stalled" and stall.get("finish_reason") == "length"


def cut_offs_since_human(history, project_id: str, task_id: str) -> int:
    """Attempts cut off while thinking, since the task's last human action."""
    count = 0
    for e in history.events(project_id=project_id, task_id=task_id,
                            types=[EventType.HUMAN_ACTION, EventType.ATTEMPT_FINISHED]):
        if e.type is EventType.HUMAN_ACTION:
            count = 0
        elif _cut_off(e.payload):
            count += 1
    return count


def recovery_note(history, project_id: str, task_id: str) -> Optional[str]:
    """The instruction for the attempt after a cut-off, or None."""
    if cut_offs_since_human(history, project_id, task_id) < 1:
        return None
    return ("IMPORTANT: your previous attempt at this task was cut off: it spent its whole "
            "output on planning and wrote nothing. This time, write incrementally: within "
            "your first steps create the file with only its first part (for example the "
            "skeleton and the first item), then add the rest one part at a time with "
            "further edits. Do not plan everything before writing.")


def split_request(task: dict, guidance: Optional[str] = None) -> str:
    """The planner message asking for a split of one task."""
    text = (f"Split task {task['id']} ({task.get('title')}) into an ordered sequence of "
            "smaller tasks: workers could not write it in one go (they ran out of output "
            "while planning it). Draft the split with \"replaces\": \"" + task["id"] + "\", "
            "keeping its epic, each task one small part (about 150 lines at most) with its "
            "own tests and estimate_lines, ordered with depends_on. Keep the task's intent "
            "and the project direction.\n\nThe task's description:\n"
            + str(task.get("description") or ""))
    if guidance:
        text += f"\n\nThe owner's guidance for the split: {guidance}"
    return text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SplitStore:
    def __init__(self, state_dir: Path):
        self._dir = Path(state_dir) / "splits"

    def _path(self, project_id, task_id) -> Path:
        return self._dir / project_id / f"{task_id}.json"

    def get(self, project_id, task_id) -> dict:
        try:
            return json.loads(self._path(project_id, task_id).read_text())
        except (OSError, ValueError):
            return {}

    def save(self, project_id, task_id, data: dict) -> None:
        path = self._path(project_id, task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True))

    def clear(self, project_id, task_id) -> None:
        self._path(project_id, task_id).unlink(missing_ok=True)

    def pending(self, project_id) -> dict:
        """task id -> pending split state."""
        folder = self._dir / project_id
        found = {}
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            try:
                found[path.stem] = json.loads(path.read_text())
            except ValueError:
                continue
        return found

    def waiting(self, project_id) -> dict:
        """task id -> why it waits (a split waits for the owner)."""
        return {task: "waiting for your decision on a split"
                for task in self.pending(project_id)}


def summarize_draft(draft: dict, check: Optional[dict]) -> str:
    lines = []
    for task in draft.get("tasks") or []:
        estimate = f", ~{task['estimate_lines']} lines" if task.get("estimate_lines") else ""
        lines.append(f"- {task.get('id')} {task.get('title')} ({task.get('type')}{estimate})")
    if check is not None:
        lines.append("Checks: " + ("passed" if check.get("ok") else "FAILED")
                     + (f"; {len(check.get('size') or {})} still too big"
                        if check.get("size") else ""))
    return "\n".join(lines)


class SplitStep:
    """Service step: draft a split for tasks cut off twice; hand it to the owner."""

    def __init__(self, master, history, state_dir: Path, planner: Callable, notifier,
                 escalate: bool = False):
        self._master, self._history = master, history
        self._store = SplitStore(state_dir)
        #: (project_id, chat_id or None) -> a PlannerChat (core.ms.build_planner).
        self._planner = planner
        self._notifier = notifier
        self._escalate = escalate

    def __call__(self, project_id: str) -> list:
        lines = []
        pending = self._store.pending(project_id)
        for task in self._master.status(project_id)["tasks"]:
            if task.get("status") not in ("planned", "in_progress") or task["id"] in pending:
                continue
            if cut_offs_since_human(self._history, project_id, task["id"]) \
                    >= CUT_OFFS_BEFORE_SPLIT:
                lines.append(self.draft(project_id, task["id"]))
        return lines

    def draft(self, project_id: str, task_id: str, guidance: Optional[str] = None) -> str:
        """Ask the planner for a split, check it, and hand it to the owner."""
        task = self._master.project_state(project_id).get_task(task_id)
        chat = self._planner(project_id, None)
        result = chat.turn(split_request(task, guidance))
        state = {"task_id": task_id, "chat_id": chat.state.chat_id, "at": _now(),
                 "reply": result["reply"], "questions": result["questions"]}
        check = None
        if chat.state.draft and chat.state.draft.get("replaces") == task_id:
            try:
                check = chat.check()
                state.update(check_ok=bool(check.get("ok")), size_ok=bool(check.get("size_ok")))
            except Exception as error:  # an unusable draft goes to the owner as it is
                state.update(check_ok=False, check_error=str(error))
        self._store.save(project_id, task_id, state)
        if check is not None:
            message = (f"{task_id} ({task.get('title')}) was too big for the worker. The "
                       f"planner drafted a split:\n{summarize_draft(chat.state.draft, check)}")
            actions = [("Approve", {"op": "split", "project": project_id, "task": task_id,
                                    "decision": "approve"}),
                       ("Reject", {"op": "split", "project": project_id, "task": task_id,
                                   "decision": "reject"})]
            if self._escalate:
                actions.append(("Escalate", {"op": "split", "project": project_id,
                                             "task": task_id, "decision": "escalate"}))
        else:
            message = (f"{task_id} was too big for the worker, and the planner did not draft "
                       f"a split yet: {result['reply']} "
                       + " ".join(result["questions"])
                       + f" Continue in ms chat {project_id}.")
            actions = None
        self._notifier.send(f"{project_id}: needs you", message, tags="scissors",
                            priority="high", actions=actions)
        return f"{task_id}: split drafted for the owner ({chat.state.chat_id})"
