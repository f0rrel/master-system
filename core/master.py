"""Master: the deterministic orchestration boundary over the work system.

Master answers "what work should exist, and what should happen next?". It owns
no storage, no validation rules and no YAML parsing:

    Master            orchestration API, structured arguments only
      |
      v
    ProjectManager    project discovery and read-only overview
    WorkManager       domain rules for creating and updating work
      |
      v
    ProjectState      validated reads and durable atomic writes
      |
      v
    filesystem / YAML

Every Master operation is a thin, named delegation to one of the two layers
below it. Master deliberately adds no validation of its own, because
duplicating a rule in two places guarantees they will drift apart: an id that
Master accepts but WorkManager rejects, or worse, a check that only one of them
performs.

Errors are not wrapped. WorkManager's DuplicateRecordError,
RecordNotFoundError and InvalidFieldError, and ProjectManager's
UnknownProjectError, reach the caller unchanged, so a future reasoning layer
can react to the specific failure rather than to a string.

Why the API takes structured arguments
--------------------------------------
The intended end state is that a user says "Move the system from MVP1 to
MVP2" and a reasoning layer above Master turns that into explicit calls. That
works only if Master's surface is unambiguous and machine-addressable, so
there is no natural-language entry point here. Adding one later means adding a
new layer on top, not reshaping this one.

Master v0 is a control API, not an agent. It does not reason, execute, plan,
spawn, or touch git. Those belong to layers that will be built on top of this
one, and keeping them out is what makes this layer testable at all.

Command line interface
----------------------
This module is also the executable entry point, run as ``python -m core.master``.
The CLI lives here rather than in a separate module because this file is the
required entry point: a sibling ``core/cli.py`` would have to import Master
from here while this module imported the CLI back from there.

The CLI does four things and nothing else: parse arguments, format output,
call one Master method, return an exit status. It holds no project rules. It
never imports yaml or ProjectState, never validates a status or an id, and
never writes a file; every mutation goes through Master and therefore through
WorkManager. That is why there are deliberately no argparse ``choices`` for
status values: repeating WorkManager's vocabulary here would create a second
place to update when the vocabulary changes.

Read commands cannot mutate. They call ProjectManager methods only.
"""

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.project_manager import ProjectManager, UnknownProjectError
from core.work_manager import WorkManager

# Failures a caller is expected to be able to handle. These are the domain's
# own error types, not a second hierarchy: WorkManagerError and Master's
# argument guard both raise ValueError, and UnknownProjectError comes from
# ProjectManager. Anything else propagates with its traceback, so a genuine bug
# stays visible instead of being dressed up as a user error.
#
# Both layers above Master translate exactly this set: the CLI renders one
# readable line, and the reasoning interface turns it into a structured result.
# It is defined here, next to Master, so the two cannot drift apart.
EXPECTED_ERRORS = (FileNotFoundError, UnknownProjectError, ValueError)


class Master:
    def __init__(self, root=None):
        """Create a Master over a projects root.

        root defaults to the projects directory shipped with this repository,
        matching ProjectManager. ProjectManager raises FileNotFoundError if the
        root does not exist, and that is deliberately not caught here.
        """
        self._projects = ProjectManager(root)

    @property
    def root(self):
        return self._projects.root

    def list_projects(self):
        """Return the ids of every usable project."""
        return self._projects.list_projects()

    def overview(self):
        """Return the aggregated read-only overview of all managed projects."""
        return self._projects.overview()

    def problems(self):
        """Return why any discovered project was rejected."""
        return self._projects.problems()

    def status(self, project_id):
        """Return everything currently known about one project.

        This is the inspection entry point for a later reasoning layer: it gets
        identity, status and the full milestone and task lists in one call, so
        deciding what to change does not require piecing together several
        reads. The result is plain data, not a ProjectState, so the caller
        cannot reach past the boundary into storage.
        """
        state = self._resolve(project_id)
        snapshot = state.snapshot()

        return {
            "project_id": snapshot.project.get("id"),
            "name": snapshot.project.get("name"),
            "status": snapshot.project.get("status"),
            "path": str(state.project_path),
            "progress": state.progress(),
            "milestones": snapshot.milestones,
            "tasks": snapshot.tasks,
        }

    def create_milestone(self, project_id, milestone_id, name, status="planned"):
        """Create a milestone in project_id and return the stored record."""
        return self._work(project_id).create_milestone(
            milestone_id,
            name,
            status=status,
        )

    def update_milestone(self, project_id, milestone_id, **changes):
        """Change a milestone in project_id and return the stored record."""
        return self._work(project_id).update_milestone(milestone_id, **changes)

    def create_task(
        self,
        project_id,
        task_id,
        milestone,
        title,
        status="planned",
        assigned_to=None,
    ):
        """Create a task in project_id and return the stored record."""
        return self._work(project_id).create_task(
            task_id,
            milestone=milestone,
            title=title,
            status=status,
            assigned_to=assigned_to,
        )

    def update_task(self, project_id, task_id, **changes):
        """Change a task in project_id and return the stored record."""
        return self._work(project_id).update_task(task_id, **changes)

    def _resolve(self, project_id):
        """Return the ProjectState for project_id.

        Resolution is deliberately not cached. get_project re-scans the root
        and raises UnknownProjectError for an id that is unknown *or* currently
        malformed, so every read and every write is gated on the project being
        in a usable state. A cached path would let a write proceed against a
        project that had since become broken.
        """
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id must be a non-empty string")

        return self._projects.get_project(project_id)

    def _work(self, project_id):
        """Return a WorkManager bound to project_id."""
        return WorkManager(self._resolve(project_id).project_path)


