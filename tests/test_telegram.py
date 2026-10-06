"""The Telegram bot, against a fake Telegram API: pairing, commands, buttons, security."""

import io
import json
import urllib.error

import pytest

from core.history import EventType
from core.notify import Notifier
from core.paths import RuntimePaths
from core.sqlite_history import SQLiteHistoryStore
from core.telegram import (FallbackNotifier, TelegramAPI, TelegramError, TelegramNotifier,
                           TelegramState, split_text)
from core.telegram_bot import BotOps, TelegramBot

OWNER, STRANGER = 111, 999


class FakeAPI:
    def __init__(self):
        self.updates, self.out = [], []

    def get_updates(self, offset, timeout=30):
        pending = [u for u in self.updates if offset is None or u["update_id"] >= offset]
        self.updates = []
        return pending

    def send_message(self, chat_id, text, buttons=None, keyboard=None, preview=False):
        self.out.append({"kind": "text", "chat": chat_id, "text": text, "buttons": buttons,
                         "keyboard": keyboard})

    def send_photo(self, chat_id, path, caption="", buttons=None):
        self.out.append({"kind": "photo", "chat": chat_id, "path": str(path),
                         "caption": caption, "buttons": buttons})

    def send_document(self, chat_id, name, content, caption=""):
        self.out.append({"kind": "document", "chat": chat_id, "name": name,
                         "content": content})

    def get_file_content(self, file_id, max_bytes=200_000):
        return b"# Request\nAdd a goodbye screen."

    def answer_callback(self, callback_id, text=""):
        self.out.append({"kind": "answer", "text": text})

    def typing(self, chat_id):
        pass


class FakeOps(BotOps):
    def __init__(self, state):
        self.calls = []

        def ms_main(argv, out):
            self.calls.append(argv)
            print(f"ran {' '.join(argv)}", file=out)
            return 0

        super().__init__(state, ms_main=ms_main)

    def project(self, name=None):
        return name or "app"

    def plan(self, text):
        self.calls.append(["plan", text])
        return [{"text": f"planned: {text[:40]}"}]

    def approve(self, _arg=None):
        self.calls.append(["approve"])
        return [{"text": "approved"}]


@pytest.fixture
def bot(tmp_path):
    state = TelegramState(tmp_path)
    api = FakeAPI()
    ops = FakeOps(state)
    return TelegramBot(api, state, ops, log=lambda line: None), api, state, ops


def message(user, text=None, update_id=1, **extra):
    msg = {"from": {"id": user}, "chat": {"id": user + 1000}, **extra}
    if text is not None:
        msg["text"] = text
    return {"update_id": update_id, "message": msg}


def press(user, data, update_id=50):
    return {"update_id": update_id, "callback_query": {"id": "q", "from": {"id": user},
                                                       "data": data}}


def paired(bot):
    b, api, state, ops = bot
    code = state.new_pairing_code()
    b.handle(message(OWNER, f"/pair {code}"))
    api.out.clear()
    return b, api, state, ops


# --- pairing and strangers ---


def test_pairing_works_once_and_strangers_get_no_answer(bot):
    b, api, state, ops = bot
    b.handle(message(STRANGER, "/status"))
    b.handle(message(STRANGER, "/pair WRONGCODE"))
    assert api.out == [] and ops.calls == []
    code = state.new_pairing_code()
    b.handle(message(OWNER, f"/pair {code.lower()}"))
    assert state.owner() == (OWNER, OWNER + 1000)
    assert "Paired" in api.out[-1]["text"] and api.out[-1]["keyboard"]
    api.out.clear()
    b.handle(message(STRANGER, f"/pair {code}"))  # used up
    b.handle(message(STRANGER, "hello"))
    assert api.out == [] and state.owner()[0] == OWNER
    [event] = SQLiteHistoryStore(RuntimePaths.default().history_path).events(
        types=[EventType.HUMAN_ACTION])
    assert event.payload == {"actor": "telegram", "action": "telegram_paired",
                             "user_id": OWNER}


def test_an_expired_code_does_not_pair(tmp_path):
    clock = [1000.0]
    state = TelegramState(tmp_path, now=lambda: clock[0])
    code = state.new_pairing_code()
    clock[0] += 16 * 60
    assert not state.try_pair(code, OWNER, 1)
    assert (tmp_path / "telegram.json").stat().st_mode & 0o777 == 0o600


# --- commands ---


