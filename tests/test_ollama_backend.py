"""Tests for the Ollama execution adapter.

Ollama is never contacted here. Every test drives the adapter through an
injected transport that returns canned ``/api/chat`` replies, so the suite runs
anywhere and in milliseconds. The real-model path is verified separately by a
disposable script, not by the test suite.
"""

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import attach_repository, workspace_for
from core.execution import ExecutionBackend, ExecutionResult
from core.execution_runner import TaskExecutionRunner
from core.master import Master
from core.ollama_backend import (
    DEFAULT_MODEL,
    OllamaBackendError,
    OllamaExecutionBackend,
)


def make_project(root, tasks):
    proj = Path(root) / "projects" / "p"
    proj.mkdir(parents=True)
    (proj / "project.yaml").write_text("id: p\nname: P\nstatus: active\n")
    (proj / "milestones.yaml").write_text(
        "milestones:\n  - id: m1\n    name: M1\n    status: in_progress\n"
    )
    lines = ["tasks:"]
    for t in tasks:
        lines.append(f"  - id: {t['id']}")
        lines.append(f"    milestone: {t.get('milestone','m1')}")
        lines.append(f"    title: {t.get('title', t['id'])}")
        lines.append(f"    status: {t['status']}")
        lines.append(f"    assigned_to: {t.get('assigned_to','master')}")
    (proj / "tasks.yaml").write_text("\n".join(lines) + "\n")
    return Path(root) / "projects"


def tool_call(name, **arguments):
    return {"function": {"name": name, "arguments": arguments}}


def tool_reply(*calls, content=""):
    """A /api/chat reply asking for tool calls."""
    return {"message": {"role": "assistant", "content": content, "tool_calls": list(calls)}}


def done_reply(content="All done."):
    """A /api/chat reply with no tool calls, i.e. the model is done."""
    return {"message": {"role": "assistant", "content": content}}


class FakeTransport:
    """Stand-in for the HTTP call. Records requests, replays scripted replies."""

    def __init__(self, *replies, error=None):
        self.replies = list(replies)
        self.error = error
        self.requests = []

    def __call__(self, payload, url, timeout):
        # Snapshot, because the adapter keeps appending to one message list and
        # an assertion about what was *sent* must not see later turns.
        self.requests.append({"payload": copy.deepcopy(payload), "url": url, "timeout": timeout})
        if self.error is not None:
            raise self.error
        if not self.replies:
            raise AssertionError("model asked for more turns than the test scripted")
        return self.replies.pop(0)


TASK = {"id": "t1", "title": "Implement normalize", "status": "in_progress"}
CONTEXT = {"project_id": "p", "task_id": "t1"}


# --- the interface -------------------------------------------------------


def test_backend_implements_the_execution_contract(tmp_path):
    assert isinstance(OllamaExecutionBackend(), ExecutionBackend)


def test_model_and_runtime_are_configuration(tmp_path):
    backend = OllamaExecutionBackend(model="llama3.2:3b", host="http://box:11434")

    assert backend.model == "llama3.2:3b"
    assert backend._url == "http://box:11434/api/chat"


def test_defaults_are_only_defaults(tmp_path):
    assert OllamaExecutionBackend().model == DEFAULT_MODEL


@pytest.mark.parametrize("kwargs", [
    {"model": ""},
    {"model": "   "},
    {"model": None},
    {"host": ""},
    {"max_turns": 0},
])
def test_bad_configuration_is_refused(tmp_path, kwargs):
    with pytest.raises(ValueError):
        OllamaExecutionBackend(**kwargs)


# --- a successful run ----------------------------------------------------


