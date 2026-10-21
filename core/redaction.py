"""Removing secrets from text before it is stored, shown or sent to a worker.

Two entry points, for two jobs:

* :func:`redact` is for configuration and diagnostics (``ms doctor``): it also
  blanks any ``name = value`` line whose *name* looks secret, which is right for a
  config dump and too aggressive for program output.
* :func:`redact_output` is for what workers and verification commands print. It
  removes secret-shaped strings and environment-style ``SOME_API_KEY=value``
  assignments, and leaves ordinary source lines (``if key == "a":``) readable,
  because the text is evidence for diagnosis and for the next attempt.

Both are best effort. A pattern list cannot recognise every secret; the real
protection is not handing workers secrets (``core.worker_env``).
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["SECRET_PATTERNS", "SECRET_KEYS", "redact", "redact_output", "redact_json"]

REDACTED = "<redacted>"

#: Strings that are secrets wherever they appear.
SECRET_PATTERNS = [
    r"ghs_[A-Za-z0-9]+",
    r"gh[pousr]_[A-Za-z0-9]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"sk-[A-Za-z0-9-]{16,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
]
#: Secret shapes only :func:`redact_output` removes (they are rare in config dumps).
_OUTPUT_PATTERNS = [
    r"AKIA[0-9A-Z]{16}",
    r"xox[abprs]-[A-Za-z0-9-]{10,}",
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}",
]
SECRET_KEYS = ("key", "token", "secret", "password", "topic", "pass")

#: ``https://user:password@host`` -> the password is removed.
_URL_CREDENTIALS = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
#: ``Authorization: ...`` headers, whole line.
_AUTH_HEADER = re.compile(r"(?im)^(\s*(?:proxy-)?authorization\s*:).*$")
#: ``SOME_API_KEY=value`` / ``export TOKEN: "value"`` (an uppercase, secret-named variable).
_ENV_ASSIGNMENT = re.compile(
    r"\b([A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)[A-Z0-9_]*)"
    r"(\s*[=:]\s*)(?:\"[^\"\n]+\"|'[^'\n]+'|[^\s'\"]{4,})")


def _remove_patterns(text: str, patterns, secrets) -> str:
    for value in secrets:
        if value and len(value) >= 6:
            text = text.replace(value, REDACTED)
    for pattern in patterns:
        text = re.sub(pattern, REDACTED, text)
    return text


def redact(text: str, secrets=()) -> str:
    """Remove known secret values, secret-looking strings and secret-named config values."""
    text = _remove_patterns(text, SECRET_PATTERNS, secrets)
    lines = []
    for line in text.splitlines():
        name = line.split("=", 1)[0].strip().lower() if "=" in line else ""
        if name and any(word in name for word in SECRET_KEYS) and not name.startswith("#"):
            line = line.split("=", 1)[0] + "= " + REDACTED
        lines.append(line)
    return "\n".join(lines)


def redact_output(text: Any, secrets=()) -> Any:
    """:func:`redact` for program output: secret-shaped strings and env-style assignments."""
    if not isinstance(text, str) or not text:
        return text
    text = _remove_patterns(text, [*SECRET_PATTERNS, *_OUTPUT_PATTERNS], secrets)
    text = _URL_CREDENTIALS.sub(r"\1" + REDACTED + "@", text)
    text = _AUTH_HEADER.sub(r"\1 " + REDACTED, text)
    return _ENV_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)


def redact_json(value: Any, secrets=()) -> Any:
    """A copy of a JSON-like structure with every string passed through :func:`redact_output`."""
    if isinstance(value, str):
        return redact_output(value, secrets)
    if isinstance(value, dict):
        return {key: redact_json(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_json(item, secrets) for item in value]
    return value
