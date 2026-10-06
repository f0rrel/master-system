"""Task size: keep every task small enough for a worker to write in one go.

A worker writes a task's code in a few model replies, and each reply has an output
limit. A task that asks for many new things at once (six drawings, four screens)
or several hundred lines gets cut off before anything is written. So:

* the planner's rule: one task = one new "thing" (one module piece, one screen, one
  asset group), at most about ``max_lines`` (150) new or changed lines; bigger work
  is an ordered sequence of tasks with ``depends_on``;
* the planner gives each task an ``estimate_lines``;
* ``check`` flags a task as oversized when its estimate is too large or missing, when
  its description asks for many items at once, or when it touches many files, and
  suggests a split. ``approve`` refuses oversized drafts unless the owner says
  ``approve anyway``.

The item count is a heuristic over the description ("six monster faces", "4 screens").
"""

from __future__ import annotations

import math
import re
from typing import Mapping

__all__ = ["size_findings", "MAX_LINES", "MAX_ITEMS", "MAX_FILES"]

MAX_LINES = 150
#: More distinct new things than this in one task is oversized.
MAX_ITEMS = 3
MAX_FILES = 3

_NUMBERS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
            "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "a dozen": 12}
_COUNT = re.compile(
    r"(?<![\w.-])(\d{1,2}|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|a dozen)"
    r"((?:\s+[a-z][\w-]*){0,3}?)\s+([a-z][\w-]*s)\b", re.IGNORECASE)
#: Plural words that count things to create, not quantities or units.
_NOT_ITEMS = {"lines", "times", "seconds", "minutes", "ms", "pixels", "px", "percent",
              "degrees", "steps", "moves", "rounds", "players", "points", "stars", "tests",
              "attempts", "words", "characters", "hours", "days", "weeks", "values",
              "variables", "paths", "commands", "colours", "colors", "checks", "times",
              "kinds", "levels"}


#: A count after these words refers to existing things ("all six", "the other four").
_REFERENCES = {"all", "other", "remaining", "existing", "previous", "those", "these"}


def _items(text: str) -> int:
    """The largest count of things a description asks to create ("six monster faces")."""
    best = 0
    text = str(text or "")
    for match in _COUNT.finditer(text):
        word, noun = match.group(1).lower(), match.group(3).lower()
        if noun in _NOT_ITEMS:
            continue
        before = text[:match.start()].split()[-1:]
        if before and before[0].lower().strip(",.;:(") in _REFERENCES:
            continue  # "all six types", "the other four": things that already exist
        count = int(word) if word.isdigit() else _NUMBERS.get(word, 0)
        if 2 <= count <= 50:
            best = max(best, count)
    return best


def size_findings(task: Mapping, max_lines: int = MAX_LINES) -> list:
    """Why a drafted task is too big, each with a suggested split (empty: it is fine)."""
    findings = []
    estimate = task.get("estimate_lines")
    if not isinstance(estimate, int) or isinstance(estimate, bool) or estimate <= 0:
        findings.append("no estimate_lines: say how many lines the task adds or changes")
    elif estimate > max_lines:
        parts = math.ceil(estimate / max_lines)
        findings.append(f"about {estimate} lines (limit {max_lines}): split it into {parts} "
                        f"tasks of at most {max_lines} lines, in order, with depends_on")
    items = _items(f"{task.get('title', '')}. {task.get('description', '')}")
    if items > MAX_ITEMS:
        parts = math.ceil(items / 2)
        findings.append(f"creates about {items} things at once: split it into {parts} tasks "
                        f"(for example the skeleton with the first 2, then 2 more per task), "
                        "each depending on the previous one")
    files = [f for f in task.get("files") or [] if isinstance(f, str)]
    if len(files) > MAX_FILES:
        findings.append(f"changes {len(files)} files: split it so each task changes at most "
                        f"{MAX_FILES}")
    return findings
