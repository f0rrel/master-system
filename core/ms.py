"""`ms`: the owner's command. Plain words in, plain text out.

    ms status [--details]      a headline, what needs you, news, what's next, spend, links
    ms publish <project>       push develop and the preview site now
    ms report [session]        a session's report (default: the latest)
    ms chat <project> [--new]  talk to the planner: it drafts tasks with tests; `approve` queues them
    ms release <project>       release notes + a pull request develop -> main for you to merge
    ms pause | resume          stop or allow new work (a running task finishes)
    ms stop                    stop the current run now, and pause
    ms daemon                  the background service loop (run by systemd)
    ms install                 put `ms` on your PATH (~/.local/bin/ms)
    ms service install         install and start the service (starts at login)
    ms service uninstall
    ms notify setup            create the phone-notification topic, show how to subscribe
    ms notify test             send a test notification
    ms notify send "text"      send your own short message (optional --link URL)
    ms github setup --app-id N save the GitHub App's id (docs/github-setup.md)
    ms github check            check the GitHub App, rulesets and Pages, in plain words

Lower-level tools stay in ``core.run_cli``.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
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
            f'PYTHONPATH="{repo}" exec "{repo}/.venv/bin/python" -m core.ms "$@"\n')


def _command_install(args, out):
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
    publisher = Publisher(master, history, lambda repo: github_app(config, repo),
                          paths.state_dir)
    releaser = Releaser(master, history, lambda repo: github_app(config, repo),
                        askpass_dir=paths.state_dir, prices=config.prices, publisher=publisher)
    return Daemon(
        master=master, history=history, config=config, state_dir=paths.state_dir,
        notifier=Notifier.from_file(config.daemon.ntfy_server),
        run=subprocess_runner(config_path, env_file, paths.state_dir / "logs" / "runs",
                              paths.state_dir),
        max_failures=max(3, 2 * len(config.worker.ladder)),
        after_run=[publisher, release_ready_notice(releaser)], is_busy=is_busy,
        preview_url=publisher.preview_url, watchers=[releaser.watch],
    )


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


def _command_daemon(args, out):
    config = _config(args)
    daemon = build_daemon(config, args.config)
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
    print(f"master-system service: every {config.daemon.interval_s:.0f}s, "
          f"daily cap ${config.budget.daily_usd:.2f}, run cap ${config.budget.run_usd:.2f}, "
          f"notifications {'on' if daemon.notifier.enabled else 'off'}", file=out, flush=True)
    if args.once:
        for line in daemon.cycle():
            print(line, file=out)
        return 0

    def sleep(seconds):
        import time

        end = time.monotonic() + seconds
        while not stopping and time.monotonic() < end:
            time.sleep(min(5, end - time.monotonic()))

    daemon.serve(sleep=sleep, should_stop=lambda: bool(stopping), out=out)
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
        Notifier.from_file(config.daemon.ntfy_server).send(
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


def project_overview(master, history, project_id, *, is_busy=None, max_failures=3):
    """Everything `ms status` shows for one project, as plain data."""
    from core.daemon import work_for
    from core.history import EventType
    from core.publish import pages_url
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
    if groups["in develop"]:
        needs_you.append(f"release ready: {len(groups['in develop'])} task(s) in develop "
                         "are not released yet (`ms release`)")
    url = pages_url(github["repo"]) if github.get("repo") else None
    return {"project_id": project_id, "name": status.get("name"),
            "busy": bool(is_busy and is_busy(project_id)), "groups": groups,
            "ready": runnable, "needs_you": needs_you,
            "preview_url": url + "develop/" if url else None, "live_url": url}


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
        view = project_overview(master, history, project_id, is_busy=busy)
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

        needs = [f"{t['id']} {titles[t['id']]}: it is blocked; tell the planner what to change "
                 "(ms chat)" for t in tasks if t.get("status") == "blocked"]
        needs += [f"{t} {titles[t]}: failed several attempts; change its description "
                  "(ms chat)" for t in exhausted]
        if project_id in stalled:
            needs.append("The last run made no progress; the service waits for a change.")
        if pending_release:
            needs.append(f"Release {pending_release['version']} waits for your merge: "
                         f"{pending_release.get('pr_url')}")
        view = project_overview(master, history, project_id)
        if view["groups"]["in develop"] and not pending_release:
            needs.append(f"{len(view['groups']['in develop'])} finished task(s) are in the "
                         "preview but not released; when you like them: ms release "
                         f"{project_id}")

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
            lines.append(f"  Live game: {view['live_url']}")
        lines.append("")
    lines.append(f"Spent today: ${spend['total_usd']:.2f} of ${config.budget.daily_usd:.2f}."
                 + (" The service is paused." if paused else ""))
    return "\n".join(lines) + "\n"


def _command_status(args, out):
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


def _command_publish(args, out):
    from core.master import Master
    from core.publish import Publisher
    from core.sqlite_history import SQLiteHistoryStore

    config = _config(args)
    paths = _paths()
    master = Master(config.run.projects_root)
    publisher = Publisher(master, SQLiteHistoryStore(paths.history_path),
                          lambda repo: github_app(config, repo), paths.state_dir)
    lines = publisher(args.project)
    print("\n".join(lines) if lines else "Already published; nothing changed.", file=out)
    return 0


def _command_report(args, out):
    from core.report import build_report, render_report
    from core.session_store import FileSessionStore
    from core.sqlite_history import SQLiteHistoryStore

    config = _config(args)
    paths = _paths()
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
             "checks), approve (queue the tasks), discard, release (open the release pull "
             "request), help, quit.")


def chat_loop(chat, read=input, out=None, releaser=None):
    """The conversation. ``read`` returns the owner's next line (EOFError ends it)."""
    from core.planner import DraftProblem, render_draft

    out = out or sys.stdout
    while True:
        try:
            line = read("you> ").strip()
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
                print(CHAT_HELP, file=out)
            elif command == "show":
                print(render_draft(chat.state.draft, chat.state.check), file=out)
                for problem in chat.problems() if chat.state.draft else []:
                    print(f"  (incomplete) {problem}", file=out)
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
            elif command == "approve":
                print("Re-checking on the latest code and queueing...", file=out, flush=True)
                ids = chat.approve()
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
                answer = chat.turn(line)
                print(f"planner> {answer['reply']}", file=out)
                for question in answer["questions"]:
                    print(f"  ? {question}", file=out)
                if answer["draft_changed"]:
                    tasks = len((chat.state.draft or {}).get("tasks") or [])
                    print(f"  (draft updated: {tasks} task(s); `show` to read it, `check` to "
                          "test it)", file=out)
                print(f"  [this chat: ${chat.state.cost_usd:.4f} of "
                      f"${chat.max_cost_usd:.2f}]", file=out)
        except DraftProblem as problem:
            print(f"  {problem}", file=out)
        except Exception as error:  # the provider or git failed; the chat is saved
            print(f"  error: {error}", file=out)


