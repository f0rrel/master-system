"""Programmatic creation and updating of milestones and tasks.

WorkManager sits directly above ProjectState:

    ProjectState   validated reads, durable atomic storage
    WorkManager    the domain rules for changing planned work

ProjectState deliberately stays small: it knows how to read, validate and
safely persist a project. It does not know what a "task" operation means.
WorkManager owns those rules, so the storage layer does not grow a mutation
API for every entity it happens to store.

Read-only inspection across projects stays in ProjectManager, and project
status mutation stays in ProjectState.update_task_status, which remains the
narrow write primitive it always was.

Nothing here knows about models, agents, prompts or execution environments.
Every operation is synchronous, local, and touches exactly one YAML file with
a single atomic replacement.
"""

import sys
from copy import deepcopy
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.project_state import (
    VALID_MILESTONE_STATUSES,
    acceptance_problems,
    VALID_TASK_STATUSES,
    ProjectState,
    write_yaml_atomically,
)


TASK_FIELDS = ("id", "milestone", "title", "status", "assigned_to")
MILESTONE_FIELDS = ("id", "name", "status")

# Ids are identity: they are never rewritten in place. ``acceptance`` is
# deliberately absent: it is the human's definition of done and is changed only
# through set_task_acceptance, which no model operation reaches.
MUTABLE_TASK_FIELDS = ("milestone", "title", "status", "assigned_to")
MUTABLE_MILESTONE_FIELDS = ("name", "status")


class WorkManagerError(ValueError):
    """Base error for a rejected work mutation.

    Subclasses ValueError so callers that already handle ProjectState's
    validation errors keep working unchanged.
    """


class DuplicateRecordError(WorkManagerError):
    """Raised when a new id collides with an existing record."""


class RecordNotFoundError(WorkManagerError):
    """Raised when the requested milestone or task does not exist."""


class InvalidFieldError(WorkManagerError):
    """Raised when a field name or value is not acceptable."""


def _require_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise InvalidFieldError(f"{label} must be a non-empty string")

    return value


def _require_task_status(status):
    if status not in VALID_TASK_STATUSES:
        raise InvalidFieldError(
            f"Invalid task status: {status}. "
            f"Valid statuses: {sorted(VALID_TASK_STATUSES)}"
        )

    return status


def _require_milestone_status(status):
    if status not in VALID_MILESTONE_STATUSES:
        raise InvalidFieldError(
            f"Invalid milestone status: {status}. "
            f"Valid statuses: {sorted(VALID_MILESTONE_STATUSES)}"
        )

    return status


def _reject_unknown_fields(changes, allowed, label):
    for field in changes:
        if field not in allowed:
            raise InvalidFieldError(
                f"Cannot change {label} field {field!r}. "
                f"Updatable fields: {sorted(allowed)}"
            )


def _find(records, record_id, label):
    for record in records:
        if record.get("id") == record_id:
            return record

    raise RecordNotFoundError(f"{label} not found: {record_id}")



def calculate_readiness(state, task):
    """Calculate readiness for a task.

    Returns (readiness, blocked_by, reason) where readiness is one of:
    'ready', 'blocked', 'waiting'.
    """
    if not isinstance(task, dict):
        return ("blocked", None, "invalid task")

    status = task.get("status")

    # If already started or finished, not a start candidate for readiness
    if status in ("in_progress", "completed", "cancelled"):
        return ("waiting", None, f"task already {status}")

    if status == "blocked":
        return ("blocked", None, "task explicitly blocked")

    if status == "planned":
        depends_on = task.get("depends_on")
        if not depends_on:
            return ("ready", None, None)
        if not isinstance(depends_on, list):
            return ("blocked", None, "invalid depends_on")
        # validate all deps completed
        for dep_id in depends_on:
            dep = _find_by_id(state.snapshot().tasks, dep_id)
            if dep.get("status") != "completed":
                return (
                    "blocked",
                    dep_id,
                    f"Dependency {dep_id} is not completed.",
                )
        return ("ready", None, None)

    return ("waiting", None, f"unhandled status {status}")


def _find_by_id(records, record_id):
    for record in records:
        if record.get("id") == record_id:
            return record
    raise RecordNotFoundError(f"task not found: {record_id}")



