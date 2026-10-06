"""Task types: what kind of work an attempt may do, and with which tools.

Every planned task has a type. A type brings three things:

* a **skill doc**: guidance for the worker, from this repository
  (``skills/<type>.md``) plus, optionally, the project's own doc (a path in the
  project's repository);
* **allowed paths**: globs the attempt may change. They are frozen into the
  task's ``acceptance.allowed_paths`` when the plan is approved, so the verifier
  fails an attempt that changes anything else;
* **allowed tools**: the worker's tools for the attempt. The orchestrator passes
  an inline OpenCode config that disables (and denies) every other known tool.

project.yaml narrows the defaults per type (all keys optional)::

    task_types:
      visual:
        allowed_paths: ["www/css/*", "www/assets/*"]
        tools: [read, edit, write, grep, glob, list, bash]
        skill: docs/skills/visual.md

Globs use ``fnmatch`` on repository-relative paths (``*`` also matches ``/``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

__all__ = ["TYPES", "ALL_TOOLS", "type_settings", "opencode_config", "skill_text",
           "default_type"]

from core.project_state import TASK_TYPES as TYPES  # one definition
#: OpenCode's built-in tools. Tools not allowed for a type are disabled.
ALL_TOOLS = ("bash", "edit", "write", "patch", "read", "grep", "glob", "list",
             "todowrite", "todoread", "webfetch", "websearch", "task")
_CODE_TOOLS = ("bash", "edit", "write", "patch", "read", "grep", "glob", "list",
               "todowrite", "todoread")
DEFAULTS = {
    "developer": {"allowed_paths": ["*"], "tools": _CODE_TOOLS},
    "visual": {"allowed_paths": ["*"], "tools": _CODE_TOOLS},
    "logic": {"allowed_paths": ["*"], "tools": _CODE_TOOLS},
    "docs": {"allowed_paths": ["*.md", "docs/*"],
             "tools": ("edit", "write", "patch", "read", "grep", "glob", "list")},
}
SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
SKILL_BUDGET = 4000


def default_type() -> str:
    return "developer"


def type_settings(project: dict, task_type: str) -> dict:
    """allowed_paths, tools and the project skill path for one type."""
    if task_type not in TYPES:
        raise ValueError(f"unknown task type {task_type!r}; types: {', '.join(TYPES)}")
    configured = project.get("task_types") if isinstance(project.get("task_types"), dict) else {}
    own = configured.get(task_type) if isinstance(configured.get(task_type), dict) else {}
    paths = [str(g) for g in own.get("allowed_paths") or DEFAULTS[task_type]["allowed_paths"]]
    tools = [str(t) for t in own.get("tools") or DEFAULTS[task_type]["tools"]]
    return {"type": task_type, "allowed_paths": paths,
            "tools": [t for t in tools if t in ALL_TOOLS],
            "skill": str(own["skill"]) if own.get("skill") else None}


def frozen_allowed_paths(settings: dict) -> Optional[list]:
    """What goes into acceptance.allowed_paths: None when the type allows everything."""
    return None if "*" in settings["allowed_paths"] else list(settings["allowed_paths"])


def opencode_config(tools) -> str:
    """Inline OpenCode config (OPENCODE_CONFIG_CONTENT) allowing only ``tools``."""
    allowed = set(tools)
    config = {"tools": {t: False for t in ALL_TOOLS if t not in allowed}}
    permission = {}
    if not {"edit", "write", "patch"} & allowed:
        permission["edit"] = "deny"
    for name in ("bash", "webfetch"):
        if name not in allowed:
            permission[name] = "deny"
    if permission:
        config["permission"] = permission
    return json.dumps(config, sort_keys=True)


def skill_text(project: dict, settings: dict, git: Optional[Callable] = None,
               budget: int = SKILL_BUDGET) -> str:
    """The type's generic skill doc plus the project's own, within a budget."""
    parts = []
    generic = SKILLS_DIR / f"{settings['type']}.md"
    if generic.is_file():
        parts.append(generic.read_text().strip())
    if settings.get("skill") and project.get("repository") and project.get("base_branch"):
        if git is None:
            from core.host import git
        try:
            own = git(["show", f"refs/heads/{project['base_branch']}:{settings['skill']}"],
                      Path(project["repository"]).expanduser())
            if own.strip():
                parts.append(own.strip())
        except (RuntimeError, OSError):
            pass
    text = "\n\n".join(parts)
    return text if len(text) <= budget else text[:budget].rstrip() + "\n…(truncated)"
