import ast
from pathlib import Path

import pytest
import yaml

from core.master import Master
from core.project_manager import ProjectManager, UnknownProjectError
from core.project_state import ProjectState
from core.work_manager import (
    DuplicateRecordError,
    InvalidFieldError,
    RecordNotFoundError,
    WorkManager,
    WorkManagerError,
)

MASTER_SOURCE_PATH = Path(__file__).parent.parent / "core" / "master.py"


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
        status="paused",
        milestones=[{"id": "solo", "name": "Solo", "status": "planned"}],
        tasks=[],
    )

    return tmp_path / "projects"


@pytest.fixture
def master(root):
    return Master(root)


@pytest.fixture
def alpha_path(root):
    return root / "alpha-project"


def test_master_uses_a_configurable_root(root):
    master = Master(root)

    assert master.root == root
    assert master.list_projects() == ["alpha", "beta"]


def test_master_rejects_a_missing_root(tmp_path):
    with pytest.raises(FileNotFoundError):
        Master(tmp_path / "absent")


def test_master_defaults_to_the_repository_projects_root():
    master = Master()

    assert master.root.is_dir()
    assert "sample-project" in master.list_projects()


def test_status_reports_identity_and_progress(master):
    status = master.status("alpha")

    assert status["project_id"] == "alpha"
    assert status["name"] == "Alpha Project"
    assert status["status"] == "active"
    assert status["path"].endswith("alpha-project")
    assert status["progress"]["total_tasks"] == 2
    assert status["progress"]["total_milestones"] == 2
    assert status["progress"]["task_status_counts"]["completed"] == 1


def test_status_includes_full_work_lists(master):
    status = master.status("alpha")

    assert status["milestones"] == [ALPHA, BETA]
    assert status["tasks"] == [TASK_ONE, TASK_TWO]


def test_status_returns_plain_data_not_state_objects(master):
    status = master.status("alpha")

    assert isinstance(status, dict)

    for value in (status["milestones"], status["tasks"], status["progress"]):
        assert isinstance(value, (dict, list))

    assert not any(
        isinstance(value, ProjectState) for value in status.values()
    )


def test_status_of_a_project_with_no_work(master):
    status = master.status("beta")

    assert status["tasks"] == []
    assert status["progress"]["total_tasks"] == 0
    assert status["progress"]["total_milestones"] == 1


def test_overview_is_delegated_and_complete(master):
    overview = master.overview()

    assert overview["project_count"] == 2
    assert overview["problem_count"] == 0
    assert overview["totals"]["tasks"] == 2
    assert overview["totals"]["milestones"] == 3
    assert overview["totals"]["task_status_counts"]["completed"] == 1
    assert overview == ProjectManager(master.root).overview()


def test_problems_is_delegated(root, master):
    write_project(
        root / "broken-project",
        "broken",
        tasks=[dict(TASK_ONE, status="finished-ish")],
    )

    problems = master.problems()

    assert len(problems) == 1
    assert problems[0]["kind"] == "unreadable"
    assert problems[0]["path"].endswith("broken-project")
    assert "finished-ish" in problems[0]["error"]
    assert master.list_projects() == ["alpha", "beta"]


def test_unreadable_projects_report_their_declared_id(root, master):
    """A broken project is distinguishable from a nonexistent one.

    Master inherits this from ProjectManager: the declared id is recovered for
    diagnosis, so a reasoning layer can tell "this project is invalid" from
    "this project does not exist" and react differently. Recovering the id
    does not make the project usable.
    """
    write_project(
        root / "broken-project",
        "broken",
        tasks=[dict(TASK_ONE, status="finished-ish")],
    )

    problem = master.problems()[0]

    assert problem["project_id"] == "broken"
    assert "finished-ish" in problem["error"]
    assert "broken" not in master.list_projects()

    with pytest.raises(UnknownProjectError, match="Project 'broken' is not available"):
        master.status("broken")

    with pytest.raises(UnknownProjectError, match="Unknown project: ghost"):
        master.status("ghost")


