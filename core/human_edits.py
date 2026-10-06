"""Human edits of a task's spec, recorded in history.

A task's ``description`` and ``acceptance`` are the human's definition of the
work, so changing them is a human touch that M2 requires to be recorded. Edits
made through ``run_cli task ...`` go through here: under the project lock
(after project-wide recovery), Master writes the change, then a
``human_action`` event records who did what and the task's spec hash before
and after. YAML stays authoritative: if the process died between the two,
the report shows the spec change as unexplained, not hidden.

Edits made directly in YAML or with ``core.master`` are not recorded
(docs/ARCHITECTURE.md, gap M4); the report flags them as unexplained spec changes.
"""

from __future__ import annotations

import uuid
from typing import Optional

from core.evidence import spec_hash
from core.history import EventType, HistoryStore
from core.master import Master
from core.paths import RuntimePaths
from core.recovery import recover_project
from core.run_lock import ProjectLock
from core.session_store import FileSessionStore
from core.task_orchestrator import default_worktrees

__all__ = ["ACTOR", "set_acceptance", "set_description", "set_manual_check"]

ACTOR = "human-cli"


def _edit(master: Master, history: HistoryStore, project_id: str, task_id: str,
          action: str, apply, paths: Optional[RuntimePaths]) -> dict:
    paths = paths if paths is not None else RuntimePaths.default()
    state = master.project_state(project_id)
    with ProjectLock(state.project_path, holder=f"{ACTOR} {action}"):
        recover_project(history, project_id, store=FileSessionStore(paths.sessions_dir),
                        worktrees=default_worktrees(master, paths))
        before = state.get_task(task_id)
        if before is None:
            raise ValueError(f"Task not found: {task_id}")
        before_hash = spec_hash(before)
        record = apply()
        history.append(
            type=EventType.HUMAN_ACTION,
            run_id=uuid.uuid4().hex,
            project_id=project_id,
            task_id=task_id,
            payload={
                "actor": ACTOR,
                "action": action,
                "spec_hash_before": before_hash,
                "spec_hash_after": spec_hash(record),
            },
        )
        return record


def set_description(master, history, project_id, task_id, description,
                    paths: Optional[RuntimePaths] = None) -> dict:
    action = "clear_description" if description is None else "set_description"
    return _edit(master, history, project_id, task_id, action,
                 lambda: master.set_task_description(project_id, task_id, description), paths)


def set_manual_check(master, history, project_id, task_id, manual_check,
                     paths: Optional[RuntimePaths] = None) -> dict:
    action = "clear_manual_check" if manual_check is None else "set_manual_check"
    return _edit(master, history, project_id, task_id, action,
                 lambda: master.set_task_manual_check(project_id, task_id, manual_check),
                 paths)


def set_acceptance(master, history, project_id, task_id, acceptance,
                   paths: Optional[RuntimePaths] = None) -> dict:
    action = "clear_acceptance" if acceptance is None else "set_acceptance"
    return _edit(master, history, project_id, task_id, action,
                 lambda: master.set_task_acceptance(project_id, task_id, acceptance), paths)