def test_the_model_can_write_a_file_and_finish(tmp_path):
    work = tmp_path / "ws"
    transport = FakeTransport(
        tool_reply(tool_call("write_file", path="mod.py", content="x = 1\n")),
        done_reply("Added mod.py."),
    )
    backend = OllamaExecutionBackend(transport=transport)

    result = backend.execute(TASK, CONTEXT, workspace=workspace_for(work))

    assert result.status == "success"
    assert (work / "mod.py").read_text() == "x = 1\n"
    assert result.artifacts["model"] == DEFAULT_MODEL
    assert result.artifacts["turns"] == 2
    assert result.artifacts["summary"] == "Added mod.py."
    assert len(transport.requests) == 2


def test_the_request_carries_the_task_and_the_workspace(tmp_path):
    work = tmp_path / "ws"
    transport = FakeTransport(done_reply())
    backend = OllamaExecutionBackend(transport=transport)

    backend.execute(TASK, CONTEXT, workspace=workspace_for(work))

    sent = transport.requests[0]["payload"]
    assert sent["model"] == DEFAULT_MODEL
    assert sent["stream"] is False
    assert sent["tools"], "the worker needs to be offered its tools"
    roles = [m["role"] for m in sent["messages"]]
    assert roles == ["system", "user"]
    user = sent["messages"][1]["content"]
    assert "t1" in user
    assert "Implement normalize" in user
    assert str(work.resolve()) in user


def test_tool_results_are_fed_back_to_the_model(tmp_path):
    transport = FakeTransport(
        tool_reply(tool_call("write_file", path="a.txt", content="hello")),
        done_reply("done"),
    )
    backend = OllamaExecutionBackend(transport=transport)

    backend.execute(TASK, CONTEXT, workspace=workspace_for(tmp_path / "ws"))

    second = transport.requests[1]["payload"]["messages"]
    assert second[-1] == {"role": "tool", "content": "wrote 5 characters to 'a.txt'"}


def test_reading_a_file_shows_its_contents_to_the_model(tmp_path):
    work = tmp_path / "ws"
    (work / "existing.py").mkdir(parents=True)
    (work / "existing.py" / "a.py").write_text("ORIGINAL = 1\n")
    transport = FakeTransport(
        tool_reply(tool_call("read_file", path="existing.py/a.py")),
        done_reply("read it"),
    )
    backend = OllamaExecutionBackend(transport=transport)

    result = backend.execute(TASK, CONTEXT, workspace=workspace_for(work))

    assert "ORIGINAL = 1" in result.artifacts["tool_calls"][0]["output"]


def test_listing_files_reports_the_workspace(tmp_path):
    work = tmp_path / "ws"
    work.mkdir()
    (work / "a.py").write_text("x")
    (work / "b.py").write_text("y")
    transport = FakeTransport(tool_reply(tool_call("list_files")), done_reply("ok"))

    result = OllamaExecutionBackend(transport=transport).execute(TASK, CONTEXT, workspace=workspace_for(work))

    assert '"a.py"' in result.artifacts["tool_calls"][0]["output"]
    assert '"b.py"' in result.artifacts["tool_calls"][0]["output"]


def test_several_calls_in_one_turn_all_run(tmp_path):
    work = tmp_path / "ws"
    transport = FakeTransport(
        tool_reply(
            tool_call("write_file", path="one.py", content="1"),
            tool_call("write_file", path="two.py", content="2"),
        ),
        done_reply("both written"),
    )

    result = OllamaExecutionBackend(transport=transport).execute(TASK, CONTEXT, workspace=workspace_for(work))

    assert (work / "one.py").read_text() == "1"
    assert (work / "two.py").read_text() == "2"
    assert len(result.artifacts["tool_calls"]) == 2


# --- the workspace is a boundary ----------------------------------------


@pytest.mark.parametrize("escape", [
    "../outside.py",
    "../../etc/passwd",
    "sub/../../escape.py",
])
def test_a_model_cannot_write_outside_the_workspace(tmp_path, escape):
    work = tmp_path / "ws"
    transport = FakeTransport(
        tool_reply(tool_call("write_file", path=escape, content="pwned")),
        done_reply("tried"),
    )

    result = OllamaExecutionBackend(transport=transport).execute(TASK, CONTEXT, workspace=workspace_for(work))

    assert "error:" in result.artifacts["tool_calls"][0]["output"]
    assert not (tmp_path / "outside.py").exists()
    assert not (tmp_path / "escape.py").exists()


