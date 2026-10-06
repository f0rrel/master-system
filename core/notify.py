"""Phone notifications through ntfy (https://ntfy.sh), best effort.

A notification is an HTTP POST of a short text to ``<server>/<topic>``. The
topic is a long random string kept in ``~/.config/master-system/ntfy-topic``
(mode 600): anyone who knows it can read the messages, so messages carry no
secrets and no code, only a short summary and a link. Sending never raises:
a phone being offline must not stop the system.
"""

from __future__ import annotations

import secrets
import urllib.request
from pathlib import Path
from typing import Callable, Optional

__all__ = ["Notifier", "default_topic_path", "new_topic"]


def default_topic_path() -> Path:
    from core.run_config import default_config_path

    return default_config_path().parent / "ntfy-topic"


def new_topic() -> str:
    return "ms-" + secrets.token_urlsafe(18).replace("_", "x").replace("-", "y")


class Notifier:
    """Sends to one ntfy topic. Without a topic, every send is a no-op."""

    def __init__(self, server: str = "https://ntfy.sh", topic: Optional[str] = None,
                 opener: Optional[Callable] = None, timeout: float = 10):
        self.server = server.rstrip("/")
        self.topic = topic
        self._open = opener or urllib.request.urlopen
        self._timeout = timeout
        self.sent: list = []

    @classmethod
    def from_file(cls, server: str, path: Optional[Path] = None, **kwargs) -> "Notifier":
        path = Path(path) if path is not None else default_topic_path()
        try:
            topic = path.read_text().strip() or None
        except OSError:
            topic = None
        return cls(server, topic, **kwargs)

    @property
    def enabled(self) -> bool:
        return bool(self.topic)

    def send(self, title: str, message: str, *, tags: str = "", click: Optional[str] = None,
             priority: str = "default", **_ignored) -> bool:
        """``actions`` and ``photos`` (Telegram only) are ignored here."""
        self.sent.append({"title": title, "message": message, "tags": tags, "click": click,
                          "priority": priority})
        if not self.enabled:
            return False
        headers = {"Title": title.encode("ascii", "replace").decode(), "Priority": priority}
        if tags:
            headers["Tags"] = tags
        if click:
            headers["Click"] = click
        request = urllib.request.Request(f"{self.server}/{self.topic}",
                                         data=message.encode("utf-8"), headers=headers,
                                         method="POST")
        try:
            with self._open(request, timeout=self._timeout) as response:
                return 200 <= getattr(response, "status", 200) < 300
        except Exception:
            return False
