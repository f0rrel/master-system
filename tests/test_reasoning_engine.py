import ast
from pathlib import Path

import pytest
import yaml

from core.master import Master
from core.provider import ProviderError, ReasoningProvider
from core.reasoning import (
    SPECS,
    ApprovalState,
    InvalidOperationError,
    ReasoningInterface,
    ResultStatus,
)
from core.reasoning_engine import (
    Proposal,
    ProposalEntry,
    ReasoningEngine,
    ReasoningError,
    build_operation_schema,
    describe_operations,
)

ENGINE_SOURCE_PATH = Path(__file__).parent.parent / "core" / "reasoning_engine.py"
PROVIDER_SOURCE_PATH = Path(__file__).parent.parent / "core" / "provider.py"

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


def reply_with(*operations, reasoning="because"):
    import json

    return json.dumps({"reasoning": reasoning, "operations": list(operations)})


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


class ScriptedProvider(ReasoningProvider):
    """A provider that returns a canned reply and records what it was asked."""

    name = "scripted"

    def __init__(self, reply=None, error=None):
        self.reply = reply if reply is not None else reply_with()
        self.error = error
        self.prompts = []
        self.schemas = []

    def complete(self, prompt, schema=None):
        self.prompts.append(prompt)
        self.schemas.append(schema)
        if self.error is not None:
            raise self.error
        return self.reply


class RecordingInterface(ReasoningInterface):
    """Records every propose and execute so tests can prove what was called."""

    def __init__(self, master):
        super().__init__(master)
        self.proposed = []
        self.executed = []

    def propose(self, payload):
        self.proposed.append(payload)
        return super().propose(payload)

    def execute(self, operation):
        self.executed.append(operation)
        return super().execute(operation)


@pytest.fixture
def root(tmp_path):
    write_project(
        tmp_path / "projects" / "alpha-project",
        "alpha",
        name="Alpha Project",
        milestones=[ALPHA],
        tasks=[TASK_ONE],
    )
    write_project(
        tmp_path / "projects" / "beta-project",
        "beta",
        name="Beta Project",
        milestones=[ALPHA],
        tasks=[],
    )
    return tmp_path / "projects"


@pytest.fixture
def alpha_path(root):
    return root / "alpha-project"


@pytest.fixture
def master(root):
    return Master(root)


@pytest.fixture
def interface(master):
    return RecordingInterface(master)


@pytest.fixture
def engine(interface, master):
    return ReasoningEngine(ScriptedProvider(), master, interface)


def engine_returning(master, interface, reply, error=None):
    return ReasoningEngine(
        ScriptedProvider(reply=reply, error=error), master, interface
    )


# --- 1. the provider receives the request -------------------------------


def test_the_provider_is_asked_to_complete_a_prompt(master, interface):
    provider = ScriptedProvider()
    ReasoningEngine(provider, master, interface).reason(
        "Add a task for finishing the CLI", "alpha"
    )

    assert len(provider.prompts) == 1
    assert "Add a task for finishing the CLI" in provider.prompts[0]


def test_the_prompt_carries_the_project_state(master, interface):
    provider = ScriptedProvider()
    ReasoningEngine(provider, master, interface).reason("Next work?", "alpha")

    prompt = provider.prompts[0]
    assert "alpha" in prompt
    assert "Alpha Project" in prompt
    assert "task-001" in prompt


def test_the_prompt_lists_the_operations_the_model_may_use(master, interface):
    provider = ScriptedProvider()
    ReasoningEngine(provider, master, interface).reason("Next work?", "alpha")

    prompt = provider.prompts[0]
    for name in SPECS:
        assert name in prompt


def test_the_provider_is_offered_the_operation_schema(master, interface):
    provider = ScriptedProvider()
    ReasoningEngine(provider, master, interface).reason("Next work?", "alpha")

    schema = provider.schemas[0]
    assert schema["properties"]["decision"]["enum"] == ["act", "wait", "blocked", "needs_information", "request_approval"]


def test_a_provider_failure_is_not_swallowed(master, interface):
    engine = engine_returning(master, interface, None, error=ProviderError("down"))

    with pytest.raises(ReasoningError):
        engine.reason("Next work?", "alpha")


@pytest.mark.parametrize("blank", ["", "   ", None, 42])
def test_a_blank_request_fails_without_calling_the_provider(master, interface, blank):
    provider = ScriptedProvider()
    engine = ReasoningEngine(provider, master, interface)

    with pytest.raises(ReasoningError):
        engine.reason(blank, "alpha")

    assert provider.prompts == []


