"""Run the Master System for real: ``python -m core.run_cli``.

The composition root for autonomous runs. It builds the configured Master
provider, the OpenCode CLI worker, the AcceptanceVerifier and the stores in
the state directory, and runs everything through ``SessionRunner`` -- the
project lock, recovery, the completion gate and the attempt limit all apply.
It adds no framework: argparse, the config file (``core/run_config.py``) and
the existing modules.

Commands:

    task describe <project> <task> (--text TEXT | --clear)      human edit, recorded
    task set-acceptance <project> <task> (--command C ... [--protect GLOB ...] | --clear)

Global options: ``--config PATH``, ``--root PATH`` (projects root) and
``--state-dir PATH`` (history and sessions).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import human_edits
from core.master import EXPECTED_ERRORS, Master
from core.paths import RuntimePaths
from core.run_config import ConfigError, load_config
from core.run_lock import ProjectBusyError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


class Context:
    """What every command needs, built once from the flags and the config."""

    def __init__(self, args):
        self.config = load_config(args.config)
        defaults = RuntimePaths.default()
        self.paths = RuntimePaths(
            state_dir=Path(args.state_dir) if args.state_dir else defaults.state_dir,
            worktrees_root=defaults.worktrees_root,
        )
        root = args.root or self.config.run.projects_root
        self.master = Master(root)
        self._history = None

    @property
    def history(self):
        if self._history is None:
            from core.sqlite_history import SQLiteHistoryStore

            self._history = SQLiteHistoryStore(self.paths.history_path)
        return self._history


# --- task: human edits, recorded ---------------------------------------------------


def _command_task_describe(ctx, args, out):
    if args.clear == bool(args.text):
        print("error: pass exactly one of --text or --clear", file=sys.stderr)
        return EXIT_USAGE
    record = human_edits.set_description(ctx.master, ctx.history, args.project_id,
                                         args.task_id, None if args.clear else args.text,
                                         paths=ctx.paths)
    print(f"task {record['id']}: description "
          f"{'cleared' if args.clear else 'set'} (recorded)", file=out)
    return EXIT_OK


def _command_task_acceptance(ctx, args, out):
    if args.clear:
        if args.command or args.protect:
            print("error: --clear cannot be combined with --command or --protect",
                  file=sys.stderr)
            return EXIT_USAGE
        acceptance = None
    else:
        if not args.command:
            print("error: pass at least one --command, or --clear", file=sys.stderr)
            return EXIT_USAGE
        acceptance = {"commands": list(args.command)}
        if args.protect:
            acceptance["protected_paths"] = list(args.protect)
    record = human_edits.set_acceptance(ctx.master, ctx.history, args.project_id,
                                        args.task_id, acceptance, paths=ctx.paths)
    print(f"task {record['id']}: acceptance {'cleared' if args.clear else 'set'} "
          f"(recorded)", file=out)
    return EXIT_OK


def build_parser():
    parser = argparse.ArgumentParser(prog="python -m core.run_cli",
                                     description="Run the Master System.")
    parser.add_argument("--config", default=None, metavar="PATH",
                        help="Config file (default ~/.config/master-system/config.toml).")
    parser.add_argument("--root", default=None, metavar="PATH", help="Projects root.")
    parser.add_argument("--state-dir", default=None, metavar="PATH",
                        help="State directory (history.sqlite, sessions/).")
    commands = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    task = commands.add_parser("task", help="Edit a task's spec (recorded in history).")
    task_commands = task.add_subparsers(dest="task_command", metavar="<edit>", required=True)

    describe = task_commands.add_parser("describe", help="Set or clear a task's description.")
    describe.add_argument("project_id")
    describe.add_argument("task_id")
    describe.add_argument("--text", default=None)
    describe.add_argument("--clear", action="store_true")
    describe.set_defaults(handler=_command_task_describe)

    acceptance = task_commands.add_parser("set-acceptance",
                                          help="Set or clear a task's acceptance.")
    acceptance.add_argument("project_id")
    acceptance.add_argument("task_id")
    acceptance.add_argument("--command", action="append", default=[])
    acceptance.add_argument("--protect", action="append", default=[], metavar="GLOB")
    acceptance.add_argument("--clear", action="store_true")
    acceptance.set_defaults(handler=_command_task_acceptance)

    return parser


def main(argv=None, out=None) -> int:
    out = out if out is not None else sys.stdout
    args = build_parser().parse_args(argv)
    try:
        ctx = Context(args)
        return args.handler(ctx, args, out)
    except ConfigError as error:
        print(f"error: config: {error}", file=sys.stderr)
        return EXIT_USAGE
    except (ProjectBusyError, *EXPECTED_ERRORS) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
