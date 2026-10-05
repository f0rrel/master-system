"""Ollama as a concrete local execution backend.

This module is the whole of the Ollama-specific knowledge in the system. It
implements :class:`core.execution.ExecutionBackend` and nothing above it knows
it exists: ``AutonomousLoop``, ``TaskOrchestrator`` and ``TaskExecutionRunner``
receive an ``ExecutionBackend`` and never inspect which one they got. Swapping
Ollama for another local runtime means writing another class like this one and
changing the line that constructs it.

Two things are configuration, not architecture
-----------------------------------------------
The runtime and the model are both constructor arguments::

    OllamaExecutionBackend(model="qwen3:8b")

``qwen3:8b`` is today's model and Ollama is today's runtime. Neither appears in
this file's logic, in the orchestration layer, or in project state. The tool
protocol below is served by any Ollama model that supports function calling.

Why this is not a plain completion call
---------------------------------------
``ollama run qwen3:8b "..."`` returns text and cannot touch the filesystem, so
it cannot implement anything; the verification step would always fail. OpenCode
is an *agent* -- it has file tools. To let a plain model do the same job
through the same contract, this adapter runs a small bounded tool loop:
the model asks for a tool, the adapter performs it in the workspace, and the
result goes back as a tool message until the model answers without asking for
anything.

That loop is the minimum needed to be useful, and it stays here. It is not a
worker framework: there is no scheduler, no session store, no retry policy and
no plan type. The three tools are the whole surface, they are confined to one
directory, and running arbitrary commands is deliberately not among them --
the deterministic verifier is the only thing that executes anything.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable, Optional

from core.execution import ExecutionBackend, ExecutionResult

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_MAX_TURNS",
    "DEFAULT_MODEL",
    "OllamaBackendError",
    "OllamaExecutionBackend",
]

#: Runtime and model are defaults, not decisions. Override either per instance.
DEFAULT_MODEL = "qwen3:8b"
DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_MAX_TURNS = 12

#: Tool surface exposed to the worker. Read what exists, write what the task
#: asks for, list what is there. No shell: the deterministic verifier owns
#: execution of anything, so a worker cannot satisfy or defeat a test by
#: running it itself.
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List the files in the workspace, relative to its root.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Replace a workspace file with exactly the content given. "
                "Read the file first and include everything already in it: "
                "this replaces the whole file, so anything you leave out is lost."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
]

_SYSTEM_PROMPT = """You are a worker that implements one small change.

You can only touch files through the tools you were given, and only inside the
workspace root. Always read a file before you write it. Writing replaces the
whole file, so the content you send must include everything that was already
there; anything you leave out is deleted, including functions you were not asked
to change. Read the tests first, since they show what has to keep working.

Keep the change as small as the task allows and match the style of the code
already there.

You cannot run tests or commands. A separate deterministic process does that
after you finish, so do not claim a result you have not verified -- just make
the change and describe what you did.

When the change is complete, reply with a short plain-text summary and do not
ask for another tool."""


class OllamaBackendError(Exception):
    """Raised when Ollama cannot be reached or replies with unusable output.

    Reserved for transport and configuration faults, mirroring
    :class:`core.opencode_backend.OpenCodeBackendError`: the runtime is absent
    or the model is not pulled. A model that runs and then does the work badly
    is not an error here; that is a ``failed`` result for the verifier to judge.
    """


def _default_transport(payload: Mapping[str, Any], url: str, timeout: Optional[int]) -> Mapping[str, Any]:
    """POST one JSON payload to Ollama and return the decoded reply."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise OllamaBackendError(
            f"Ollama returned HTTP {error.code} for {url}: {detail}"
        ) from error
    except urllib.error.URLError as error:
        raise OllamaBackendError(f"could not reach Ollama at {url}: {error.reason}") from error
    except json.JSONDecodeError as error:
        raise OllamaBackendError(f"Ollama reply was not valid JSON: {error}") from error


