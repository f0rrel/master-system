"""Run the Master System for real: ``python -m core.run_cli``.

The composition root for autonomous runs. It builds the configured Master
provider, the OpenCode CLI worker, the AcceptanceVerifier and the stores in
the state directory, and runs everything through ``SessionRunner`` -- the
project lock, recovery, the completion gate and the attempt limit all apply.
It adds no framework: argparse, the config file (``core/run_config.py``) and
the existing modules.

Commands:

    start  <project> --objective TEXT [--session ID] [--until-stopped ...]
    resume <session> [--until-stopped ...]
    status [<project>]
    report <session> [--json]                                    from history alone
    integrate <project> <attempt> [--rebase]                     human only
    task describe <project> <task> (--text TEXT | --clear)      human edit, recorded
    task manual-check <project> <task> (--text TEXT | --clear)  human edit, recorded
    task set-acceptance <project> <task> (--command C ... [--protect GLOB ...] | --clear)

``--max-cost-usd X`` stops the run, before the next paid Master call, once the
session's recorded Master cost reaches X (stop reason ``budget_exhausted``).

``--until-stopped`` keeps resuming while a run ends at the step limit, bounded
by ``--max-runs`` (default 10) and ``--max-hours`` (default 8). It stops on any
other stop reason: approval needed, attempt limit, no actionable work, an
error, or Ctrl-C (recorded as ``run_error``).

Secrets: the Master's API key is read from this process's environment by the
provider. Workers never see it: they get the allowlisted environment of
``core/worker_env.py`` and their own worker home.

Global options: ``--config PATH``, ``--root PATH`` (projects root) and
``--state-dir PATH`` (history and sessions).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import human_edits
from core.master import EXPECTED_ERRORS, Master
from core.paths import RuntimePaths
from core.run_config import ConfigError, load_config
from core.run_lock import LOCK_FILENAME, ProjectBusyError, ProjectLock

STEP_LIMIT = "step_limit"

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
        self._root = args.root or self.config.run.projects_root
        self._master = None
        self._history = None

    @property
    def master(self):
        # Built on first use, so 'report' works from history alone.
        if self._master is None:
            self._master = Master(self._root)
        return self._master

    @property
    def history(self):
        if self._history is None:
            from core.sqlite_history import SQLiteHistoryStore

            self._history = SQLiteHistoryStore(self.paths.history_path)
        return self._history


class SetupError(RuntimeError):
    """The run cannot start: something on this machine is missing."""


# --- composition ---------------------------------------------------------------------


def build_provider(config):
    """The Master's reasoning provider, from [master]. The one place names live."""
    m = config.master
    if m.provider == "deepseek":
        from core.deepseek_provider import DEFAULT_BASE_URL, DeepSeekProvider

        if not os.environ.get("DEEPSEEK_API_KEY"):
            raise SetupError("DEEPSEEK_API_KEY is not set in run_cli's environment")
        return DeepSeekProvider(base_url=m.base_url or DEFAULT_BASE_URL, model=m.model,
                                timeout=m.timeout_s)
    if m.provider == "ollama":
        from core.ollama_provider import DEFAULT_BASE_URL, OllamaProvider

        return OllamaProvider(base_url=m.base_url or DEFAULT_BASE_URL, model=m.model,
                              timeout=m.timeout_s)
    from core.opencode_provider import DEFAULT_BASE_URL, OpenCodeProvider

    provider_id, _, model_id = m.model.partition("/")
    if not model_id:
        raise SetupError("[master] model for opencode must be 'providerID/modelID'")
    return OpenCodeProvider(base_url=m.base_url or DEFAULT_BASE_URL, provider_id=provider_id,
                            model_id=model_id, timeout=m.timeout_s,
                            total_timeout=m.timeout_s)


def build_worker(config):
    from core.opencode_backend import OpenCodeCliBackend

    w = config.worker
    if not Path(w.opencode_bin).is_file():
        raise SetupError(f"OpenCode not found at {w.opencode_bin} ([worker] opencode_bin)")
    return OpenCodeCliBackend(opencode_bin=w.opencode_bin, model=w.model,
                              extra_args=w.extra_args)


