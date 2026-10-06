"""`ms`: the owner's command. Plain words in, plain text out.

    ms status [--details]      a headline, what needs you, news, what's next, spend, links
    ms publish <project>       push develop and the preview site now
    ms doctor                  a paste-ready diagnostic block for any AI helper (no secrets)
    ms report [session]        a session's report (default: the latest)
    ms report --summary        the latest morning summary: done, blocked, screenshots, cost
    ms chat <project> [--new]  talk to the planner: it drafts tasks with tests; `approve` queues them
    ms release <project>       release notes + a pull request develop -> main for you to merge
    ms backlog <project>       epics in priority order; `add "Title"`, `priority <epic> <N>`
    ms lessons <project>       lessons workers proposed; --approve all|IDS, --reject IDS|rest
    ms pick <project> <task> <asset> <n>   choose one of a task's generated images
    ms limit <project> [wait|free|paid]    answer a worker limit (see ms status)
    ms telegram pair|status|test|unpair    the Telegram bot (the phone interface)
    ms reopen <project> <task> [--reason]  put a blocked task back in the queue (recorded)
    ms split <project> <task> draft [guidance] | approve [anyway] | reject | escalate
    ms pause | resume          stop or allow new work (a running task finishes)
    ms stop                    stop the current run now, and pause
    ms daemon                  the background service loop (run by systemd)
    ms install                 put `ms` on your PATH (~/.local/bin/ms)
    ms service install         install and start the service (starts at login)
    ms service uninstall
    ms notify setup            create the phone-notification topic, show how to subscribe
    ms notify test             send a test notification
    ms notify send "text"      send your own short message (optional --link URL)
    ms github setup --app-id N save the GitHub App's id (docs/GITHUB-SETUP.md)
    ms github check            check the GitHub App, rulesets and Pages, in plain words

Lower-level tools stay in ``core.run_cli``.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import uuid
from pathlib import Path

from core.notify import Notifier, default_topic_path, new_topic
from core.run_config import ConfigError, default_config_path, load_config
from core.paths import RuntimePaths

__all__ = ["main", "service_unit"]

REPO = Path(__file__).resolve().parent.parent
UNIT_NAME = "master-system.service"


def _paths():
    return RuntimePaths.default()


def _config(args):
    return load_config(args.config)


# --- pause / resume / stop -----------------------------------------------------------------


def _pause_flag() -> Path:
    return _paths().state_dir / "paused"


def _command_pause(args, out):
    flag = _pause_flag()
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("paused by the owner\n")
    print("Paused. A task that is running now will finish; nothing new starts. "
          "`ms resume` to continue.", file=out)
    return 0


def _command_resume(args, out):
    _pause_flag().unlink(missing_ok=True)
    print("Resumed. The service picks up approved tasks within a few minutes.", file=out)
    return 0


def _command_stop(args, out):
    _command_pause(args, open(os.devnull, "w"))
    pid_file = _paths().state_dir / "run.pid"
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        print("No run is active. The service is paused; `ms resume` to continue.", file=out)
        return 0
    try:
        os.kill(pid, signal.SIGINT)
    except ProcessLookupError:
        pid_file.unlink(missing_ok=True)
        print("No run is active. The service is paused; `ms resume` to continue.", file=out)
        return 0
    print("Stopping the current run (it is recorded as interrupted; its work is kept). "
          "The service is paused; `ms resume` to continue.", file=out)
    return 0


def wrapper_script(repo: Path) -> str:
    return (f"#!/bin/sh\n# The Master System's owner command (installed by `ms install`).\n"
            f'PYTHONPATH="{repo}" exec "{repo}/.venv/bin/python" -P -m core.ms "$@"\n')


def projects_root(config) -> Path:
    from core.paths import default_projects_root

    return Path(config.run.projects_root or default_projects_root()).expanduser()


def missing_projects_root(config) -> Optional[str]:
    """A one-line hint when the projects root does not exist yet, else None."""
    root = projects_root(config)
    if root.is_dir():
        return None
    return (f"No projects yet: {root} does not exist. Run `ms install` to create it, then "
            "add a project (README, \"Try it\").")


def wait_for_projects_root(config, sleep, should_stop, out) -> bool:
    """The service without a projects root: say so once and idle until it exists.

    Exiting would make systemd restart the service every minute (and notify each time).
    Returns False if asked to stop first."""
    hint = missing_projects_root(config)
    if hint is None:
        return True
    print(f"master-system service: {hint} Waiting for it.", file=out, flush=True)
    while not should_stop():
        sleep(config.daemon.interval_s)
        if missing_projects_root(config) is None:
            return True
    return False


def _command_install(args, out):
    root = projects_root(_config(args))
    if not root.is_dir():
        root.mkdir(parents=True, mode=0o700)
        print(f"Created {root} for your project definitions.", file=out)
    target = Path.home() / ".local" / "bin" / "ms"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(wrapper_script(REPO))
    target.chmod(0o755)
    print(f"Installed {target}. Try: ms status", file=out)
    return 0


# --- daemon --------------------------------------------------------------------------------


def build_daemon(config, config_path):
    from core.daemon import Daemon
    from core.host import subprocess_runner
    from core.splits import SplitStep, SplitStore
    from core.worker_limits import LimitState
    from core.worker_models import FreeModelCheck, opencode_lister
    from core.images import waiting_tasks
    from core.master import Master
    from core.publish import Publisher
    from core.release import Releaser
    from core.run_cli import _lock_holder
    from core.sqlite_history import SQLiteHistoryStore

    paths = _paths()
    master = Master(config.run.projects_root)
    history = SQLiteHistoryStore(paths.history_path)

    def is_busy(project_id):
        return _lock_holder(master.project_state(project_id).project_path) is not None

    env_file = default_config_path().parent / "master.env"
    notifier = build_notifier(config)
    publisher = Publisher(master, history, lambda repo: github_app(config, repo),
                          paths.state_dir, notifier=notifier)
    releaser = Releaser(master, history, lambda repo: github_app(config, repo),
                        askpass_dir=paths.state_dir, prices=config.prices, publisher=publisher)
    return Daemon(
        master=master, history=history, config=config, state_dir=paths.state_dir,
        notifier=notifier,
        run=subprocess_runner(config_path, env_file, paths.state_dir / "logs" / "runs",
                              paths.state_dir),
        max_failures=max(3, 2 * len(config.worker.ladder)),
        after_run=[under_project_lock(master, "publish", publisher),
                   release_ready_notice(releaser)], is_busy=is_busy,
        preview_url=publisher.preview_url,
        watchers=[under_project_lock(master, "release check", releaser.watch)],
        prepare=[asset_step(config, master, paths, env_file),
                 SplitStep(master, history, paths.state_dir,
                           lambda project_id, chat_id: build_planner(config, project_id,
                                                                     chat_id),
                           notifier, escalate=bool(config.worker.ladder))],
        waiting=lambda project_id: {**waiting_tasks(master, project_id),
                                    **SplitStore(paths.state_dir).waiting(project_id)},
        summarize=morning_summary(config, master, history, paths, releaser, publisher),
        limits=LimitState(paths.state_dir),
        checks=[FreeModelCheck(config, paths.state_dir, opencode_lister(config.worker.opencode_bin),
                               notifier)],
    )


def morning_summary(config, master, history, paths, releaser, publisher):
    """The service's summary hook: write the report page, return the notification."""
    def summarize(since, reason, stalled):
        from core.summary import build_summary, headline, write_summary

        projects = [p for p in master.list_projects()
                    if master.project_state(p).project().get("auto_integrate") is True]
        summary = build_summary(
            master, history, since, config.prices, config.master.model,
            needs_you=lambda project_id: owner_needs(
                master, history, config, project_id, stalled=stalled,
                pending_release=releaser.open_pending(project_id)),
            max_failures=max(3, 2 * len(config.worker.ladder)), reason=reason,
            project_ids=projects,
            labels={n: p.get("label", n) for n, p in config.worker.profiles.items()})
        written = write_summary(summary, paths.state_dir / "reports")
        from core.summary import screenshots
        message = (f"{headline(summary)}\nThe service stopped: {reason}.\n"
                   f"Details: ms report --summary ({written['html'].as_uri()})")
        click = next((publisher.preview_url(p) for p in projects if publisher.preview_url(p)),
                     None)
        return "Morning summary", message, click, screenshots(summary)

    return summarize


def asset_step(config, master, paths, env_file):
    """Images for visual tasks, generated by the service with keys from master.env."""
    from core.daemon import load_env_file
    from core.images import AssetStep, build_image_providers
    from core.task_orchestrator import default_worktrees

    env = {**load_env_file(env_file), **os.environ}
    return AssetStep(master, paths.state_dir, build_image_providers(config.images, env),
                     default_worktrees(master, paths))


