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
    description_problems,
    VALID_TASK_STATUSES,
    ProjectState,
    write_yaml_atomically,
)


TASK_FIELDS = ("id", "milestone", "title", "status", "assigned_to")
MILESTONE_FIELDS = ("id", "name", "status")

# Ids are identity: they are never rewritten in place. ``acceptance`` and
# ``description`` are deliberately absent: they are the human's spec and are
# changed only through set_task_acceptance / set_task_description, which no
# model operation reaches.
MUTABLE_TASK_FIELDS = ("milestone", "title", "status", "assigned_to")
#: A human-set estimate of a task's difficulty; it picks the starting worker tier.
TASK_SIZES = ("small", "medium", "hard")
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


PROPOSED = "proposed"
BACKLOG_ONLY = ("an epic becomes or stops being a proposed backlog epic only through the "
                "backlog (ms backlog) or an approved planner draft")


def _require_priority(priority):
    if not isinstance(priority, int) or isinstance(priority, bool) or priority < 0:
        raise InvalidFieldError(f"Priority must be a non-negative integer: {priority!r}")


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
        if status == PROPOSED:
            raise InvalidFieldError(BACKLOG_ONLY)

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
            if PROPOSED in (changes["status"], record.get("status")) \
                    and changes["status"] != record.get("status"):
                raise InvalidFieldError(BACKLOG_ONLY)

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
        if any(m.get("id") == milestone and m.get("status") == PROPOSED
               for m in snapshot.milestones):
            raise InvalidFieldError(
                f"Milestone {milestone} is an unapproved backlog epic; its tasks come "
                "from an approved planner draft")

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

    def set_task_description(self, task_id, description):
        """Set or, with ``None``, remove a task's description. Human-only."""
        return self._set_text_field(task_id, "description", description)

    def set_task_manual_check(self, task_id, manual_check):
        """Set or remove how a human checks the task's result by hand. Human-only."""
        return self._set_text_field(task_id, "manual_check", manual_check)

    def add_planned_work(self, milestone, tasks):
        """Add one milestone (an epic) and its fully specified tasks, all or nothing.

        Human-only: used by the planner when the owner approves a draft. The
        records are complete task records (description, acceptance,
        manual_check, size, depends_on); the result is validated like any
        project state, and both files are restored if it is invalid.
        """
        snapshot = self._state.snapshot()
        milestones_doc = deepcopy(snapshot.milestones_doc)
        tasks_doc = deepcopy(snapshot.tasks_doc)
        _require_text(milestone.get("id"), "Milestone id")
        _require_text(milestone.get("name"), "Milestone name")
        backlog_epic = next((m for m in milestones_doc.get("milestones", [])
                             if m.get("id") == milestone["id"]), None)
        if backlog_epic is not None and (
                backlog_epic.get("status") != PROPOSED
                or any(t.get("milestone") == milestone["id"]
                       for t in tasks_doc.get("tasks", []))):
            raise DuplicateRecordError(f"Milestone already exists: {milestone['id']}")
        existing = {t.get("id") for t in tasks_doc.get("tasks", [])}
        for task in tasks:
            if task.get("id") in existing:
                raise DuplicateRecordError(f"Task already exists: {task.get('id')}")
            if task.get("size") not in (None, *TASK_SIZES):
                raise InvalidFieldError(f"Invalid task size: {task.get('size')}")
        old_milestones, old_tasks = deepcopy(milestones_doc), deepcopy(tasks_doc)
        if backlog_epic is not None:
            # The owner approved the plan of a backlog epic: it keeps its place.
            backlog_epic["status"] = "planned"
        else:
            milestones_doc.setdefault("milestones", []).append(
                {"id": milestone["id"], "name": milestone["name"], "status": "planned"})
        tasks_doc.setdefault("tasks", []).extend(deepcopy(list(tasks)))
        write_yaml_atomically(self._milestones_path, milestones_doc)
        write_yaml_atomically(self._tasks_path, tasks_doc)
        try:
            self._state.snapshot()
        except ValueError:
            write_yaml_atomically(self._milestones_path, old_milestones)
            write_yaml_atomically(self._tasks_path, old_tasks)
            raise
        return deepcopy(list(tasks))

    def set_task_size(self, task_id, size):
        """Set a task's size (small, medium, hard): it picks the worker tier. Human-only."""
        if size not in TASK_SIZES:
            raise InvalidFieldError(f"Invalid task size: {size}")
        tasks = deepcopy(self._state.snapshot().tasks_doc)
        record = _find(tasks.get("tasks", []), task_id, "Task")
        record["size"] = size
        write_yaml_atomically(self._tasks_path, tasks)
        return record

    def split_task(self, task_id, records):
        """Replace an open task with an ordered sequence of smaller tasks. Human-only.

        A record that reuses the task's own id replaces it in place; otherwise the task
        is cancelled (``replaced_by`` names the new ids), the new records go in its
        place, and every task that depended on it depends on all of them. All or
        nothing, like add_planned_work.
        """
        snapshot = self._state.snapshot()
        tasks_doc = deepcopy(snapshot.tasks_doc)
        tasks = tasks_doc.get("tasks", [])
        index = next((i for i, t in enumerate(tasks) if t.get("id") == task_id), None)
        if index is None:
            raise InvalidFieldError(f"Task not found: {task_id}")
        old = tasks[index]
        if old.get("status") not in ("planned", "in_progress", "blocked"):
            raise InvalidFieldError(f"Task {task_id} is {old.get('status')}; only open or "
                                    "blocked tasks can be split")
        existing = {t.get("id") for t in tasks if t.get("id") != task_id}
        new_ids = [r.get("id") for r in records]
        for record in records:
            if record.get("id") in existing:
                raise DuplicateRecordError(f"Task already exists: {record.get('id')}")
            if record.get("milestone") != old.get("milestone"):
                raise InvalidFieldError("the replacement tasks must stay in the task's epic")
        if task_id in new_ids:
            # A replacement that keeps the task's id: update it in place. Any other
            # records are inserted after it as new tasks.
            replacement = next(r for r in records if r.get("id") == task_id)
            old.update(deepcopy(replacement))
            tasks[index + 1:index + 1] = deepcopy(
                [r for r in records if r.get("id") != task_id])
        else:
            old.update(status="cancelled", replaced_by=new_ids)
            tasks[index + 1:index + 1] = deepcopy(list(records))
            for task in tasks:
                deps = task.get("depends_on") or []
                if task_id in deps and task.get("id") not in new_ids:
                    task["depends_on"] = [d for d in deps if d != task_id] + [
                        i for i in new_ids if i not in deps]
        previous = deepcopy(snapshot.tasks_doc)
        write_yaml_atomically(self._tasks_path, tasks_doc)
        try:
            self._state.snapshot()
        except ValueError:
            write_yaml_atomically(self._tasks_path, previous)
            raise
        return deepcopy(list(records))

    def add_backlog_epic(self, epic_id, name, summary=None, priority=None):
        """Add an unapproved backlog epic (status ``proposed``). Human-only."""
        milestones = deepcopy(self._state.snapshot().milestones_doc)
        _require_text(epic_id, "Epic id")
        _require_text(name, "Epic name")
        existing = milestones.setdefault("milestones", [])
        if any(item.get("id") == epic_id for item in existing):
            raise DuplicateRecordError(f"Milestone already exists: {epic_id}")
        if priority is None:
            priority = max((m.get("priority") for m in existing
                            if isinstance(m.get("priority"), int)), default=0) + 1
        _require_priority(priority)
        record = {"id": epic_id, "name": name, "status": PROPOSED, "priority": priority}
        if summary:
            problems = description_problems(summary, "summary")
            if problems:
                raise InvalidFieldError("; ".join(problems))
            record["summary"] = summary
        existing.append(record)
        write_yaml_atomically(self._milestones_path, milestones)
        return record

    def set_epic_priority(self, epic_id, priority):
        """Set an epic's backlog priority (lower runs first). Human-only."""
        _require_priority(priority)
        milestones = deepcopy(self._state.snapshot().milestones_doc)
        record = _find(milestones.get("milestones", []), epic_id, "Milestone")
        record["priority"] = priority
        write_yaml_atomically(self._milestones_path, milestones)
        return record

    def _set_text_field(self, task_id, field, value):
        if value is not None:
            problems = description_problems(value, field)
            if problems:
                raise InvalidFieldError("; ".join(problems))

        tasks = deepcopy(self._state.snapshot().tasks_doc)
        record = _find(tasks.get("tasks", []), task_id, "Task")

        if value is None:
            record.pop(field, None)
        else:
            record[field] = value

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