def build_verifier(config, master=None, paths=None):
    """The acceptance verifier, wrapped by the visual reviewer when one is configured."""
    from core.acceptance_verifier import AcceptanceVerifier

    verifier = AcceptanceVerifier()
    r = config.reviewer
    if master is None or paths is None or r.provider == "none" \
            or not os.environ.get("DEEPSEEK_API_KEY"):
        return verifier
    from core.deepseek_provider import DEFAULT_BASE_URL, DeepSeekVision
    from core.visual_review import VisualReviewVerifier

    judge = DeepSeekVision(base_url=r.base_url or DEFAULT_BASE_URL, model=r.model,
                           timeout=r.timeout_s, detail=r.detail)
    return VisualReviewVerifier(verifier, master, judge, paths.state_dir / "artifacts",
                                prices=config.prices, reviewer_label=judge.name)


def build_tiers(config):
    """The worker ladder from [worker.profiles] and [worker] ladder, or None."""
    from core.opencode_backend import OpenCodeCliBackend
    from core.worker_tiers import TierSet

    w = config.worker
    if not w.ladder:
        return None
    backends, envs, models = {}, {}, {}
    for name in w.ladder:
        profile = w.profiles[name]
        backends[name] = OpenCodeCliBackend(opencode_bin=w.opencode_bin, model=profile["model"],
                                            extra_args=w.extra_args)
        envs[name] = build_worker_env(config, home=profile["home"] or w.home)
        models[name] = profile["model"]
    return TierSet(tuple(w.ladder), backends, envs, models)


def build_worker_env(config, home=None):
    from core.worker_env import find_node_bin, worker_environment

    w = config.worker
    home = Path(home) if home is not None else Path(w.home)
    node_bin = find_node_bin(w.node_min_major)
    if node_bin is None:
        raise SetupError(f"no Node >= {w.node_min_major} found in nvm's install directory "
                         "(install it with 'nvm install', or lower [worker] node_min_major)")
    home.mkdir(parents=True, exist_ok=True)
    extra = {}
    if w.playwright_browsers_path:
        extra["PLAYWRIGHT_BROWSERS_PATH"] = str(w.playwright_browsers_path)
    return worker_environment(home, path_dirs=[node_bin], extra=extra)


def budget_check(ctx, session_id, max_cost_usd):
    """A stop_check that ends the run once the session's Master cost reaches the cap."""
    if max_cost_usd is None:
        return None
    from core.report import _price, master_cost_usd

    model = ctx.config.master.model
    if _price({"input_tokens": 1}, model, ctx.config.prices) is None:
        raise SetupError(f"--max-cost-usd needs a price for {model!r} in [prices]")

    def check():
        spent = master_cost_usd(ctx.history, session_id, ctx.config.prices, model)
        if spent >= max_cost_usd:
            return f"Master cost ${spent:.4f} reached the cap of ${max_cost_usd:.2f}"
        return None

    return check


def build_runner(ctx, stop_check=None):
    from core.session_runner import SessionRunner
    from core.session_store import FileSessionStore

    from core.auto_integrate import AutoIntegrator

    r = ctx.config.run
    verifier = build_verifier(ctx.config, ctx.master, ctx.paths)
    worker_env = build_worker_env(ctx.config)
    return SessionRunner(
        ctx.master,
        build_provider(ctx.config),
        build_worker(ctx.config),
        verifier,
        store=FileSessionStore(ctx.paths.sessions_dir),
        history=ctx.history,
        paths=ctx.paths,
        max_steps=r.max_steps,
        max_retries=r.max_retries,
        max_attempts_per_task=r.max_attempts_per_task,
        attempt_timeout_s=r.attempt_timeout_s,
        verification_timeout_s=r.verification_timeout_s,
        worker_env=worker_env,
        stop_check=stop_check,
        tiers=build_tiers(ctx.config),
        auto_integrate=AutoIntegrator(ctx.master, ctx.history, ctx.paths, verifier=verifier,
                                      worker_env=worker_env,
                                      verification_timeout_s=r.verification_timeout_s),
    )


