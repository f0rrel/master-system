import ast
from pathlib import Path

import pytest
import yaml

from core.master import Master
from core.reasoning import (
    SPECS,
    ApprovalState,
    BatchResult,
    InvalidOperationError,
    Operation,
    OperationResult,
    ReasoningInterface,
    RequestError,
    ResultStatus,
)

REASONING_SOURCE_PATH = Path(__file__).parent.parent / "core" / "reasoning.py"

ALPHA = {"id": "alpha", "name": "Alpha", "status": "in_progress"}
BETA = {"id": "beta", "name": "Beta", "status": "planned"}

TASK_ONE = {
    "id": "task-001",
    "milestone": "alpha",
    "title": "First",
    "status": "completed",
    "assigned_to": "master",
}
TASK_TWO = {
    "id": "task-002",
    "milestone": "beta",
    "title": "Second",
    "status": "planned",
}


def write_project(
    root,
    project_id,
    name="A Project",
    status="active",
    milestones=None,
    tasks=None,
):
    root.mkdir(parents=True, exist_ok=True)

    files = {
        "project.yaml": {"id": project_id, "name": name, "status": status},
        "milestones.yaml": {"milestones": milestones or []},
        "tasks.yaml": {"tasks": tasks or []},
    }

    for filename, data in files.items():
        (root / filename).write_text(yaml.safe_dump(data), encoding="utf-8")

    return root


def bytes_of(root):
    return {
        path.name: path.read_bytes()
        for path in sorted(root.iterdir())
        if path.is_file()
    }


def payload(operation, **arguments):
    return dict(operation=operation, **arguments)


