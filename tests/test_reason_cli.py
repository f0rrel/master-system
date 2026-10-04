import ast
import io
import json
import urllib.error
from pathlib import Path

import pytest
import yaml

from core import reason_cli
from core.provider import ProviderError, ReasoningProvider
from core.reason_cli import main
from core.reasoning import ApprovalState, ResultStatus

CLI_SOURCE_PATH = Path(__file__).parent.parent / "core" / "reason_cli.py"

ALPHA = {"id": "alpha", "name": "Alpha", "status": "in_progress"}
TASK_ONE = {
    "id": "task-001",
    "milestone": "alpha",
    "title": "First",
    "status": "completed",
    "assigned_to": "master",
}


def write_project(root, project_id, name="A Project", milestones=None, tasks=None):
    root.mkdir(parents=True, exist_ok=True)
    files = {
        "project.yaml": {"id": project_id, "name": name, "status": "active"},
        "milestones.yaml": {"milestones": milestones or []},
        "tasks.yaml": {"tasks": tasks or []},
    }
    for filename, data in files.items():
        (root / filename).write_text(yaml.safe_dump(data), encoding="utf-8")
    return root


def bytes_of(root):
    return {p.name: p.read_bytes() for p in sorted(root.iterdir()) if p.is_file()}


def create_task_payload(task_id="task-002", **overrides):
    payload = {
        "operation": "create_task",
        "project_id": "alpha",
        "task_id": task_id,
        "milestone": "alpha",
        "title": "Proposed",
    }
    payload.update(overrides)
    return payload


