"""Telegram as the owner's phone interface: the Bot API client, state and notifier.

Transport: the Telegram Bot API over HTTPS with long polling (``getUpdates``), from
inside the service. There is no webhook and no open port. The token comes from
``TELEGRAM_BOT_TOKEN`` in master.env; it is part of every request URL, so it is
never logged: every error leaving this module has the token removed.

Security: the bot serves exactly one Telegram user, paired once with a one-time
code (``ms telegram pair``). Messages from anyone else are ignored without an
answer. The bot only offers fixed actions (core.telegram_bot); nothing the owner
types is ever run as a command.

State (``<state dir>/telegram.json``, mode 600): the paired user and chat, the
pairing code's hash and expiry, the update offset, the active project, and
pending actions behind buttons (short random ids, so callback data never carries
an action the bot did not create itself).
"""

from __future__ import annotations

import hashlib
import html
import json
import mimetypes
import os
import secrets
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Optional

__all__ = ["TelegramAPI", "TelegramError", "TelegramState", "TelegramNotifier",
           "FallbackNotifier", "split_text", "esc", "MAX_TEXT"]

API = "https://api.telegram.org"
MAX_TEXT = 4096
MAX_CAPTION = 1024
PAIRING_MINUTES = 15
ACTION_HOURS = 48


class TelegramError(RuntimeError):
    pass


def esc(text) -> str:
    """Text for parse_mode=HTML."""
    return html.escape(str(text), quote=False)


def split_text(text: str, limit: int = MAX_TEXT) -> list:
    """Pieces of at most ``limit`` characters, cut at paragraphs, then lines, then spaces."""
    text = str(text)
    pieces = []
    while len(text) > limit:
        cut = -1
        for separator in ("\n\n", "\n", " "):
            cut = text.rfind(separator, 0, limit)
            if cut > limit // 3:
                break
        if cut <= 0:
            cut = limit
        pieces.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    if text.strip() or not pieces:
        pieces.append(text)
    return pieces


class TelegramAPI:
    """The few Bot API methods the system uses. ``opener`` is injectable for tests."""

    def __init__(self, token: str, opener: Optional[Callable] = None, timeout: float = 30):
        if not token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
        self._token = token
        self._open = opener or urllib.request.urlopen
        self._timeout = timeout

    def _clean(self, text) -> str:
        return str(text).replace(self._token, "<token>")

    def call(self, method: str, params: Optional[dict] = None, files: Optional[dict] = None,
             timeout: Optional[float] = None):
        url = f"{API}/bot{self._token}/{method}"
        if files:
            boundary = uuid.uuid4().hex
            body = _multipart(boundary, params or {}, files)
            headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        else:
            body = json.dumps(params or {}).encode()
            headers = {"Content-Type": "application/json"}
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with self._open(request, timeout=timeout or self._timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read()).get("description", "")
            except Exception:
                detail = ""
            raise TelegramError(self._clean(f"{method}: HTTP {error.code} {detail}")) from None
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise TelegramError(self._clean(f"{method}: {error}")) from None
        if not payload.get("ok"):
            raise TelegramError(self._clean(f"{method}: {payload.get('description')}"))
        return payload.get("result")

    # --- methods ---

    def get_me(self) -> dict:
        return self.call("getMe")

    def get_updates(self, offset: Optional[int], timeout: int = 30) -> list:
        params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", params, timeout=timeout + 15) or []

    def send_message(self, chat_id, text: str, buttons=None, keyboard=None,
                     preview: bool = False) -> list:
        """Send text (HTML), split at the 4096-character limit; buttons go on the last part."""
        sent = []
        pieces = split_text(text)
        for index, piece in enumerate(pieces):
            params = {"chat_id": chat_id, "text": piece, "parse_mode": "HTML",
                      "link_preview_options": {"is_disabled": not preview}}
            if index == len(pieces) - 1:
                if buttons:
                    params["reply_markup"] = {"inline_keyboard": buttons}
                elif keyboard:
                    params["reply_markup"] = {"keyboard": keyboard, "resize_keyboard": True,
                                              "is_persistent": True}
            sent.append(self.call("sendMessage", params))
        return sent

    def send_photo(self, chat_id, path: Path, caption: str = "", buttons=None):
        params = {"chat_id": str(chat_id), "caption": caption[:MAX_CAPTION],
                  "parse_mode": "HTML"}
        if buttons:
            params["reply_markup"] = json.dumps({"inline_keyboard": buttons})
        return self.call("sendPhoto", params, files={"photo": Path(path)})

    def send_document(self, chat_id, name: str, content: bytes, caption: str = ""):
        params = {"chat_id": str(chat_id), "caption": caption[:MAX_CAPTION],
                  "parse_mode": "HTML"}
        return self.call("sendDocument", params, files={"document": (name, content)})

    def get_file_content(self, file_id: str, max_bytes: int = 200_000) -> bytes:
        info = self.call("getFile", {"file_id": file_id})
        if (info.get("file_size") or 0) > max_bytes:
            raise TelegramError("the file is too large")
        url = f"{API}/file/bot{self._token}/{info['file_path']}"
        try:
            with self._open(urllib.request.Request(url), timeout=self._timeout) as response:
                return response.read(max_bytes + 1)[:max_bytes]
        except (urllib.error.URLError, OSError) as error:
            raise TelegramError(self._clean(f"getFile: {error}")) from None

    def answer_callback(self, callback_id: str, text: str = ""):
        return self.call("answerCallbackQuery", {"callback_query_id": callback_id,
                                                 "text": text[:200]})

    def typing(self, chat_id):
        try:
            self.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except TelegramError:
            pass


