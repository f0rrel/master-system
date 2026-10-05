from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Optional

from core.execution import ExecutionBackend, ExecutionResult


class OpenCodeBackendError(Exception):
    """Raised when OpenCode CLI execution cannot be invoked or returns malformed output."""


#: Event fields read from `opencode run --format json` (recorded in the M2 plan, D4).
_USAGE_KEYS = ("input_tokens", "output_tokens", "reasoning_tokens",
               "cached_input_tokens", "cache_write_tokens")


def parse_events(stdout: str):
    """Read `opencode run --format json` output: one JSON event per line.

    Returns ``(summary, usage, counts)``: the last ``text`` part, token and cost
    usage summed over ``step_finish`` events, and how many events of each type
    were seen. Lines that are not JSON objects are ignored (counted as
    ``unparsed``); the usage is a claim, used for accounting only.
    """
    summary = ""
    usage = {key: 0 for key in _USAGE_KEYS}
    cost = 0.0
    counts: dict = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            counts["unparsed"] = counts.get("unparsed", 0) + 1
            continue
        if not isinstance(event, Mapping):
            counts["unparsed"] = counts.get("unparsed", 0) + 1
            continue
        kind = str(event.get("type"))
        counts[kind] = counts.get(kind, 0) + 1
        part = event.get("part") if isinstance(event.get("part"), Mapping) else {}
        if kind == "text" and isinstance(part.get("text"), str):
            summary = part["text"]
        elif kind == "step_finish":
            tokens = part.get("tokens") if isinstance(part.get("tokens"), Mapping) else {}
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), Mapping) else {}
            for key, value in (("input_tokens", tokens.get("input")),
                               ("output_tokens", tokens.get("output")),
                               ("reasoning_tokens", tokens.get("reasoning")),
                               ("cached_input_tokens", cache.get("read")),
                               ("cache_write_tokens", cache.get("write"))):
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    usage[key] += int(value)
            if isinstance(part.get("cost"), (int, float)) and not isinstance(part.get("cost"), bool):
                cost += float(part["cost"])
    usage["reported_cost_usd"] = round(cost, 6)
    usage["steps"] = counts.get("step_finish", 0)
    return summary, usage, counts


def build_prompt(task: Mapping) -> str:
    """The instruction given to the worker: the task's human-written spec."""
    task_id = task.get("id")
    parts = []
    if task_id:
        parts.append(f"Task ID: {task_id}")
    parts.append(f"Title: {task.get('title') or task_id or 'task'}")
    if task.get("description"):
        parts.append(f"Description:\n{task['description']}")
    acceptance = task.get("acceptance") if isinstance(task.get("acceptance"), Mapping) else None
    if acceptance:
        commands = "\n".join(f"  - {c}" for c in acceptance.get("commands") or [])
        parts.append("Your work is accepted only if all of these commands pass, run from "
                     "the repository root:\n" + commands)
        protected = acceptance.get("protected_paths") or []
        if protected:
            parts.append("Do not create, modify, rename or delete any path matching these "
                         "patterns; any change to them fails the task:\n"
                         + "\n".join(f"  - {g}" for g in protected))
    parts.append("Work only inside the current directory. Do not push, do not switch "
                 "branches, and do not modify project-management files. When you are done, "
                 "reply with a concise summary of what you changed.")
    return "\n\n".join(parts)


class OpenCodeCliBackend(ExecutionBackend):
    """ExecutionBackend that runs the OpenCode CLI in the orchestrator's workspace.

    The backend never chooses where to work: it runs ``opencode run --format
    json --dir <workspace.path>`` through ``workspace.run``, so the process gets
    the allowlisted environment, inherits the project lock, stops at the attempt
    deadline and runs in its own process group. It does NOT mutate project
    state. Its usage report and summary are the worker's claims.
    """

    def __init__(
        self,
        opencode_bin: Optional[Path] = None,
        log_level: str = "ERROR",
        model: Optional[str] = None,
        extra_args: Sequence[str] = (),
    ):
        self._opencode_bin = Path(opencode_bin) if opencode_bin else Path("opencode")
        self._log_level = log_level
        self._model = model
        self._extra_args = tuple(extra_args)

    def command(self, workspace_path, prompt) -> list:
        cmd = [str(self._opencode_bin), "run", "--format", "json",
               "--log-level", self._log_level, "--dir", str(workspace_path)]
        if self._model:
            cmd += ["--model", self._model]
        return [*cmd, *self._extra_args, prompt]

    def execute(self, task: Mapping[str, object], context: Mapping[str, object],
                *, workspace) -> ExecutionResult:
        if not isinstance(task, Mapping):
            raise OpenCodeBackendError("task must be a mapping")

        project_id = context.get("project_id") if isinstance(context, Mapping) else None
        prompt = build_prompt(task)
        cmd = self.command(workspace.path, prompt)

        try:
            outcome = workspace.run(cmd)
        except FileNotFoundError as e:
            raise OpenCodeBackendError(f"OpenCode binary not found: {self._opencode_bin}") from e
        except OSError as e:
            raise OpenCodeBackendError(f"Failed to invoke OpenCode: {e}") from e

        summary, usage, counts = parse_events(outcome.stdout)
        artifacts: dict[str, object] = {
            "command": cmd[:-1] + ["<prompt>"],
            "project_id": project_id,
            "task_id": task.get("id"),
            "model": self._model,
            "returncode": outcome.returncode,
            "summary": summary,
            "event_counts": counts,
            "stderr_tail": outcome.stderr[-4000:],
        }
        if outcome.timed_out:
            artifacts["timeout"] = True
            return ExecutionResult(status="failed", reason="the attempt deadline passed",
                                   artifacts=artifacts, usage=usage)
        if outcome.returncode == 0:
            return ExecutionResult(status="success", reason="OpenCode CLI completed",
                                   artifacts=artifacts, usage=usage)
        return ExecutionResult(
            status="failed",
            reason=f"OpenCode CLI exited with non-zero code {outcome.returncode}",
            artifacts=artifacts, usage=usage,
        )