@pytest.fixture
def root(tmp_path):
    write_project(
        tmp_path / "projects" / "alpha-project",
        "alpha",
        name="Alpha Project",
        milestones=[ALPHA, BETA],
        tasks=[TASK_ONE, TASK_TWO],
    )
    write_project(
        tmp_path / "projects" / "beta-project",
        "beta",
        name="Beta Project",
        milestones=[ALPHA],
        tasks=[TASK_ONE],
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
    return ReasoningInterface(master)


class RecordingMaster(Master):
    """Master that records the calls made through it, then really performs them."""

    def __init__(self, root=None):
        super().__init__(root)
        self.calls = []

    def status(self, project_id):
        self.calls.append(("status", {"project_id": project_id}))
        return super().status(project_id)

    def create_milestone(self, project_id, milestone_id, name, status="planned"):
        self.calls.append(
            (
                "create_milestone",
                {
                    "project_id": project_id,
                    "milestone_id": milestone_id,
                    "name": name,
                    "status": status,
                },
            )
        )
        return super().create_milestone(project_id, milestone_id, name, status)

    def update_milestone(self, project_id, milestone_id, **changes):
        self.calls.append(
            (
                "update_milestone",
                {"project_id": project_id, "milestone_id": milestone_id, **changes},
            )
        )
        return super().update_milestone(project_id, milestone_id, **changes)

    def create_task(
        self, project_id, task_id, milestone, title, status="planned", assigned_to=None
    ):
        self.calls.append(
            (
                "create_task",
                {
                    "project_id": project_id,
                    "task_id": task_id,
                    "milestone": milestone,
                    "title": title,
                    "status": status,
                    "assigned_to": assigned_to,
                },
            )
        )
        return super().create_task(
            project_id, task_id, milestone, title, status, assigned_to
        )

    def update_task(self, project_id, task_id, **changes):
        self.calls.append(
            (
                "update_task",
                {"project_id": project_id, "task_id": task_id, **changes},
            )
        )
        return super().update_task(project_id, task_id, **changes)


@pytest.fixture
def recording(root):
    return RecordingMaster(root)


@pytest.fixture
def recording_interface(recording):
    return ReasoningInterface(recording)


# --- 1. valid operation construction ------------------------------------


def test_a_create_task_request_becomes_a_valid_operation():
    operation = Operation.propose(
        payload(
            "create_task",
            project_id="alpha",
            task_id="task-003",
            milestone="alpha",
            title="Third",
        )
    )

    assert operation.operation == "create_task"
    assert operation.arguments == {
        "project_id": "alpha",
        "task_id": "task-003",
        "milestone": "alpha",
        "title": "Third",
    }
    assert operation.state is ApprovalState.PROPOSED


def test_every_supported_operation_can_be_constructed():
    requests = [
        payload("inspect_project", project_id="alpha"),
        payload("create_milestone", project_id="alpha", milestone_id="m-1", name="M"),
        payload("update_milestone", project_id="alpha", milestone_id="alpha", name="N"),
        payload(
            "create_task",
            project_id="alpha",
            task_id="t-1",
            milestone="alpha",
            title="T",
        ),
        payload("update_task", project_id="alpha", task_id="task-001", status="planned"),
    ]

    for request in requests:
        operation = Operation.propose(request)
        assert operation.operation == request["operation"]
        assert operation.is_approved is False


def test_optional_arguments_are_accepted_where_master_has_defaults():
    operation = Operation.propose(
        payload(
            "create_task",
            project_id="alpha",
            task_id="task-003",
            milestone="alpha",
            title="Third",
            status="in_progress",
            assigned_to="master",
        )
    )

    assert operation.arguments["status"] == "in_progress"
    assert operation.arguments["assigned_to"] == "master"


def test_an_argument_may_be_explicitly_none():
    operation = Operation.propose(
        payload(
            "create_task",
            project_id="alpha",
            task_id="task-003",
            milestone="alpha",
            title="Third",
            assigned_to=None,
        )
    )

    assert operation.arguments["assigned_to"] is None


def test_proposing_touches_no_state(alpha_path, interface):
    before = bytes_of(alpha_path)

    Operation.propose(
        payload(
            "create_task",
            project_id="alpha",
            task_id="task-003",
            milestone="alpha",
            title="Third",
        )
    )

    assert bytes_of(alpha_path) == before


# --- 2. malformed operation rejected ------------------------------------


@pytest.mark.parametrize(
    "malformed",
    [
        "create_task",
        42,
        None,
        ["create_task"],
        ("create_task",),
        b"create_task",
    ],
)
def test_a_request_that_is_not_a_mapping_is_rejected(malformed):
    with pytest.raises(InvalidOperationError) as caught:
        Operation.propose(malformed)

    assert caught.value.reason is RequestError.NOT_A_MAPPING


def test_a_request_without_an_operation_key_is_rejected():
    for request in ({"project_id": "alpha"}, {7: "create_task"}, {}):
        with pytest.raises(InvalidOperationError) as caught:
            Operation.propose(request)

        assert caught.value.reason is RequestError.MISSING_OPERATION


def test_a_non_string_operation_name_is_rejected():
    with pytest.raises(InvalidOperationError) as caught:
        Operation.propose({"operation": 7, "project_id": "alpha"})

    assert caught.value.reason is RequestError.INVALID_ARGUMENT_VALUE


def test_a_non_scalar_argument_is_rejected():
    with pytest.raises(InvalidOperationError) as caught:
        Operation.propose(
            payload(
                "create_task",
                project_id="alpha",
                task_id="task-003",
                milestone="alpha",
                title="Third",
                status={"nested": "object"},
            )
        )

    assert caught.value.reason is RequestError.INVALID_ARGUMENT_VALUE


# --- 3. unknown operation rejected --------------------------------------


@pytest.mark.parametrize(
    "name",
    ["delete_project", "run_command", "write_yaml", "CREATE_TASK", "", "status"],
)
def test_an_unknown_operation_is_rejected(name):
    with pytest.raises(InvalidOperationError) as caught:
        Operation.propose({"operation": name, "project_id": "alpha"})

    assert caught.value.reason is RequestError.UNKNOWN_OPERATION
    assert caught.value.operation == name


def test_master_method_names_are_not_operations():
    """A reasoning engine may not address Master by its Python method names."""
    for name in ("status", "list_projects", "overview", "problems", "root"):
        with pytest.raises(InvalidOperationError) as caught:
            Operation.propose({"operation": name, "project_id": "alpha"})

        assert caught.value.reason is RequestError.UNKNOWN_OPERATION


# --- 4. missing required argument rejected ------------------------------


@pytest.mark.parametrize(
    "incomplete",
    [
        payload("inspect_project"),
        payload("create_milestone", project_id="alpha", milestone_id="m-1"),
        payload("create_milestone", project_id="alpha", name="M"),
        payload("update_milestone", project_id="alpha"),
        payload("create_task", project_id="alpha", task_id="t", milestone="alpha"),
        payload("create_task", project_id="alpha", milestone="alpha", title="T"),
        payload("update_task", project_id="alpha"),
        payload("update_task", task_id="task-001", status="planned"),
    ],
)
def test_a_missing_required_argument_is_rejected(incomplete):
    with pytest.raises(InvalidOperationError) as caught:
        Operation.propose(incomplete)

    assert caught.value.reason is RequestError.MISSING_ARGUMENT


def test_the_error_names_the_missing_arguments():
    with pytest.raises(InvalidOperationError) as caught:
        Operation.propose(payload("create_task", project_id="alpha"))

    assert "task_id" in str(caught.value)
    assert "milestone" in str(caught.value)
    assert "title" in str(caught.value)


def test_an_update_with_no_field_to_change_is_rejected():
    with pytest.raises(InvalidOperationError) as caught:
        Operation.propose(
            payload("update_task", project_id="alpha", task_id="task-001")
        )

    assert caught.value.reason is RequestError.NO_CHANGES


# --- 5. extra / unsupported argument rejected ---------------------------


@pytest.mark.parametrize(
    "unsupported",
    [
        payload("inspect_project", project_id="alpha", extra=1),
        payload("inspect_project", project_id="alpha", projectPath="/etc/passwd"),
        payload(
            "create_milestone",
            project_id="alpha",
            milestone_id="m-1",
            name="M",
            assigned_to="master",
        ),
        payload(
            "create_task",
            project_id="alpha",
            task_id="task-003",
            milestone="alpha",
            title="Third",
            yaml="project.yaml",
        ),
        payload(
            "create_task",
            project_id="alpha",
            task_id="task-003",
            milestone="alpha",
            title="Third",
            command="rm -rf /",
        ),
    ],
)
def test_an_unsupported_argument_is_rejected(unsupported):
    with pytest.raises(InvalidOperationError) as caught:
        Operation.propose(unsupported)

    assert caught.value.reason is RequestError.UNEXPECTED_ARGUMENT


def test_the_error_names_the_unsupported_arguments():
    with pytest.raises(InvalidOperationError) as caught:
        Operation.propose(
            payload("inspect_project", project_id="alpha", sneaky=True)
        )

    assert "sneaky" in str(caught.value)


# --- 6. proposed operation cannot execute before approval ----------------


def test_a_proposed_operation_does_not_execute(alpha_path, recording_interface):
    before = bytes_of(alpha_path)
    operation = recording_interface.propose(
        payload(
            "create_task",
            project_id="alpha",
            task_id="task-003",
            milestone="alpha",
            title="Third",
        )
    )

    result = recording_interface.execute(operation)

    assert result.status is ResultStatus.REJECTED
    assert result.reason is RequestError.NOT_APPROVED
    assert result.succeeded is False
    assert recording_interface.master.calls == []
    assert bytes_of(alpha_path) == before


def test_a_rejected_operation_does_not_execute(alpha_path, recording_interface):
    before = bytes_of(alpha_path)
    operation = recording_interface.propose(
        payload(
            "create_task",
            project_id="alpha",
            task_id="task-003",
            milestone="alpha",
            title="Third",
        )
    ).reject()

    result = recording_interface.execute(operation)

    assert result.status is ResultStatus.REJECTED
    assert result.reason is RequestError.EXPLICITLY_REJECTED
    assert recording_interface.master.calls == []
    assert bytes_of(alpha_path) == before


def test_proposing_never_approves_automatically(interface):
    operation = interface.propose(payload("inspect_project", project_id="alpha"))

    assert operation.state is ApprovalState.PROPOSED
    assert operation.is_approved is False


def test_approval_and_rejection_return_new_instances(interface):
    operation = interface.propose(payload("inspect_project", project_id="alpha"))

    approved = operation.approve()
    rejected = operation.reject()

    assert operation.state is ApprovalState.PROPOSED
    assert approved is not operation
    assert approved.state is ApprovalState.APPROVED
    assert rejected.state is ApprovalState.REJECTED


def test_operation_arguments_cannot_be_mutated_after_approval(interface):
    operation = interface.propose(payload("inspect_project", project_id="alpha"))

    with pytest.raises(TypeError):
        operation.arguments["project_id"] = "beta"


# --- 7. approved operation reaches Master -------------------------------


def test_an_approved_create_task_reaches_master_create_task(
    recording_interface,
):
    operation = recording_interface.propose(
        payload(
            "create_task",
            project_id="alpha",
            task_id="task-003",
            milestone="alpha",
            title="Third",
            status="in_progress",
            assigned_to="master",
        )
    ).approve()

    result = recording_interface.execute(operation)

    assert result.status is ResultStatus.SUCCESS
    assert recording_interface.master.calls == [
        (
            "create_task",
            {
                "project_id": "alpha",
                "task_id": "task-003",
                "milestone": "alpha",
                "title": "Third",
                "status": "in_progress",
                "assigned_to": "master",
            },
        )
    ]


def test_an_approved_inspect_reaches_master_status(recording_interface):
    operation = recording_interface.propose(
        payload("inspect_project", project_id="alpha")
    ).approve()

    result = recording_interface.execute(operation)

    assert result.status is ResultStatus.SUCCESS
    assert recording_interface.master.calls == [("status", {"project_id": "alpha"})]


def test_updates_pass_their_fields_through_as_changes(recording_interface):
    operation = recording_interface.propose(
        payload("update_task", project_id="alpha", task_id="task-002", status="in_progress")
    ).approve()

    recording_interface.execute(operation)

    assert recording_interface.master.calls == [
        (
            "update_task",
            {"project_id": "alpha", "task_id": "task-002", "status": "in_progress"},
        )
    ]


def test_an_update_cannot_smuggle_a_second_identifier_to_redirect_a_write(
    alpha_path, recording_interface
):
    """A stray spelling cannot outvote the explicit addressing arguments."""
    before = bytes_of(alpha_path)
    operation = recording_interface.propose(
        payload(
            "update_task",
            project_id="alpha",
            task_id="task-002",
            projectId="beta",
        )
    ).approve()

    result = recording_interface.execute(operation)

    # The stray field is treated as a change, and the domain refuses it because
    # it is not a mutable task field. It never replaces project_id/task_id.
    assert result.status is ResultStatus.DOMAIN_ERROR
    assert result.error_type == "InvalidFieldError"

    method, arguments = recording_interface.master.calls[0]
    assert method == "update_task"
    assert arguments["project_id"] == "alpha"
    assert arguments["task_id"] == "task-002"
    assert bytes_of(alpha_path) == before


def test_a_non_variadic_operation_refuses_a_second_identifier_structurally():
    with pytest.raises(InvalidOperationError) as caught:
        Operation.propose(
            payload(
                "create_task",
                project_id="alpha",
                task_id="task-003",
                milestone="alpha",
                title="Third",
                projectId="beta",
            )
        )

    assert caught.value.reason is RequestError.UNEXPECTED_ARGUMENT


# --- 8. Master / domain validation stays authoritative ------------------


def test_a_nonexistent_milestone_is_a_domain_error_not_a_malformed_request(
    interface,
):
    operation = interface.propose(
        payload(
            "create_task",
            project_id="alpha",
            task_id="task-003",
            milestone="does-not-exist",
            title="Third",
        )
    )
    assert operation.is_approved is False

    result = interface.execute(operation.approve())

    assert result.status is ResultStatus.DOMAIN_ERROR
    assert result.error_type == "InvalidFieldError"
    assert result.reason is None


def test_an_invalid_status_is_left_to_the_domain(interface):
    """Status vocabulary belongs to the domain, so this layer accepts it."""
    operation = interface.propose(
        payload(
            "create_task",
            project_id="alpha",
            task_id="task-003",
            milestone="alpha",
            title="Third",
            status="not_a_real_status",
        )
    )

    result = interface.execute(operation.approve())

    assert result.status is ResultStatus.DOMAIN_ERROR
    assert result.error_type == "InvalidFieldError"


def test_a_blank_title_is_left_to_the_domain(interface):
    """Blankness is a domain rule; only a missing key is malformed here."""
    operation = interface.propose(
        payload(
            "create_task",
            project_id="alpha",
            task_id="task-003",
            milestone="alpha",
            title="",
        )
    )

    result = interface.execute(operation.approve())

    assert result.status is ResultStatus.DOMAIN_ERROR
    assert result.error_type == "InvalidFieldError"


def test_an_unknown_project_is_left_to_project_manager(interface):
    operation = interface.propose(
        payload(
            "create_task",
            project_id="no-such-project",
            task_id="task-003",
            milestone="alpha",
            title="Third",
        )
    )

    result = interface.execute(operation.approve())

    assert result.status is ResultStatus.DOMAIN_ERROR
    assert result.error_type == "UnknownProjectError"


def test_a_duplicate_task_is_left_to_the_domain(interface):
    operation = interface.propose(
        payload(
            "create_task",
            project_id="alpha",
            task_id="task-001",
            milestone="alpha",
            title="Clash",
        )
    )

    result = interface.execute(operation.approve())

    assert result.status is ResultStatus.DOMAIN_ERROR
    assert result.error_type == "DuplicateRecordError"


def test_unknown_update_fields_stay_the_domain_s_concern(interface):
    """Field vocabulary lives in WorkManager, so it is rejected there."""
    operation = interface.propose(
        payload("update_task", project_id="alpha", task_id="task-002", nonsense="x")
    )

    result = interface.execute(operation.approve())

    assert result.status is ResultStatus.DOMAIN_ERROR
    assert result.error_type == "InvalidFieldError"


def test_an_unexpected_exception_is_not_disguised_as_a_domain_error(
    interface, monkeypatch
):
    class Boom(RuntimeError):
        pass

    def explode(*args, **kwargs):
        raise Boom("genuine bug")

    monkeypatch.setattr(interface.master, "status", explode)
    operation = interface.propose(
        payload("inspect_project", project_id="alpha")
    ).approve()

    with pytest.raises(Boom):
        interface.execute(operation)


# --- 9. successful execution produces a structured result ---------------


def test_a_successful_create_returns_structured_data(interface, alpha_path):
    operation = interface.propose(
        payload(
            "create_task",
            project_id="alpha",
            task_id="task-003",
            milestone="alpha",
            title="Third",
        )
    ).approve()

    result = interface.execute(operation)

    assert result.status is ResultStatus.SUCCESS
    assert result.succeeded is True
    assert result.error_type is None
    assert result.reason is None
    assert result.value["id"] == "task-003"
    assert result.value["title"] == "Third"
    assert result.value["status"] == "planned"

    from core.project_state import ProjectState

    tasks = ProjectState(alpha_path).tasks()
    assert [task["id"] for task in tasks] == [
        "task-001",
        "task-002",
        "task-003",
    ]


def test_inspect_returns_project_state_as_plain_data(interface):
    operation = interface.propose(
        payload("inspect_project", project_id="alpha")
    ).approve()

    result = interface.execute(operation)

    assert result.status is ResultStatus.SUCCESS
    assert set(result.value) >= {
        "project_id",
        "name",
        "status",
        "progress",
        "milestones",
        "tasks",
    }
    assert result.value["project_id"] == "alpha"
    assert isinstance(result.value["tasks"], list)
    assert isinstance(result.value["milestones"], list)
    assert {task["id"] for task in result.value["tasks"]} == {"task-001", "task-002"}


def test_an_inspection_result_carries_no_live_objects(interface):
    """Plain data only, so a future caller cannot walk back into storage."""
    operation = interface.propose(
        payload("inspect_project", project_id="alpha")
    ).approve()

    value = interface.execute(operation).value

    for record in list(value["tasks"]) + list(value["milestones"]):
        assert type(record) is dict


def test_overview_and_problems_are_reachable_as_inspections(interface):
    """Read-only Master methods are inspectable, not just status."""
    for project_id in ("alpha", "beta"):
        result = interface.execute(
            interface.propose(
                payload("inspect_project", project_id=project_id)
            ).approve()
        )
        assert result.status is ResultStatus.SUCCESS


# --- 10. domain failure produces a structured result -------------------


def test_a_domain_failure_is_reported_without_raising(interface, alpha_path):
    before = bytes_of(alpha_path)
    operation = interface.propose(
        payload(
            "create_task",
            project_id="alpha",
            task_id="task-001",
            milestone="alpha",
            title="Clash",
        )
    ).approve()

    result = interface.execute(operation)

    assert isinstance(result, OperationResult)
    assert result.status is ResultStatus.DOMAIN_ERROR
    assert result.error_type == "DuplicateRecordError"
    assert result.message
    assert bytes_of(alpha_path) == before


def test_the_error_type_is_machine_readable_not_parsed_from_a_message(interface):
    operation = interface.propose(
        payload("update_task", project_id="alpha", task_id="ghost", status="planned")
    ).approve()

    result = interface.execute(operation)

    assert result.status is ResultStatus.DOMAIN_ERROR
    assert result.error_type == "RecordNotFoundError"


# --- 11-14. boundary: no YAML, no ProjectState, no execution, no models -


def imported_modules(source_path):
    tree = ast.parse(Path(source_path).read_text(encoding="utf-8"))
    modules = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)

    return modules