class StubProvider(ReasoningProvider):
    name = "stub"

    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.calls = 0

    def complete(self, prompt, schema=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.reply

    @classmethod
    def with_operations(cls, *operations, reasoning="because"):
        return cls(
            reply=json.dumps({"reasoning": reasoning, "operations": list(operations)})
        )


@pytest.fixture
def stub(monkeypatch):
    """Install a stub provider and return a factory for its reply."""
    holder = {}

    def factory(reply=None, error=None):
        provider = StubProvider(reply=reply, error=error)
        holder["provider"] = provider
        monkeypatch.setattr(reason_cli, "build_provider", lambda args: provider)
        return provider

    return factory


@pytest.fixture
def root(tmp_path):
    write_project(
        tmp_path / "projects" / "alpha-project",
        "alpha",
        name="Alpha Project",
        milestones=[ALPHA],
        tasks=[TASK_ONE],
    )
    return tmp_path / "projects"


@pytest.fixture
def alpha_path(root):
    return root / "alpha-project"


def run(argv, answers=()):
    """Run the CLI with canned answers, returning (code, stdout, stderr)."""
    lines = iter(answers)

    def read_line():
        try:
            return next(lines)
        except StopIteration:
            return None

    out, err = io.StringIO(), io.StringIO()
    code = main(
        argv=argv,
        read_line=read_line,
        stdout=out,
        stderr=err,
    )
    return code, out.getvalue(), err.getvalue()


# --- the proposal is displayed before anything is approved --------------


def test_the_proposal_is_shown(root, stub):
    stub(reply=StubProvider.with_operations(create_task_payload()).reply)

    code, out, err = run(
        ["Add a task", "--root", str(root), "--project", "alpha"], answers=["n"]
    )

    assert "REASONING" in out
    assert "PROPOSED OPERATIONS" in out
    assert "CREATE_TASK" in out
    assert "task-002" in out
    assert "because" in out


def test_the_approval_question_is_asked(root, stub):
    stub(reply=StubProvider.with_operations(create_task_payload()).reply)

    _, out, _ = run(
        ["Add a task", "--root", str(root), "--project", "alpha"], answers=["n"]
    )

    assert "Approve proposed operations? [y/N]" in out


# --- approval is required, and defaults to no ---------------------------


@pytest.mark.parametrize(
    "answer", ["", "\n", "n", "N", "no", "nope", "maybe", "1", "sure", "yep"]
)
def test_anything_but_an_explicit_yes_means_no(alpha_path, root, stub, answer):
    stub(reply=StubProvider.with_operations(create_task_payload()).reply)
    before = bytes_of(alpha_path)

    code, out, _ = run(
        ["Add a task", "--root", str(root), "--project", "alpha"], answers=[answer]
    )

    assert "Rejected. No changes were made." in out
    assert bytes_of(alpha_path) == before
    assert code == 0


@pytest.mark.parametrize("answer", ["y", "Y", "yes", "YES", " yes ", "Yes"])
def test_an_explicit_yes_executes(alpha_path, root, stub, answer):
    stub(reply=StubProvider.with_operations(create_task_payload()).reply)
    before = bytes_of(alpha_path)

    code, out, _ = run(
        ["Add a task", "--root", str(root), "--project", "alpha"], answers=[answer]
    )

    assert "OK        create_task" in out
    assert bytes_of(alpha_path) != before
    assert code == 0


def test_no_answer_at_all_means_no(alpha_path, root, stub):
    """A closed stdin cannot accidentally approve anything."""
    stub(reply=StubProvider.with_operations(create_task_payload()).reply)
    before = bytes_of(alpha_path)

    code, out, _ = run(["Add a task", "--root", str(root), "--project", "alpha"])

    assert "Rejected. No changes were made." in out
    assert bytes_of(alpha_path) == before
    assert code == 0


def test_dry_run_never_asks_and_never_executes(alpha_path, root, stub):
    stub(reply=StubProvider.with_operations(create_task_payload()).reply)
    before = bytes_of(alpha_path)

    code, out, _ = run(
        ["Add a task", "--root", str(root), "--project", "alpha", "--dry-run"],
        answers=["y"],
    )

    assert "PROPOSED OPERATIONS" in out
    assert "Approve proposed operations?" not in out
    assert bytes_of(alpha_path) == before
    assert code == 0


def test_approval_is_the_only_thing_that_executes(alpha_path, root, stub):
    """The same proposal: unasked means untouched, asked means applied."""
    stub(reply=StubProvider.with_operations(create_task_payload("task-002")).reply)
    before = bytes_of(alpha_path)
    run(["Add a task", "--root", str(root), "--project", "alpha"])
    assert bytes_of(alpha_path) == before

    run(["Add a task", "--root", str(root), "--project", "alpha"], answers=["y"])
    assert bytes_of(alpha_path) != before


# --- invalid proposals are shown and never applied ----------------------


def test_invalid_operations_are_displayed_with_their_reason(alpha_path, root, stub):
    stub(
        reply=StubProvider.with_operations(
            {"operation": "delete_project", "project_id": "alpha"},
            create_task_payload("task-002"),
        ).reply
    )
    before = bytes_of(alpha_path)

    code, out, _ = run(
        ["Wreck things", "--root", str(root), "--project", "alpha"], answers=["n"]
    )

    assert "INVALID" in out
    assert "unknown_operation" in out
    assert "invalid" in out.lower()
    assert bytes_of(alpha_path) == before


def test_an_invalid_operation_never_runs_even_when_approved(alpha_path, root, stub):
    stub(
        reply=StubProvider.with_operations(
            {"operation": "run_shell", "command": "rm -rf /"},
            create_task_payload("task-002"),
        ).reply
    )

    code, out, _ = run(
        ["Add a task", "--root", str(root), "--project", "alpha"], answers=["y"]
    )

    assert "run_shell" not in out.split("RESULTS")[-1]
    assert "OK        create_task" in out
    assert code == 0


def test_a_fully_invalid_proposal_is_not_offered_for_approval(alpha_path, root, stub):
    stub(reply=StubProvider.with_operations({"operation": "drop_everything"}).reply)
    before = bytes_of(alpha_path)

    code, out, _ = run(
        ["Wreck things", "--root", str(root), "--project", "alpha"], answers=["y"]
    )

    assert "No valid operations were proposed" in out
    assert "Approve proposed operations?" not in out
    assert bytes_of(alpha_path) == before
    assert code == 0


def test_an_empty_proposal_reports_nothing_to_approve(alpha_path, root, stub):
    stub(reply=StubProvider.with_operations(reasoning="Nothing to do").reply)
    before = bytes_of(alpha_path)

    code, out, _ = run(
        ["Nothing to do", "--root", str(root), "--project", "alpha"], answers=["y"]
    )

    assert "(none)" in out
    assert "Nothing to approve" in out
    assert bytes_of(alpha_path) == before


# --- failures are reported, never executed ------------------------------


def test_unusable_model_output_fails_the_run(alpha_path, root, stub):
    stub(reply="I think you should run rm -rf /")
    before = bytes_of(alpha_path)

    code, out, err = run(
        ["Do something", "--root", str(root), "--project", "alpha"], answers=["y"]
    )

    assert code == 1
    assert "not valid JSON" in err
    assert "Approve" not in out
    assert bytes_of(alpha_path) == before


def test_an_unreachable_model_fails_the_run(alpha_path, root, stub):
    stub(error=ProviderError("Cannot reach Ollama"))
    before = bytes_of(alpha_path)

    code, _, err = run(
        ["Do something", "--root", str(root), "--project", "alpha"], answers=["y"]
    )

    assert code == 1
    assert "Cannot reach Ollama" in err
    assert bytes_of(alpha_path) == before


def test_a_failing_operation_is_reported_without_crashing(root, stub):
    stub(reply=StubProvider.with_operations(create_task_payload("task-001")).reply)

    code, out, _ = run(
        ["Clash", "--root", str(root), "--project", "alpha"], answers=["y"]
    )

    assert "FAILED    create_task" in out
    assert "DuplicateRecordError" in out
    assert code == 1


# --- project selection ---------------------------------------------------


def test_a_single_project_is_selected_automatically(root, stub):
    stub(reply=StubProvider.with_operations().reply)

    code, out, _ = run(["Nothing", "--root", str(root)], answers=["n"])

    assert code == 0
    assert "PROPOSED OPERATIONS" in out


def test_an_explicit_project_is_used(root, stub):
    stub(reply=StubProvider.with_operations().reply)

    code, _, _ = run(
        ["Nothing", "--root", str(root), "--project", "alpha"], answers=["n"]
    )

    assert code == 0


def test_an_unknown_project_is_a_usage_error(root, stub):
    stub(reply=StubProvider.with_operations().reply)

    with pytest.raises(SystemExit) as caught:
        run(["Nothing", "--root", str(root), "--project", "ghost"], answers=["n"])

    assert "no usable project" in str(caught.value)


def test_an_ambiguous_root_requires_an_explicit_project(tmp_path, stub):
    write_project(tmp_path / "projects" / "one", "one", milestones=[ALPHA])
    write_project(tmp_path / "projects" / "two", "two", milestones=[ALPHA])
    stub(reply=StubProvider.with_operations().reply)

    with pytest.raises(SystemExit) as caught:
        run(["Nothing", "--root", str(tmp_path / "projects")], answers=["n"])

    assert "specify --project" in str(caught.value)


def test_an_empty_root_is_reported_clearly(tmp_path, stub):
    stub(reply=StubProvider.with_operations().reply)
    (tmp_path / "projects").mkdir()

    with pytest.raises(SystemExit) as caught:
        run(["Nothing", "--root", str(tmp_path / "projects")], answers=["n"])

    assert "specify --project" in str(caught.value)


# --- the CLI is configured, not hard-coded ------------------------------


def test_the_provider_is_built_from_flags_not_hard_coded():
    from core.ollama_provider import OllamaProvider

    args = reason_cli.build_parser().parse_args(
        ["req", "--provider", "ollama", "--model", "gemma4:e2b", "--base-url", "http://x:1"]
    )
    provider = reason_cli.build_provider(args)

    assert isinstance(provider, OllamaProvider)
    assert provider.model == "gemma4:e2b"
    assert provider.base_url == "http://x:1"


def test_an_unknown_provider_is_refused():
    with pytest.raises(SystemExit):
        run(["req", "--provider", "openai"], answers=["n"])


# --- the second backend --------------------------------------------------


def parsed(*argv):
    return reason_cli.build_parser().parse_args(["req", *argv])


def test_the_two_backends_are_exactly_the_ones_we_implemented():
    choices = next(
        action.choices
        for action in reason_cli.build_parser()._actions
        if action.dest == "provider"
    )

    assert sorted(choices) == ["deepseek", "ollama", "opencode"]


def test_ollama_is_still_the_default_backend():
    args = parsed()

    assert args.provider == "ollama"
    assert reason_cli.build_provider(args).base_url == "http://localhost:11434"


def test_opencode_is_built_from_flags():
    from core.opencode_provider import OpenCodeProvider

    provider = reason_cli.build_provider(
        parsed(
            "--provider",
            "opencode",
            "--provider-id",
            "acme",
            "--model",
            "small-1",
            "--base-url",
            "http://x:1",
            "--timeout",
            "5",
        )
    )

    assert isinstance(provider, OpenCodeProvider)
    assert provider.provider_id == "acme"
    assert provider.model_id == "small-1"
    assert provider.base_url == "http://x:1"


def test_the_timeout_bounds_the_request_and_the_whole_wait():
    provider = reason_cli.build_provider(
        parsed("--provider", "opencode", "--timeout", "5")
    )

    assert provider.timeout == 5
    assert provider.total_timeout == 5


def test_choosing_opencode_does_not_inherit_ollamas_address():
    provider = reason_cli.build_provider(parsed("--provider", "opencode"))

    assert provider.base_url == "http://127.0.0.1:4096"
    assert provider.model_id == "big-pickle"


def test_the_backend_flags_are_empty_until_a_person_asks_for_them():
    args = parsed()

    assert args.base_url is None
    assert args.model is None
    assert args.provider_id is None


def opencode_server(monkeypatch, reply):
    """A stand-in OpenCode server that answers with one finished message.

    Anything not addressed to OpenCode is refused, so a test can also show which
    backend a run actually reached.
    """
    asked = []
    session_id = "ses_test0000000000000000000"
    messages = [
        {"info": {"id": "u", "role": "user", "time": {"created": 1}}, "parts": []},
        {
            "info": {
                "id": "a",
                "role": "assistant",
                "time": {"created": 2, "completed": 3},
            },
            "parts": [{"type": "text", "text": reply}],
        },
    ]

    def fake_urlopen(request, timeout=None):
        asked.append(request.full_url)
        if not request.full_url.startswith("http://127.0.0.1:4096"):
            raise urllib.error.URLError("connection refused")
        if request.full_url.endswith("/session"):
            return reply_with(json.dumps({"id": session_id}))
        if request.full_url.endswith("/prompt_async"):
            return reply_with(b"")
        return reply_with(json.dumps(messages))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return asked


def reply_with(payload):
    class Response:
        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return Response()


def test_the_cli_reaches_opencode_and_shows_its_proposal(root, monkeypatch):
    asked = opencode_server(
        monkeypatch, StubProvider.with_operations(create_task_payload()).reply
    )

    code, out, _ = run(
        [
            "Nothing",
            "--root",
            str(root),
            "--project",
            "alpha",
            "--provider",
            "opencode",
            "--dry-run",
        ]
    )

    assert code == 0
    assert "CREATE_TASK" in out
    assert "Rejected. No changes were made." in out
    assert asked[0] == "http://127.0.0.1:4096/session"


def test_opencode_is_only_reached_when_it_is_asked_for(root, monkeypatch):
    asked = opencode_server(monkeypatch, StubProvider.with_operations().reply)

    code, _, err = run(
        [
            "Nothing",
            "--root",
            str(root),
            "--project",
            "alpha",
        ],
        answers=["n"],
    )

    assert code == reason_cli.EXIT_REASONING_FAILED
    assert "127.0.0.1:4096" not in " ".join(asked)
    assert "localhost:11434" in err


def test_the_model_and_base_url_are_configurable(root, stub):
    stub(reply=StubProvider.with_operations().reply)

    code, _, _ = run(
        [
            "Nothing",
            "--root",
            str(root),
            "--project",
            "alpha",
            "--model",
            "gemma4:e2b",
            "--base-url",
            "http://example.invalid:11434",
            "--timeout",
            "5",
        ],
        answers=["n"],
    )

    assert code == 0


def test_the_parser_exposes_no_secret_flags():
    options = {
        action.dest
        for action in reason_cli.build_parser()._actions
    }

    for word in ("key", "token", "secret", "password", "credential"):
        assert not any(word in option for option in options)


# --- boundary ------------------------------------------------------------


def imported_modules(source_path):
    tree = ast.parse(Path(source_path).read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_the_cli_does_not_import_storage_or_execution_modules():
    modules = imported_modules(CLI_SOURCE_PATH)

    assert "yaml" not in modules
    assert "core.project_state" not in modules
    assert "core.work_manager" not in modules
    assert "core.project_manager" not in modules
    assert "subprocess" not in modules
    assert "shutil" not in modules
    assert "os" not in modules


def test_the_cli_cannot_run_commands_or_execute_code():
    forbidden_attributes = {
        "system",
        "popen",
        "Popen",
        "execv",
        "execve",
        "spawn",
        "spawnl",
    }
    forbidden_names = {"eval", "exec", "compile", "__import__"}
    tree = ast.parse(CLI_SOURCE_PATH.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Attribute):
                assert function.attr not in forbidden_attributes
            elif isinstance(function, ast.Name):
                assert function.id not in forbidden_names


def test_master_stays_free_of_provider_imports():
    """The deterministic core must not know a model exists."""
    modules = imported_modules(Path(__file__).parent.parent / "core" / "master.py")

    assert modules == {
        "argparse",
        "sys",
        "pathlib",
        "core.project_manager",
        "core.work_manager",
    }


def test_the_reasoning_interface_is_unchanged_by_the_engine():
    """The engine uses the existing gate; it did not widen it."""
    modules = imported_modules(Path(__file__).parent.parent / "core" / "reasoning.py")

    assert modules == {
        "sys",
        "collections.abc",
        "dataclasses",
        "enum",
        "pathlib",
        "types",
        "core.master",
    }


def test_no_layer_below_master_gained_a_model_import():
    """Nothing under Master knows a model exists."""
    core = Path(__file__).parent.parent / "core"
    for name in ("project_state", "project_manager", "work_manager", "master"):
        modules = imported_modules(core / f"{name}.py")
        for provider in ("ollama", "openai", "anthropic", "urllib", "http", "requests"):
            assert provider not in modules, name


def test_approval_state_is_reused_not_reinvented():
    assert reason_cli.ApprovalState is ApprovalState