# --- start / resume -------------------------------------------------------------------


def _describe(session) -> str:
    return (f"session {session.session_id}: {session.status.value} "
            f"(stop: {session.last_stop_reason}, steps: {session.steps_completed})")


def _run(ctx, args, out, first, session_id):
    """Run once, then (with --until-stopped) resume while runs end at the step limit."""
    runner = build_runner(ctx, budget_check(ctx, session_id, args.max_cost_usd))
    started = time.monotonic()
    session = first(runner)
    runs = 1
    print(_describe(session), file=out)
    while (
        args.until_stopped
        and session.last_stop_reason == STEP_LIMIT
        and runs < args.max_runs
        and time.monotonic() - started < args.max_hours * 3600
    ):
        session = runner.resume(session.session_id)
        runs += 1
        print(_describe(session), file=out)
    if session.pending_approval:
        print(f"waiting for a human: {session.pending_approval}", file=out)
    return EXIT_OK


def _command_start(ctx, args, out):
    session_id = args.session or (
        f"{args.project_id}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}")
    return _run(ctx, args, out,
                lambda runner: runner.start(args.project_id, args.objective, session_id),
                session_id)


def _command_resume(ctx, args, out):
    return _run(ctx, args, out, lambda runner: runner.resume(args.session_id),
                args.session_id)


# --- status ------------------------------------------------------------------------------


def _lock_holder(project_path):
    try:
        ProjectLock(project_path).acquire().release()
        return None
    except ProjectBusyError:
        try:
            return (Path(project_path) / LOCK_FILENAME).read_text().strip() or "unknown"
        except OSError:
            return "unknown"


def _command_status(ctx, args, out):
    from core.session_store import FileSessionStore
    from core.history import EventType

    projects = [args.project_id] if args.project_id else ctx.master.list_projects()
    store = FileSessionStore(ctx.paths.sessions_dir)
    for project_id in projects:
        state = ctx.master.status(project_id)
        holder = _lock_holder(ctx.master.project_state(project_id).project_path)
        print(f"PROJECT {project_id}  ({'running: ' + holder if holder else 'idle'})", file=out)
        for task in state["tasks"]:
            print(f"  task {task['id']:<16} {task['status']:<12} {task['title']}", file=out)
        for session in store.list_sessions(project_id):
            line = f"  {_describe(session)}"
            if session.pending_approval:
                line += f"  pending: {session.pending_approval}"
            print(line, file=out)
        events = ctx.history.events(project_id=project_id)
        ended = {e.attempt_id for e in events if e.type in (EventType.ATTEMPT_FINISHED,
                                                            EventType.ATTEMPT_INTERRUPTED)}
        for e in events:
            if e.type is EventType.ATTEMPT_STARTED and e.attempt_id not in ended:
                print(f"  open attempt {e.attempt_id} on {e.task_id} "
                      f"(deadline {e.payload.get('deadline_at')})", file=out)
        print("  last events:", file=out)
        for e in events[-10:]:
            print(f"    {e.seq:>6} {e.created_at[:19]} {e.type.value:<20} "
                  f"{e.task_id or '':<12} {e.payload.get('stop_reason') or e.payload.get('decision') or e.payload.get('outcome') or e.payload.get('verdict') or ''}",
                  file=out)
    return EXIT_OK


# --- integrate ---------------------------------------------------------------------------


def _command_integrate(ctx, args, out):
    from core.attempts import IntegrationRefused, integrate

    try:
        result = integrate(
            ctx.master, ctx.history, args.project_id, args.attempt_id, paths=ctx.paths,
            rebase=args.rebase, verifier=build_verifier(ctx.config),
            worker_env=build_worker_env(ctx.config) if args.rebase else None,
            verification_timeout_s=ctx.config.run.verification_timeout_s,
        )
    except IntegrationRefused as error:
        print(f"error: refused ({error.reason}): {error}", file=sys.stderr)
        return EXIT_ERROR
    if result.method == "already_integrated":
        print(f"already integrated: {result.result_sha} is on {result.base_branch}", file=out)
    else:
        print(f"integrated {result.attempt_id}: {result.base_branch} "
              f"{result.previous_sha[:12]} -> {result.result_sha[:12]} ({result.method})"
              + (f", rebased from {result.rebased_from[:12]} and re-verified"
                 if result.rebased_from else ""), file=out)
    return EXIT_OK