def test_reasoning_interface_does_not_import_yaml_or_project_state():
    """It cannot read or write project files directly, whatever the prose says."""
    modules = imported_modules(REASONING_SOURCE_PATH)

    assert "yaml" not in modules
    assert "core.project_state" not in modules
    assert "core.project_manager" not in modules
    assert "core.work_manager" not in modules


def test_reasoning_interface_imports_only_master_and_stdlib():
    assert imported_modules(REASONING_SOURCE_PATH) == {
        "sys",
        "collections.abc",
        "dataclasses",
        "enum",
        "pathlib",
        "types",
        "core.master",
    }


def test_reasoning_interface_imports_no_execution_or_model_modules():
    modules = imported_modules(REASONING_SOURCE_PATH)

    forbidden = {
        "os",
        "subprocess",
        "shutil",
        "socket",
        "requests",
        "httpx",
        "urllib",
        "http",
        "ollama",
        "openai",
        "anthropic",
        "cohere",
        "google",
        "transformers",
        "torch",
        "sqlite3",
        "asyncio",
        "threading",
        "multiprocessing",
        "importlib",
        "pickle",
        "ctypes",
    }

    assert modules & forbidden == set()


def test_reasoning_interface_makes_no_shell_or_dynamic_execution_calls():
    forbidden_attributes = {
        "system",
        "popen",
        "run",
        "Popen",
        "call",
        "check_call",
        "check_output",
        "spawn",
        "spawnl",
        "execv",
        "execve",
        "fork",
    }
    forbidden_names = {"eval", "exec", "compile", "__import__", "input", "open"}
    tree = ast.parse(REASONING_SOURCE_PATH.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Attribute):
                assert function.attr not in forbidden_attributes
            elif isinstance(function, ast.Name):
                assert function.id not in forbidden_names