def test_an_absolute_path_is_refused(tmp_path):
    target = tmp_path / "elsewhere.py"
    transport = FakeTransport(
        tool_reply(tool_call("write_file", path=str(target), content="pwned")),
        done_reply("tried"),
    )

    result = OllamaExecutionBackend(transport=transport).execute(TASK, CONTEXT, workspace=workspace_for(tmp_path / "ws"))

    assert "error:" in result.artifacts["tool_calls"][0]["output"]
    assert not target.exists()


def test_an_unknown_tool_is_reported_rather_than_crashing(tmp_path):
    transport = FakeTransport(
        tool_reply(tool_call("rm_rf_slash")), done_reply("gave up")
    )

    result = OllamaExecutionBackend(transport=transport).execute(TASK, CONTEXT, workspace=workspace_for(tmp_path / "ws"))

    assert "unknown tool" in result.artifacts["tool_calls"][0]["output"]


def test_a_missing_file_is_reported_not_raised(tmp_path):
    transport = FakeTransport(
        tool_reply(tool_call("read_file", path="nope.py")), done_reply("gone")
    )

    result = OllamaExecutionBackend(transport=transport).execute(TASK, CONTEXT, workspace=workspace_for(tmp_path / "ws"))

    assert "no such file" in result.artifacts["tool_calls"][0]["output"]
    assert result.status == "success"


def test_a_path_that_is_not_a_string_is_refused(tmp_path):
    transport = FakeTransport(
        tool_reply(tool_call("write_file", path=42, content="x")), done_reply("gave up")
    )

    result = OllamaExecutionBackend(transport=transport).execute(TASK, CONTEXT, workspace=workspace_for(tmp_path / "ws"))

    assert "error:" in result.artifacts["tool_calls"][0]["output"]


# --- failure handling ----------------------------------------------------


def test_an_unreachable_runtime_raises(tmp_path):
    backend = OllamaExecutionBackend(transport=FakeTransport(error=OSError("connection refused")),
    )

    with pytest.raises(OllamaBackendError, match="could not reach Ollama"):
        backend.execute(TASK, CONTEXT, workspace=workspace_for(tmp_path))


def test_a_bad_http_status_raises(tmp_path):
    import urllib.error
    import io

    error = urllib.error.HTTPError("http://x", 404, "Not Found", {}, io.BytesIO(b"model not found"))
    backend = OllamaExecutionBackend(transport=FakeTransport(error=error))

    with pytest.raises(OllamaBackendError, match="404"):
        backend.execute(TASK, CONTEXT, workspace=workspace_for(tmp_path))


def test_a_timeout_is_a_failed_result_not_a_crash(tmp_path):
    backend = OllamaExecutionBackend(timeout=5, transport=FakeTransport(error=TimeoutError())
    )

    result = backend.execute(TASK, CONTEXT, workspace=workspace_for(tmp_path))

    assert result.status == "failed"
    assert "timed out" in result.reason
    assert result.artifacts["model"] == DEFAULT_MODEL


def test_a_reply_without_a_message_raises(tmp_path):
    backend = OllamaExecutionBackend(transport=FakeTransport({"done": True}))

    with pytest.raises(OllamaBackendError, match="no message object"):
        backend.execute(TASK, CONTEXT, workspace=workspace_for(tmp_path))


def test_a_non_mapping_task_is_refused(tmp_path):
    backend = OllamaExecutionBackend(transport=FakeTransport(done_reply()))

    with pytest.raises(OllamaBackendError, match="task must be a mapping"):
        backend.execute("not a task", CONTEXT, workspace=workspace_for(tmp_path))