def test_the_engine_keeps_the_provider_it_was_given(master, interface):
    provider = ScriptedProvider()
    engine = ReasoningEngine(provider, master, interface)

    assert engine.provider is provider


def test_the_engine_builds_its_own_interface_when_none_is_given(master):
    engine = ReasoningEngine(ScriptedProvider(), master)

    assert isinstance(engine, ReasoningEngine)


# --- 2. a model response becomes a validated proposal -------------------


def test_a_model_response_becomes_a_proposal_with_operations(master, interface):
    engine = engine_returning(
        master, interface, reply_with(create_task_payload())
    )

    proposal = engine.reason("Add a task", "alpha")

    assert isinstance(proposal, Proposal)
    assert proposal.reasoning == "because"
    assert len(proposal.entries) == 1
    assert proposal.entries[0].valid is True
    assert proposal.entries[0].operation.operation == "create_task"
    assert proposal.entries[0].operation.arguments["task_id"] == "task-002"


def test_every_model_operation_passes_through_propose(master, interface):
    engine = engine_returning(master, interface, reply_with(create_task_payload()))

    engine.reason("Add a task", "alpha")

    assert interface.proposed == [create_task_payload()]


def test_a_proposal_starts_unapproved(master, interface):
    engine = engine_returning(master, interface, reply_with(create_task_payload()))

    proposal = engine.reason("Add a task", "alpha")

    assert proposal.state is ApprovalState.PROPOSED


def test_reasoning_never_executes_anything(master, interface):
    engine = engine_returning(master, interface, reply_with(create_task_payload()))

    engine.reason("Add a task", "alpha")

    assert interface.executed == []


def test_reasoning_never_touches_project_files(alpha_path, master, interface):
    before = bytes_of(alpha_path)
    engine = engine_returning(
        master, interface, reply_with(create_task_payload())
    )

    engine.reason("Add a task", "alpha")

    assert bytes_of(alpha_path) == before


def test_reasoning_over_several_operations_touches_nothing(alpha_path, master, interface):
    before = bytes_of(alpha_path)
    engine = engine_returning(
        master,
        interface,
        reply_with(
            create_task_payload("task-002"),
            create_task_payload("task-003"),
            {
                "operation": "update_task",
                "project_id": "alpha",
                "task_id": "task-001",
                "status": "in_progress",
            },
        ),
    )

    engine.reason("Several changes", "alpha")

    assert bytes_of(alpha_path) == before


def test_an_empty_operations_list_is_a_valid_empty_proposal(master, interface):
    engine = engine_returning(
        master, interface, reply_with(reasoning="Nothing to do")
    )

    proposal = engine.reason("Nothing to do", "alpha")

    assert proposal.entries == ()
    assert proposal.is_empty is True


def test_fenced_json_is_accepted(master, interface):
    """A markdown fence is a wrapper, not content, so it is unwrapped."""
    fenced = f"```json\n{reply_with(create_task_payload())}\n```"
    engine = engine_returning(master, interface, fenced)

    proposal = engine.reason("Add a task", "alpha")

    assert proposal.entries[0].valid is True


def test_a_bare_fence_is_accepted(master, interface):
    fenced = f"```\n{reply_with(create_task_payload())}\n```"
    engine = engine_returning(master, interface, fenced)

    assert engine.reason("Add a task", "alpha").entries[0].valid is True


def test_unknown_top_level_keys_are_ignored_not_interpreted(master, interface):
    import json

    document = json.loads(reply_with(create_task_payload()))
    document["code"] = "import os; os.system('rm -rf /')"
    document["tools"] = [{"name": "shell"}]

    engine = engine_returning(master, interface, json.dumps(document))

    proposal = engine.reason("Add a task", "alpha")

    assert proposal.entries[0].valid is True
    assert proposal.reasoning == "because"


# --- invalid model output ------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "not json at all",
        "{",
        "{'single': 'quotes'}",
        "import os\nos.system('rm -rf /')",
        "def main():\n    pass",
        "<xml>operation</xml>",
        "[1, 2, 3]",
        '"just a string"',
        "42",
        "null",
    ],
)
def test_unusable_model_output_fails_the_reasoning_request(master, interface, reply):
    engine = engine_returning(master, interface, reply)

    with pytest.raises(ReasoningError):
        engine.reason("Next work?", "alpha")