def test_create_milestone_through_master(master, alpha_path):
    record = master.create_milestone("alpha", "gamma", "Gamma")

    assert record == {"id": "gamma", "name": "Gamma", "status": "planned"}

    state = ProjectState(alpha_path)

    assert state.get_milestone("gamma") == record
    assert state.validate() is True


def test_create_task_through_master(master, alpha_path):
    master.create_milestone("alpha", "gamma", "Gamma")
    record = master.create_task(
        "alpha",
        "task-003",
        milestone="gamma",
        title="Third",
        assigned_to="master",
    )

    assert record == {
        "id": "task-003",
        "milestone": "gamma",
        "title": "Third",
        "status": "planned",
        "assigned_to": "master",
    }
    assert ProjectState(alpha_path).get_task("task-003") == record


def test_create_task_defaults_through_master(master, alpha_path):
    master.create_task("alpha", "task-003", milestone="beta", title="Third")

    task = ProjectState(alpha_path).get_task("task-003")

    assert task["status"] == "planned"
    assert "assigned_to" not in task


def test_update_task_through_master(master, alpha_path):
    record = master.update_task(
        "alpha",
        "task-002",
        status="in_progress",
        title="Second revised",
    )

    assert record["status"] == "in_progress"
    assert record["title"] == "Second revised"

    stored = ProjectState(alpha_path).get_task("task-002")

    assert stored == record
    assert stored["milestone"] == "beta"


def test_update_milestone_through_master(master, alpha_path):
    record = master.update_milestone(
        "alpha",
        "beta",
        name="Beta Two",
        status="completed",
    )

    assert record == {"id": "beta", "name": "Beta Two", "status": "completed"}
    assert ProjectState(alpha_path).get_milestone("beta") == record


def test_work_in_one_project_does_not_touch_another(root, master):
    before = bytes_of(root / "beta-project")

    master.create_milestone("alpha", "gamma", "Gamma")
    master.update_task("alpha", "task-002", status="blocked")

    assert bytes_of(root / "beta-project") == before


def test_master_never_writes_project_yaml(master, alpha_path):
    before = (alpha_path / "project.yaml").read_bytes()

    master.create_milestone("alpha", "gamma", "Gamma")
    master.create_task("alpha", "task-003", milestone="gamma", title="Third")
    master.update_task("alpha", "task-003", status="completed")
    master.update_milestone("alpha", "gamma", status="completed")

    assert (alpha_path / "project.yaml").read_bytes() == before
    assert ProjectState(alpha_path).validate() is True


def test_unknown_project_raises_from_the_layer_below(master):
    with pytest.raises(UnknownProjectError, match="Unknown project: ghost"):
        master.status("ghost")

    with pytest.raises(UnknownProjectError, match="Unknown project: ghost"):
        master.create_milestone("ghost", "gamma", "Gamma")

    with pytest.raises(UnknownProjectError, match="Unknown project: ghost"):
        master.create_task("ghost", "task-003", milestone="alpha", title="T")

    with pytest.raises(UnknownProjectError, match="Unknown project: ghost"):
        master.update_task("ghost", "task-001", status="completed")

    with pytest.raises(UnknownProjectError, match="Unknown project: ghost"):
        master.update_milestone("ghost", "alpha", status="completed")


@pytest.mark.parametrize("project_id", ["", "   ", None, 7])
def test_blank_project_id_is_rejected_before_any_lookup(master, project_id):
    with pytest.raises(ValueError, match="project_id must be a non-empty string"):
        master.status(project_id)

    with pytest.raises(ValueError, match="project_id must be a non-empty string"):
        master.create_task(project_id, "task-003", milestone="alpha", title="T")