def test_reasoning_interface_declares_no_exception_hierarchy():
    """One error type, carrying a reason enum, instead of a parallel hierarchy."""
    source = REASONING_SOURCE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    classes = [
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
    ]

    assert classes.count("InvalidOperationError") == 1
    assert "ReasoningError" not in classes
    assert "DomainError" not in classes


def test_the_interface_holds_only_a_master():
    source = REASONING_SOURCE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    interface_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ReasoningInterface"
    )

    attributes = set()
    for node in ast.walk(interface_class):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and isinstance(node.ctx, ast.Store)
        ):
            attributes.add(node.attr)

    assert attributes == {"_master"}


def test_it_refuses_anything_that_is_not_a_master():
    with pytest.raises(TypeError):
        ReasoningInterface("not a master")

    with pytest.raises(TypeError):
        ReasoningInterface(object())


def test_it_refuses_to_execute_anything_that_is_not_an_operation(interface):
    with pytest.raises(TypeError):
        interface.execute("create_task")

    with pytest.raises(TypeError):
        interface.execute({"operation": "create_task"})


def test_it_cannot_be_handed_a_manager_instead_of_a_master():
    from core.project_manager import ProjectManager
    from core.work_manager import WorkManager

    with pytest.raises(TypeError):
        ReasoningInterface(ProjectManager())

    with pytest.raises(TypeError):
        ReasoningInterface(WorkManager())


