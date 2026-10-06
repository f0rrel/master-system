"""Where runtime state lives: outside every repository and every workspace.

Runtime state is the history database and the session files. It lives in the
state directory, ``$XDG_DATA_HOME/master-system/`` (``~/.local/share/master-system/``
when XDG_DATA_HOME is unset or not absolute). Attempt worktrees live in a
sibling, ``master-system-worktrees/``, so a worktree is never inside the state
directory. Both are configurable; these are only the defaults.

Project definitions (``project.yaml``, ``milestones.yaml``, ``tasks.yaml`` per
project) are private configuration, kept outside this repository: by default in
``$XDG_CONFIG_HOME/master-system/projects/`` (``~/.config/master-system/projects/``),
or wherever ``[run] projects_root`` points.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

__all__ = ["RuntimePaths", "STATE_DIR_NAME", "WORKTREES_DIR_NAME", "default_projects_root"]

STATE_DIR_NAME = "master-system"
WORKTREES_DIR_NAME = "master-system-worktrees"


def _data_home(env: Mapping[str, str]) -> Path:
    configured = env.get("XDG_DATA_HOME")
    # The XDG spec says a relative value is invalid and must be ignored.
    if configured and Path(configured).is_absolute():
        return Path(configured)
    return Path.home() / ".local" / "share"


def _config_home(env: Mapping[str, str]) -> Path:
    configured = env.get("XDG_CONFIG_HOME")
    if configured and Path(configured).is_absolute():
        return Path(configured)
    return Path.home() / ".config"


def default_projects_root(env: Optional[Mapping[str, str]] = None) -> Path:
    """Where project definitions live when no root is configured."""
    return _config_home(os.environ if env is None else env) / STATE_DIR_NAME / "projects"


@dataclass(frozen=True)
class RuntimePaths:
    """The state directory and the worktrees root."""

    state_dir: Path
    worktrees_root: Path

    @classmethod
    def default(cls, env: Optional[Mapping[str, str]] = None) -> "RuntimePaths":
        data_home = _data_home(os.environ if env is None else env)
        return cls(
            state_dir=data_home / STATE_DIR_NAME,
            worktrees_root=data_home / WORKTREES_DIR_NAME,
        )

    @property
    def history_path(self) -> Path:
        return self.state_dir / "history.sqlite"

    @property
    def sessions_dir(self) -> Path:
        return self.state_dir / "sessions"