def _print_pairs(pairs, indent="  "):
    width = max((len(label) for label, _ in pairs), default=0)

    for label, value in pairs:
        print(f"{indent}{label.ljust(width + 2)}{value}")


def _print_table(headers, rows):
    if not rows:
        print("  (none)")
        return

    columns = list(zip(*rows))
    widths = [
        max([len(str(header))] + [len(str(value)) for value in column])
        for header, column in zip(headers, columns)
    ]

    def render(cells):
        return "  " + "  ".join(
            str(cell).ljust(width) for cell, width in zip(cells, widths)
        ).rstrip()

    print(render(headers))

    for row in rows:
        print(render(row))


def _print_counts(counts):
    if not counts:
        print("  (none)")
        return

    width = max(len(status) for status in counts)

    for status in sorted(counts):
        print(f"  {status.ljust(width + 2)}{counts[status]}")


def _print_record(title, record, order):
    print(title)
    _print_pairs([(key, record[key]) for key in order if key in record])


def _command_projects(master, args):
    project_ids = master.list_projects()

    if not project_ids:
        print("no usable projects")
        return 0

    for project_id in project_ids:
        print(project_id)

    return 0


def _command_overview(master, args):
    overview = master.overview()
    totals = overview["totals"]

    print("PROJECTS ROOT")
    print(f"  {overview['root']}")

    print(f"\nPROJECTS ({overview['project_count']})")
    _print_table(
        ["ID", "NAME", "STATUS", "TASKS", "MILESTONES"],
        [
            [
                project["project_id"],
                project["project_name"],
                project["project_status"],
                project["total_tasks"],
                project["total_milestones"],
            ]
            for project in overview["projects"]
        ],
    )

    print("\nTOTALS")
    _print_pairs(
        [
            ("tasks", totals["tasks"]),
            ("milestones", totals["milestones"]),
        ]
    )

    print("\nTASK STATUS")
    _print_counts(totals["task_status_counts"])

    print("\nMILESTONE STATUS")
    _print_counts(totals["milestone_status_counts"])

    print(f"\nPROBLEMS ({overview['problem_count']})")
    _print_table(
        ["KIND", "PROJECT", "DETAIL"],
        [
            [
                problem["kind"],
                problem["project_id"] or "-",
                problem["error"].replace("\n", " | "),
            ]
            for problem in overview["problems"]
        ],
    )

    return 0


def _command_status(master, args):
    status = master.status(args.project_id)
    progress = status["progress"]

    print("PROJECT")
    _print_pairs(
        [
            ("id", status["project_id"]),
            ("name", status["name"]),
            ("status", status["status"]),
            ("path", status["path"]),
        ]
    )

    print("\nTOTALS")
    _print_pairs(
        [
            ("tasks", progress["total_tasks"]),
            ("milestones", progress["total_milestones"]),
        ]
    )

    print("\nTASK STATUS")
    _print_counts(progress["task_status_counts"])

    print("\nMILESTONE STATUS")
    _print_counts(progress["milestone_status_counts"])

    print(f"\nMILESTONES ({len(status['milestones'])})")
    _print_table(
        ["ID", "STATUS", "NAME"],
        [
            [milestone["id"], milestone["status"], milestone["name"]]
            for milestone in status["milestones"]
        ],
    )

    print(f"\nTASKS ({len(status['tasks'])})")
    _print_table(
        ["ID", "STATUS", "MILESTONE", "ASSIGNEE", "TITLE"],
        [
            [
                task["id"],
                task["status"],
                task["milestone"],
                task.get("assigned_to") or "-",
                task["title"],
            ]
            for task in status["tasks"]
        ],
    )

    return 0


def _command_create_milestone(master, args):
    record = master.create_milestone(
        args.project_id,
        args.milestone_id,
        args.name,
        status=args.status,
    )
    _print_record("MILESTONE CREATED", record, ("id", "name", "status"))

    return 0