# --- 15. rejected operation does not mutate state -----------------------


def test_a_rejected_operation_leaves_every_file_byte_identical(
    alpha_path, interface
):
    before = bytes_of(alpha_path)

    operation = interface.propose(
        payload(
            "create_task",
            project_id="alpha",
            task_id="task-003",
            milestone="alpha",
            title="Third",
        )
    ).reject()

    result = interface.execute(operation)

    assert result.status is ResultStatus.REJECTED
    assert bytes_of(alpha_path) == before


def test_a_rejected_milestone_change_leaves_state_untouched(alpha_path, interface):
    before = bytes_of(alpha_path)

    operation = interface.propose(
        payload(
            "update_milestone",
            project_id="alpha",
            milestone_id="alpha",
            status="completed",
        )
    ).reject()

    interface.execute(operation)

    assert bytes_of(alpha_path) == before


def test_a_batch_of_unapproved_payloads_changes_nothing(alpha_path, interface):
    before = bytes_of(alpha_path)

    batch = interface.execute_batch(
        [
            payload(
                "create_task",
                project_id="alpha",
                task_id="task-003",
                milestone="alpha",
                title="Third",
            )
        ]
    )

    assert [r.status for r in batch.results] == [ResultStatus.REJECTED]
    assert bytes_of(alpha_path) == before


