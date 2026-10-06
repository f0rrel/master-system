import contextlib
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

import yaml


VALID_TASK_STATUSES = {
    "planned",
    "in_progress",
    "blocked",
    "completed",
    "cancelled",
}

VALID_MILESTONE_STATUSES = {
    "proposed",  # an unapproved backlog epic: no tasks until the owner approves a plan
    "planned",
    "in_progress",
    "completed",
}


ACCEPTANCE_KEYS = frozenset({"commands", "protected_paths"})
#: A human-written task description is bounded so it stays a spec, not a dump.
MAX_DESCRIPTION_CHARS = 4000


def description_problems(description, field="description"):
    """Why a task's ``description`` (or ``manual_check``) is malformed."""
    if not isinstance(description, str) or not description.strip():
        return [f"{field} must be a non-empty string"]
    if len(description) > MAX_DESCRIPTION_CHARS:
        return [f"{field} must be at most {MAX_DESCRIPTION_CHARS} characters"]
    return []


def acceptance_problems(acceptance):
    """Why a task's ``acceptance`` is malformed, as a list of messages.

    ``acceptance`` is the human-written definition of done:
    ``{commands: [str, ...], protected_paths: [glob, ...]}``. ``commands`` is
    required and non-empty; ``protected_paths`` is optional. Globs are
    repository-relative: no absolute paths and no ``..``.
    """
    if not isinstance(acceptance, dict):
        return ["acceptance must be a mapping"]
    problems = []
    unknown = sorted(set(acceptance) - ACCEPTANCE_KEYS)
    if unknown:
        problems.append(f"acceptance has unknown keys {unknown}")
    commands = acceptance.get("commands")
    if (
        not isinstance(commands, list)
        or not commands
        or not all(isinstance(c, str) and c.strip() for c in commands)
    ):
        problems.append("acceptance.commands must be a non-empty list of non-empty strings")
    protected = acceptance.get("protected_paths", [])
    if not isinstance(protected, list) or not all(
        isinstance(g, str) and g.strip() for g in protected
    ):
        problems.append("acceptance.protected_paths must be a list of non-empty strings")
    else:
        for glob in protected:
            if glob.startswith("/") or ".." in Path(glob).parts:
                problems.append(
                    f"acceptance.protected_paths entry {glob!r} must be relative "
                    "and must not contain '..'"
                )
    return problems


def repository_problems(project):
    """Why project.yaml's ``repository`` / ``base_branch`` are malformed.

    Shape only: whether the path exists and is a git repository is checked by
    the orchestrator when an attempt is about to run.
    """
    repository = project.get("repository")
    base_branch = project.get("base_branch")
    if repository is None:
        if base_branch is not None:
            return ["project.yaml has 'base_branch' but no 'repository'"]
        return []
    problems = []
    if not isinstance(repository, str) or not repository.strip():
        problems.append("project.yaml 'repository' must be a non-empty string")
    elif not Path(repository).expanduser().is_absolute():
        problems.append("project.yaml 'repository' must be an absolute path")
    if (
        not isinstance(base_branch, str)
        or not base_branch.strip()
        or base_branch.startswith("-")
        or any(c.isspace() for c in base_branch)
    ):
        problems.append(
            "project.yaml 'base_branch' must be a branch name when 'repository' is set"
        )
    return problems


def _count_by_status(items, statuses):
    counts = {status: 0 for status in sorted(statuses)}

    for item in items:
        status = item.get("status")

        if status in counts:
            counts[status] += 1

    return counts


def _fsync_directory(directory):
    with contextlib.suppress(OSError):
        descriptor = os.open(directory, os.O_RDONLY)

        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def write_yaml_atomically(path, document):
    """Replace path with document so readers never see a partial file.

    The new content is written to a temporary file in the same directory,
    flushed, fsynced and then moved into place with os.replace. If anything
    fails before that move the original file is left untouched, and the
    temporary file is removed.
    """
    path = Path(path)
    mode = stat.S_IMODE(path.stat().st_mode)

    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            yaml.safe_dump(
                document,
                file,
                sort_keys=False,
                allow_unicode=True,
            )
            file.flush()
            os.fsync(file.fileno())

        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary_path.unlink(missing_ok=True)

        raise


