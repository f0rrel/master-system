"""The Telegram bot: the owner's phone interface to the service.

It long-polls Telegram from a thread inside the service and serves only the paired
owner (core.telegram). Everything it does is one of the fixed operations below;
most reuse the `ms` commands (with ``--actor telegram``), so behaviour and history
are the same as at the terminal. Actions that change something are recorded as
a ``human_action`` with ``actor: telegram``. Approve, release, a paid worker and
worker-limit choices ask for a confirmation button first.

There are no payment or billing actions.
"""

from __future__ import annotations

import io
import threading
import time
import uuid
from contextlib import redirect_stderr
from pathlib import Path
from typing import Callable, Optional

from core.telegram import TelegramError, esc

__all__ = ["TelegramBot", "BotOps", "MENU", "COMMANDS"]

MENU = [["/status", "/summary", "/spend"],
        ["/backlog", "/report", "/lessons"],
        ["/show", "/check", "/approve"],
        ["/pick", "/release", "/doctor"],
        ["/pause", "/resume", "/stop"]]

COMMANDS = {
    "status": "What the service is doing and what needs you",
    "summary": "The latest morning summary, with screenshots",
    "report": "The latest run's report",
    "spend": "Today's spend, the caps and the DeepSeek balance",
    "backlog": "The backlog (optionally: /backlog <project>)",
    "project": "Set the active project: /project <name>",
    "show": "The planner's current draft",
    "check": "Run the checks on the draft",
    "approve": "Approve the draft (asks to confirm)",
    "discard": "Discard the planner chat",
    "release": "Open the release pull request (asks to confirm)",
    "pick": "Choose generated images",
    "lessons": "Review lessons workers proposed",
    "limit": "Answer a worker limit",
    "pause": "Start nothing new",
    "resume": "Allow new work",
    "stop": "Stop the current run now, and pause",
    "doctor": "Diagnostics (keys redacted)",
    "menu": "Show the button menu",
}
LONG = 3500