def _multipart(boundary: str, fields: dict, files: dict) -> bytes:
    lines = []
    for name, value in fields.items():
        lines += [f"--{boundary}".encode(),
                  f'Content-Disposition: form-data; name="{name}"'.encode(), b"",
                  str(value).encode()]
    for name, source in files.items():
        if isinstance(source, tuple):
            filename, content = source
        else:
            filename, content = Path(source).name, Path(source).read_bytes()
        kind = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        lines += [f"--{boundary}".encode(),
                  f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'
                  .encode(), f"Content-Type: {kind}".encode(), b"", content]
    lines += [f"--{boundary}--".encode(), b""]
    return b"\r\n".join(lines)


class TelegramState:
    """The bot's private state file. Every method re-reads it: the CLI and the bot share it."""

    def __init__(self, state_dir: Path, now=time.time):
        self.path = Path(state_dir) / "telegram.json"
        self._now = now

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def update(self, **changes) -> dict:
        data = self.load()
        data.update(changes)
        self.save(data)
        return data

    # --- pairing ---

    @staticmethod
    def _hash(code: str) -> str:
        return hashlib.sha256(code.strip().upper().encode()).hexdigest()

    def new_pairing_code(self) -> str:
        code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
        self.update(pairing={"hash": self._hash(code),
                             "expires": self._now() + PAIRING_MINUTES * 60})
        return code

    def try_pair(self, code: str, user_id: int, chat_id: int) -> bool:
        data = self.load()
        pairing = data.get("pairing") or {}
        if not pairing or pairing.get("expires", 0) < self._now():
            return False
        if not secrets.compare_digest(pairing["hash"], self._hash(code)):
            return False
        data.pop("pairing", None)
        data.update(user_id=user_id, chat_id=chat_id, paired_at=self._now())
        self.save(data)
        return True

    def owner(self) -> Optional[tuple]:
        data = self.load()
        if data.get("user_id") and data.get("chat_id"):
            return data["user_id"], data["chat_id"]
        return None

    def unpair(self) -> None:
        data = self.load()
        for key in ("user_id", "chat_id", "paired_at", "pairing", "actions"):
            data.pop(key, None)
        self.save(data)

    # --- pending button actions ---

    def add_action(self, action: dict) -> str:
        """Store a fixed action behind a button; returns its short id."""
        data = self.load()
        actions = {k: v for k, v in (data.get("actions") or {}).items()
                   if v.get("expires", 0) > self._now()}
        action_id = secrets.token_hex(6)
        actions[action_id] = {**action, "expires": self._now() + ACTION_HOURS * 3600}
        data["actions"] = actions
        self.save(data)
        return action_id

    def take_action(self, action_id: str) -> Optional[dict]:
        """The action behind a button, used once."""
        data = self.load()
        actions = data.get("actions") or {}
        action = actions.pop(action_id, None)
        data["actions"] = actions
        self.save(data)
        if action is None or action.get("expires", 0) < self._now():
            return None
        return action


class TelegramNotifier:
    """Notifier with ntfy's interface, sending to the paired owner's chat."""

    def __init__(self, api: Optional[TelegramAPI], state: TelegramState):
        self.api = api
        self.state = state
        self.sent: list = []

    @property
    def enabled(self) -> bool:
        return self.api is not None and self.state.owner() is not None

    def buttons(self, actions) -> Optional[list]:
        """Inline buttons for [(label, action dict), ...]."""
        if not actions:
            return None
        return [[{"text": label, "callback_data": "a:" + self.state.add_action(action)}
                 for label, action in actions]]

    def send(self, title: str, message: str, *, tags: str = "", click: Optional[str] = None,
             priority: str = "default", actions=None, photos=None) -> bool:
        self.sent.append({"title": title, "message": message, "click": click,
                          "actions": actions, "photos": photos})
        if not self.enabled:
            return False
        _, chat_id = self.state.owner()
        text = f"<b>{esc(title)}</b>\n{esc(message)}"
        if click:
            text += f"\n{esc(click)}"
        try:
            self.api.send_message(chat_id, text, buttons=self.buttons(actions))
            for path in (photos or [])[:10]:
                self.api.send_photo(chat_id, Path(path), caption=esc(Path(path).stem))
            return True
        except (TelegramError, OSError):
            return False


class FallbackNotifier:
    """Telegram first; ntfy when Telegram is not paired or a send fails."""

    def __init__(self, primary, fallback):
        self.primary, self.fallback = primary, fallback

    @property
    def sent(self) -> list:
        return self.primary.sent

    @property
    def enabled(self) -> bool:
        return self.primary.enabled or self.fallback.enabled

    def send(self, title, message, **kwargs) -> bool:
        if self.primary.send(title, message, **kwargs):
            return True
        plain = {k: v for k, v in kwargs.items() if k in ("tags", "click", "priority")}
        return self.fallback.send(title, message, **plain)