class OllamaExecutionBackend(ExecutionBackend):
    """ExecutionBackend that drives a local Ollama model in one workspace.

    Like every backend it only decides *how* to carry out a task. It reads the
    task and context, does the work inside the orchestrator's workspace (never
    a location of its own choosing), stops by the attempt deadline, and returns an
    :class:`ExecutionResult`. It never reads or writes project state; a
    task's status changes only when Master decides, through the approval path.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str = DEFAULT_HOST,
        timeout: Optional[int] = 600,
        max_turns: int = DEFAULT_MAX_TURNS,
        tools: Optional[Sequence[Mapping[str, Any]]] = None,
        transport: Optional[Callable[..., Mapping[str, Any]]] = None,
    ):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        if not isinstance(host, str) or not host.strip():
            raise ValueError("host must be a non-empty string")
        if max_turns < 1:
            raise ValueError("max_turns must be at least 1")

        self._model = model.strip()
        self._host = host.rstrip("/")
        self._url = f"{self._host}/api/chat"
        self._timeout = timeout
        self._max_turns = max_turns
        self._tools = [dict(t) for t in (tools if tools is not None else TOOL_DEFINITIONS)]
        # Injected so the suite can exercise this adapter without Ollama.
        self._transport = transport or _default_transport

    @property
    def model(self) -> str:
        return self._model

    # --- workspace ------------------------------------------------------

    def _resolve_inside(self, workdir: Path, relative: Any) -> Path:
        """Resolve a model-supplied path, refusing anything outside the workspace.

        A model is not trusted with the filesystem: it does not get to reach a
        parent directory, an absolute path, or a sibling checkout, however it
        spells the path.
        """
        if not isinstance(relative, str) or not relative.strip():
            raise ValueError("path must be a non-empty string")
        candidate = Path(relative.strip())
        if candidate.is_absolute():
            raise ValueError(f"absolute paths are not allowed: {relative!r}")
        resolved = (workdir / candidate).resolve()
        if resolved != workdir and workdir not in resolved.parents:
            raise ValueError(f"path escapes the workspace: {relative!r}")
        return resolved

    def _run_tool(self, name: str, arguments: Mapping[str, Any], workdir: Path) -> str:
        """Run one tool call and return the text to show the model."""
        try:
            if name == "list_files":
                return json.dumps(
                    {
                        "files": sorted(
                            str(p.relative_to(workdir))
                            for p in workdir.rglob("*")
                            if p.is_file() and not any(
                                part.startswith(".") for part in p.relative_to(workdir).parts
                            )
                        )
                    }
                )
            if name == "read_file":
                target = self._resolve_inside(workdir, arguments.get("path"))
                if not target.is_file():
                    return f"error: no such file: {arguments.get('path')!r}"
                return target.read_text(encoding="utf-8", errors="replace")
            if name == "write_file":
                target = self._resolve_inside(workdir, arguments.get("path"))
                target.parent.mkdir(parents=True, exist_ok=True)
                content = arguments.get("content")
                if not isinstance(content, str):
                    return "error: content must be a string"
                target.write_text(content, encoding="utf-8")
                return f"wrote {len(content)} characters to {arguments.get('path')!r}"
            return f"error: unknown tool {name!r}"
        except ValueError as error:
            return f"error: {error}"
        except OSError as error:
            return f"error: {error}"

    # --- prompt ---------------------------------------------------------

    @staticmethod
    def _build_request(task: Mapping[str, object], context: Mapping[str, object], workdir: Path) -> list:
        task_id = task.get("id") if isinstance(task, Mapping) else None
        title = (task.get("title") if isinstance(task, Mapping) else None) or task_id or "task"
        description = ""
        if isinstance(task, Mapping):
            description = task.get("description") or task.get("notes") or ""
        project_id = context.get("project_id") if isinstance(context, Mapping) else None

        parts = [f"Workspace root: {workdir}"]
        if task_id:
            parts.append(f"Task ID: {task_id}")
        parts.append(f"Title: {title}")
        if project_id:
            parts.append(f"Project ID: {project_id}")
        if description:
            parts.append(f"Description: {description}")
        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(parts)},
        ]

    # --- execution ------------------------------------------------------

    def execute(self, task: Mapping[str, object], context: Mapping[str, object],
                *, workspace) -> ExecutionResult:
        if not isinstance(task, Mapping):
            raise OllamaBackendError("task must be a mapping")

        workdir = Path(workspace.path).resolve()
        messages = self._build_request(task, context, workdir)

        artifacts: dict[str, Any] = {
            "runtime": "ollama",
            "model": self._model,
            "url": self._url,
            "project_id": context.get("project_id") if isinstance(context, Mapping) else None,
            "task_id": task.get("id") if isinstance(task, Mapping) else None,
        }
        calls: list[dict[str, Any]] = []
        wrote_something = False

        for turn in range(self._max_turns):
            # In-process work cannot be killed from outside, so the deadline
            # is checked before every turn and bounds every request.
            if workspace.expired():
                artifacts["turns"] = turn
                artifacts["tool_calls"] = calls
                return ExecutionResult(
                    status="partial" if wrote_something else "failed",
                    reason="the attempt deadline passed",
                    artifacts=artifacts,
                )
            remaining = workspace.remaining()
            timeout = remaining if self._timeout is None else min(self._timeout, remaining)

            payload = {
                "model": self._model,
                "messages": messages,
                "tools": self._tools,
                "stream": False,
            }

            try:
                reply = self._transport(payload, self._url, timeout)
            except OllamaBackendError:
                raise
            except TimeoutError:
                # TimeoutError subclasses OSError, so it must be caught first or
                # a slow local model would be reported as an unreachable runtime.
                return ExecutionResult(
                    status="failed",
                    reason=f"Ollama request timed out after {timeout:.0f}s",
                    artifacts=artifacts,
                )
            except (OSError, urllib.error.URLError) as error:
                raise OllamaBackendError(f"could not reach Ollama: {error}") from error

            message = reply.get("message") if isinstance(reply, Mapping) else None
            if not isinstance(message, Mapping):
                raise OllamaBackendError("Ollama reply contained no message object")
            messages.append(dict(message))

            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                artifacts["turns"] = turn + 1
                artifacts["tool_calls"] = calls
                artifacts["summary"] = str(message.get("content") or "")
                return ExecutionResult(
                    status="success",
                    reason="Ollama model completed the task",
                    artifacts=artifacts,
                )

            for call in tool_calls:
                function = call.get("function") if isinstance(call, Mapping) else None
                name = function.get("name") if isinstance(function, Mapping) else None
                raw_arguments = function.get("arguments") if isinstance(function, Mapping) else None
                if isinstance(raw_arguments, str):
                    try:
                        arguments = json.loads(raw_arguments or "{}")
                    except json.JSONDecodeError:
                        arguments = {}
                elif isinstance(raw_arguments, Mapping):
                    arguments = dict(raw_arguments)
                else:
                    arguments = {}
                if not isinstance(arguments, Mapping):
                    arguments = {}

                output = self._run_tool(name or "", arguments, workdir)
                calls.append({"tool": name, "arguments": arguments, "output": output})
                if name == "write_file" and not output.startswith("error:"):
                    wrote_something = True
                messages.append({"role": "tool", "content": output})

        artifacts["turns"] = self._max_turns
        artifacts["tool_calls"] = calls
        # Reading files is not progress towards the task, so only an actual
        # write distinguishes "got partway" from "never got started".
        return ExecutionResult(
            status="partial" if wrote_something else "failed",
            reason=f"Ollama model did not finish within {self._max_turns} turn(s)",
            artifacts=artifacts,
        )