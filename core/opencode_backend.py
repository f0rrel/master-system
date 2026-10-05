from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Optional

from core.execution import ExecutionBackend, ExecutionResult


class OpenCodeBackendError(Exception):
    """Raised when OpenCode CLI execution cannot be invoked or returns malformed output."""


class OpenCodeCliBackend(ExecutionBackend):
    """ExecutionBackend that invokes OpenCode CLI in a controlled workspace.

    This backend uses the OpenCode CLI as an external process. It does NOT
    mutate project state. It captures the CLI's result and maps it to
    ExecutionResult. The backend is intentionally conservative and replaceable.
    """

    def __init__(
        self,
        workdir: Optional[Path] = None,
        timeout: Optional[int] = None,
        opencode_bin: Optional[Path] = None,
        log_level: str = "ERROR",
    ):
        self._workdir = Path(workdir) if workdir else None
        self._timeout = timeout
        self._opencode_bin = Path(opencode_bin) if opencode_bin else Path("opencode")
        self._log_level = log_level

    def execute(self, task: Mapping[str, object], context: Mapping[str, object],
                workspace=None) -> ExecutionResult:
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

        workdir = workspace.path if workspace is not None else self._workdir
        if workdir is None:
            workdir = Path(tempfile.mkdtemp(prefix="opencode-backend-"))

        workdir = Path(workdir).resolve()
        workdir.mkdir(parents=True, exist_ok=True)

        cmd = [
            str(self._opencode_bin),
            "run",
            "--log-level",
            self._log_level,
            "--dir",
            str(workdir),
            prompt,
        ]

        artifacts: dict[str, object] = {
            "command": cmd,
            "workdir": str(workdir),
            "project_id": project_id,
            "task_id": task_id,
        }

        if workspace is not None:
            return self._run_in_workspace(workspace, cmd[1:], artifacts)

        try:
            result = subprocess.run(
                cmd,
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired as e:
            artifacts["timeout"] = True
            artifacts["stdout"] = e.stdout.decode("utf-8", errors="replace") if e.stdout else ""
            artifacts["stderr"] = e.stderr.decode("utf-8", errors="replace") if e.stderr else ""
            return ExecutionResult(
                status="failed",
                reason="OpenCode CLI execution timed out",
                artifacts=artifacts,
            )
        except FileNotFoundError as e:
            raise OpenCodeBackendError(f"OpenCode binary not found: {self._opencode_bin}") from e
        except OSError as e:
            raise OpenCodeBackendError(f"Failed to invoke OpenCode: {e}") from e

        artifacts["returncode"] = result.returncode
        artifacts["stdout"] = result.stdout
        artifacts["stderr"] = result.stderr

        if result.returncode == 0:
            status = "success"
            reason = "OpenCode CLI completed successfully"
        else:
            status = "failed"
            reason = f"OpenCode CLI exited with non-zero code {result.returncode}"

        return ExecutionResult(status=status, reason=reason, artifacts=artifacts)

    def _run_in_workspace(self, workspace, arguments, artifacts) -> ExecutionResult:
        """Run through the orchestrator's workspace: its lock, deadline and group."""
        try:
            outcome = workspace.run([str(self._opencode_bin), *arguments])
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
