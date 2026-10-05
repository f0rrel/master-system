"""`ms`: the owner's command. Plain words in, plain text out.

    ms pause | resume          stop or allow new work (a running task finishes)
    ms stop                    stop the current run now, and pause
    ms daemon                  the background service loop (run by systemd)
    ms service install         install and start the service (starts at login)
    ms service uninstall
    ms notify setup            create the phone-notification topic, show how to subscribe
    ms notify test             send a test notification

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


# --- daemon --------------------------------------------------------------------------------


def build_daemon(config, config_path):
    from core.daemon import Daemon
    from core.host import subprocess_runner
    from core.master import Master
    from core.run_cli import _lock_holder
    from core.sqlite_history import SQLiteHistoryStore

    paths = _paths()
    master = Master(config.run.projects_root)
    history = SQLiteHistoryStore(paths.history_path)

    def is_busy(project_id):
        return _lock_holder(master.project_state(project_id).project_path) is not None

    env_file = default_config_path().parent / "master.env"
    return Daemon(
        master=master, history=history, config=config, state_dir=paths.state_dir,
        notifier=Notifier.from_file(config.daemon.ntfy_server),
        run=subprocess_runner(config_path, env_file, paths.state_dir / "logs" / "runs",
                              paths.state_dir),
        is_busy=is_busy,
    )


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
    ok = notifier.send("Master System", "Test notification: notifications work.",
                       tags="bell")
    print("Sent." if ok else "Sending failed (no network, or the server refused).", file=out)
    return 0 if ok else 1


# --- parser --------------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(prog="ms", description="The Master System, for its owner.")
    parser.add_argument("--config", default=None, metavar="PATH")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("pause", help="Start nothing new.").set_defaults(handler=_command_pause)
    commands.add_parser("resume", help="Allow new work.").set_defaults(handler=_command_resume)
    commands.add_parser("stop", help="Stop the current run now, and pause.").set_defaults(
        handler=_command_stop)
    daemon = commands.add_parser("daemon", help="The background service loop.")
    daemon.add_argument("--once", action="store_true", help="One cycle, then exit.")
    daemon.set_defaults(handler=_command_daemon)
    service = commands.add_parser("service", help="Install or remove the background service.")
    service.add_argument("action", choices=["install", "uninstall", "stopped"])
    service.set_defaults(handler=_command_service)
    notify = commands.add_parser("notify", help="Phone notifications.")
    notify.add_argument("action", choices=["setup", "test"])
    notify.set_defaults(handler=_command_notify)
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