class BotOps:
    """The fixed operations. Each returns a list of replies:
    ``{"text"}``, ``{"text", "buttons"}``, ``{"photo", "caption", "buttons"}`` or
    ``{"document", "name", "caption"}``. ``buttons`` are [(label, action dict)]."""

    def __init__(self, state, config_path=None, ms_main: Optional[Callable] = None):
        self.state = state
        self._config_path = config_path
        if ms_main is None:
            from core.ms import main as ms_main
        self._ms_main = ms_main

    # --- helpers ---

    def ms(self, *argv) -> tuple:
        out, err = io.StringIO(), io.StringIO()
        prefix = ["--actor", "telegram"]
        if self._config_path:
            prefix = ["--config", str(self._config_path)] + prefix
        with redirect_stderr(err):
            code = self._ms_main(prefix + [str(a) for a in argv], out=out)
        return code, (out.getvalue() + err.getvalue()).strip()

    def config(self):
        from core.run_config import load_config

        return load_config(self._config_path)

    def master(self):
        from core.master import Master

        return Master(self.config().run.projects_root)

    def project(self, name: Optional[str] = None) -> Optional[str]:
        """The named project, the active one, or the only hands-off project."""
        if name:
            return name
        active = self.state.load().get("project")
        if active:
            return active
        master = self.master()
        hands_off = [p for p in master.list_projects()
                     if master.project_state(p).project().get("auto_integrate") is True]
        return hands_off[0] if len(hands_off) == 1 else None

    def record(self, action: str, project: Optional[str] = None, **payload) -> None:
        from core.history import EventType
        from core.ms import _paths
        from core.sqlite_history import SQLiteHistoryStore

        SQLiteHistoryStore(_paths().history_path).append(
            # System-wide actions (pairing, pause) are recorded under "_service".
            type=EventType.HUMAN_ACTION, run_id=uuid.uuid4().hex,
            project_id=project or "_service",
            payload={"actor": "telegram", "action": action, **payload})

    @staticmethod
    def pre(text: str) -> dict:
        return {"text": f"<pre>{esc(text)}</pre>"}

    def long_text(self, text: str, name: str, caption: str) -> list:
        if len(text) > LONG:
            return [{"document": text.encode(), "name": name, "caption": caption}]
        return [self.pre(text)]

    # --- reading ---

    def status(self, _arg=None) -> list:
        return [self.pre(self.ms("status")[1])]

    def report(self, _arg=None) -> list:
        return self.long_text(self.ms("report")[1], "report.txt", "The latest run's report")

    def summary(self, _arg=None) -> list:
        import json

        from core.ms import _paths

        text = self.ms("report", "--summary")[1]
        replies = self.long_text(text, "summary.txt", "The latest morning summary")
        data = _paths().state_dir / "reports" / "latest.json"
        try:
            summary = json.loads(data.read_text())
        except (OSError, ValueError):
            return replies
        for project in summary.get("projects", []):
            for review in project.get("reviews", []):
                for shot in review.get("screenshots", [])[:4]:
                    if Path(shot).is_file():
                        replies.append({"photo": shot, "caption": esc(
                            f"{review['id']} {Path(shot).stem} — review: {review['verdict']}")})
        return replies

    def backlog(self, arg=None) -> list:
        project = self.project(arg)
        if not project:
            return [{"text": "Which project? /project <name> first, or /backlog <name>."}]
        return [self.pre(self.ms("backlog", project)[1])]

    def set_project(self, arg=None) -> list:
        projects = self.master().list_projects()
        if not arg or arg not in projects:
            return [{"text": "Projects: " + ", ".join(projects) + "\nUse /project <name>."}]
        self.state.update(project=arg)
        return [{"text": f"Active project: <b>{esc(arg)}</b>"}]

    def spend(self, _arg=None) -> list:
        from core.daemon import start_of_today_utc
        from core.ms import _paths
        from core.report import spend_since
        from core.sqlite_history import SQLiteHistoryStore

        config = self.config()
        spend = spend_since(SQLiteHistoryStore(_paths().history_path), start_of_today_utc(),
                            config.prices, config.master.model)
        lines = [f"Spent today: ${spend['total_usd']:.3f} of ${config.budget.daily_usd:.2f}",
                 f"  Master and planner ${spend['master_usd']:.3f}, workers "
                 f"${spend['worker_usd']:.3f}, reviewer ${spend.get('reviewer_usd', 0):.3f}",
                 f"Caps: ${config.budget.run_usd:.2f} per run, "
                 f"${config.planner.chat_usd:.2f} per planner chat"]
        balance = deepseek_balance()
        if balance:
            lines.append(f"DeepSeek balance: {balance} (top up on DeepSeek's website)")
        return [{"text": esc("\n".join(lines))}]

    def doctor(self, _arg=None) -> list:
        text = self.ms("doctor")[1]
        head = [line for line in text.splitlines() if line.startswith(("ms:", "branch:",
                                                                        "systemd:", "paused:"))]
        return [{"text": esc("\n".join(head) or "Diagnostics attached.")},
                {"document": text.encode(), "name": "doctor.txt",
                 "caption": "ms doctor (keys, tokens and topics redacted)"}]

    # --- the service ---

    def pause(self, _arg=None) -> list:
        self.record("pause")
        return [{"text": esc(self.ms("pause")[1])}]

    def resume(self, _arg=None) -> list:
        self.record("resume")
        return [{"text": esc(self.ms("resume")[1])}]

    def stop(self, _arg=None) -> list:
        self.record("stop")
        return [{"text": esc(self.ms("stop")[1])}]

    # --- the planner ---

    def _chat(self, project, new=False):
        from core.ms import _paths, build_planner, latest_open_chat

        chat_id = None if new else latest_open_chat(_paths().state_dir / "planner", project)
        return build_planner(self.config(), project, chat_id)

    def plan(self, text: str) -> list:
        project = self.project()
        if not project:
            return [{"text": "Which project? Set one with /project <name>."}]
        from core.planner import DraftProblem

        try:
            result = self._chat(project).turn(text)
        except DraftProblem as error:
            return [{"text": esc(str(error))}]
        lines = [esc(result["reply"])]
        if result["questions"]:
            lines.append("")
            lines += [f"<b>{n}.</b> {esc(q)}" for n, q in enumerate(result["questions"], 1)]
        if result["draft_changed"]:
            lines.append("\nThe draft changed: /show to read it, /check, then /approve.")
        return [{"text": "\n".join(lines)}]

    def show(self, _arg=None) -> list:
        from core.planner import render_draft

        import json

        from core.ms import _paths, latest_open_chat

        project = self.project()
        if not project:
            return [{"text": "Which project? /project <name>."}]
        store = _paths().state_dir / "planner"
        chat_id = latest_open_chat(store, project)
        state = json.loads((store / f"{chat_id}.json").read_text()) if chat_id else {}
        text = render_draft(state.get("draft"), state.get("check"), width=70)
        if len(text) > LONG:
            return [{"document": text.encode(), "name": f"draft-{chat_id}.md",
                     "caption": "The current draft"}]
        return [self.pre(text)]

    def check(self, _arg=None) -> list:
        from core.planner import DraftProblem

        project = self.project()
        try:
            result = self._chat(project).check()
        except DraftProblem as error:
            return [{"text": esc(str(error))}]
        lines = ["All checks passed: ready to /approve." if result["ok"] else "Checks FAILED:"]
        lines += [("✅ " if i["ok"] else "❌ ") + i["what"]
                  + ("" if i["ok"] else f"\n   {i['note'][-300:]}") for i in result["items"]]
        return [{"text": esc("\n".join(lines))}]

    def approve(self, _arg=None) -> list:
        from core.planner import DraftProblem

        project = self.project()
        try:
            ids = self._chat(project).approve()
        except DraftProblem as error:
            return [{"text": esc(str(error))}]
        self.record("planner_approve", project, ids=ids)
        return [{"text": esc(f"Approved and queued: {', '.join(ids)}. The service starts "
                             "within a few minutes.")}]

    def discard(self, _arg=None) -> list:
        project = self.project()
        self._chat(project).discard()
        self.record("planner_discard", project)
        return [{"text": "The planner chat is discarded. The next message starts a new one."}]

    # --- releases, images, lessons, limits ---

    def release(self, _arg=None) -> list:
        from core.ms import build_releaser

        project = self.project()
        result = build_releaser(self.config()).prepare(project)
        self.record("release_prepare", project, opened=bool(result.get("opened")))
        return [{"text": esc(result["message"]) + "\nMerging stays on GitHub.", "preview": True}]

    def pick_menu(self, _arg=None) -> list:
        from core.images import candidates, missing_assets, task_assets
        from core.ms import _paths

        project = self.project()
        master = self.master()
        definition = master.project_state(project).project()
        replies = []
        for task in master.status(project)["tasks"]:
            if task.get("status") not in ("planned", "in_progress") or not task_assets(task):
                continue
            for asset in missing_assets(definition, task):
                for n, path in enumerate(candidates(_paths().state_dir, project, task["id"],
                                                    asset["name"]), 1):
                    replies.append({"photo": str(path), "caption": esc(
                        f"{task['id']} {asset['name']}: candidate {n}"),
                        "buttons": [(f"Pick {n}", {"op": "pick", "project": project,
                                                   "task": task["id"], "asset": asset["name"],
                                                   "n": n})]})
        return replies or [{"text": "No images wait for a pick."}]

    def pick(self, action) -> list:
        return [{"text": esc(self.ms("pick", action["project"], action["task"],
                                     action["asset"], action["n"])[1])}]

    def lessons_menu(self, _arg=None) -> list:
        from core.lessons import LessonStore

        project = self.project()
        pending = LessonStore(self.master().project_state(project).project_path).pending()
        if not pending:
            return [{"text": "No lessons wait for review."}]
        return [{"text": esc(f"{r['id']} [{r['type']}] {r['text']}\n(from {r.get('task_id')})"),
                 "buttons": [("Approve", {"op": "lesson", "project": project, "id": r["id"],
                                          "decision": "approve"}),
                             ("Reject", {"op": "lesson", "project": project, "id": r["id"],
                                         "decision": "reject"})]} for r in pending]

    def lesson(self, action) -> list:
        return [{"text": esc(self.ms("lessons", action["project"], f"--{action['decision']}",
                                     action["id"])[1].splitlines()[0])}]

    def limit_menu(self, _arg=None) -> list:
        project = self.project()
        code, text = self.ms("limit", project)
        return [{"text": esc(text), "buttons": limit_buttons(project)}]

    def limit(self, action) -> list:
        return [{"text": esc(self.ms("limit", action["project"], action["choice"])[1])}]