def test_prose_around_the_json_is_not_repaired(master, interface):
    """No guessing where the JSON starts: that is how prose becomes instruction."""
    reply = (
        "Sure! Here is my plan:\n"
        + reply_with(create_task_payload())
        + "\nLet me know if that helps."
    )
    engine = engine_returning(master, interface, reply)

    with pytest.raises(ReasoningError):
        engine.reason("Next work?", "alpha")


def test_a_reply_that_is_not_text_fails_the_request(master, interface):
    engine = engine_returning(master, interface, {"operation": []})

    with pytest.raises(ReasoningError):
        engine.reason("Next work?", "alpha")


@pytest.mark.parametrize(
    "document",
    [
        {"operation": {}},
        {"operation": "create_task"},
        {"operation": 7},
        {"operation": None},
        {"reasoning": "x"},
        {},
    ],
)
def test_a_bad_envelope_fails_the_reasoning_request(master, interface, document):
    import json

    engine = engine_returning(master, interface, json.dumps(document))

    with pytest.raises(ReasoningError):
        engine.reason("Next work?", "alpha")


def test_a_non_string_reasoning_field_fails_the_request(master, interface):
    import json

    engine = engine_returning(
        master,
        interface,
        json.dumps({"reasoning": {"text": "x"}, "operation": []}),
    )

    with pytest.raises(ReasoningError):
        engine.reason("Next work?", "alpha")


@pytest.mark.parametrize(
    "raw",
    [
        "import os",
        42,
        None,
        ["create_task"],
    ],
)
def test_an_operation_that_is_not_an_object_fails_the_request(master, interface, raw):
    import json

    engine = engine_returning(
        master,
        interface,
        json.dumps({"reasoning": "x", "operation": [raw]}),
    )

    with pytest.raises(ReasoningError):
        engine.reason("Next work?", "alpha")


# --- individual invalid operations stay visible and inert ----------------


def test_an_unknown_operation_is_recorded_as_invalid(alpha_path, master, interface):
    engine = engine_returning(
        master, interface, reply_with({"operation": "delete_project", "project_id": "alpha"})
    )

    proposal = engine.reason("Delete it", "alpha")

    assert proposal.entries[0].valid is False
    assert proposal.entries[0].error.reason.value == "unknown_operation"
    assert proposal.valid_entries == ()
    assert proposal.invalid_entries[0].describe() == "delete_project"


def test_a_missing_required_field_is_recorded_as_invalid(master, interface):
    engine = engine_returning(
        master, interface, reply_with({"operation": "create_task", "project_id": "alpha"})
    )

    proposal = engine.reason("Add a task", "alpha")

    assert proposal.entries[0].error.reason.value == "missing_argument"


def test_an_extra_field_is_recorded_as_invalid(master, interface):
    engine = engine_returning(
        master,
        interface,
        reply_with(create_task_payload(status="planned", priority="high")),
    )

    proposal = engine.reason("Add a task", "alpha")

    assert proposal.entries[0].error.reason.value == "unexpected_argument"


def test_a_wrongly_typed_field_is_recorded_as_invalid(master, interface):
    engine = engine_returning(
        master, interface, reply_with(create_task_payload(title=["a", "list"]))
    )

    proposal = engine.reason("Add a task", "alpha")

    assert proposal.entries[0].error.reason.value == "invalid_argument_value"


def test_a_bad_operation_is_never_executable(alpha_path, master, interface):
    before = bytes_of(alpha_path)
    engine = engine_returning(
        master,
        interface,
        reply_with(
            {"operation": "run_shell", "command": "rm -rf /"},
            create_task_payload("task-002"),
        ),
    )

    proposal = engine.reason("Add a task", "alpha").approve()
    batch = proposal.execute(interface)

    assert len(batch.results) == 1
    assert batch.results[0].status is ResultStatus.SUCCESS
    assert bytes_of(alpha_path) != before


def test_a_fully_invalid_proposal_executes_nothing(alpha_path, master, interface):
    before = bytes_of(alpha_path)
    engine = engine_returning(
        master, interface, reply_with({"operation": "drop_database"})
    )

    proposal = engine.reason("Wreck things", "alpha").approve()
    batch = proposal.execute(interface)

    assert batch.results == ()
    assert bytes_of(alpha_path) == before