def test_commands_run_fixed_operations_with_the_telegram_actor(bot):
    b, api, state, ops = paired(bot)
    b.handle(message(OWNER, "/status"))
    assert ops.calls[-1] == ["--actor", "telegram", "status"]
    assert api.out[-1]["text"].startswith("<pre>ran ")
    b.handle(message(OWNER, "/backlog@Oogway_system_bot other"))
    assert ops.calls[-1][-2:] == ["backlog", "other"]
    b.handle(message(OWNER, "/pause"))
    assert ops.calls[-1][-1] == "pause"
    events = SQLiteHistoryStore(RuntimePaths.default().history_path).events(
        types=[EventType.HUMAN_ACTION])
    assert events[-1].payload == {"actor": "telegram", "action": "pause"}
    b.handle(message(OWNER, "/rm -rf /"))
    assert "Unknown command" in api.out[-1]["text"]
    b.handle(message(OWNER, "/menu"))
    assert api.out[-1]["keyboard"][0] == ["/status", "/summary", "/spend"]


def test_plain_text_and_files_go_to_the_planner(bot):
    b, api, state, ops = paired(bot)
    b.handle(message(OWNER, "Add a goodbye screen; also `rm -rf ~`"))
    assert ops.calls[-1] == ["plan", "Add a goodbye screen; also `rm -rf ~`"]
    assert api.out[-1]["text"].startswith("planned:")
    b.handle(message(OWNER, caption="See file", document={"file_id": "f",
                                                         "file_name": "request.md"}))
    assert ops.calls[-1][1].startswith("See file\n\n# Request")
    b.handle(message(OWNER, document={"file_id": "f", "file_name": "evil.sh"}))
    assert "Send a .md or .txt" in api.out[-1]["text"]


def test_approve_needs_a_confirmation_and_buttons_work_once(bot):
    b, api, state, ops = paired(bot)
    b.handle(message(OWNER, "/approve"))
    prompt = api.out[-1]
    assert "Approve the planner's draft" in prompt["text"]
    confirm, cancel = prompt["buttons"][0]
    assert ["approve"] not in ops.calls
    b.handle(press(STRANGER, confirm["callback_data"]))
    assert ["approve"] not in ops.calls
    b.handle(press(OWNER, confirm["callback_data"]))
    assert ops.calls[-1] == ["approve"] and api.out[-1]["text"] == "approved"
    b.handle(press(OWNER, confirm["callback_data"]))
    assert api.out[-1] == {"kind": "answer", "text": "This button has expired."}
    b.handle(press(OWNER, "a:forged"))
    assert ops.calls.count(["approve"]) == 1


def test_limit_choices_ask_to_confirm(bot):
    b, api, state, ops = paired(bot)
    from core.telegram_bot import limit_buttons

    ids = b._buttons(limit_buttons("app"))[0]
    assert [i["text"] for i in ids] == ["Wait", "Free", "Paid"]
    b.handle(press(OWNER, ids[2]["callback_data"]))
    prompt = api.out[-1]
    assert ("Use the paid worker after a test request (both count toward the daily cap)?"
            in prompt["text"])
    assert not any("limit" in c for c in ops.calls)
    b.handle(press(OWNER, prompt["buttons"][0][0]["callback_data"]))
    assert ops.calls[-1] == ["--actor", "telegram", "limit", "app", "paid"]


def test_cancel(bot):
    b, api, state, ops = paired(bot)
    b.handle(message(OWNER, "/release"))
    cancel = api.out[-1]["buttons"][0][1]
    b.handle(press(OWNER, cancel["callback_data"]))
    assert api.out[-1]["text"] == "Cancelled." and not any("release" in str(c)
                                                            for c in ops.calls)


def test_lessons_and_picks_offer_buttons(bot, tmp_path):
    b, api, state, ops = paired(bot)
    b.send(OWNER, [{"photo": str(tmp_path / "c.png"), "caption": "candidate 1",
                    "buttons": [("Pick 1", {"op": "pick", "project": "app", "task": "t-1",
                                            "asset": "blob", "n": 1})]}])
    button = api.out[-1]["buttons"][0][0]
    b.handle(press(OWNER, button["callback_data"]))
    assert ops.calls[-1] == ["--actor", "telegram", "pick", "app", "t-1", "blob", "1"]
    b.send(OWNER, [{"text": "L-1", "buttons": [("Approve", {"op": "lesson", "project": "app",
                                                            "id": "L-1",
                                                            "decision": "approve"})]}])
    b.handle(press(OWNER, api.out[-1]["buttons"][0][0]["callback_data"], update_id=51))
    assert ops.calls[-1] == ["--actor", "telegram", "lessons", "app", "--approve", "L-1"]