def release_ready_notice(releaser):
    """After a run: say once per batch that a release is possible (H-D: approve releases)."""
    def notice(project_id, session_id):
        from core.workspace import is_ancestor

        if releaser.open_pending(project_id):
            return []
        try:
            repo, develop, release, _ = releaser._setup(project_id)
        except ValueError:
            return []
        from core.workspace import branch_tip

        from core.host import git as host_git
        from core.release import released_ref

        tip = branch_tip(repo, develop)
        if tip and not is_ancestor(repo, tip, released_ref(host_git, repo, release)):
            return ["Release ready: develop has work that is not released yet. "
                    "Say `release` in `ms chat`, or run `ms release`."]
        return []
    return notice


def github_app(config, repo):
    """The configured GitHub App for repo, or None when it is not set up yet."""
    from core.github import GitHubApp, default_key_path

    key = Path(config.github.key_path).expanduser() if config.github.key_path \
        else default_key_path()
    if not config.github.app_id or not key.exists():
        return None
    return GitHubApp(app_id=config.github.app_id, repo=repo, key_path=key)


def telegram_token() -> Optional[str]:
    """TELEGRAM_BOT_TOKEN from the environment or master.env; never printed."""
    from core.daemon import load_env_file

    return (os.environ.get("TELEGRAM_BOT_TOKEN")
            or load_env_file(default_config_path().parent / "master.env")
            .get("TELEGRAM_BOT_TOKEN"))


def build_notifier(config):
    """Telegram when a bot token is set (ntfy as the fallback), otherwise ntfy."""
    ntfy = Notifier.from_file(config.daemon.ntfy_server)
    token = telegram_token()
    if not token:
        return ntfy
    from core.telegram import FallbackNotifier, TelegramAPI, TelegramNotifier, TelegramState

    return FallbackNotifier(TelegramNotifier(TelegramAPI(token),
                                             TelegramState(_paths().state_dir)), ntfy)


def start_telegram_bot(config_path, out):
    """The bot's long-polling thread inside the service; None without a token."""
    import threading

    token = telegram_token()
    if not token:
        return None
    from core.daemon import load_env_file
    from core.telegram import TelegramAPI, TelegramState
    from core.telegram_bot import BotOps, TelegramBot

    for key, value in load_env_file(default_config_path().parent / "master.env").items():
        os.environ.setdefault(key, value)  # the planner and /spend use the DeepSeek key
    state = TelegramState(_paths().state_dir)
    bot = TelegramBot(TelegramAPI(token, timeout=60), state, BotOps(state, config_path),
                      log=lambda line: print(line, file=out, flush=True))
    stop = threading.Event()
    thread = threading.Thread(target=bot.run, args=(stop,), name="telegram", daemon=True)
    thread.start()
    return stop


def _command_daemon(args, out):
    config = _config(args)
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))

    def sleep(seconds):
        import time

        end = time.monotonic() + seconds
        while not stopping and time.monotonic() < end:
            time.sleep(min(5, end - time.monotonic()))

    if args.once and missing_projects_root(config):
        print(missing_projects_root(config), file=out)
        return 0
    if not wait_for_projects_root(config, sleep, lambda: bool(stopping), out):
        return 0
    daemon = build_daemon(config, args.config)
    telegram = start_telegram_bot(args.config, out)
    print(f"master-system service: every {config.daemon.interval_s:.0f}s, "
          f"daily cap ${config.budget.daily_usd:.2f}, run cap ${config.budget.run_usd:.2f}, "
          f"notifications {'on' if daemon.notifier.enabled else 'off'}, "
          f"telegram {'on' if telegram else 'off'}", file=out, flush=True)
    if args.once:
        for line in daemon.cycle():
            print(line, file=out)
        return 0

    daemon.serve(sleep=sleep, should_stop=lambda: bool(stopping), out=out)
    if telegram is not None:
        telegram.set()
    return 0


# --- service -------------------------------------------------------------------------------


def service_unit(python: str, repo: Path, config_path: str | None) -> str:
    config = f" --config {config_path}" if config_path else ""
    return f"""[Unit]
Description=Master System: processes approved tasks
After=network-online.target

[Service]
Type=simple
WorkingDirectory={repo}
Environment=PYTHONUNBUFFERED=1
ExecStart={python} -m core.ms{config} daemon
ExecStopPost={python} -m core.ms{config} service stopped
Restart=on-failure
RestartSec=60

[Install]
WantedBy=default.target
"""


def _unit_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd" / "user" / UNIT_NAME


def _systemctl(*argv) -> int:
    from core.host import systemctl

    return systemctl(*argv)


def _command_service(args, out):
    if args.action == "install":
        python = str(REPO / ".venv" / "bin" / "python")
        config_path = str(Path(args.config).expanduser().resolve()) if args.config else None
        unit = _unit_path()
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(service_unit(python, REPO, config_path))
        code = _systemctl("daemon-reload") or _systemctl("enable", "--now", UNIT_NAME)
        print(f"Installed {unit}. It starts when you log in and is running now. "
              f"Logs: journalctl --user -u {UNIT_NAME}" if code == 0 else
              f"Wrote {unit}, but systemctl failed (exit {code}).", file=out)
        return code
    if args.action == "uninstall":
        _systemctl("disable", "--now", UNIT_NAME)
        _unit_path().unlink(missing_ok=True)
        _systemctl("daemon-reload")
        print("The service is stopped and removed.", file=out)
        return 0
    # "stopped": systemd's ExecStopPost; tell the owner if the stop was not clean.
    result = os.environ.get("SERVICE_RESULT", "success")
    if result != "success":
        config = _config(args)
        build_notifier(config).send(
            "Master System service stopped",
            f"The background service stopped unexpectedly ({result}). systemd restarts it "
            "in a minute; if this repeats, run `ms status`.", tags="warning", priority="high")
    return 0


# --- notifications -------------------------------------------------------------------------


def _command_notify(args, out):
    config = _config(args)
    path = default_topic_path()
    if args.action == "setup":
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new_topic() + "\n")
            path.chmod(0o600)
        topic = path.read_text().strip()
        url = f"{config.daemon.ntfy_server}/{topic}"
        print("Phone notifications:\n"
              "  1. Install the free 'ntfy' app (Play Store / App Store / F-Droid).\n"
              "  2. In the app tap +, 'Subscribe to topic', and enter this topic name:\n"
              f"       {topic}\n"
              f"     (server: {config.daemon.ntfy_server}; or open {url} on the phone).\n"
              "  3. Run `ms notify test`.\n"
              "Keep the topic private: anyone who knows it can read the notifications.",
              file=out)
        return 0
    notifier = Notifier.from_file(config.daemon.ntfy_server, path)
    if not notifier.enabled:
        print("Notifications are not set up yet: run `ms notify setup`.", file=out)
        return 1
    if args.action == "send":
        if not args.message:
            print('Usage: ms notify send "one short sentence" [--link URL]', file=out)
            return 1
        ok = notifier.send("Master System", args.message, tags="bell", click=args.link)
    else:
        ok = notifier.send("Master System", "Test notification: notifications work.",
                           tags="bell")
    print("Sent." if ok else "Sending failed (no network, or the server refused).", file=out)
    return 0 if ok else 1


# --- status / report -----------------------------------------------------------------------


def image_picks(master, project_id, state_dir) -> list:
    """'Needs you' lines for tasks that wait for images."""
    from core.images import candidates, missing_assets, task_assets

    if state_dir is None:
        return []
    project = master.project_state(project_id).project()
    lines = []
    for task in master.status(project_id)["tasks"]:
        if task.get("status") not in ("planned", "in_progress") or not task_assets(task):
            continue
        for asset in missing_assets(project, task):
            files = candidates(state_dir, project_id, task["id"], asset["name"])
            if int(asset.get("candidates", 1)) > 1 and files:
                sheet = files[0].parent / "index.html"
                lines.append(f"{task['id']}: pick an image for {asset['name']} "
                             f"({len(files)} candidates: {sheet.as_uri()}), then "
                             f"ms pick {project_id} {task['id']} {asset['name']} <n>")
            elif not files:
                lines.append(f"{task['id']}: waiting for images ({asset['name']}); the "
                             "service generates them (an image key must be in master.env)")
    return lines