def test_validation_errors_from_work_manager_propagate(master, alpha_path):
    before = bytes_of(alpha_path)

    with pytest.raises(DuplicateRecordError):
        master.create_task("alpha", "task-001", milestone="alpha", title="Dup")

    with pytest.raises(DuplicateRecordError):
        master.create_milestone("alpha", "alpha", "Dup")

    with pytest.raises(InvalidFieldError, match="Invalid task status"):
        master.create_task(
            "alpha", "task-003", milestone="alpha", title="T", status="nope"
        )

    with pytest.raises(InvalidFieldError, match="unknown milestone"):
        master.create_task(
            "alpha", "task-003", milestone="ghost", title="T"
        )

    with pytest.raises(InvalidFieldError, match="Cannot change task field"):
        master.update_task("alpha", "task-001", statues="completed")

    with pytest.raises(InvalidFieldError, match="Cannot change task field 'id'"):
        master.update_task("alpha", "task-001", id="task-999")

    with pytest.raises(RecordNotFoundError, match="Task not found"):
        master.update_task("alpha", "ghost", status="completed")

    with pytest.raises(RecordNotFoundError, match="Milestone not found"):
        master.update_milestone("alpha", "ghost", status="completed")

    assert bytes_of(alpha_path) == before
    assert ProjectState(alpha_path).validate() is True


def test_errors_are_propagated_not_wrapped(master):
    with pytest.raises(WorkManagerError):
        master.create_milestone("alpha", "alpha", "Dup")

    with pytest.raises(WorkManagerError):
        master.update_task("alpha", "ghost", status="completed")


def test_master_declares_no_exception_hierarchy():
    """The CLI may translate domain errors, but must not define its own.

    Only classes defined in this module count. Imported error types are
    expected here: the CLI catches ProjectManager's and WorkManager's errors
    so it can print them readably instead of raising a parallel hierarchy.
    """
    import core.master as master_module

    defined_here = [
        name
        for name, value in vars(master_module).items()
        if isinstance(value, type)
        and issubclass(value, BaseException)
        and value.__module__ == master_module.__name__
    ]

    assert defined_here == []


def test_cli_translates_domain_errors_without_defining_new_ones():
    """Errors surfaced to the CLI must be the lower layers' own types."""
    from core.project_manager import UnknownProjectError
    from core.work_manager import (
        DuplicateRecordError,
        InvalidFieldError,
        RecordNotFoundError,
        WorkManagerError,
    )

    for error_type in (
        DuplicateRecordError,
        RecordNotFoundError,
        InvalidFieldError,
    ):
        assert issubclass(error_type, WorkManagerError)

    assert issubclass(WorkManagerError, ValueError)
    assert issubclass(FileNotFoundError, OSError)

    assert not issubclass(UnknownProjectError, WorkManagerError)


def test_writes_are_blocked_while_a_project_is_malformed(root, master):
    """The safety property: a malformed project can never be written to."""
    path = write_project(
        root / "sick-project",
        "sick",
        milestones=[ALPHA],
        tasks=[dict(TASK_ONE, status="finished-ish")],
    )
    before = bytes_of(path)

    for operation in (
        lambda: master.create_task(
            "sick", "task-009", milestone="alpha", title="New"
        ),
        lambda: master.create_milestone("sick", "gamma", "Gamma"),
        lambda: master.update_task("sick", "task-001", status="completed"),
        lambda: master.update_milestone("sick", "alpha", status="completed"),
    ):
        with pytest.raises(UnknownProjectError):
            operation()

    assert bytes_of(path) == before
    assert "sick" not in master.list_projects()


def test_a_project_repaired_becomes_writable_again(root, master):
    path = write_project(
        root / "sick-project",
        "sick",
        milestones=[ALPHA],
        tasks=[dict(TASK_ONE, status="finished-ish")],
    )

    with pytest.raises(UnknownProjectError):
        master.create_milestone("sick", "gamma", "Gamma")

    (path / "tasks.yaml").write_text(
        yaml.safe_dump({"tasks": [TASK_ONE]}), encoding="utf-8"
    )

    assert master.create_milestone("sick", "gamma", "Gamma")["id"] == "gamma"


def test_mutations_delegate_to_work_manager(monkeypatch, master, alpha_path):
    calls = []
    original = WorkManager.create_task

    def recording_create_task(self, task_id, **kwargs):
        calls.append((self.project_path, task_id, kwargs))

        return original(self, task_id, **kwargs)

    monkeypatch.setattr(WorkManager, "create_task", recording_create_task)

    master.create_task(
        "alpha",
        "task-003",
        milestone="alpha",
        title="Third",
        status="planned",
        assigned_to=None,
    )

    assert len(calls) == 1
    project_path, task_id, kwargs = calls[0]

    assert project_path == alpha_path
    assert task_id == "task-003"
    assert kwargs == {
        "milestone": "alpha",
        "title": "Third",
        "status": "planned",
        "assigned_to": None,
    }


