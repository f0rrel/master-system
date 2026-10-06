"""The environment every worker and verification process gets: an allowlist.

Workers run as the owner's user (docs/ARCHITECTURE.md, gap N1), so the least
the orchestrator can do is not hand them secrets. A worker or verification
process never inherits the control plane's environment. It gets exactly:

* ``PATH``: the toolchain directories given (for example a Node 22 ``bin``
  directory), then ``/usr/local/bin:/usr/bin:/bin``. It is built here, so it never
  depends on shell startup files (``.bashrc``, nvm's shell function).
* ``HOME`` = the **worker home**, a directory of its own, with
  ``XDG_CONFIG_HOME``, ``XDG_DATA_HOME`` and ``XDG_CACHE_HOME`` inside it. A worker's
  own tool configuration and credentials (for example the worker's OpenCode
  key) live there, and nowhere else.
* ``LANG``, ``LC_ALL``, ``TERM`` and ``TZ``, copied if set.
* ``CI=1``, and ``npm_config_cache`` inside the worker home.
* any explicitly configured extras, for example ``PLAYWRIGHT_BROWSERS_PATH``.

Nothing else: in particular no API keys, tokens, ``SSH_AUTH_SOCK`` or askpass
helpers. A configured extra whose name looks like a secret is refused.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, Mapping, Optional

__all__ = [
    "SYSTEM_PATH",
    "find_node_bin",
    "worker_environment",
    "default_worker_home",
]

SYSTEM_PATH = ("/usr/local/bin", "/usr/bin", "/bin")
_COPIED = ("LANG", "LC_ALL", "TERM", "TZ")
_SECRET_LIKE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASS|AUTH|CREDENTIAL|SSH_)", re.I)
_VERSION_DIR = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def default_worker_home(env: Optional[Mapping[str, str]] = None) -> Path:
    from core.paths import RuntimePaths

    return RuntimePaths.default(env).state_dir.parent / "master-system-worker"


def find_node_bin(min_major: int = 22, env: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """The ``bin`` directory of the newest installed Node >= min_major, or None.

    Looks in nvm's install directory (``$NVM_DIR`` or ``~/.nvm``) by its
    ``versions/node/vX.Y.Z`` layout. It reads directory names only: no shell, no
    ``nvm`` function, no process is started.
    """
    env = os.environ if env is None else env
    nvm_dir = Path(env.get("NVM_DIR") or Path.home() / ".nvm")
    versions = nvm_dir / "versions" / "node"
    found = []
    if versions.is_dir():
        for entry in versions.iterdir():
            match = _VERSION_DIR.match(entry.name)
            if match and (entry / "bin" / "node").is_file():
                version = tuple(int(part) for part in match.groups())
                if version[0] >= min_major:
                    found.append((version, entry / "bin"))
    return max(found)[1] if found else None


def worker_environment(
    worker_home,
    *,
    path_dirs: Iterable = (),
    extra: Optional[Mapping[str, str]] = None,
    base_env: Optional[Mapping[str, str]] = None,
) -> dict:
    """Build the allowlisted environment for worker and verification processes."""
    base_env = os.environ if base_env is None else base_env
    home = Path(worker_home)
    env = {
        "PATH": os.pathsep.join([*(str(Path(d)) for d in path_dirs), *SYSTEM_PATH]),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "npm_config_cache": str(home / ".npm"),
        "CI": "1",
    }
    for name in _COPIED:
        if name in base_env:
            env[name] = base_env[name]
    for name, value in (extra or {}).items():
        if _SECRET_LIKE.search(name):
            raise ValueError(f"refusing to pass {name!r} to workers: it looks like a secret")
        env[name] = str(value)
    return env