def project_overview(master, history, project_id, *, is_busy=None, max_failures=3,
                     state_dir=None):
    """Everything `ms status` shows for one project, as plain data."""
    from core.daemon import work_for
    from core.history import EventType
    from core.publish import site_urls
    from core.workspace import is_ancestor

    status = master.status(project_id)
    project = master.project_state(project_id).project()
    tasks = status["tasks"]
    runnable, exhausted = work_for(status, history, max_failures)
    integrations = {}
    for e in history.events(project_id=project_id, types=[EventType.INTEGRATION]):
        integrations[e.task_id] = e.payload.get("result_sha")
    github = project.get("github") if isinstance(project.get("github"), dict) else {}
    release = github.get("release_branch")
    repo = Path(project.get("repository") or ".").expanduser()
    groups = {"running": [], "waiting": [], "blocked": [], "in develop": [], "released": [],
              "done": [], "other": []}
    for task in tasks:
        state = task.get("status")
        label = f"{task['id']} {task.get('title') or ''}".rstrip()
        if state == "in_progress":
            groups["running"].append(label)
        elif state == "planned":
            groups["waiting"].append(label)
        elif state == "blocked":
            groups["blocked"].append(label)
        elif state == "completed" and task["id"] in integrations and release:
            sha = integrations[task["id"]]
            try:
                from core.host import git as host_git
                from core.release import released_ref

                shipped = bool(sha) and is_ancestor(repo, sha,
                                                    released_ref(host_git, repo, release))
            except Exception:
                shipped = False
            groups["released" if shipped else "in develop"].append(label)
        elif state == "completed":
            groups["done"].append(label)
        elif state != "cancelled":
            groups["other"].append(label)
    needs_you = [f"{t}: failed {max_failures} attempts; change its description or acceptance"
                 for t in exhausted]
    needs_you += [f"{label}: blocked" for label in groups["blocked"]]
    needs_you += image_picks(master, project_id, state_dir)
    if groups["in develop"]:
        needs_you.append(f"release ready: {len(groups['in develop'])} task(s) in develop "
                         "are not released yet (`ms release`)")
    live_url, preview_url = site_urls(github)
    return {"project_id": project_id, "name": status.get("name"),
            "busy": bool(is_busy and is_busy(project_id)), "groups": groups,
            "ready": runnable, "needs_you": needs_you,
            "preview_url": preview_url, "live_url": live_url}


def _status_details(args, out):
    from core.master import Master
    from core.report import spend_since
    from core.daemon import start_of_today_utc
    from core.run_cli import _lock_holder
    from core.sqlite_history import SQLiteHistoryStore

    config = _config(args)
    paths = _paths()
    master = Master(config.run.projects_root)
    history = SQLiteHistoryStore(paths.history_path)
    paused = (paths.state_dir / "paused").exists()
    service = "paused (ms resume)" if paused else "on"
    spend = spend_since(history, start_of_today_utc(), config.prices, config.master.model)
    print(f"Master System: {service}. Spent today ${spend['total_usd']:.3f} of "
          f"${config.budget.daily_usd:.2f}"
          f" (Master ${spend['master_usd']:.3f}, workers ${spend['worker_usd']:.3f}).", file=out)

    def busy(project_id):
        return _lock_holder(master.project_state(project_id).project_path) is not None

    for project_id in master.list_projects():
        view = project_overview(master, history, project_id, is_busy=busy,
                                state_dir=paths.state_dir)
        state = "running now" if view["busy"] else (
            f"{len(view['ready'])} task(s) ready" if view["ready"] else "idle")
        print(f"\n{view['name'] or project_id}: {state}", file=out)
        for group, items in view["groups"].items():
            if items:
                print(f"  {group} ({len(items)}): " + "; ".join(items), file=out)
        if view["needs_you"]:
            print("  Waiting for you:", file=out)
            for item in view["needs_you"]:
                print(f"    - {item}", file=out)
        from core.report import tier_stats

        for t in tier_stats(history, config.prices, project_id=project_id):
            print(f"  tier {t['tier']} ({t['model']}): {t['passes']}/{t['attempts']} passed, "
                  f"${t['cost_usd']:.3f}", file=out)
        if view["preview_url"]:
            print(f"  Preview: {view['preview_url']}", file=out)
            print(f"  Live:    {view['live_url']}", file=out)
    return 0


def _local(iso: str) -> str:
    """A history timestamp as local time, e.g. 'Mon 21:48'."""
    from datetime import datetime

    return datetime.fromisoformat(iso).astimezone().strftime("%a %d %b, %H:%M")