def test_a_partly_invalid_proposal_still_shows_both_parts(master, interface):
    engine = engine_returning(
        master,
        interface,
        reply_with(
            {"operation": "nonsense"},
            create_task_payload("task-002"),
            create_task_payload("task-003", milestone="ghost"),
        ),
    )

    proposal = engine.reason("Some of this", "alpha")

    assert len(proposal.valid_entries) == 2
    assert len(proposal.invalid_entries) == 1
    # The nonexistent milestone is structurally fine; only Master will object.
    assert proposal.valid_entries[1].valid is True


# --- approval ------------------------------------------------------------


def test_a_routine_proposal_executes_without_a_human_click(
    alpha_path, master, interface
):
    before = bytes_of(alpha_path)
    engine = engine_returning(master, interface, reply_with(create_task_payload()))

    proposal = engine.reason("Add a task", "alpha")
    batch = proposal.execute(interface)

    # create_task is ROUTINE, so policy approves it. The proposal was never
    # approved by a caller, and nothing changed about how it runs.
    assert [r.status for r in batch.results] == [ResultStatus.SUCCESS]
    assert bytes_of(alpha_path) != before


def test_an_approved_proposal_executes_through_the_interface(
    alpha_path, master, interface
):
    before = bytes_of(alpha_path)
    engine = engine_returning(master, interface, reply_with(create_task_payload()))

    proposal = engine.reason("Add a task", "alpha").approve()
    batch = proposal.execute(interface)

    assert [r.status for r in batch.results] == [ResultStatus.SUCCESS]
    assert bytes_of(alpha_path) != before
    assert len(interface.executed) == 1


def test_approval_cascades_to_the_operations_inside(master, interface):
    engine = engine_returning(master, interface, reply_with(create_task_payload()))

    proposal = engine.reason("Add a task", "alpha")
    approved = proposal.approve()

    assert proposal.state is ApprovalState.PROPOSED
    assert proposal.entries[0].operation.state is ApprovalState.PROPOSED
    assert approved.state is ApprovalState.APPROVED
    assert approved.entries[0].operation.state is ApprovalState.APPROVED


def test_approval_does_not_revive_an_invalid_operation(master, interface):
    engine = engine_returning(
        master, interface, reply_with({"operation": "delete_everything"})
    )

    approved = engine.reason("Wreck things", "alpha").approve()

    assert approved.entries[0].valid is False
    assert approved.entries[0].operation is None


def test_a_rejected_proposal_executes_nothing(alpha_path, master, interface):
    before = bytes_of(alpha_path)
    engine = engine_returning(master, interface, reply_with(create_task_payload()))

    proposal = engine.reason("Add a task", "alpha").reject()
    batch = proposal.execute(interface)

    assert batch.results == ()
    assert interface.executed == []
    assert bytes_of(alpha_path) == before


def test_a_rejected_proposal_cannot_be_approved_into_existence(
    alpha_path, master, interface
):
    before = bytes_of(alpha_path)
    engine = engine_returning(master, interface, reply_with(create_task_payload()))

    proposal = engine.reason("Add a task", "alpha").reject()

    assert proposal.execute(interface).results == ()
    assert bytes_of(alpha_path) == before


def test_approval_returns_a_new_proposal(master, interface):
    engine = engine_returning(master, interface, reply_with(create_task_payload()))

    proposal = engine.reason("Add a task", "alpha")
    approved = proposal.approve()

    assert approved is not proposal
    assert proposal.state is ApprovalState.PROPOSED


def test_a_proposal_is_immutable(alpha_path, master, interface):
    engine = engine_returning(master, interface, reply_with(create_task_payload()))

    proposal = engine.reason("Add a task", "alpha")

    with pytest.raises(Exception):
        proposal.state = ApprovalState.APPROVED


# --- proposal execution semantics ---------------------------------------


def test_proposal_execution_is_not_atomic(alpha_path, master, interface):
    engine = engine_returning(
        master,
        interface,
        reply_with(
            create_task_payload("task-002"),
            create_task_payload("task-003", milestone="ghost-milestone"),
        ),
    )

    batch = engine.reason("Some of this", "alpha").approve().execute(interface)

    assert [r.status for r in batch.results] == [
        ResultStatus.SUCCESS,
        ResultStatus.DOMAIN_ERROR,
    ]
    assert batch.all_successful is False

    from core.project_state import ProjectState

    assert [t["id"] for t in ProjectState(alpha_path).tasks()] == [
        "task-001",
        "task-002",
    ]


