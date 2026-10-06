"""Read-only discovery and overview of every project under a projects root.

This layer answers management questions ("what projects exist?", "what is
happening across my projects?") by delegating to ProjectState. It owns no YAML
parsing, no validation rules and no write path of its own: ProjectState remains
the single source of truth for reading and validating project data.

It is deliberately a state/management layer, not an AI layer. Nothing here
knows about models, agents, prompts or execution environments, so the future
Master can query it without knowing which worker is running.

Malformed projects
-----------------
A broken project never makes discovery unusable. Any project that cannot be
read and validated is excluded from the results and reported through
problems() with an explicit reason. Valid projects stay available.

Projects that share a project id are all excluded and reported, because the id
is the lookup key: if it is ambiguous, any answer could be about the wrong
project. A malformed project still makes a claim on the id it declares, so it
takes part in this check even though it can never be used. Identity claims are
counted separately from usable projects.

Every call re-scans the filesystem. Nothing is cached, so results always
reflect the current files on disk.
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from core.paths import default_projects_root
from core.project_state import ProjectState


PROJECT_FILENAME = "project.yaml"

# Expected data failures while reading one project. Anything else is a bug in
# this layer and is allowed to surface instead of being silently skipped.
PROJECT_READ_ERRORS = (OSError, ValueError, yaml.YAMLError)


class ProjectManagerError(Exception):
    """Base error for the project management layer."""


class UnknownProjectError(ProjectManagerError):
    """Raised when a project id cannot be resolved to a usable project."""


def _merge_counts(targets, sources):
    for counts in sources:
        for status, count in counts.items():
            targets[status] = targets.get(status, 0) + count

    return targets


def _declared_id(state):
    """Return the id a rejected project declared, for reporting only.

    A project is rejected because its state failed validation, and that
    validation is what reads the id in the first place. Recovering the id
    afterwards lets a caller tell "this project exists but is broken" apart
    from "no such project", which need different responses.

    Recovery never makes a project usable: rejected projects stay out of
    list_projects() and get_project() keeps raising for them. The id is
    metadata for diagnosis, not permission to bypass validation.

    Returns None when project.yaml itself cannot be read, which is the one
    case where the id is genuinely unrecoverable.
    """
    try:
        return state.declared_id()
    except PROJECT_READ_ERRORS:
        return None


class ProjectManager:
    def __init__(self, root=None):
        self.root = Path(root).expanduser() if root is not None else default_projects_root()

        if not self.root.is_dir():
            raise FileNotFoundError(f"Projects root not found: {self.root}")

    def list_projects(self):
        """Return the sorted ids of every usable project."""
        entries, _ = self._scan()

        return [entry["id"] for entry in entries]

    def get_project(self, project_id):
        """Return the ProjectState for project_id.

        Raises UnknownProjectError if the id is unknown, or if a project with
        that id was rejected as malformed.
        """
        entries, problems = self._scan()

        for entry in entries:
            if entry["id"] == project_id:
                return entry["state"]

        for problem in problems:
            if problem["project_id"] == project_id:
                raise UnknownProjectError(
                    f"Project '{project_id}' is not available: "
                    f"{problem['error']}"
                )

        raise UnknownProjectError(f"Unknown project: {project_id}")

    def overview(self):
        """Return an aggregated, read-only snapshot of every usable project."""
        entries, problems = self._scan()

        projects = []

        for entry in entries:
            projects.append(
                {
                    **entry["state"].progress(),
                    "project_status": entry["status"],
                    "path": str(entry["path"]),
                }
            )

        return {
            "root": str(self.root),
            "project_count": len(projects),
            "projects": projects,
            "totals": {
                "projects": len(projects),
                "tasks": sum(item["total_tasks"] for item in projects),
                "task_status_counts": _merge_counts(
                    {},
                    [item["task_status_counts"] for item in projects],
                ),
                "milestones": sum(
                    item["total_milestones"] for item in projects
                ),
                "milestone_status_counts": _merge_counts(
                    {},
                    [item["milestone_status_counts"] for item in projects],
                ),
            },
            "problem_count": len(problems),
            "problems": problems,
        }

    def problems(self):
        """Return explicit reasons why discovered projects were rejected."""
        _, problems = self._scan()

        return problems

    def _project_paths(self):
        return sorted(
            path
            for path in self.root.iterdir()
            if path.is_dir() and (path / PROJECT_FILENAME).is_file()
        )

    def _scan(self):
        claims = []
        problems = []

        for path in self._project_paths():
            state = ProjectState(path)

            try:
                document = state.project()
            except PROJECT_READ_ERRORS as error:
                declared_id = _declared_id(state)

                problems.append(
                    {
                        "path": str(path),
                        "project_id": declared_id,
                        "kind": "unreadable",
                        "error": str(error),
                    }
                )
                claims.append(
                    {
                        "id": declared_id,
                        "document": None,
                        "state": state,
                        "path": path,
                    }
                )
                continue

            claims.append(
                {
                    "id": document["id"],
                    "document": document,
                    "state": state,
                    "path": path,
                }
            )

        counts = {}

        for claim in claims:
            project_id = claim["id"]

            if project_id is not None:
                counts[project_id] = counts.get(project_id, 0) + 1

        entries = []

        for claim in claims:
            project_id = claim["id"]

            if project_id is not None and counts[project_id] > 1:
                problems.append(
                    {
                        "path": str(claim["path"]),
                        "project_id": project_id,
                        "kind": "duplicate_id",
                        "error": (
                            f"Duplicate project id '{project_id}': "
                            f"{counts[project_id]} directories under "
                            f"{self.root} declare it"
                        ),
                    }
                )
                continue

            if claim["document"] is None:
                continue

            entries.append(
                {
                    "id": project_id,
                    "name": claim["document"].get("name"),
                    "status": claim["document"].get("status"),
                    "state": claim["state"],
                    "path": claim["path"],
                }
            )

        entries.sort(key=lambda entry: entry["id"])
        problems.sort(key=lambda problem: (problem["kind"], problem["path"]))

        return entries, problems


def main():
    manager = ProjectManager()

    print("PROJECTS ROOT")
    print(manager.root)

    print("\nPROJECTS")

    for project_id in manager.list_projects():
        project = manager.get_project(project_id)
        document = project.project()

        print(
            f"  - {project_id} "
            f"({document.get('name')}, {document.get('status')})"
        )

    print("\nOVERVIEW")
    print(manager.overview())

    print("\nPROBLEMS")
    problems = manager.problems()

    if not problems:
        print("  none")

    for problem in problems:
        print(f"  - {problem['path']}: {problem['error']}")


if __name__ == "__main__":
    main()