# --- batch behaviour: partial success, no implied transaction -----------


def test_batch_execution_continues_past_a_failure_and_reports_each_result(
    alpha_path, interface
):
    operations = [
        interface.propose(
            payload(
                "create_task",
                project_id="alpha",
                task_id="task-003",
                milestone="alpha",
                title="Third",
            )
        ).approve(),
        interface.propose(
            payload(
                "create_task",
                project_id="alpha",
                task_id="task-004",
                milestone="alpha",
                title="Fourth",
            )
        ).approve(),
        interface.propose(
            payload(
                "create_task",
                project_id="alpha",
                task_id="task-005",
                milestone="ghost-milestone",
                title="Fifth",
            )
        ).approve(),
    ]

    batch = interface.execute_batch(operations)

    assert isinstance(batch, BatchResult)
    assert len(batch) == 3
    assert [r.status for r in batch.results] == [
        ResultStatus.SUCCESS,
        ResultStatus.SUCCESS,
        ResultStatus.DOMAIN_ERROR,
    ]
    assert batch.all_successful is False
    assert len(batch.succeeded) == 2
    assert len(batch.failed) == 1
    assert batch.failed[0].error_type == "InvalidFieldError"


def test_a_failed_batch_leaves_earlier_writes_applied(alpha_path, interface):
    """Not a transaction: two tasks exist and the third never happened."""
    operations = [
        interface.propose(
            payload(
                "create_task",
                project_id="alpha",
                task_id="task-003",
                milestone="alpha",
                title="Third",
            )
        ).approve(),
        interface.propose(
            payload(
                "create_task",
                project_id="alpha",
                task_id="task-004",
                milestone="ghost-milestone",
                title="Fourth",
            )
        ).approve(),
    ]

    interface.execute_batch(operations)

    from core.project_state import ProjectState

    tasks = ProjectState(alpha_path).tasks()
    assert [task["id"] for task in tasks] == [
        "task-001",
        "task-002",
        "task-003",
    ]