def _ago(iso: str, now=None) -> str:
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc)
    minutes = int((now - datetime.fromisoformat(iso)).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    return f"{hours} h {minutes % 60} min ago" if hours < 24 else _local(iso)


def owner_needs(master, history, config, project_id, *, stalled=(), pending_release=None,
                status=None, exhausted=None) -> list:
    """What needs the owner in one project, in plain words (ms status, the summary)."""
    from core.daemon import work_for
    from core.lessons import LessonStore

    status = status or master.status(project_id)
    tasks = [t for t in status["tasks"] if t.get("status") != "cancelled"]
    titles = {t["id"]: t.get("title") or t["id"] for t in tasks}
    if exhausted is None:
        _, exhausted = work_for(status, history, max(3, 2 * len(config.worker.ladder)))
    needs = [f"{t['id']} {titles[t['id']]}: it is blocked; tell the planner what to change "
             "(ms chat)" for t in tasks if t.get("status") == "blocked"]
    needs += [f"{t} {titles[t]}: failed several attempts; change its description "
              "(ms chat)" for t in exhausted]
    from core.evidence import held_cancellations

    for task_id, reason in held_cancellations(history, project_id).items():
        if task_id in titles:
            needs.append(f"{task_id} {titles[task_id]}: the orchestrating model wants to "
                         f"cancel it" + (f": {reason}" if reason else "") + ". Keep it: "
                         f"ms reopen {project_id} {task_id} --reason \"…\" / Accept: "
                         f"ms cancel {project_id} {task_id} --reason \"…\"")
    if project_id in stalled:
        needs.append("The last run made no progress; the service waits for a change.")
    if pending_release:
        needs.append(f"Release {pending_release['version']} waits for your merge: "
                     f"{pending_release.get('pr_url')}")
    needs += image_picks(master, project_id, _paths().state_dir)
    from core.worker_limits import LimitState, describe

    limit = describe(project_id, LimitState(_paths().state_dir).get(project_id),
                     {n: p.get("label", n) for n, p in config.worker.profiles.items()})
    if limit:
        needs.append(limit)
    from core.splits import SplitStore

    for task_id, split in SplitStore(_paths().state_dir).pending(project_id).items():
        needs.append(f"{task_id} was too big for the worker; the planner drafted a split "
                     f"(chat {split.get('chat_id')}). See it with ms chat {project_id} → show, "
                     f"then: ms split {project_id} {task_id} approve | reject"
                     + (" | escalate" if config.worker.ladder else ""))
    pending_lessons = LessonStore(master.project_state(project_id).project_path).pending()
    if pending_lessons:
        needs.append(f"{len(pending_lessons)} lesson(s) from workers wait for your review: "
                     f"ms lessons {project_id}")
    view = project_overview(master, history, project_id)
    if view["groups"]["in develop"] and not pending_release:
        needs.append(f"{len(view['groups']['in develop'])} finished task(s) are in the "
                     "preview but not released; when you like them: ms release "
                     f"{project_id}")
    return needs


def friendly_status(master, history, config, *, paused, last_looked, is_busy, spend,
                    pending_release=None, stalled=(), now=None) -> str:
    """`ms status` for the owner: a headline, then what needs them, news, what's next."""
    from core.daemon import work_for
    from core.history import EventType

    lines = []
    for project_id in master.list_projects():
        project = master.project_state(project_id).project()
        if project.get("auto_integrate") is not True:
            continue
        status = master.status(project_id)
        name = status.get("name") or project_id
        tasks = [t for t in status["tasks"] if t.get("status") != "cancelled"]
        titles = {t["id"]: t.get("title") or t["id"] for t in tasks}
        done = sum(1 for t in tasks if t.get("status") == "completed")
        runnable, exhausted = work_for(status, history, max(3, 2 * len(config.worker.ladder)))

        needs = owner_needs(master, history, config, project_id, stalled=stalled,
                            pending_release=pending_release, status=status,
                            exhausted=exhausted)
        view = project_overview(master, history, project_id)

        # Headline.
        started = history.events(project_id=project_id, types=[EventType.ATTEMPT_STARTED])
        open_attempt = None
        if started:
            last = started[-1]
            ended = history.events(project_id=project_id, attempt_id=last.attempt_id,
                                   types=[EventType.ATTEMPT_FINISHED,
                                          EventType.ATTEMPT_INTERRUPTED])
            open_attempt = None if ended else last
        if is_busy(project_id) and open_attempt is not None:
            head = (f"Working on {open_attempt.task_id} ({titles.get(open_attempt.task_id, '')}),"
                    f" started {_ago(open_attempt.created_at, now)}.")
        elif is_busy(project_id):
            head = "Working (deciding the next step)."
        elif paused:
            head = "Paused (ms resume to continue)."
        else:
            head = "Idle."
        head += f" {done} of {len(tasks)} tasks done."
        head += (" Nothing needs you." if not needs else
                 f" {len(needs)} thing{'s' if len(needs) > 1 else ''} need{'' if len(needs) > 1 else 's'} you.")
        lines.append(f"{name}: {head}")

        if needs:
            lines.append("\n  Needs you")
            lines += [f"    - {n}" for n in needs]

        news = [e for e in history.events(project_id=project_id,
                                          types=[EventType.INTEGRATION, EventType.RELEASE])
                if e.created_at > last_looked]
        news_lines = []
        for e in news:
            if e.type is EventType.INTEGRATION:
                where = ("in the preview" if e.payload.get("base_branch") == project.get(
                    "base_branch") else f"in {e.payload.get('base_branch')}")
                news_lines.append(f"{_local(e.created_at)}  {e.task_id} "
                                  f"{titles.get(e.task_id, '')}: done, {where}")
            elif e.payload.get("stage") == "published":
                news_lines.append(f"{_local(e.created_at)}  released "
                                  f"{e.payload.get('version')}")
        lines.append("\n  Done since you last looked")
        lines += [f"    - {n}" for n in news_lines] or ["    (nothing new)"]

        coming = [f"{t} {titles[t]}" for t in runnable]
        coming += [f"{t['id']} {titles[t['id']]} (after "
                   f"{', '.join(t.get('depends_on') or [])})" for t in tasks
                   if t.get("status") == "planned" and t["id"] not in runnable
                   and t["id"] not in exhausted]
        lines.append("\n  Coming up")
        lines += [f"    - {c}" for c in coming] or ["    (no tasks queued; plan more with "
                                                    f"ms chat {project_id})"]
        if view["preview_url"]:
            lines.append(f"\n  Preview: {view['preview_url']}")
            lines.append(f"  Live:    {view['live_url']}")
        lines.append("")
    lines.append(f"Spent today: ${spend['total_usd']:.2f} of ${config.budget.daily_usd:.2f}."
                 + (" The service is paused." if paused else ""))
    return "\n".join(lines) + "\n"


def _command_status(args, out):
    hint = missing_projects_root(_config(args))
    if hint:
        print(hint, file=out)
        return 0
    if args.details:
        return _status_details(args, out)
    from datetime import datetime, timedelta, timezone

    from core.daemon import start_of_today_utc
    from core.master import Master
    from core.release import Releaser
    from core.report import spend_since
    from core.run_cli import _lock_holder
    from core.sqlite_history import SQLiteHistoryStore

    config = _config(args)
    paths = _paths()
    master = Master(config.run.projects_root)
    history = SQLiteHistoryStore(paths.history_path)
    marker = paths.state_dir / "last-looked"
    try:
        last_looked = marker.read_text().strip()
    except OSError:
        last_looked = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    import json

    try:
        memory = json.loads((paths.state_dir / "daemon-state.json").read_text())
    except (OSError, ValueError):
        memory = {}
    for line in check_releases(config, master, history, paths):
        print(line, file=out)
    releaser = Releaser(master, history, lambda repo: None, askpass_dir=paths.state_dir)
    pending = None
    for project_id in master.list_projects():
        pending = pending or releaser.open_pending(project_id)
    text = friendly_status(
        master, history, config, paused=(paths.state_dir / "paused").exists(),
        last_looked=last_looked,
        is_busy=lambda p: _lock_holder(master.project_state(p).project_path) is not None,
        spend=spend_since(history, start_of_today_utc(), config.prices, config.master.model),
        pending_release=pending, stalled=set(memory.get("stalled", {})))
    print(text, file=out, end="")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(datetime.now(timezone.utc).isoformat())
    return 0


def under_project_lock(master, holder: str, action):
    """``action(project_id, ...)`` holding the project's lock; no lines while it is busy.

    For ref writers outside a run (release check, publishing): while a run holds the
    lock, a change to the repository's refs is the worker's and is undone as tampering.
    """
    from core.run_lock import ProjectBusyError, ProjectLock

    def call(project_id, *args):
        try:
            with ProjectLock(master.project_state(project_id).project_path, holder=holder):
                return action(project_id, *args)
        except ProjectBusyError:
            return []
    return call


def check_releases(config, master, history, paths, *, max_age_s: float = 60, now=None,
                   releaser=None, notifier=None) -> list:
    """Finish any release whose PR the owner merged (cheap; at most once a minute).

    The service does this every cycle; `ms status` does it too, so a merge is
    followed through even while the service is idle or paused.
    """
    import time

    now = now if now is not None else time.time()
    stamp = paths.state_dir / "release-check"
    try:
        if now - float(stamp.read_text().strip()) < max_age_s:
            return []
    except (OSError, ValueError):
        pass
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(str(now))
    if releaser is None:
        from core.publish import Publisher
        from core.release import Releaser

        factory = lambda repo: github_app(config, repo)  # noqa: E731
        releaser = Releaser(master, history, factory, askpass_dir=paths.state_dir,
                            prices=config.prices,
                            publisher=Publisher(master, history, factory, paths.state_dir))
    if notifier is None:
        notifier = build_notifier(config)
    lines = []
    watch = under_project_lock(master, "release check", releaser.watch)
    for project_id in master.list_projects():
        try:
            done = watch(project_id)
        except Exception as error:  # GitHub unreachable: say so, keep the status
            done = [f"(could not check the release on GitHub: {error})"]
        if done and not done[0].startswith("(could not"):
            notifier.send(f"{project_id}: release", "\n".join(done), tags="rocket")
        lines += done
    return lines


SECRET_PATTERNS = [r"ghs_[A-Za-z0-9]+", r"gh[pousr]_[A-Za-z0-9]{20,}", r"sk-[A-Za-z0-9-]{16,}",
                   r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"]
SECRET_KEYS = ("key", "token", "secret", "password", "topic", "pass")


def redact(text: str, secrets=()) -> str:
    """Remove known secret values, secret-looking strings and secret-named config values."""
    import re

    for value in secrets:
        if value and len(value) >= 6:
            text = text.replace(value, "<redacted>")
    for pattern in SECRET_PATTERNS:
        text = re.sub(pattern, "<redacted>", text)
    lines = []
    for line in text.splitlines():
        name = line.split("=", 1)[0].strip().lower() if "=" in line else ""
        if name and any(word in name for word in SECRET_KEYS) and not name.startswith("#"):
            line = line.split("=", 1)[0] + "= <redacted>"
        lines.append(line)
    return "\n".join(lines)


def doctor_report(args) -> str:
    """A paste-ready diagnostic block for any AI helper. Never contains secrets."""
    import io
    import platform
    import shutil

    from core.daemon import load_env_file
    from core.host import capture
    from core.workspace import control_git_argv

    paths = _paths()
    config_path = Path(args.config).expanduser() if args.config else default_config_path()
    secrets = list(load_env_file(config_path.parent / "master.env").values())
    try:
        secrets.append(default_topic_path().read_text().strip())
    except OSError:
        pass
    sections = []

    def section(title, body):
        sections.append(f"### {title}\n{str(body).rstrip() or '(empty)'}\n")

    from core.worker_env import find_node_bin

    node = find_node_bin(22)
    section("Versions", "\n".join([
        f"ms: {capture([*control_git_argv(), '-C', str(REPO), 'describe', '--always', '--dirty', '--tags'])}",
        f"git: {capture([*control_git_argv(), '--version'])}",
        f"python: {platform.python_version()} ({sys.executable})",
        f"node (workers): {capture([str(Path(node) / 'node'), '--version']) if node else 'none >= 22'}",
        f"system: {platform.platform()}"]))
    section("Master System checkout", "\n".join([
        f"path: {REPO}",
        f"branch: {capture([*control_git_argv(), '-C', str(REPO), 'branch', '--show-current'])}",
        f"commit: {capture([*control_git_argv(), '-C', str(REPO), 'log', '-1', '--format=%h %s (%cr)'])}",
        "changes: " + (capture([*control_git_argv(), '-C', str(REPO), 'status', '--short']) or "none")]))
    try:
        config = _config(args)
        hint = missing_projects_root(config)
        section("Projects", f"projects: none yet. {hint}" if hint else
                f"root: {projects_root(config)}\nprojects: "
                + (", ".join(sorted(p.name for p in projects_root(config).iterdir()
                                    if p.is_dir())) or "none"))
    except Exception as error:
        section("Projects", f"(could not read: {error})")
    pid_file = paths.state_dir / "run.pid"
    section("Service", "\n".join([
        f"systemd: {capture(['systemctl', '--user', 'is-active', UNIT_NAME])}, "
        f"{capture(['systemctl', '--user', 'is-enabled', UNIT_NAME])}",
        f"paused: {'yes' if (paths.state_dir / 'paused').exists() else 'no'}",
        f"run in progress: {'pid ' + pid_file.read_text().strip() if pid_file.exists() else 'no'}"]))
    section("Last 30 service log lines",
            capture(["journalctl", "--user", "-u", UNIT_NAME, "-n", "30", "--no-pager",
                     "-o", "short-iso"]))
    report = io.StringIO()
    try:
        _command_report(argparse.Namespace(config=args.config, session=None), report)
    except Exception as error:
        report.write(f"(no report: {error})")
    section("Last run report", report.getvalue())
    try:
        config_text = config_path.read_text()
    except OSError:
        config_text = "(no config file; defaults)"
    env_names = sorted(load_env_file(config_path.parent / "master.env"))
    section(f"Config {config_path}", config_text + "\n# master.env: "
            + (", ".join(f"{n}=<redacted>" for n in env_names) or "missing"))
    check = io.StringIO()
    try:
        _command_github(argparse.Namespace(config=args.config, action="check", app_id=None),
                        check)
    except Exception as error:
        check.write(f"(github check failed: {error})")
    section("ms github check", check.getvalue())
    disk = []
    for label, where in (("home", Path.home()), ("state", paths.state_dir),
                         ("worktrees", paths.worktrees_root)):
        try:
            usage = shutil.disk_usage(where if where.exists() else Path.home())
            disk.append(f"{label} ({where}): {usage.free / 1e9:.1f} GB free of "
                        f"{usage.total / 1e9:.1f} GB")
        except OSError as error:
            disk.append(f"{label}: {error}")
    section("Disk space", "\n".join(disk))
    text = "## Master System diagnostics (ms doctor)\n\n" + "\n".join(sections)
    return redact(text, secrets)


def _command_doctor(args, out):
    print("```\n" + doctor_report(args).rstrip() + "\n```", file=out)
    return 0


def _command_publish(args, out):
    from core.master import Master
    from core.publish import Publisher
    from core.sqlite_history import SQLiteHistoryStore

    config = _config(args)
    paths = _paths()
    master = Master(config.run.projects_root)
    publisher = Publisher(master, SQLiteHistoryStore(paths.history_path),
                          lambda repo: github_app(config, repo), paths.state_dir)
    from core.run_lock import ProjectBusyError, ProjectLock

    try:
        with ProjectLock(master.project_state(args.project).project_path, holder="ms publish"):
            if args.accept_tip:
                sha = publisher.accept_tip(args.project)
                print(f"Accepted {sha[:12]} as the system's develop tip.", file=out)
            lines = publisher(args.project)
    except ProjectBusyError:
        print("A run is in progress; the service publishes after it.", file=out)
        return 1
    print("\n".join(lines) if lines else "Already published; nothing changed.", file=out)
    return 0


def _command_report(args, out):
    from core.report import build_report, render_report
    from core.session_store import FileSessionStore
    from core.sqlite_history import SQLiteHistoryStore

    config = _config(args)
    paths = _paths()
    if args.summary:
        latest = paths.state_dir / "reports" / "latest.txt"
        if not latest.exists():
            print("No summary yet: the service writes one when its work runs out.", file=out)
            return 0
        print(latest.read_text(), file=out, end="")
        print(f"\nWith screenshots: {(latest.parent / 'latest.html').as_uri()}", file=out)
        return 0
    session_id = args.session
    if session_id is None:
        sessions = sorted(FileSessionStore(paths.sessions_dir).list_sessions(),
                          key=lambda s: s.updated_at)
        if not sessions:
            print("No sessions yet.", file=out)
            return 0
        session_id = sessions[-1].session_id
    try:
        report = build_report(SQLiteHistoryStore(paths.history_path), session_id, config.prices)
    except ValueError as error:
        print(f"error: {error}", file=out)
        return 1
    print(render_report(report), file=out, end="")
    return 0


# --- planner chat ---------------------------------------------------------------------------


def build_planner(config, project_id, chat_id=None):
    from types import SimpleNamespace

    from core.daemon import load_env_file
    from core.master import Master
    from core.planner import PlannerChat
    from core.planner_checks import make_approver, make_checker
    from core.run_cli import build_provider, build_worker_env
    from core.run_config import MasterConfig
    from core.sqlite_history import SQLiteHistoryStore

    for key, value in load_env_file(default_config_path().parent / "master.env").items():
        os.environ.setdefault(key, value)
    p = config.planner
    provider = build_provider(SimpleNamespace(master=MasterConfig(
        provider=p.provider, model=p.model, base_url=p.base_url, timeout_s=p.timeout_s)))
    if hasattr(provider, "max_completion_tokens"):
        provider.max_completion_tokens = p.max_output_tokens
    paths = _paths()
    master = Master(config.run.projects_root)
    history = SQLiteHistoryStore(paths.history_path)
    checker = make_checker(master, paths, build_worker_env(config),
                           timeout_s=config.run.verification_timeout_s)
    return PlannerChat(project_id, master, history, provider,
                       store_dir=paths.state_dir / "planner", prices=config.prices,
                       model_label=f"{p.provider}:{p.model}", checker=checker,
                       approver=make_approver(master, history, paths, checker),
                       max_cost_usd=p.chat_usd, chat_id=chat_id)


def latest_open_chat(store_dir: Path, project_id: str):
    import json

    chats = []
    for path in sorted(Path(store_dir).glob(f"{project_id}-*.json")):
        try:
            data = json.loads(path.read_text())
        except ValueError:
            continue
        if data.get("status") == "open":
            chats.append(data["chat_id"])
    return chats[-1] if chats else None


CHAT_HELP = ("Type what you want in plain words. Commands: show (the draft), check (run the "
             "checks), approve (queue the tasks; 'approve anyway' also queues tasks flagged too "
             "big), discard, release (open the release pull "
             "request), help, quit.")
PASTE_HINT = ('To send several lines as one message, just paste them, or put them between '
              'two lines containing only """.')


def _width(out) -> int:
    import shutil

    return min(100, shutil.get_terminal_size((100, 24)).columns)


def _dim(text, out) -> str:
    isatty = getattr(out, "isatty", lambda: False)()
    return f"\033[2m{text}\033[0m" if isatty else text


def setup_line_editing() -> None:
    """Arrow keys, history, visible wrapping and bracketed paste for input()."""
    try:
        import readline
    except ImportError:
        return
    for setting in ("set enable-bracketed-paste on", "set horizontal-scroll-mode off"):
        try:
            readline.parse_and_bind(setting)
        except Exception:
            pass


def _pending_input(timeout=0.05) -> bool:
    """True when more pasted text is already waiting on a terminal stdin."""
    import select

    try:
        if not sys.stdin.isatty():
            return False
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        return bool(ready)
    except (OSError, ValueError):
        return False


def read_message(read, pending=_pending_input) -> str:
    """One owner message: a line, a paste (lines arriving together), or a \"\"\" block."""
    first = read("you> ")
    if first.strip() == '"""':
        lines = []
        while True:
            line = read("... ")
            if line.strip() == '"""':
                return "\n".join(lines).strip()
            lines.append(line)
    lines = [first]
    while pending():
        try:
            lines.append(read(""))
        except EOFError:
            break
    return "\n".join(lines).strip()


def _print_answer(answer, chat, out) -> None:
    from core.planner import _wrap

    width = _width(out)
    print("", file=out)
    print("\n".join(_wrap(answer["reply"], width, "  ")) or "  (no reply)", file=out)
    if answer["questions"]:
        print("", file=out)
        print("  Questions:", file=out)
        for n, question in enumerate(answer["questions"], 1):
            print("", file=out)
            wrapped = _wrap(question, width, "       ")
            wrapped[0] = f"  {n:>2}.  " + wrapped[0].lstrip()
            print("\n".join(wrapped), file=out)
    if answer["draft_changed"]:
        tasks = len((chat.state.draft or {}).get("tasks") or [])
        print("", file=out)
        print(f"  (draft updated: {tasks} task(s); `show` to read it, `check` to test it)",
              file=out)
    print("", file=out)
    print(_dim(f"  [this chat: ${chat.state.cost_usd:.4f} of ${chat.max_cost_usd:.2f}]", out),
          file=out)
    print("", file=out)


def chat_loop(chat, read=input, out=None, releaser=None, first_message=None,
              pending=_pending_input):
    """The conversation. ``read`` returns the owner's next line (EOFError ends it)."""
    from core.planner import DraftProblem, render_draft

    out = out or sys.stdout
    queued = [first_message] if first_message else []
    while True:
        try:
            line = queued.pop(0) if queued else read_message(read, pending)
        except (EOFError, KeyboardInterrupt):
            print("\nBye. The chat is saved; `ms chat` continues it.", file=out)
            return 0
        if not line:
            continue
        command = line.lower()
        try:
            if command in ("quit", "exit", "bye"):
                print("The chat is saved; `ms chat` continues it.", file=out)
                return 0
            if command == "help":
                print(CHAT_HELP + "\n" + PASTE_HINT, file=out)
            elif command == "show":
                print("", file=out)
                print(render_draft(chat.state.draft, chat.state.check, _width(out)), file=out)
                for problem in chat.problems() if chat.state.draft else []:
                    print(f"  (incomplete) {problem}", file=out)
                print("", file=out)
            elif command == "check":
                print("Checking the tests on the latest code (this can take a few minutes)...",
                      file=out, flush=True)
                result = chat.check()
                for item in result["items"]:
                    print(f"  [{'ok' if item['ok'] else '!!'}] {item['what']}"
                          + ("" if item["ok"] else f"\n       {item['note']}"), file=out)
                print("All checks passed. Type `approve` to queue the tasks." if result["ok"]
                      else "Some checks failed; tell the planner what to fix (or paste the "
                      "failure).", file=out)
            elif command in ("approve", "approve anyway"):
                print("Re-checking on the latest code and queueing...", file=out, flush=True)
                ids = chat.approve(size_override=command == "approve anyway")
                print(f"Approved. Queued {', '.join(ids)}; the service starts on them within a "
                      "few minutes. You'll get a notification when they're done.", file=out)
                return 0
            elif command == "release":
                if releaser is None:
                    print("  Releases are not available here.", file=out)
                else:
                    print(f"  {releaser.prepare(chat.project_id)['message']}", file=out)
            elif command == "discard":
                chat.discard()
                print("Discarded. Nothing was queued.", file=out)
                return 0
            else:
                _print_answer(chat.turn(line), chat, out)
        except DraftProblem as problem:
            print(f"  {problem}", file=out)
        except Exception as error:  # the provider or git failed; the chat is saved
            print(f"  error: {error}", file=out)


def _command_chat(args, out):
    config = _config(args)
    first = None
    if args.file:
        try:
            first = Path(args.file).expanduser().read_text().strip()
        except OSError as error:
            print(f"Cannot read {args.file}: {error}", file=out)
            return 1
    chat_id = None if args.new else latest_open_chat(_paths().state_dir / "planner",
                                                     args.project)
    chat = build_planner(config, args.project, chat_id)
    print(f"Planner for {args.project} ({config.planner.model}, up to "
          f"${config.planner.chat_usd:.2f} per chat)"
          + (f", continuing chat {chat_id}" if chat_id else "") + ".", file=out)
    print(CHAT_HELP, file=out)
    print(PASTE_HINT, file=out)
    setup_line_editing()
    return chat_loop(chat, out=out, releaser=build_releaser(config), first_message=first)


# --- release -------------------------------------------------------------------------------


def build_releaser(config):
    from core.master import Master
    from core.publish import Publisher
    from core.release import Releaser
    from core.sqlite_history import SQLiteHistoryStore

    paths = _paths()
    master = Master(config.run.projects_root)
    history = SQLiteHistoryStore(paths.history_path)
    factory = lambda repo: github_app(config, repo)  # noqa: E731
    publisher = Publisher(master, history, factory, paths.state_dir)
    return Releaser(master, history, factory, askpass_dir=paths.state_dir,
                    prices=config.prices, publisher=publisher)


def slug(title: str, prefix: str = "epic-") -> str:
    """A milestone id from a title: lowercase words joined by dashes."""
    import re

    words = re.findall(r"[a-z0-9]+", title.lower())
    text = prefix + "-".join(words)
    return text[:40].rstrip("-") or prefix.rstrip("-")


def _command_backlog(args, out):
    from core.backlog import backlog_rows, render_backlog
    from core.history import EventType
    from core.master import EXPECTED_ERRORS, Master
    from core.run_lock import ProjectBusyError, ProjectLock
    from core.sqlite_history import SQLiteHistoryStore

    config = _config(args)
    master = Master(config.run.projects_root)
    if args.action:
        history = SQLiteHistoryStore(_paths().history_path)
        state = master.project_state(args.project)
        try:
            with ProjectLock(state.project_path, holder="ms backlog"):
                if args.action == "add":
                    if not args.values:
                        print('usage: ms backlog <project> add "Title" [--summary TEXT] '
                              "[--priority N] [--id EPIC_ID]", file=out)
                        return 2
                    title = " ".join(args.values)
                    record = master.add_backlog_epic(args.project, args.id or slug(title),
                                                     title, args.summary, args.priority)
                    action = {"action": "backlog_add", "epic": record["id"]}
                    print(f"Added {record['id']} (priority {record['priority']}, proposed).",
                          file=out)
                else:
                    if len(args.values) != 2 or not args.values[1].isdigit():
                        print("usage: ms backlog <project> priority <epic> <N>", file=out)
                        return 2
                    record = master.set_epic_priority(args.project, args.values[0],
                                                      int(args.values[1]))
                    action = {"action": "backlog_priority", "epic": record["id"],
                              "priority": record["priority"]}
                    print(f"{record['id']} now has priority {record['priority']}.", file=out)
        except (*EXPECTED_ERRORS, ProjectBusyError) as error:
            print(f"error: {error}", file=out)
            return 1
        history.append(type=EventType.HUMAN_ACTION, run_id=uuid.uuid4().hex,
                       project_id=args.project, payload={"actor": args.actor, **action})
    rows = backlog_rows(master.status(args.project))
    print(f"Backlog of {args.project} (lower priority number runs first):", file=out)
    print(render_backlog(rows), file=out)
    proposed = [r for r in rows if r["status"] == "proposed"]
    if proposed:
        print(f"\nTo plan one: ms chat {args.project}, then \"plan epic "
              f"{proposed[0]['number']} from the backlog\".", file=out)
    return 0


def _command_lessons(args, out):
    from core.history import EventType
    from core.lessons import LessonStore
    from core.master import EXPECTED_ERRORS, Master
    from core.run_lock import ProjectBusyError, ProjectLock
    from core.sqlite_history import SQLiteHistoryStore

    master = Master(_config(args).run.projects_root)
    try:
        state = master.project_state(args.project)
    except EXPECTED_ERRORS as error:
        print(f"error: {error}", file=out)
        return 1
    store = LessonStore(state.project_path)
    if args.approve or args.reject:
        pending = [r["id"] for r in store.pending()]

        def ids(text):
            if not text:
                return []
            if text in ("all", "rest"):
                return None
            return [part.strip() for part in text.split(",") if part.strip()]

        approve, reject = ids(args.approve), ids(args.reject)
        if approve is None:
            approve = list(pending)
        if reject is None:
            reject = [i for i in pending if i not in approve]
        try:
            with ProjectLock(state.project_path, holder="ms lessons"):
                moved = store.decide(approve, reject)
        except (ValueError, ProjectBusyError) as error:
            print(f"error: {error}", file=out)
            return 1
        SQLiteHistoryStore(_paths().history_path).append(
            type=EventType.HUMAN_ACTION, run_id=uuid.uuid4().hex, project_id=args.project,
            payload={"actor": args.actor, "action": "lessons_decided",
                     "approved": [r["id"] for r in moved["approved"]],
                     "rejected": [r["id"] for r in moved["rejected"]]})
        print(f"Approved {len(moved['approved'])}, rejected {len(moved['rejected'])}.",
              file=out)
    pending = store.pending()
    approved = store.approved()
    print(f"Lessons for {args.project}: {len(pending)} pending, {len(approved)} approved "
          "(only approved lessons are used).", file=out)
    for record in pending:
        print(f"  {record['id']}  [{record['type']}] {record['text']}"
              f"  (from {record.get('task_id')})", file=out)
    if pending:
        print(f"\nApprove: ms lessons {args.project} --approve all  (or --approve L-1,L-3 "
              "--reject rest)", file=out)
    return 0


def _command_pick(args, out):
    from core.history import EventType
    from core.images import candidates, commit_asset, task_assets
    from core.master import EXPECTED_ERRORS, Master
    from core.run_lock import ProjectBusyError, ProjectLock
    from core.sqlite_history import SQLiteHistoryStore
    from core.task_orchestrator import default_worktrees

    config, paths = _config(args), _paths()
    master = Master(config.run.projects_root)
    try:
        state = master.project_state(args.project)
        task = state.get_task(args.task)
    except EXPECTED_ERRORS as error:
        print(f"error: {error}", file=out)
        return 1
    asset = next((a for a in task_assets(task or {}) if a["name"] == args.asset), None)
    if asset is None:
        print(f"error: task {args.task} has no image named {args.asset}", file=out)
        return 1
    files = candidates(paths.state_dir, args.project, args.task, args.asset)
    if not 1 <= args.n <= len(files):
        print(f"error: there are {len(files)} candidate(s); pick 1 to {len(files)}", file=out)
        return 1
    try:
        with ProjectLock(state.project_path, holder="ms pick"):
            commit = commit_asset(master, args.project, task, asset,
                                  files[args.n - 1].read_bytes(),
                                  default_worktrees(master, paths))
    except (ProjectBusyError, RuntimeError) as error:
        print(f"error: {error}", file=out)
        return 1
    SQLiteHistoryStore(paths.history_path).append(
        type=EventType.HUMAN_ACTION, run_id=uuid.uuid4().hex, project_id=args.project,
        task_id=args.task, payload={"actor": args.actor, "action": "asset_picked",
                                    "asset": args.asset, "candidate": args.n,
                                    "commit": commit})
    print(f"Committed candidate {args.n} of {args.asset} to {task['id']}'s base branch "
          f"({commit[:12]}). The task can run now.", file=out)
    return 0


def _command_limit(args, out):
    from core.history import EventType
    from core.master import EXPECTED_ERRORS, Master
    from core.sqlite_history import SQLiteHistoryStore
    from core.worker_limits import LimitState, _local_time, choice_profile, describe

    config, paths = _config(args), _paths()
    limits = LimitState(paths.state_dir)
    labels = {n: p.get("label", n) for n, p in config.worker.profiles.items()}
    state = limits.get(args.project)
    if args.choice is None:
        print(describe(args.project, state, labels) or
              f"No worker limit in {args.project}.", file=out)
        return 0
    try:
        project = Master(config.run.projects_root).project_state(args.project).project()
    except EXPECTED_ERRORS as error:
        print(f"error: {error}", file=out)
        return 1
    order = project.get("workers") or config.worker.workers
    profile = choice_profile(args.choice, order, config.worker.profiles, state.get("profile"))
    try:
        state = limits.choose(args.project, args.choice, profile)
    except ValueError as error:
        print(f"error: {error}", file=out)
        return 1
    SQLiteHistoryStore(paths.history_path).append(
        type=EventType.HUMAN_ACTION, run_id=uuid.uuid4().hex, project_id=args.project,
        payload={"actor": args.actor, "action": "worker_limit_choice", "choice": args.choice,
                 "profile": profile, "limited": state.get("override_for")
                 or state.get("profile")})
    if args.choice == "wait":
        print(f"The project waits until {_local_time(state['until'])}, then continues with "
              f"{labels.get(state.get('profile'), state.get('profile'))}.", file=out)
    else:
        paid = " It is paid: its cost counts toward the daily cap." if args.choice == "paid" \
            else ""
        print(f"The project continues with {labels.get(profile, profile)} until "
              f"{_local_time(state['override_until'])}.{paid}", file=out)
    return 0


def _command_telegram(args, out):
    from core.telegram import TelegramAPI, TelegramError, TelegramState

    state = TelegramState(_paths().state_dir)
    token = telegram_token()
    if not token:
        print("No bot token: put TELEGRAM_BOT_TOKEN=... in ~/.config/master-system/master.env "
              "(docs/TELEGRAM-SETUP.md).", file=out)
        return 1
    api = TelegramAPI(token)
    if args.action == "unpair":
        state.unpair()
        print("Unpaired: the bot ignores everyone until you pair again.", file=out)
        return 0
    try:
        bot = api.get_me().get("username")
    except TelegramError as error:
        print(f"error: {error}", file=out)
        return 1
    owner = state.owner()
    if args.action == "status":
        print(f"Bot @{bot}: " + (f"paired with Telegram user {owner[0]}." if owner
                                 else "not paired (ms telegram pair)."), file=out)
        return 0
    if args.action == "test":
        if not owner:
            print("Not paired yet: ms telegram pair.", file=out)
            return 1
        api.send_message(owner[1], "Test message from the Master System.")
        print("Sent.", file=out)
        return 0
    code = state.new_pairing_code()
    print(f"Pairing code (valid 15 minutes, once): {code}\n"
          f"In Telegram, open @{bot} and send:\n  /pair {code}\n"
          "The service must be running; your user id is stored and everyone else is "
          "ignored." + ("\nPairing again replaces the current owner." if owner else ""),
          file=out)
    return 0


def _command_reopen(args, out):
    from core.human_edits import reopen
    from core.master import EXPECTED_ERRORS, Master
    from core.run_lock import ProjectBusyError
    from core.sqlite_history import SQLiteHistoryStore

    paths = _paths()
    master = Master(_config(args).run.projects_root)
    try:
        record = reopen(master, SQLiteHistoryStore(paths.history_path), args.project, args.task,
                        args.reason, actor=args.actor, paths=paths)
    except (*EXPECTED_ERRORS, ProjectBusyError) as error:
        print(f"error: {error}", file=out)
        return 1
    print(f"{record['id']} is planned again; its attempt budget starts over.", file=out)
    return 0


def _command_cancel(args, out):
    from core.human_edits import cancel
    from core.master import EXPECTED_ERRORS, Master
    from core.run_lock import ProjectBusyError
    from core.sqlite_history import SQLiteHistoryStore

    paths = _paths()
    master = Master(_config(args).run.projects_root)
    try:
        record, dependents = cancel(master, SQLiteHistoryStore(paths.history_path),
                                    args.project, args.task, args.reason, actor=args.actor,
                                    paths=paths)
    except (*EXPECTED_ERRORS, ProjectBusyError) as error:
        print(f"error: {error}", file=out)
        return 1
    print(f"{record['id']} is cancelled.", file=out)
    if dependents:
        print(f"These tasks depend on it and cannot start now: {', '.join(dependents)}. "
              "Change their dependencies in ms chat, or cancel them too.", file=out)
    return 0


def split_decision(config, project_id, task_id, decision, actor="owner",
                   size_override=False) -> str:
    """Approve, reject or escalate a pending split. Returns a plain answer."""
    import uuid as _uuid

    from core.history import EventType
    from core.master import Master
    from core.run_lock import ProjectLock
    from core.splits import SplitStore
    from core.sqlite_history import SQLiteHistoryStore

    paths = _paths()
    store = SplitStore(paths.state_dir)
    split = store.get(project_id, task_id)
    if not split:
        raise ValueError(f"no split of {task_id} waits for a decision")
    master = Master(config.run.projects_root)
    history = SQLiteHistoryStore(paths.history_path)
    if decision == "approve":
        ids = build_planner(config, project_id, split["chat_id"]).approve(
            size_override=size_override)
        store.clear(project_id, task_id)
        return f"Approved: {task_id} is replaced by {', '.join(ids)}."
    state = master.project_state(project_id)
    with ProjectLock(state.project_path, holder=f"{actor} split {decision}"):
        if decision == "reject":
            master.update_task(project_id, task_id, status="blocked")
            answer = (f"Rejected. {task_id} is blocked: change it in ms chat {project_id}, or "
                      f"ms reopen {project_id} {task_id}.")
        elif decision == "escalate":
            if not config.worker.ladder:
                raise ValueError("escalation needs worker tiers ([worker] ladder)")
            master.set_task_size(project_id, task_id, "hard")
            answer = f"{task_id} runs again on the strongest worker tier."
        else:
            raise ValueError("choose approve, reject or escalate")
        history.append(type=EventType.HUMAN_ACTION, run_id=_uuid.uuid4().hex,
                       project_id=project_id, task_id=task_id,
                       payload={"actor": actor, "action": f"split_{decision}",
                                "chat_id": split.get("chat_id")})
    store.clear(project_id, task_id)
    return answer


def _command_split(args, out):
    from core.master import EXPECTED_ERRORS, Master
    from core.planner import DraftProblem
    from core.run_lock import ProjectBusyError
    from core.splits import SplitStep
    from core.sqlite_history import SQLiteHistoryStore

    config = _config(args)
    try:
        if args.decision in ("draft", "recheck"):
            for key, value in __import__("core.daemon", fromlist=["load_env_file"]) \
                    .load_env_file(default_config_path().parent / "master.env").items():
                os.environ.setdefault(key, value)
            paths = _paths()
            master = Master(config.run.projects_root)
            step = SplitStep(master, SQLiteHistoryStore(paths.history_path), paths.state_dir,
                             lambda p, c: build_planner(config, p, c), build_notifier(config),
                             escalate=bool(config.worker.ladder))
            if args.decision == "recheck":
                print(step.recheck(args.project, args.task), file=out)
            else:
                print(step.draft(args.project, args.task, " ".join(args.text) or None),
                      file=out)
            return 0
        print(split_decision(config, args.project, args.task, args.decision, args.actor,
                             size_override=args.text == ["anyway"]), file=out)
        return 0
    except (*EXPECTED_ERRORS, ProjectBusyError, DraftProblem) as error:
        print(f"error: {error}", file=out)
        return 1


def _command_release(args, out):
    result = build_releaser(_config(args)).prepare(args.project)
    print(result["message"], file=out)
    if result.get("notes") and not result.get("opened"):
        print("\n" + result["notes"], file=out)
    return 0


# --- github ---------------------------------------------------------------------------------


def _set_toml_value(path: Path, section: str, key: str, value: str) -> None:
    """Set ``key = "value"`` in ``[section]`` of a TOML file, keeping everything else."""
    lines = path.read_text().splitlines() if path.exists() else []
    out, in_section, done = [], False, False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            if in_section and not done:
                out.append(f'{key} = "{value}"')
                done = True
            in_section = stripped == f"[{section}]"
        elif in_section and stripped.split("=", 1)[0].strip() == key:
            out.append(f'{key} = "{value}"')
            done = True
            continue
        out.append(line)
    if not done:
        if not in_section:
            out += ["", f"[{section}]"]
        out.append(f'{key} = "{value}"')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n")


def _command_github(args, out):
    from core.github import GitHubError, default_key_path

    config_path = Path(args.config).expanduser() if args.config else default_config_path()
    if args.action == "setup":
        if not args.app_id or not args.app_id.isdigit():
            print("Give the App ID shown on the app's settings page: "
                  "ms github setup --app-id 1234567", file=out)
            return 1
        _set_toml_value(config_path, "github", "app_id", args.app_id)
        print(f"Saved the App ID in {config_path}. Now run: ms github check", file=out)
        return 0

    config = _config(args)
    ok = True

    def line(passed, text, fix=""):
        nonlocal ok
        ok = ok and passed
        print(f"  [{'ok' if passed else '!!'}] {text}" + ("" if passed or not fix
                                                     else f"\n       -> {fix}"), file=out)

    key = Path(config.github.key_path).expanduser() if config.github.key_path \
        else default_key_path()
    print("GitHub setup check:", file=out)
    line(bool(config.github.app_id), f"App ID configured ({config.github.app_id or 'missing'})",
         "ms github setup --app-id <the App ID>  (docs/GITHUB-SETUP.md, step 3)")
    line(key.exists(), f"private key at {key}", "docs/GITHUB-SETUP.md, step 4")
    if key.exists():
        line(oct(key.stat().st_mode)[-3:] == "600", "private key readable only by you",
             f"chmod 600 {key}")
    if not ok:
        return 1
    from core.master import Master

    master = Master(config.run.projects_root)
    for project_id in master.list_projects():
        github = master.project_state(project_id).project().get("github")
        if not isinstance(github, dict) or not github.get("repo"):
            continue
        repo = github["repo"]
        print(f"{project_id} ({repo}):", file=out)
        app = github_app(config, repo)
        try:
            app.token()
            line(True, "the app is installed on the repository and can sign in")
        except (GitHubError, RuntimeError) as error:
            line(False, f"the app cannot sign in: {error}", "docs/GITHUB-SETUP.md, step 5")
            continue
        try:
            installations = app.request("GET", "/installation/repositories")
            names = [r.get("full_name") for r in (installations or {}).get("repositories", [])]
            line(names == [repo], f"the app sees only this repository ({', '.join(names)})",
                 "install it on 'Only select repositories' with just this one (step 5)")
        except GitHubError as error:
            line(False, f"could not list the app's repositories: {error}")
        try:
            rules = app.request("GET", f"/repos/{repo}/rules/branches/"
                                       f"{github.get('release_branch', 'main')}") or []
            kinds = {r.get("type") for r in rules}
            line("pull_request" in kinds, "main requires a pull request (ruleset)", "step 6")
            line("non_fast_forward" in kinds and "deletion" in kinds,
                 "main blocks force pushes and deletion", "step 6")
        except GitHubError as error:
            line(False, f"could not read main's rules: {error}", "step 6")
        if not github.get("site_dir"):
            continue  # no preview site: Pages is not needed
        try:
            pages = app.request("GET", f"/repos/{repo}/pages") or {}
            source = pages.get("source") or {}
            line(source.get("branch") == "gh-pages", f"Pages serves gh-pages ({pages.get('html_url')})",
                 "step 7 (Settings -> Pages -> branch gh-pages)")
        except GitHubError as error:
            line(False, "Pages is not turned on yet", "step 7 (Settings -> Pages -> branch gh-pages)")
    print("All set." if ok else "Some steps are missing (see -> above).", file=out)
    return 0 if ok else 1


# --- parser --------------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(prog="ms", description="The Master System, for its owner.")
    parser.add_argument("--config", default=None, metavar="PATH")
    # Who acts, for the history record (the Telegram bot passes "telegram").
    parser.add_argument("--actor", default="owner", help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="Everything, in plain words.")
    status.add_argument("--details", action="store_true", help="The technical view.")
    status.set_defaults(handler=_command_status)
    commands.add_parser("doctor", help="A paste-ready diagnostic block (no secrets).") \
        .set_defaults(handler=_command_doctor)
    publish = commands.add_parser("publish", help="Push develop and the preview site now.")
    publish.add_argument("project")
    publish.add_argument("--accept-tip", action="store_true",
                         help="Accept a develop you changed outside Master System, then publish.")
    publish.set_defaults(handler=_command_publish)
    report = commands.add_parser("report", help="A session's report (default: the latest).")
    report.add_argument("session", nargs="?")
    report.add_argument("--summary", action="store_true",
                        help="The latest morning summary (with screenshots).")
    report.set_defaults(handler=_command_report)
    commands.add_parser("pause", help="Start nothing new.").set_defaults(handler=_command_pause)
    commands.add_parser("resume", help="Allow new work.").set_defaults(handler=_command_resume)
    commands.add_parser("install", help="Put `ms` on your PATH.").set_defaults(
        handler=_command_install)
    commands.add_parser("stop", help="Stop the current run now, and pause.").set_defaults(
        handler=_command_stop)
    daemon = commands.add_parser("daemon", help="The background service loop.")
    daemon.add_argument("--once", action="store_true", help="One cycle, then exit.")
    daemon.set_defaults(handler=_command_daemon)
    service = commands.add_parser("service", help="Install or remove the background service.")
    service.add_argument("action", choices=["install", "uninstall", "stopped"])
    service.set_defaults(handler=_command_service)
    notify = commands.add_parser("notify", help="Phone notifications.")
    notify.add_argument("action", choices=["setup", "test", "send"])
    notify.add_argument("message", nargs="?", help="For send: the text.")
    notify.add_argument("--link", default=None, help="For send: a link to open on tap.")
    notify.set_defaults(handler=_command_notify)
    release = commands.add_parser("release", help="Open the release pull request.")
    release.add_argument("project")
    release.set_defaults(handler=_command_release)
    chat = commands.add_parser("chat", help="Talk to the planner about what you want.")
    chat.add_argument("project")
    chat.add_argument("--new", action="store_true", help="Start a new chat.")
    chat.add_argument("--file", default=None, help="Send this file as the first message.")
    chat.set_defaults(handler=_command_chat)
    backlog = commands.add_parser("backlog", help="The project's epics in priority order.")
    backlog.add_argument("project")
    backlog.add_argument("action", nargs="?", choices=["add", "priority"])
    backlog.add_argument("values", nargs="*")
    backlog.add_argument("--summary", default=None)
    backlog.add_argument("--priority", type=int, default=None)
    backlog.add_argument("--id", default=None)
    backlog.set_defaults(handler=_command_backlog)
    split = commands.add_parser("split", help="Draft or decide a split of a too-big task.")
    split.add_argument("project")
    split.add_argument("task")
    split.add_argument("decision", choices=["draft", "recheck", "approve", "reject",
                                            "escalate"])
    split.add_argument("text", nargs="*", help="draft: guidance; approve: 'anyway'")
    split.set_defaults(handler=_command_split)
    reopen = commands.add_parser("reopen", help="Put a blocked task back in the queue.")
    reopen.add_argument("project")
    reopen.add_argument("task")
    reopen.add_argument("--reason", default="reopened by the owner")
    reopen.set_defaults(handler=_command_reopen)
    cancel = commands.add_parser("cancel", help="Cancel an open task (recorded).")
    cancel.add_argument("project")
    cancel.add_argument("task")
    cancel.add_argument("--reason", required=True)
    cancel.set_defaults(handler=_command_cancel)
    telegram = commands.add_parser("telegram", help="The Telegram bot: pair, status, test.")
    telegram.add_argument("action", choices=["pair", "status", "test", "unpair"])
    telegram.set_defaults(handler=_command_telegram)
    limit = commands.add_parser("limit", help="Answer a worker limit: wait, free or paid.")
    limit.add_argument("project")
    limit.add_argument("choice", nargs="?", choices=["wait", "free", "paid"])
    limit.set_defaults(handler=_command_limit)
    pick = commands.add_parser("pick", help="Choose a generated image for a task.")
    pick.add_argument("project")
    pick.add_argument("task")
    pick.add_argument("asset")
    pick.add_argument("n", type=int)
    pick.set_defaults(handler=_command_pick)
    lessons = commands.add_parser("lessons", help="Review lessons workers proposed.")
    lessons.add_argument("project")
    lessons.add_argument("--approve", default=None, metavar="IDS|all")
    lessons.add_argument("--reject", default=None, metavar="IDS|rest")
    lessons.set_defaults(handler=_command_lessons)
    github = commands.add_parser("github", help="Set up and check the GitHub App.")
    github.add_argument("action", choices=["setup", "check"])
    github.add_argument("--app-id", default=None)
    github.set_defaults(handler=_command_github)
    return parser


def main(argv=None, out=None) -> int:
    out = out if out is not None else sys.stdout
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args, out)
    except ConfigError as error:
        print(f"error: config: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