class ProjectSnapshot(NamedTuple):
    project: dict
    milestones_doc: dict
    tasks_doc: dict

    @property
    def milestones(self):
        return self.milestones_doc.get("milestones", [])

    @property
    def tasks(self):
        return self.tasks_doc.get("tasks", [])


class ProjectState:
    def __init__(self, project_path):
        self.project_path = Path(project_path)

    def _load(self, filename):
        path = self.project_path / filename

        if not path.exists():
            raise FileNotFoundError(f"Missing project file: {path}")

        with path.open("r", encoding="utf-8") as file:
            return yaml.safe_load(file) or {}

    def snapshot(self):
        """Return a validated, self-consistent view of the three state files.

        The snapshot holds the parsed documents, so callers that need to
        rewrite a file can do so without discarding unknown top-level keys.
        Mutating it does not change anything on disk.
        """
        snapshot = ProjectSnapshot(
            project=self._load("project.yaml"),
            milestones_doc=self._load("milestones.yaml"),
            tasks_doc=self._load("tasks.yaml"),
        )

        self._check(
            snapshot.project,
            snapshot.milestones,
            snapshot.tasks,
        )

        return snapshot

    def project(self):
        return self.snapshot().project

    def declared_id(self):
        """Return the id declared in project.yaml, or None if there is none.

        This is a diagnostic read only. A project whose other state files fail
        validation still has to be nameable when reporting why it was
        rejected, so this deliberately does not validate anything.

        It grants no access to an invalid project: every other read goes
        through snapshot(), which validates all three files. Recoverable
        identity is not the same thing as a valid project.
        """
        document = self._load("project.yaml")

        if not isinstance(document, dict):
            return None

        declared = document.get("id")

        if isinstance(declared, str) and declared.strip():
            return declared

        return None

    def milestones(self):
        return self.snapshot().milestones

    def tasks(self):
        return self.snapshot().tasks

    def get_task(self, task_id):
        for task in self.tasks():
            if task["id"] == task_id:
                return task

        return None

    def get_milestone(self, milestone_id):
        for milestone in self.milestones():
            if milestone["id"] == milestone_id:
                return milestone

        return None

    def tasks_in_milestone(self, milestone_id):
        return [
            task
            for task in self.tasks()
            if task.get("milestone") == milestone_id
        ]

    def tasks_with_status(self, status):
        if status not in VALID_TASK_STATUSES:
            raise ValueError(
                f"Invalid task status: {status}. "
                f"Valid statuses: {sorted(VALID_TASK_STATUSES)}"
            )

        return [task for task in self.tasks() if task.get("status") == status]

    def progress(self):
        snapshot = self.snapshot()

        return {
            "project_id": snapshot.project.get("id"),
            "project_name": snapshot.project.get("name"),
            "total_tasks": len(snapshot.tasks),
            "task_status_counts": _count_by_status(
                snapshot.tasks, VALID_TASK_STATUSES
            ),
            "total_milestones": len(snapshot.milestones),
            "milestone_status_counts": _count_by_status(
                snapshot.milestones, VALID_MILESTONE_STATUSES
            ),
        }

    def validate(self):
        self.snapshot()

        return True

    def _check(self, project, milestones, tasks):
        problems = []

        for field in ("id", "name"):
            if not project.get(field):
                problems.append(f"project.yaml is missing '{field}'")

        problems.extend(repository_problems(project))

        milestone_ids = set()

        for milestone in milestones:
            milestone_id = milestone.get("id")

            if not milestone_id:
                problems.append("milestone is missing an 'id'")
                continue

            if milestone_id in milestone_ids:
                problems.append(f"duplicate milestone id: {milestone_id}")

            milestone_ids.add(milestone_id)

            status = milestone.get("status")

            if status is None:
                problems.append(f"milestone {milestone_id} is missing a 'status'")
            elif status not in VALID_MILESTONE_STATUSES:
                problems.append(
                    f"milestone {milestone_id} has unknown status "
                    f"'{status}'; valid statuses: "
                    f"{sorted(VALID_MILESTONE_STATUSES)}"
                )

            priority = milestone.get("priority")
            if priority is not None and (not isinstance(priority, int)
                                         or isinstance(priority, bool)):
                problems.append(f"milestone {milestone_id} has a non-integer priority")

        task_ids = set()

        for task in tasks:
            task_id = task.get("id")

            if not task_id:
                problems.append("task is missing an 'id'")
                continue

            if task_id in task_ids:
                problems.append(f"duplicate task id: {task_id}")

            task_ids.add(task_id)

            status = task.get("status")

            if status is None:
                problems.append(f"task {task_id} is missing a 'status'")
            elif status not in VALID_TASK_STATUSES:
                problems.append(
                    f"task {task_id} has unknown status "
                    f"'{status}'; valid statuses: "
                    f"{sorted(VALID_TASK_STATUSES)}"
                )

            milestone_id = task.get("milestone")

            if not milestone_id:
                problems.append(f"task {task_id} is not assigned to a milestone")
            elif milestone_id not in milestone_ids:
                problems.append(
                    f"task {task_id} references unknown milestone "
                    f"'{milestone_id}'"
                )

            if "description" in task:
                problems.extend(
                    f"task {task_id}: {problem}"
                    for problem in description_problems(task["description"])
                )

            if "manual_check" in task:
                problems.extend(
                    f"task {task_id}: {problem}"
                    for problem in description_problems(task["manual_check"], "manual_check")
                )

            if "size" in task and task["size"] not in ("small", "medium", "hard"):
                problems.append(f"task {task_id} has invalid size {task['size']!r} "
                                "(small, medium or hard)")

            if "acceptance" in task:
                problems.extend(
                    f"task {task_id}: {problem}"
                    for problem in acceptance_problems(task["acceptance"])
                )

            depends_on = task.get("depends_on")
            if depends_on is not None:
                if not isinstance(depends_on, list):
                    problems.append(f"task {task_id} has invalid depends_on")
                else:
                    seen = set()
                    for dep in depends_on:
                        if not isinstance(dep, str) or not dep.strip():
                            problems.append(f"task {task_id} has invalid dependency id")
                            continue
                        d = dep.strip()
                        if d in seen:
                            continue  # normalize duplicates
                        seen.add(d)
                        if d not in task_ids:
                            problems.append(f"task {task_id} references unknown dependency {d}")

        if problems:
            raise ValueError(
                f"Invalid project state in {self.project_path}:\n"
                + "\n".join(f"  - {problem}" for problem in problems)
            )

    def update_task_status(self, task_id, status):
        snapshot = self.snapshot()

        if status not in VALID_TASK_STATUSES:
            raise ValueError(
                f"Invalid task status: {status}. "
                f"Valid statuses: {sorted(VALID_TASK_STATUSES)}"
            )

        for task in snapshot.tasks:
            if task["id"] == task_id:
                task["status"] = status
                write_yaml_atomically(
                    self.project_path / "tasks.yaml",
                    snapshot.tasks_doc,
                )

                return task

        raise ValueError(f"Task not found: {task_id}")


if __name__ == "__main__":
    # Usage: python core/project_state.py PROJECT_DIR
    # (a directory holding project.yaml, milestones.yaml and tasks.yaml).
    if len(sys.argv) != 2:
        print("usage: python core/project_state.py PROJECT_DIR", file=sys.stderr)
        sys.exit(2)
    state = ProjectState(Path(sys.argv[1]))

    print("PROJECT")
    print(state.project())

    print("\nVALIDATION")
    state.validate()
    print("ok")

    print("\nMILESTONES")
    for milestone in state.milestones():
        print(milestone)

    for milestone in state.milestones()[:1]:
        print(f"\nTASKS IN MILESTONE '{milestone['id']}'")
        for task in state.tasks_in_milestone(milestone["id"]):
            print(task)

    print("\nTASKS WITH STATUS 'in_progress'")
    for task in state.tasks_with_status("in_progress"):
        print(task)

    print("\nPROGRESS")
    print(state.progress())