class WorkManager:
    def __init__(self, project_path):
        self._state = ProjectState(project_path)

    @property
    def project_path(self):
        return self._state.project_path

    def create_milestone(self, milestone_id, name, status="planned"):
        """Add a milestone and return the stored record."""
        milestones = deepcopy(self._state.snapshot().milestones_doc)

        _require_text(milestone_id, "Milestone id")
        _require_text(name, "Milestone name")
        _require_milestone_status(status)

        existing = milestones.get("milestones", [])

        if any(item.get("id") == milestone_id for item in existing):
            raise DuplicateRecordError(
                f"Milestone already exists: {milestone_id}"
            )

        record = {"id": milestone_id, "name": name, "status": status}

        existing.append(record)
        write_yaml_atomically(self._milestones_path, milestones)

        return record

    def update_milestone(self, milestone_id, **changes):
        """Change a milestone's name and/or status and return the record."""
        milestones = deepcopy(self._state.snapshot().milestones_doc)

        _reject_unknown_fields(changes, MUTABLE_MILESTONE_FIELDS, "milestone")

        record = _find(milestones.get("milestones", []), milestone_id, "Milestone")

        if "name" in changes:
            _require_text(changes["name"], "Milestone name")

        if "status" in changes:
            _require_milestone_status(changes["status"])

        record.update(changes)
        write_yaml_atomically(self._milestones_path, milestones)

        return record

    def create_task(
        self,
        task_id,
        milestone,
        title,
        status="planned",
        assigned_to=None,
    ):
        """Add a task to an existing milestone and return the stored record."""
        snapshot = self._state.snapshot()
        tasks = deepcopy(snapshot.tasks_doc)

        _require_text(task_id, "Task id")
        _require_text(title, "Task title")
        _require_task_status(status)

        if assigned_to is not None:
            _require_text(assigned_to, "Assigned to")

        if milestone not in self._milestone_ids(snapshot):
            raise InvalidFieldError(
                f"Task references unknown milestone: {milestone}"
            )

        existing = tasks.get("tasks", [])

        if any(item.get("id") == task_id for item in existing):
            raise DuplicateRecordError(f"Task already exists: {task_id}")

        record = {
            "id": task_id,
            "milestone": milestone,
            "title": title,
            "status": status,
        }

        if assigned_to is not None:
            record["assigned_to"] = assigned_to

        existing.append(record)
        write_yaml_atomically(self._tasks_path, tasks)

        return record

    def update_task(self, task_id, **changes):
        """Change a task's fields and return the updated record."""
        snapshot = self._state.snapshot()
        tasks = deepcopy(snapshot.tasks_doc)

        _reject_unknown_fields(changes, MUTABLE_TASK_FIELDS, "task")

        record = _find(tasks.get("tasks", []), task_id, "Task")

        if "title" in changes:
            _require_text(changes["title"], "Task title")

        if "status" in changes:
            _require_task_status(changes["status"])

        if "milestone" in changes:
            if changes["milestone"] not in self._milestone_ids(snapshot):
                raise InvalidFieldError(
                    f"Task references unknown milestone: {changes['milestone']}"
                )

        if changes.get("assigned_to") is not None:
            _require_text(changes["assigned_to"], "Assigned to")

        record.update(changes)
        write_yaml_atomically(self._tasks_path, tasks)

        return record

    def set_task_acceptance(self, task_id, acceptance):
        """Set or, with ``None``, remove a task's acceptance criteria.

        Human-only: no operation in ``core.reasoning.SPECS`` maps here.
        """
        if acceptance is not None:
            problems = acceptance_problems(acceptance)
            if problems:
                raise InvalidFieldError("; ".join(problems))

        tasks = deepcopy(self._state.snapshot().tasks_doc)
        record = _find(tasks.get("tasks", []), task_id, "Task")

        if acceptance is None:
            record.pop("acceptance", None)
        else:
            record["acceptance"] = deepcopy(acceptance)

        write_yaml_atomically(self._tasks_path, tasks)

        return record

    @property
    def _tasks_path(self):
        return self.project_path / "tasks.yaml"

    @property
    def _milestones_path(self):
        return self.project_path / "milestones.yaml"

    def _milestone_ids(self, snapshot):
        return {milestone.get("id") for milestone in snapshot.milestones}
