"""The backlog: epics in priority order, each with its tasks in file order.

An epic is a milestone. Two optional fields make it a backlog entry:
``priority`` (an integer, lower runs first; epics without one come after, in file
order) and ``summary`` (what the epic is about). A milestone with status
``proposed`` is an unapproved backlog epic: it has no tasks until the owner
approves a planner draft for it.

The service and the Master take ready tasks in backlog order: by epic priority,
then epic position in milestones.yaml, then task position in tasks.yaml.
"""

from __future__ import annotations

from typing import Iterable, Mapping

__all__ = ["epic_rank", "ordered_tasks", "backlog_rows", "render_backlog"]

_LAST = float("inf")


def epic_rank(milestones: Iterable[Mapping]) -> dict:
    """epic id -> sort key (priority, position)."""
    ranks = {}
    for position, milestone in enumerate(milestones or []):
        priority = milestone.get("priority")
        valid = isinstance(priority, int) and not isinstance(priority, bool)
        ranks[milestone.get("id")] = (priority if valid else _LAST, position)
    return ranks


def ordered_tasks(milestones, tasks) -> list:
    """Tasks in backlog order (a stable sort; unknown epics go last)."""
    ranks = epic_rank(milestones)
    indexed = list(enumerate(tasks or []))
    indexed.sort(key=lambda item: (ranks.get(item[1].get("milestone"), (_LAST, _LAST)),
                                   item[0]))
    return [task for _, task in indexed]


def backlog_rows(status: Mapping) -> list:
    """One row per epic, in backlog order, numbered from 1."""
    milestones = status.get("milestones") or []
    tasks = status.get("tasks") or []
    ranks = epic_rank(milestones)
    rows = []
    for milestone in sorted(milestones, key=lambda m: ranks[m.get("id")]):
        own = [t for t in tasks if t.get("milestone") == milestone.get("id")]
        rows.append({
            "id": milestone.get("id"),
            "name": milestone.get("name"),
            "status": milestone.get("status"),
            "priority": milestone.get("priority"),
            "summary": milestone.get("summary"),
            "tasks": len(own),
            "done": sum(1 for t in own if t.get("status") == "completed"),
            "open": sum(1 for t in own if t.get("status") in ("planned", "in_progress")),
            "blocked": sum(1 for t in own if t.get("status") == "blocked"),
        })
    for number, row in enumerate(rows, 1):
        row["number"] = number
    return rows


def _state(row) -> str:
    if row["status"] == "proposed":
        return "proposed (not planned yet)"
    if row["tasks"] and row["done"] == row["tasks"]:
        return "done"
    parts = [f"{row['done']}/{row['tasks']} done"]
    if row["blocked"]:
        parts.append(f"{row['blocked']} blocked")
    return f"{row['status']}, " + ", ".join(parts)


def render_backlog(rows, include_done: bool = True) -> str:
    if not rows:
        return "  The backlog is empty. Add an epic with `ms backlog <project> add \"Title\"` " \
               "or ask the planner (ms chat)."
    lines = []
    for row in rows:
        if not include_done and _state(row) == "done":
            continue
        priority = f"p{row['priority']}" if row["priority"] is not None else "p-"
        lines.append(f"  {row['number']:>2}. [{priority}] {row['id']}: {row['name']}"
                     f"  ({_state(row)})")
        if row.get("summary"):
            lines.append(f"        {' '.join(str(row['summary']).split())[:200]}")
    return "\n".join(lines)