def limit_buttons(project: str) -> list:
    return [(label, {"op": "limit", "project": project, "choice": choice})
            for label, choice in (("Wait", "wait"), ("Free", "free"), ("Paid", "paid"))]


def deepseek_balance() -> Optional[str]:
    """DeepSeek's read-only balance, when its endpoint answers; None otherwise."""
    import os

    try:
        from core.deepseek_provider import DeepSeekProvider

        if not os.environ.get("DEEPSEEK_API_KEY"):
            return None
        return DeepSeekProvider().balance()
    except Exception:  # the balance is a nicety; never fail /spend for it
        return None


class TelegramBot:
    """Long-polls Telegram and answers the paired owner."""

    def __init__(self, api, state, ops: BotOps, log: Callable[[str], None] = print,
                 sleep=time.sleep):
        self.api, self.state, self.ops = api, state, ops
        self._log = log
        self._sleep = sleep
        self._commands = {
            "status": ops.status, "summary": ops.summary, "report": ops.report,
            "spend": ops.spend, "backlog": ops.backlog, "project": ops.set_project,
            "show": ops.show, "check": ops.check, "discard": ops.discard,
            "pick": ops.pick_menu, "lessons": ops.lessons_menu, "limit": ops.limit_menu,
            "pause": ops.pause, "resume": ops.resume, "stop": ops.stop, "doctor": ops.doctor,
        }
        self._actions = {"pick": ops.pick, "lesson": ops.lesson, "limit": ops.limit,
                         "approve": lambda a: ops.approve(), "release": lambda a: ops.release()}

    # --- the loop ---

    def run(self, stop: threading.Event) -> None:
        delay = 5
        while not stop.is_set():
            try:
                self.poll_once()
                delay = 5
            except TelegramError as error:
                self._log(f"telegram: {error}")
                self._sleep(delay)
                delay = min(delay * 2, 300)
            except Exception as error:  # the bot must never take the service down
                self._log(f"telegram: unexpected {type(error).__name__}: {error}")
                self._sleep(delay)
                delay = min(delay * 2, 300)

    def poll_once(self, timeout: int = 30) -> None:
        offset = self.state.load().get("offset")
        for update in self.api.get_updates(offset, timeout=timeout):
            self.state.update(offset=update["update_id"] + 1)
            try:
                self.handle(update)
            except Exception as error:
                self._log(f"telegram: update failed: {type(error).__name__}: {error}")
                owner = self.state.owner()
                if owner:
                    self.api.send_message(owner[1], esc(f"That failed: {error}"))

    # --- updates ---

    def handle(self, update: dict) -> None:
        if "callback_query" in update:
            return self._callback(update["callback_query"])
        message = update.get("message") or {}
        user = (message.get("from") or {}).get("id")
        chat = (message.get("chat") or {}).get("id")
        text = (message.get("text") or "").strip()
        owner = self.state.owner()
        if owner is None or user != owner[0]:
            if text.startswith("/pair ") and self.state.try_pair(text[6:], user, chat):
                self.ops.record("telegram_paired", user_id=user)
                self.api.send_message(chat, "Paired. This chat now controls the Master "
                                            "System.", keyboard=MENU)
            return  # everyone else: no answer at all
        if message.get("document"):
            return self._document(chat, message["document"], message.get("caption") or "")
        if not text:
            return
        if text.startswith("/"):
            command, _, arg = text[1:].partition(" ")
            command = command.split("@", 1)[0].lower()
            return self._command(chat, command, arg.strip() or None)
        self.api.typing(chat)
        self.send(chat, self.ops.plan(text))

    def _command(self, chat, command, arg) -> None:
        if command in ("start", "menu", "help"):
            lines = [f"/{name} — {esc(text)}" for name, text in COMMANDS.items()]
            self.api.send_message(chat, "Commands:\n" + "\n".join(lines), keyboard=MENU)
            return
        if command in ("approve", "release"):
            project = self.ops.project()
            what = ("Approve the planner's draft and queue its tasks?" if command == "approve"
                    else "Open the release pull request (develop → main)?")
            self.send(chat, [{"text": esc(f"{project}: {what}"), "buttons": [
                ("Confirm", {"op": command, "confirmed": True}),
                ("Cancel", {"op": "cancel"})]}])
            return
        handler = self._commands.get(command)
        if handler is None:
            self.api.send_message(chat, "Unknown command. /menu lists them.")
            return
        self.api.typing(chat)
        self.send(chat, handler(arg))

    def _callback(self, query: dict) -> None:
        owner = self.state.owner()
        if owner is None or (query.get("from") or {}).get("id") != owner[0]:
            return
        chat = owner[1]
        data = str(query.get("data") or "")
        action = self.state.take_action(data[2:]) if data.startswith("a:") else None
        if action is None:
            self.api.answer_callback(query["id"], "This button has expired.")
            return
        self.api.answer_callback(query["id"])
        op = action.get("op")
        if op == "cancel":
            self.api.send_message(chat, "Cancelled.")
            return
        needs_confirm = op in ("approve", "release") or (op == "limit")
        if needs_confirm and not action.get("confirmed"):
            label = {"wait": "Keep waiting for the reset", "free": "Switch to the next free worker",
                     "paid": "Use the paid worker (counts toward the daily cap)"}.get(
                action.get("choice"), op)
            self.send(chat, [{"text": esc(f"{action.get('project', '')}: {label}?"),
                              "buttons": [("Confirm", {**action, "confirmed": True}),
                                          ("Cancel", {"op": "cancel"})]}])
            return
        handler = self._actions.get(op)
        if handler is None:
            self.api.send_message(chat, "This button is not supported any more.")
            return
        self.api.typing(chat)
        self.send(chat, handler(action))

    def _document(self, chat, document: dict, caption: str) -> None:
        name = str(document.get("file_name") or "")
        if not name.lower().endswith((".md", ".txt")):
            self.api.send_message(chat, "Send a .md or .txt file for the planner.")
            return
        content = self.api.get_file_content(document["file_id"]).decode("utf-8", "replace")
        self.api.typing(chat)
        self.send(chat, self.ops.plan((caption + "\n\n" + content).strip()))

    # --- replies ---

    def send(self, chat, replies) -> None:
        for reply in replies:
            buttons = self._buttons(reply.get("buttons"))
            if "photo" in reply:
                self.api.send_photo(chat, Path(reply["photo"]), reply.get("caption", ""),
                                    buttons=buttons)
            elif "document" in reply:
                self.api.send_document(chat, reply["name"], reply["document"],
                                       reply.get("caption", ""))
            else:
                self.api.send_message(chat, reply["text"], buttons=buttons,
                                      preview=reply.get("preview", False))

    def _buttons(self, buttons) -> Optional[list]:
        if not buttons:
            return None
        return [[{"text": label, "callback_data": "a:" + self.state.add_action(action)}
                 for label, action in buttons]]