def test_the_turn_budget_stops_a_model_that_never_finishes(tmp_path):
    transport = FakeTransport(*[tool_reply(tool_call("write_file", path=f"f{i}.txt", content="x"))
                                for i in range(5)])
    backend = OllamaExecutionBackend(transport=transport, max_turns=3)

    result = backend.execute(TASK, CONTEXT, workspace=workspace_for(tmp_path / "ws"))

    assert result.status == "partial"
    assert "did not finish" in result.reason
    assert result.artifacts["turns"] == 3
    assert len(transport.requests) == 3


def test_the_turn_budget_is_reported_as_failed_when_nothing_was_done(tmp_path):
    transport = FakeTransport(*[tool_reply(tool_call("list_files")) for _ in range(3)])
    backend = OllamaExecutionBackend(transport=transport, max_turns=2)

    result = backend.execute(TASK, CONTEXT, workspace=workspace_for(tmp_path / "ws"))

    assert result.status == "failed"


def test_malformed_tool_arguments_do_not_stop_the_loop(tmp_path):
    reply = {"message": {"role": "assistant", "content": "",
                         "tool_calls": [{"function": {"name": "write_file",
                                                      "arguments": "{not json"}}]}}
    transport = FakeTransport(reply, done_reply("recovered"))
    backend = OllamaExecutionBackend(transport=transport)

    result = backend.execute(TASK, CONTEXT, workspace=workspace_for(tmp_path / "ws"))

    assert result.status == "success"
    assert "error:" in result.artifacts["tool_calls"][0]["output"]


# --- the backend does not touch project state ---------------------------


