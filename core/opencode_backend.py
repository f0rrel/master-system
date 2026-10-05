from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Optional

from core.execution import ExecutionBackend, ExecutionResult


class OpenCodeBackendError(Exception):
    """Raised when OpenCode CLI execution cannot be invoked or returns malformed output."""


class OpenCodeCliBackend(ExecutionBackend):
    """ExecutionBackend that runs the OpenCode CLI in the orchestrator's workspace.

    The backend never chooses where to work: it runs ``opencode run --dir
    <workspace.path>`` through ``workspace.run``, so the process inherits the
    project lock, stops at the attempt deadline and runs in its own process
    group. It does NOT mutate project state. The backend is intentionally
    conservative and replaceable.
    """

    def __init__(
        self,
        opencode_bin: Optional[Path] = None,
        log_level: str = "ERROR",
    ):
        self._opencode_bin = Path(opencode_bin) if opencode_bin else Path("opencode")
        self._log_level = log_level

    def execute(self, task: Mapping[str, object], context: Mapping[str, object],
                *, workspace) -> ExecutionResult:
        if not isinstance(task, Mapping):
            raise OpenCodeBackendError("task must be a mapping")

        project_id = context.get("project_id") if isinstance(context, Mapping) else None
        task_id = task.get("id")

        title = task.get("title") or task_id or "task"
        description = task.get("description") or task.get("notes") or ""

        prompt_parts = [f"Task ID: {task_id}" if task_id else "", f"Title: {title}"]
        if description:
            prompt_parts.append(f"Description: {description}")
        prompt_parts.append(
            "Do not modify project state. Return only a concise summary of what you did."
        )
        prompt = "\n\n".join(p for p in prompt_parts if p)

        cmd = [
            str(self._opencode_bin),
            "run",
            "--log-level",
            self._log_level,
            "--dir",
            str(workspace.path),
            prompt,
        ]

        artifacts: dict[str, object] = {
            "command": cmd,
            "project_id": project_id,
            "task_id": task_id,
        }

        try:
            outcome = workspace.run(cmd)
        except FileNotFoundError as e:
            raise OpenCodeBackendError(f"OpenCode binary not found: {self._opencode_bin}") from e
        except OSError as e:
            raise OpenCodeBackendError(f"Failed to invoke OpenCode: {e}") from e

        artifacts["returncode"] = outcome.returncode
        artifacts["stdout"] = outcome.stdout
        artifacts["stderr"] = outcome.stderr
        if outcome.timed_out:
            artifacts["timeout"] = True
            return ExecutionResult(
                status="failed", reason="the attempt deadline passed", artifacts=artifacts
            )
        if outcome.returncode == 0:
            return ExecutionResult(
                status="success", reason="OpenCode CLI completed successfully",
                artifacts=artifacts,
            )
        return ExecutionResult(
            status="failed",
            reason=f"OpenCode CLI exited with non-zero code {outcome.returncode}",
            artifacts=artifacts,
        )
