"""A project's direction document: what every proposal is judged against.

Each project may keep a direction document in its own repository (by default
``docs/DIRECTION.md``): vision, audience, the feel, what never changes. The
planner, the Master, the worker and the visual reviewer read it from the base
branch, truncated to a size budget so it cannot crowd out the rest of a prompt.

project.yaml (both keys optional; these are the defaults)::

    direction:
      path: docs/DIRECTION.md
      max_chars: 6000

The text is project content, read with ``git show``; nothing here interprets it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

__all__ = ["Direction", "DEFAULT_PATH", "DEFAULT_MAX_CHARS", "direction_settings",
           "direction_text", "read_direction"]

DEFAULT_PATH = "docs/DIRECTION.md"
DEFAULT_MAX_CHARS = 6000
TRUNCATED = "\n…(direction truncated to its size budget)"


@dataclass(frozen=True)
class Direction:
    path: str
    text: str
    truncated: bool

    def block(self, title: str = "PROJECT DIRECTION") -> str:
        """The direction as a prompt section."""
        return f"{title} ({self.path}):\n{self.text}"


def direction_settings(project: dict) -> dict:
    raw = project.get("direction") if isinstance(project.get("direction"), dict) else {}
    max_chars = raw.get("max_chars", DEFAULT_MAX_CHARS)
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
        max_chars = DEFAULT_MAX_CHARS
    path = str(raw.get("path") or DEFAULT_PATH).strip().lstrip("/")
    return {"path": path, "max_chars": max_chars}


def read_direction(project: dict, git: Optional[Callable] = None,
                   max_chars: Optional[int] = None) -> Optional[Direction]:
    """The direction text on the project's base branch, or None when there is none."""
    if git is None:
        from core.host import git
    settings = direction_settings(project)
    repository, branch = project.get("repository"), project.get("base_branch")
    if not repository or not branch:
        return None
    try:
        text = git(["show", f"refs/heads/{branch}:{settings['path']}"],
                   Path(repository).expanduser())
    except (RuntimeError, OSError):
        return None
    text = str(text or "").strip()
    if not text:
        return None
    budget = max_chars or settings["max_chars"]
    truncated = len(text) > budget
    if truncated:
        text = text[:budget].rstrip() + TRUNCATED
    return Direction(path=settings["path"], text=text, truncated=truncated)


def direction_text(master, project_id: str) -> Optional[str]:
    """The direction text of a project known to ``master``, or None."""
    from core.master import EXPECTED_ERRORS

    try:
        project = master.project_state(project_id).project()
    except EXPECTED_ERRORS:
        return None
    direction = read_direction(project)
    return direction.text if direction else None