def test_the_backend_never_mutates_authoritative_state(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress", "title": "Do X"}])
    master = Master(root)
    before = master.status("p")
    transport = FakeTransport(
        tool_reply(tool_call("write_file", path="out.py", content="x = 1")),
        done_reply("done"),
    )

    (tmp_path / "ws").mkdir()
    result = TaskExecutionRunner(
        master, OllamaExecutionBackend(transport=transport)
    ).execute("p", "t1", workspace=workspace_for(tmp_path / "ws"))

    assert result.status == "success"
    assert master.status("p") == before


def test_state_updates_are_empty_so_nothing_can_leak(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    master = Master(root)
    transport = FakeTransport(done_reply())

    (tmp_path / "ws").mkdir()
    result = TaskExecutionRunner(
        master, OllamaExecutionBackend(transport=transport)
    ).execute("p", "t1", workspace=workspace_for(tmp_path / "ws"))

    assert result.state_updates == {}


# --- the orchestration layer cannot tell which backend it got ------------


def test_the_runner_passes_the_task_through_untouched(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress", "title": "Do X"}])
    master = Master(root)
    seen = {}

    class RecordingBackend:
        def execute(self, task, context, workspace=None):
            seen["task"] = dict(task)
            seen["context"] = dict(context)
            return ExecutionResult(status="success", reason="ok")

    TaskExecutionRunner(master, RecordingBackend()).execute(
        "p", "t1", workspace=workspace_for(tmp_path)
    )

    assert seen["task"]["id"] == "t1"
    assert seen["context"]["project_id"] == "p"
    assert "ollama" not in str(seen).lower()


# --- the architectural claim, made checkable -----------------------------

#: The layers that must never learn what today's worker happens to be.
ORCHESTRATION_MODULES = [
    "autonomous_loop",
    "execution_runner",
    "task_orchestrator",
    "execution",
    "master",
    "reasoning",
    "reasoning_engine",
    "work_manager",
]

#: Concrete adapters, i.e. things a composition root may choose between.
ADAPTER_MODULES = ["ollama_backend", "opencode_backend"]


def test_no_orchestration_module_imports_a_concrete_adapter():
    """Ollama and OpenCode must be swappable without touching the core.

    Checked on the import graph rather than the text: a docstring may
    legitimately name a provider while arguing that it does not matter, but
    an import is a dependency. Parsing for imports is what makes this test
    about architecture instead of about prose.
    """
    import ast

    repo = Path(__file__).resolve().parent.parent / "core"
    offenders = []
    for name in ORCHESTRATION_MODULES:
        tree = ast.parse((repo / f"{name}.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for imported in names:
                leaf = imported.rsplit(".", 1)[-1]
                if leaf in ADAPTER_MODULES:
                    offenders.append(f"{name}.py imports {imported}")
    assert not offenders, offenders


def test_the_model_name_appears_only_in_its_own_adapter():
    """`qwen3:8b` is configuration, not something the core repeats."""
    repo = Path(__file__).resolve().parent.parent / "core"
    leaks = [
        name
        for name in ORCHESTRATION_MODULES
        if "qwen3" in (repo / f"{name}.py").read_text()
    ]
    assert not leaks, f"model name leaked into {leaks}"


# --- the whole path, deterministically -----------------------------------


class ScriptedProvider:
    """Replays Master replies; the reasoning side is not what is under test."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def complete(self, prompt, schema=None):
        self.prompts.append(prompt)
        return self.replies.pop(0)


def act(task_id, status):
    return (
        '{"decision": "act", "reason": "move it along", "operation": '
        '{"operation": "update_task", "project_id": "p", "task_id": "%s", "status": "%s"}}'
        % (task_id, status)
    )


def dispatch(task_id):
    return (
        '{"decision": "act", "reason": "run it", "operation": '
        '{"operation": "run_task", "project_id": "p", "task_id": "%s"}}' % task_id
    )


class SubprocessPytestVerifier:
    """Deterministic verification: run the worker's real tests for real."""

    def verify(self, task, context, evidence=None, *, workspace):
        from core.verification import VerificationResult

        done = workspace.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                             timeout=120)
        return VerificationResult(
            verdict="pass" if done.returncode == 0 else "fail",
            summary=done.stdout.strip().splitlines()[-1] if done.stdout.strip() else "",
            evidence={"exit_code": done.returncode},
        )


def test_the_full_loop_runs_through_ollama_without_touching_the_loop(tmp_path):
    """AutonomousLoop -> runner -> OllamaExecutionBackend -> verify -> Master.

    The only thing stubbed is the HTTP call to Ollama, and the verification is
    a real pytest run. AutonomousLoop, Master, TaskOrchestrator and
    TaskExecutionRunner are the production ones, untouched.
    """
    from core.autonomous_loop import STOP_NO_WORK, AutonomousLoop

    root = make_project(tmp_path, [{
        "id": "t1",
        "status": "planned",
        "title": "In greet.py define greet(name) returning 'Hello, ' + name. Make the test pass.",
    }])
    attach_repository(root / "p", files={
        "test_greet.py": "from greet import greet\n\n\ndef test_greet():\n"
                         "    assert greet('Ada') == 'Hello, Ada'\n",
    })
    master = Master(root)
    provider = ScriptedProvider(
        act("t1", "in_progress"), dispatch("t1"), act("t1", "completed")
    )

    # The "model" writes the implementation the task asked for, then stops.
    transport = FakeTransport(
        tool_reply(tool_call("write_file", path="greet.py",
                             content="def greet(name):\n    return 'Hello, ' + name\n")),
        done_reply("Implemented greet."),
    )

    loop = AutonomousLoop(
        master,
        provider,
        OllamaExecutionBackend(model="qwen3:8b", transport=transport),
        SubprocessPytestVerifier(),
        request="Work on t1.",
        max_steps=5,
    )
    result = loop.run("p")

    assert result.stop_reason == STOP_NO_WORK
    assert result.last_output["execution"]["status"] == "success"
    assert result.last_output["verification"]["verdict"] == "pass"
    from core.history import EventType
    started = loop.history.events(types=[EventType.ATTEMPT_STARTED])[0].payload
    assert (Path(started["worktree"]) / "greet.py").exists()
    assert master.status("p")["tasks"][0]["status"] == "completed"


def test_the_loop_is_unchanged_by_which_backend_it_was_given(tmp_path):
    """Same loop, two different backends: no branching anywhere in between."""
    from core.autonomous_loop import STOP_NO_WORK, AutonomousLoop
    from core.verification import VerificationResult

    class AlwaysPasses:
        """What is compared here is the loop, not the verifier."""

        def verify(self, task, context, evidence=None, workspace=None):
            return VerificationResult(verdict="pass", summary="ok")

    def run_with(backend_factory):
        scratch = tmp_path / backend_factory.__name__
        root = make_project(scratch, [{"id": "t1", "status": "planned", "title": "Do X"}])
        attach_repository(root / "p")
        master = Master(root)
        provider = ScriptedProvider(
            act("t1", "in_progress"), dispatch("t1"), act("t1", "completed")
        )
        loop = AutonomousLoop(
            master, provider, backend_factory(),
            AlwaysPasses(), request="r", max_steps=5,
        )
        return loop.run("p"), master

    def via_ollama():
        return OllamaExecutionBackend(transport=FakeTransport(done_reply("nothing to do")))

    class SomeOtherLocalRuntime:
        """Stands in for whatever replaces Ollama next year."""

        def execute(self, task, context, workspace=None):
            return ExecutionResult(status="success", reason="other runtime did it")

    def via_other():
        return SomeOtherLocalRuntime()

    a, master_a = run_with(via_ollama)
    b, master_b = run_with(via_other)

    assert a.stop_reason == b.stop_reason == STOP_NO_WORK
    assert a.steps == b.steps
    # Same authoritative outcome whichever runtime did the work. Only the
    # workspace path differs, so compare the task, not the whole state blob.
    assert master_a.status("p")["tasks"] == master_b.status("p")["tasks"]

# --- the orchestrator's workspace -------------------------------------------


def test_the_backend_has_no_workspace_of_its_own():
    import inspect

    assert "workdir" not in inspect.signature(OllamaExecutionBackend).parameters
    assert not hasattr(OllamaExecutionBackend, "workdir")


def test_a_deadline_already_passed_stops_before_asking_the_model(tmp_path):
    transport = FakeTransport(done_reply())

    result = OllamaExecutionBackend(transport=transport).execute(
        TASK, CONTEXT, workspace=workspace_for(tmp_path, seconds=-1)
    )

    assert result.status == "failed"
    assert "deadline" in result.reason
    assert transport.requests == []


def test_the_deadline_is_checked_between_turns(tmp_path):
    clock = {"now": 0.0}
    transport = FakeTransport(
        tool_reply(tool_call("write_file", path="a.txt", content="1")),
        tool_reply(tool_call("write_file", path="b.txt", content="2")),
        done_reply(),
    )

    def advancing_transport(payload, url, timeout):
        reply = transport(payload, url, timeout)
        clock["now"] += 10  # each model call takes 10 s
        return reply

    import dataclasses
    workspace = dataclasses.replace(workspace_for(tmp_path), deadline=15.0,
                                    clock=lambda: clock["now"])

    result = OllamaExecutionBackend(transport=advancing_transport).execute(
        TASK, CONTEXT, workspace=workspace
    )

    assert result.status == "partial" and "deadline" in result.reason
    assert len(transport.requests) == 2
    assert (tmp_path / "a.txt").exists() and (tmp_path / "b.txt").exists()


def test_each_request_is_bounded_by_the_time_left(tmp_path):
    transport = FakeTransport(done_reply())

    OllamaExecutionBackend(timeout=600, transport=transport).execute(
        TASK, CONTEXT, workspace=workspace_for(tmp_path, seconds=30)
    )

    assert transport.requests[0]["timeout"] <= 30