def test_reads_delegate_to_project_manager(monkeypatch, master):
    calls = []
    original = ProjectManager.get_project

    def recording_get_project(self, project_id):
        calls.append(project_id)

        return original(self, project_id)

    monkeypatch.setattr(ProjectManager, "get_project", recording_get_project)

    master.status("alpha")

    assert calls == ["alpha"]


def test_master_does_not_import_yaml_or_project_state():
    """Master must not parse YAML or reach past ProjectManager.

    Asserted on the import graph rather than by searching the source text: if
    Master cannot import yaml or ProjectState, it cannot read a project file
    directly or call ProjectState internals, whatever the prose says.
    """
    tree = ast.parse(MASTER_SOURCE_PATH.read_text(encoding="utf-8"))
    modules = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module)

    assert "yaml" not in modules
    assert "core.project_state" not in modules
    assert modules == {
        "argparse",
        "sys",
        "pathlib",
        "core.project_manager",
        "core.run_lock",
        "core.work_manager",
    }


def test_master_does_not_import_execution_or_network_modules():
    modules = set()
    tree = ast.parse(MASTER_SOURCE_PATH.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)

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
        "sqlite3",
        "asyncio",
        "threading",
    }

    assert modules & forbidden == set()


def test_master_makes_no_shell_or_dynamic_execution_calls():
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
    }
    forbidden_names = {"eval", "exec", "compile", "__import__", "input"}
    tree = ast.parse(MASTER_SOURCE_PATH.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function = node.func

            if isinstance(function, ast.Attribute):
                assert function.attr not in forbidden_attributes, (
                    f"Master calls .{function.attr}()"
                )
            elif isinstance(function, ast.Name):
                assert function.id not in forbidden_names, (
                    f"Master calls {function.id}()"
                )


def test_master_does_not_invent_public_methods_beyond_the_documented_api():
    """Guard against scope creep into the excluded later stages."""
    from core.master import Master as master_class

    public = {
        name
        for name in vars(master_class)
        if not name.startswith("_")
        and callable(getattr(master_class, name))
    }

    assert public == {
        "list_projects",
        "overview",
        "problems",
        "status",
        "create_milestone",
        "update_milestone",
        "create_task",
        "update_task",
        "set_task_acceptance",
        "set_task_description",
        "set_task_manual_check",
        "add_planned_work",
        "add_backlog_epic",
        "split_task",
        "set_task_size",
        "set_epic_priority",
        "project_state",
    }


def test_public_methods_take_no_natural_language_parameter():
    """Master must stay structured for a future reasoning layer."""
    import inspect

    from core.master import Master as master_class

    for name in (
        "status",
        "create_milestone",
        "update_milestone",
        "create_task",
        "update_task",
        "overview",
    ):
        parameters = list(
            inspect.signature(getattr(master_class, name)).parameters
        )
        forbidden = {
            "prompt",
            "instruction",
            "instructions",
            "request",
            "query",
            "command",
            "text",
            "message",
            "natural_language",
            "goal",
        }

        assert not set(parameters) & forbidden, f"{name} accepts free text"


def test_existing_projects_stay_valid_after_master_use(root, master):
    for project_id in master.list_projects():
        path = root / f"{project_id}-project"
        assert ProjectState(path).validate() is True

    master.create_milestone("alpha", "gamma", "Gamma")
    master.create_task("alpha", "task-003", milestone="gamma", title="Third")
    master.update_task("alpha", "task-003", status="completed")
    master.update_milestone("alpha", "gamma", status="completed")

    for project_id in master.list_projects():
        path = root / f"{project_id}-project"
        assert ProjectState(path).validate() is True

    assert master.overview()["problem_count"] == 0
    assert master.overview()["totals"]["tasks"] == 3


def test_real_ai_system_project_is_not_modified_by_master(root):
    master = Master(root)

    real = Master()

    assert "sample-project" in real.list_projects()

    before = real.status("sample-project")

    for project_id in master.list_projects():
        master.status(project_id)

    assert real.status("sample-project") == before