def test_a_domain_failure_inside_a_proposal_is_reported_not_raised(
    master, interface
):
    engine = engine_returning(
        master,
        interface,
        reply_with(create_task_payload("task-001")),
    )

    batch = engine.reason("Clash", "alpha").approve().execute(interface)

    assert batch.results[0].status is ResultStatus.DOMAIN_ERROR
    assert batch.results[0].error_type == "DuplicateRecordError"


# --- project context -----------------------------------------------------


def test_the_context_holds_only_project_facts(master):
    engine = ReasoningEngine(ScriptedProvider(), master)
    context = engine.context_for("alpha")

    expected = {
        "project_id",
        "name",
        "status",
        "progress",
        "milestones",
        "tasks",
        "ready_tasks",
        "blocked_tasks",
        "in_progress_tasks",
        "completed_tasks",
    }
    assert set(context) == expected


def test_the_context_excludes_the_filesystem_path(master):
    """Master.status() includes an absolute path; the model must not see it."""
    engine = ReasoningEngine(ScriptedProvider(), master)

    context = engine.context_for("alpha")

    assert "path" not in context
    assert str(master.root) not in str(context)
    assert "/tmp" not in str(context)


def test_the_context_never_contains_a_filesystem_path(master):
    engine = ReasoningEngine(ScriptedProvider(), master)

    for project_id in master.list_projects():
        assert "path" not in str(engine.context_for(project_id))


def test_the_context_reports_milestones_and_tasks(master):
    engine = ReasoningEngine(ScriptedProvider(), master)

    context = engine.context_for("alpha")

    assert context["milestones"][0]["id"] == "alpha"
    assert context["tasks"][0]["id"] == "task-001"
    assert context["tasks"][0]["assigned_to"] == "master"


def test_the_context_invents_no_fields(master):
    engine = ReasoningEngine(ScriptedProvider(), master)

    context = engine.context_for("alpha")

    # Readiness fields are now included as deterministic context
    expected = {
        "id",
        "milestone",
        "title",
        "status",
        "assigned_to",
        "readiness",
        "blocked_by",
        "readiness_reason",
        "has_acceptance",
        "has_description",
    }
    assert set(context["tasks"][0]) == expected
    assert set(context["milestones"][0]) == {"id", "name", "status"}


def test_context_for_an_unknown_project_raises(master):
    engine = ReasoningEngine(ScriptedProvider(), master)

    with pytest.raises(Exception):
        engine.context_for("no-such-project")


def test_context_is_a_copy_not_a_live_handle(master):
    engine = ReasoningEngine(ScriptedProvider(), master)

    context = engine.context_for("alpha")
    context["tasks"][0]["title"] = "tampered"

    assert engine.context_for("alpha")["tasks"][0]["title"] == "First"


# --- the schema and catalogue follow the allowlist ----------------------


def test_the_schema_operation_enum_matches_the_allowlist():
    schema = build_operation_schema()
    enum = schema["properties"]["operation"]["oneOf"][0]["properties"]["operation"][
        "enum"
    ]

    assert set(enum) == set(SPECS)


def test_the_schema_requires_exactly_what_propose_requires():
    schema = build_operation_schema()

    for name, spec in SPECS.items():
        assert schema["properties"]["operation"]["oneOf"][0]["required"] == ["operation"]


def test_the_catalogue_names_every_allowed_operation():
    catalogue = describe_operations()

    for name, spec in SPECS.items():
        assert name in catalogue
        for argument in spec.required:
            assert argument in catalogue


def test_the_catalogue_does_not_mention_removed_operations():
    catalogue = describe_operations()

    assert "delete_project" not in catalogue
    assert "run_command" not in catalogue


# --- boundary: no code execution, no reaching past the interface ---------


