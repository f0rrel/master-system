"""What a worker could reach on this machine, reported (never fixed) by ``ms doctor``.

Workers run as the owner's own user (docs/ARCHITECTURE.md, gap N1). Everything that user
can read or run, a worker can too: ``core.worker_env`` only controls what the
orchestrator *hands* it. This module lists the facts that matter, read-only, so the
owner can judge them without re-deriving them:

* file and group facts come from the file system and the user database;
* nothing is opened, read or executed: a secret file is reported by its name and
  permissions, never its contents; no ``sudo``, no ``docker`` command is run.

The remedy for each finding is host configuration (a separate user, VM or container),
described in docs/SECURITY-RELIABILITY-HARDENING.md. Nothing here changes the host.
"""

from __future__ import annotations

import grp
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

__all__ = ["Exposure", "worker_exposure", "render_exposure"]

#: Browser profile directories (cookies, saved logins), relative to the home directory.
_BROWSER_PROFILES = (".mozilla", ".config/google-chrome", ".config/chromium",
                     "snap/firefox", "snap/chromium", ".config/BraveSoftware")


@dataclass(frozen=True)
class Exposure:
    #: critical: a worker can become root or act as the owner. high: it can read credentials.
    severity: str
    what: str


def _private_keys(ssh_dir: Path) -> int:
    count = 0
    try:
        entries = list(ssh_dir.iterdir())
    except OSError:
        return 0
    for entry in entries:
        name = entry.name
        if name.startswith("id_") and not name.endswith(".pub") and entry.is_file():
            count += 1
    return count


def _mode(path: Path) -> Optional[str]:
    try:
        return oct(stat.S_IMODE(path.stat().st_mode))[2:]
    except OSError:
        return None


def worker_exposure(*, home: Optional[Path] = None, group_names: Optional[Iterable[str]] = None,
                    docker_socket: str = "/var/run/docker.sock",
                    config_dir: Optional[Path] = None) -> list:
    """The things a worker (the owner's user) can reach, most serious first."""
    home = Path(home) if home is not None else Path.home()
    config_dir = Path(config_dir) if config_dir is not None else home / ".config" / "master-system"
    if group_names is None:
        group_names = [grp.getgrgid(g).gr_name for g in os.getgroups() if _group_exists(g)]
    groups = set(group_names)
    found = []

    if "docker" in groups or os.access(docker_socket, os.W_OK):
        found.append(Exposure(
            "critical", "the Docker socket is usable by this user: a worker can start a "
            "privileged container and become root on this machine"))
    for admin in sorted(groups & {"sudo", "wheel", "admin"}):
        found.append(Exposure(
            "critical", f"this user is in the '{admin}' group: if sudo needs no password, or "
            "the password is known, a worker can become root"))
    keys = _private_keys(home / ".ssh")
    if keys:
        found.append(Exposure(
            "high", f"{keys} SSH private key file(s) in ~/.ssh are readable (OpenSSH finds "
            "them through the user database, so the worker's separate HOME does not hide them)"))
    for name in ("github-app.pem", "master.env"):
        path = config_dir / name
        mode = _mode(path)
        if mode is not None:
            found.append(Exposure(
                "high", f"{path} (mode {mode}) is readable: file permissions do not stop a "
                "process running as the same user"))
    if (home / ".config" / "gh").is_dir():
        found.append(Exposure("high", "a GitHub CLI configuration exists (~/.config/gh): "
                              "a login there can act as the owner"))
    profiles = [p for p in _BROWSER_PROFILES if (home / p).is_dir()]
    if profiles:
        found.append(Exposure("high", "browser profiles (cookies, saved logins) are readable: "
                              + ", ".join(f"~/{p}" for p in profiles)))
    return found


def _group_exists(gid: int) -> bool:
    try:
        grp.getgrgid(gid)
        return True
    except KeyError:
        return False


def render_exposure(found: list) -> str:
    if not found:
        return "nothing found in the places checked (this does not make workers sandboxed)"
    lines = [f"{e.severity.upper():8} {e.what}" for e in found]
    lines.append("Workers are NOT sandboxed: they run as this user. See "
                 "docs/SECURITY-RELIABILITY-HARDENING.md for the host-level remedies.")
    return "\n".join(lines)