def test_a_batch_reports_a_malformed_payload_without_aborting(alpha_path, interface):
    operations = [
        payload("inspect_project", project_id="alpha"),
        {"operation": "not_an_operation"},
        payload("inspect_project", project_id="beta"),
    ]

    batch = interface.execute_batch(operations)

    assert [r.status for r in batch.results] == [
        ResultStatus.REJECTED,
        ResultStatus.INVALID_REQUEST,
        ResultStatus.REJECTED,
    ]
    assert batch.results[1].reason is RequestError.UNKNOWN_OPERATION
    assert batch.all_successful is False


def test_a_batch_accepts_approved_operations_and_raw_payloads_together(
    recording_interface,
):
    batch = recording_interface.execute_batch(
        [
            recording_interface.propose(
                payload("inspect_project", project_id="alpha")
            ).approve(),
            payload("inspect_project", project_id="beta"),
        ]
    )

    assert [r.status for r in batch.results] == [
        ResultStatus.SUCCESS,
        ResultStatus.REJECTED,
    ]
    assert recording_interface.master.calls == [("status", {"project_id": "alpha"})]


def test_an_empty_batch_is_vacuously_successful(interface):
    batch = interface.execute_batch([])

    assert len(batch) == 0
    assert batch.all_successful is True


# --- the allowlist cannot drift from Master -----------------------------