def _command_update_milestone(master, args):
    changes = {}

    if args.name is not None:
        changes["name"] = args.name

    if args.status is not None:
        changes["status"] = args.status

    if not changes:
        print("error: nothing to update; pass --name and/or --status",
              file=sys.stderr)
        return 1

    record = master.update_milestone(
        args.project_id,
        args.milestone_id,
        **changes,
    )
    _print_record("MILESTONE UPDATED", record, ("id", "name", "status"))

    return 0


def _command_create_task(master, args):
    record = master.create_task(
        args.project_id,
        args.task_id,
        milestone=args.milestone,
        title=args.title,
        status=args.status,
        assigned_to=args.assignee,
    )
    _print_record(
        "TASK CREATED",
        record,
        ("id", "milestone", "title", "status", "assigned_to"),
    )

    return 0


def _command_update_task(master, args):
    changes = {}

    if args.milestone is not None:
        changes["milestone"] = args.milestone

    if args.title is not None:
        changes["title"] = args.title

    if args.status is not None:
        changes["status"] = args.status

    if args.assignee is not None:
        changes["assigned_to"] = args.assignee

    if not changes:
        print(
            "error: nothing to update; pass --milestone, --title, "
            "--status and/or --assignee",
            file=sys.stderr,
        )
        return 1

    record = master.update_task(args.project_id, args.task_id, **changes)
    _print_record(
        "TASK UPDATED",
        record,
        ("id", "milestone", "title", "status", "assigned_to"),
    )

    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m core.master",
        description=(
            "Inspect and update AI work projects through the Master control "
            "API."
        ),
    )
    parser.add_argument(
        "--root",
        default=None,
        metavar="PATH",
        help=(
            "Projects root to operate on. Defaults to the projects directory "
            "shipped with this repository. Place it before the command."
        ),
    )

    commands = parser.add_subparsers(
        dest="command",
        metavar="<command>",
        required=True,
    )

    projects = commands.add_parser(
        "projects",
        help="List the id of every usable project.",
        description="List the id of every usable project.",
    )
    projects.set_defaults(handler=_command_projects)

    overview = commands.add_parser(
        "overview",
        help="Show an aggregated view of every managed project.",
        description="Show an aggregated view of every managed project.",
    )
    overview.set_defaults(handler=_command_overview)

    status = commands.add_parser(
        "status",
        help="Show identity, progress, milestones and tasks for one project.",
        description="Show identity, progress, milestones and tasks for one "
                    "project.",
    )
    status.add_argument("project_id", help="Project id to inspect.")
    status.set_defaults(handler=_command_status)

    create_milestone = commands.add_parser(
        "create-milestone",
        help="Create a milestone.",
        description="Create a milestone in a project.",
    )
    create_milestone.add_argument("project_id", help="Project id.")
    create_milestone.add_argument("milestone_id", help="New milestone id.")
    create_milestone.add_argument("name", help="Human readable milestone name.")
    create_milestone.add_argument(
        "--status",
        default="planned",
        help="Milestone status (default: planned).",
    )
    create_milestone.set_defaults(handler=_command_create_milestone)

    update_milestone = commands.add_parser(
        "update-milestone",
        help="Change a milestone's name and/or status.",
        description="Change a milestone's name and/or status.",
    )
    update_milestone.add_argument("project_id", help="Project id.")
    update_milestone.add_argument("milestone_id", help="Milestone id to change.")
    update_milestone.add_argument("--name", help="New milestone name.")
    update_milestone.add_argument("--status", help="New milestone status.")
    update_milestone.set_defaults(handler=_command_update_milestone)

    create_task = commands.add_parser(
        "create-task",
        help="Create a task in an existing milestone.",
        description="Create a task in an existing milestone.",
    )
    create_task.add_argument("project_id", help="Project id.")
    create_task.add_argument("task_id", help="New task id.")
    create_task.add_argument(
        "--milestone",
        required=True,
        help="Id of the milestone this task belongs to.",
    )
    create_task.add_argument("--title", required=True, help="Task title.")
    create_task.add_argument(
        "--status",
        default="planned",
        help="Task status (default: planned).",
    )
    create_task.add_argument("--assignee", help="Who the task is assigned to.")
    create_task.set_defaults(handler=_command_create_task)

    update_task = commands.add_parser(
        "update-task",
        help="Change a task's fields.",
        description="Change one or more fields of an existing task.",
    )
    update_task.add_argument("project_id", help="Project id.")
    update_task.add_argument("task_id", help="Task id to change.")
    update_task.add_argument("--milestone", help="Move the task to a milestone.")
    update_task.add_argument("--title", help="New task title.")
    update_task.add_argument("--status", help="New task status.")
    update_task.add_argument("--assignee", help="Assign the task to someone.")
    update_task.set_defaults(handler=_command_update_task)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return args.handler(Master(args.root), args)
    except EXPECTED_ERRORS as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())