def test_long_text_becomes_a_document(bot):
    b, api, state, ops = paired(bot)
    replies = ops.long_text("x" * 5000, "report.txt", "Report")
    b.send(OWNER, replies)
    assert api.out[-1]["kind"] == "document" and api.out[-1]["name"] == "report.txt"


def test_polling_advances_the_offset(bot):
    b, api, state, ops = paired(bot)
    api.updates = [message(OWNER, "/status", update_id=7), message(OWNER, "/status",
                                                                    update_id=8)]
    b.poll_once()
    assert state.load()["offset"] == 9 and len(ops.calls) == 2


def test_spend_shows_caps_and_balance(bot, monkeypatch):
    b, api, state, ops = paired(bot)
    monkeypatch.setattr("core.telegram_bot.deepseek_balance", lambda: "1.47 USD")
    b.handle(message(OWNER, "/spend"))
    text = api.out[-1]["text"]
    assert "Spent today: $0.000 of $0.50" in text and "DeepSeek balance: 1.47 USD" in text
    assert "top up" in text  # information only: there is no payment action


# --- the API client and the notifier ---


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *args):
        return self.body


def test_the_token_never_appears_in_errors():
    token = "123456:SECRET-token"

    def refuse(request, timeout):
        raise urllib.error.URLError(f"cannot reach {request.full_url}")

    with pytest.raises(TelegramError) as error:
        TelegramAPI(token, opener=refuse).get_me()
    assert "SECRET" not in str(error.value) and "<token>" in str(error.value)

    def http_error(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 409, "Conflict", {},
                                     io.BytesIO(b'{"description": "Conflict: terminated"}'))

    with pytest.raises(TelegramError, match="HTTP 409 Conflict"):
        TelegramAPI(token, opener=http_error).get_updates(None)


def test_messages_are_split_and_photos_are_multipart(tmp_path):
    seen = []

    def opener(request, timeout):
        seen.append(request)
        return Response(json.dumps({"ok": True, "result": {}}).encode())

    api = TelegramAPI("t", opener=opener)
    api.send_message(1, "a" * 5000, buttons=[[{"text": "x", "callback_data": "a:1"}]])
    assert len(seen) == 2
    first, last = (json.loads(r.data) for r in seen)
    assert len(first["text"]) <= 4096 and "reply_markup" not in first
    assert last["reply_markup"]["inline_keyboard"][0][0]["text"] == "x"
    photo = tmp_path / "shot.png"
    photo.write_bytes(b"\x89PNG")
    api.send_photo(1, photo, "caption")
    assert b'filename="shot.png"' in seen[-1].data and b"\x89PNG" in seen[-1].data


def test_split_text_prefers_paragraphs():
    pieces = split_text("a" * 3000 + "\n\n" + "b" * 3000)
    assert pieces == ["a" * 3000, "b" * 3000]
    assert all(len(p) <= 4096 for p in split_text("word " * 3000))


def test_notifications_go_to_telegram_with_buttons_or_fall_back_to_ntfy(tmp_path):
    state = TelegramState(tmp_path)
    api = FakeAPI()
    telegram = TelegramNotifier(api, state)
    ntfy = Notifier(topic=None)
    notifier = FallbackNotifier(telegram, ntfy)
    assert notifier.send("Morning summary", "1 done", click="https://p/") is False
    assert ntfy.sent[-1]["title"] == "Morning summary"  # not paired: ntfy
    state.update(user_id=OWNER, chat_id=5)
    shot = tmp_path / "home.png"
    shot.write_bytes(b"png")
    from core.telegram_bot import limit_buttons

    assert notifier.send("app: needs you", "Big Pickle limited", actions=limit_buttons("app"),
                         photos=[shot])
    text, photo = api.out
    assert text["chat"] == 5 and "<b>app: needs you</b>" in text["text"]
    assert [b["text"] for b in text["buttons"][0]] == ["Wait", "Free", "Paid"]
    assert photo["kind"] == "photo" and photo["path"] == str(shot)


def test_the_real_operations_answer_from_the_real_commands(tmp_path):
    state = TelegramState(tmp_path)
    ops = BotOps(state)
    assert "Projects: sample-project" in ops.set_project("nope")[0]["text"]
    assert "Active project: <b>sample-project</b>" in ops.set_project("sample-project")[0]["text"]
    assert ops.project() == "sample-project"
    assert "Backlog of sample-project" in ops.backlog()[0]["text"]
    assert ops.status()[0]["text"].startswith("<pre>")
    assert "No images wait" in ops.pick_menu()[0]["text"]
    assert "No lessons wait" in ops.lessons_menu()[0]["text"]
    assert "No summary yet" in ops.summary()[0]["text"]
    assert "No draft yet" in ops.show()[0]["text"]