def test_every_operation_maps_to_a_method_master_already_has():
    for name, spec in SPECS.items():
        assert hasattr(Master, spec.method), f"{name} -> Master.{spec.method}"


def test_the_argument_table_matches_masters_real_signatures():
    """If Master gains or renames a parameter, this must be updated too."""
    import inspect

    for name, spec in SPECS.items():
        parameters = [
            parameter
            for parameter in inspect.signature(
                getattr(Master, spec.method)
            ).parameters.values()
            if parameter.name != "self"
        ]
        named = [
            parameter
            for parameter in parameters
            if parameter.kind
            in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY)
        ]

        required = [
            parameter.name
            for parameter in named
            if parameter.default is parameter.empty
        ]
        optional = [
            parameter.name
            for parameter in named
            if parameter.default is not parameter.empty
        ]

        assert required == list(spec.required), name
        assert optional == list(spec.optional), name

        accepts_changes = any(
            parameter.kind is parameter.VAR_KEYWORD for parameter in parameters
        )
        assert accepts_changes is spec.variadic, name


def test_the_conformance_check_would_notice_a_renamed_parameter(monkeypatch):
    """Proves the drift guard above is load-bearing, not decorative."""
    import inspect

    def renamed(self, project_id, task_id, milestone, name, status="planned", assigned_to=None):
        raise AssertionError("not reached")

    monkeypatch.setattr(Master, "create_task", renamed)

    spec = SPECS["create_task"]
    parameters = [
        parameter.name
        for parameter in inspect.signature(Master.create_task).parameters.values()
        if parameter.name != "self"
    ]

    assert parameters != list(spec.required) + list(spec.optional)


def test_the_allowlist_is_exactly_the_documented_operations():
    assert set(SPECS) == {
        "inspect_project",
        "create_milestone",
        "update_milestone",
        "create_task",
        "update_task",
    }


def test_the_allowlist_does_not_grow_by_reflecting_over_master():
    """Adding a Master method must not silently widen what may be called."""
    public = {
        name
        for name in vars(Master)
        if not name.startswith("_") and callable(getattr(Master, name))
    }
    exposed = {spec.method for spec in SPECS.values()}

    assert exposed < public
    assert "list_projects" not in exposed
    assert "overview" not in exposed
    assert "problems" not in exposed


def test_the_spec_table_cannot_be_mutated_by_a_caller():
    with pytest.raises(TypeError):
        SPECS["delete_project"] = None