def _command_chat(args, out):
    config = _config(args)
    chat_id = None if args.new else latest_open_chat(_paths().state_dir / "planner",
                                                     args.project)
    chat = build_planner(config, args.project, chat_id)
    print(f"Planner for {args.project} ({config.planner.model}, up to "
          f"${config.planner.chat_usd:.2f} per chat)"
          + (f", continuing chat {chat_id}" if chat_id else "") + ".", file=out)
    print(CHAT_HELP, file=out)
    return chat_loop(chat, out=out, releaser=build_releaser(config))


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
         "ms github setup --app-id <the App ID>  (docs/github-setup.md, step 3)")
    line(key.exists(), f"private key at {key}", "docs/github-setup.md, step 4")
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
            line(False, f"the app cannot sign in: {error}", "docs/github-setup.md, step 5")
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
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="Everything, in plain words.")
    status.add_argument("--details", action="store_true", help="The technical view.")
    status.set_defaults(handler=_command_status)
    publish = commands.add_parser("publish", help="Push develop and the preview site now.")
    publish.add_argument("project")
    publish.set_defaults(handler=_command_publish)
    report = commands.add_parser("report", help="A session's report (default: the latest).")
    report.add_argument("session", nargs="?")
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
    chat.set_defaults(handler=_command_chat)
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