# --- report ----------------------------------------------------------------------------


def _command_report(ctx, args, out):
    from core.report import build_report, render_report

    try:
        report = build_report(ctx.history, args.session_id, ctx.config.prices)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_ERROR
    if args.json:
        import json

        print(json.dumps(report, indent=2), file=out)
    else:
        print(render_report(report), file=out, end="")
    return EXIT_OK


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


def _command_task_manual_check(ctx, args, out):
    if args.clear == bool(args.text):
        print("error: pass exactly one of --text or --clear", file=sys.stderr)
        return EXIT_USAGE
    record = human_edits.set_manual_check(ctx.master, ctx.history, args.project_id,
                                          args.task_id, None if args.clear else args.text,
                                          paths=ctx.paths)
    print(f"task {record['id']}: manual check {'cleared' if args.clear else 'set'} "
          f"(recorded)", file=out)
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

    def run_options(sub):
        sub.add_argument("--until-stopped", action="store_true",
                         help="Keep resuming while runs end at the step limit.")
        sub.add_argument("--max-runs", type=int, default=10)
        sub.add_argument("--max-hours", type=float, default=8.0)
        sub.add_argument("--max-cost-usd", type=float, default=None,
                         help="Stop before the next Master call once the session's Master "
                              "cost (priced from [prices]) reaches this.")

    start = commands.add_parser("start", help="Start a session and run it.")
    start.add_argument("project_id")
    start.add_argument("--objective", required=True)
    start.add_argument("--session", default=None, help="Session id (default: project-time).")
    run_options(start)
    start.set_defaults(handler=_command_start)

    resume = commands.add_parser("resume", help="Run a session again.")
    resume.add_argument("session_id")
    run_options(resume)
    resume.set_defaults(handler=_command_resume)

    integrate_cmd = commands.add_parser(
        "integrate", help="Human only: move the base branch to a verified attempt's result.")
    integrate_cmd.add_argument("project_id")
    integrate_cmd.add_argument("attempt_id")
    integrate_cmd.add_argument("--rebase", action="store_true",
                               help="If the base moved: replay, re-verify, then integrate.")
    integrate_cmd.set_defaults(handler=_command_integrate)

    report = commands.add_parser("report", help="The session's report, from history alone.")
    report.add_argument("session_id")
    report.add_argument("--json", action="store_true")
    report.set_defaults(handler=_command_report)

    status = commands.add_parser("status", help="Tasks, sessions, open attempts, events.")
    status.add_argument("project_id", nargs="?")
    status.set_defaults(handler=_command_status)

    task = commands.add_parser("task", help="Edit a task's spec (recorded in history).")
    task_commands = task.add_subparsers(dest="task_command", metavar="<edit>", required=True)

    describe = task_commands.add_parser("describe", help="Set or clear a task's description.")
    describe.add_argument("project_id")
    describe.add_argument("task_id")
    describe.add_argument("--text", default=None)
    describe.add_argument("--clear", action="store_true")
    describe.set_defaults(handler=_command_task_describe)

    manual = task_commands.add_parser("manual-check",
                                      help="Set or clear how to check the result by hand.")
    manual.add_argument("project_id")
    manual.add_argument("task_id")
    manual.add_argument("--text", default=None)
    manual.add_argument("--clear", action="store_true")
    manual.set_defaults(handler=_command_task_manual_check)

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
    except SetupError as error:
        print(f"error: setup: {error}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("interrupted: the run was recorded as run_error and the session stopped",
              file=sys.stderr)
        return 130
    except (ProjectBusyError, *EXPECTED_ERRORS, *_session_errors()) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_ERROR


def _session_errors():
    from core.session_store import SessionStoreError
    from core.work_session import SessionError

    return (SessionStoreError, SessionError)


if __name__ == "__main__":
    sys.exit(main())