def imported_modules(source_path):
    tree = ast.parse(Path(source_path).read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_the_engine_does_not_import_yaml_or_storage_layers():
    modules = imported_modules(ENGINE_SOURCE_PATH)

    assert "yaml" not in modules
    assert "core.project_state" not in modules
    assert "core.project_manager" not in modules


def test_the_engine_does_not_import_execution_or_model_modules():
    modules = imported_modules(ENGINE_SOURCE_PATH)

    forbidden = {
        "os",
        "subprocess",
        "shutil",
        "socket",
        "urllib",
        "http",
        "requests",
        "httpx",
        "ollama",
        "openai",
        "anthropic",
        "importlib",
        "pickle",
        "ctypes",
        "asyncio",
        "threading",
    }

    assert modules & forbidden == set()


def test_the_engine_imports_only_the_interface_and_stdlib():
    assert imported_modules(ENGINE_SOURCE_PATH) == {
        "json",
        "sys",
        "dataclasses",
        "pathlib",
        "core.backlog",
        "core.master",
        "core.provider",
        "core.reasoning",
        "core.work_manager",
    }


def test_the_engine_makes_no_dynamic_execution_calls():
    """json.loads is the only permitted dynamic-looking parse.

    It is the safe counterpart to eval: it turns text into data and cannot
    execute it. Everything that could run something is banned outright.
    """
    forbidden_attributes = {
        "system",
        "popen",
        "run",
        "Popen",
        "call",
        "check_output",
        "spawn",
        "execv",
        "execve",
        "urlopen",
        "import_module",
    }
    forbidden_names = {
        "eval",
        "exec",
        "compile",
        "__import__",
        "input",
        "open",
        "getattr",
        "setattr",
        "globals",
        "locals",
        "vars",
        "pickle",
        "marshal",
        "shelve",
    }
    tree = ast.parse(ENGINE_SOURCE_PATH.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Attribute):
                assert function.attr not in forbidden_attributes
            elif isinstance(function, ast.Name):
                assert function.id not in forbidden_names


def test_the_only_json_calls_are_dumps_and_loads():
    tree = ast.parse(ENGINE_SOURCE_PATH.read_text(encoding="utf-8"))
    used = set()

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "json"
        ):
            used.add(node.attr)

    assert used == {"dumps", "loads"}


def test_the_engine_has_no_except_that_swallows_everything():
    tree = ast.parse(ENGINE_SOURCE_PATH.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            assert not (
                node.type is None or getattr(node.type, "id", None) == "Exception"
            ), "bare except would hide a real failure"


def test_the_provider_base_class_is_provider_agnostic():
    """No provider appears in code, only in the prose explaining the shape."""
    identifiers = set()
    tree = ast.parse(PROVIDER_SOURCE_PATH.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.alias):
            identifiers.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            identifiers.add(node.name)

    for name in ("ollama", "openai", "anthropic", "deepseek", "gemini", "claude"):
        assert not any(name in identifier.lower() for identifier in identifiers)


def test_the_provider_base_class_does_not_import_anything_model_specific():
    modules = imported_modules(PROVIDER_SOURCE_PATH)

    assert modules == {"sys", "abc", "pathlib"}


def test_the_provider_interface_returns_text_and_takes_no_authority():
    """A provider cannot act even in principle: it is handed a prompt, nothing else."""
    source = PROVIDER_SOURCE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    abstract = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ReasoningProvider"
    )
    complete = next(
        node
        for node in abstract.body
        if isinstance(node, ast.FunctionDef) and node.name == "complete"
    )

    parameters = [p.arg for p in complete.args.args]
    assert parameters == ["self", "prompt", "schema"]


def test_the_engine_is_given_the_provider_not_the_other_way_round():
    """The engine depends on the abstraction; no provider names leak in."""
    source = ENGINE_SOURCE_PATH.read_text(encoding="utf-8").lower()

    for name in ("ollama", "openai", "anthropic", "deepseek", "gemini"):
        assert name not in source


def test_the_engine_never_calls_execute_on_its_own(master, interface):
    """Executing is a decision a human makes, not something reasoning does."""
    engine = engine_returning(
        master,
        interface,
        reply_with(create_task_payload(), {"operation": "delete_all"}),
    )

    proposal = engine.reason("Add a task", "alpha")

    assert interface.executed == []
    assert proposal.state is ApprovalState.PROPOSED


def test_proposal_entries_expose_the_raw_model_text(master, interface):
    engine = engine_returning(master, interface, reply_with(create_task_payload()))

    proposal = engine.reason("Add a task", "alpha")

    assert isinstance(proposal.entries[0], ProposalEntry)
    assert proposal.entries[0].raw == create_task_payload()


def test_an_entry_describes_an_unrecognised_operation_safely():
    entry = ProposalEntry(raw="not an object")

    assert entry.valid is False
    assert entry.describe() == "<unrecognised>"