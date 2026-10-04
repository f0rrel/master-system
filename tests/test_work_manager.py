import ast
import os
from pathlib import Path

import pytest
import yaml

from core.project_manager import ProjectManager
from core.project_state import ProjectState
from core.work_manager import (
    TASK_FIELDS,
    DuplicateRecordError,
    InvalidFieldError,
    RecordNotFoundError,
    WorkManager,
    WorkManagerError,
)


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
    project=None,
    milestones=None,
    tasks=None,
):
    root.mkdir(parents=True, exist_ok=True)

    files = {
        "project.yaml": project
        if project is not None
        else {
            "id": "work",
            "name": "Work Project",
            "status": "active",
        },
        "milestones.yaml": {"milestones": milestones or []},
        "tasks.yaml": {"tasks": tasks or []},
    }

    for filename, data in files.items():
        (root / filename).write_text(yaml.safe_dump(data), encoding="utf-8")

    return root


def files_in(root):
    return sorted(path.name for path in root.iterdir())


def snapshot(root):
    return {
        path.name: path.read_bytes() for path in sorted(root.iterdir())
    }


EXPECTED_FILES = ["milestones.yaml", "project.yaml", "tasks.yaml"]


@pytest.fixture
def project_path(tmp_path):
    return write_project(
        tmp_path / "work-project",
        milestones=[ALPHA, BETA],
        tasks=[TASK_ONE, TASK_TWO],
    )


@pytest.fixture
def work(project_path):
    return WorkManager(project_path)


@pytest.fixture
def broken_path(tmp_path):
    return write_project(
        tmp_path / "broken-project",
        milestones=[ALPHA],
        tasks=[dict(TASK_ONE, status="finished-ish")],
    )


def test_create_task(work, project_path):
    record = work.create_task(
        "task-003",
        milestone="alpha",
        title="Third",
        assigned_to="master",
    )

    assert record == {
        "id": "task-003",
        "milestone": "alpha",
        "title": "Third",
        "status": "planned",
        "assigned_to": "master",
    }

    assert ProjectState(project_path).get_task("task-003") == record


def test_create_task_defaults_to_planned_and_no_assignee(work, project_path):
    work.create_task("task-003", milestone="beta", title="Third")

    task = ProjectState(project_path).get_task("task-003")

    assert task["status"] == "planned"
    assert "assigned_to" not in task


def test_create_milestone(work, project_path):
    record = work.create_milestone("gamma", "Gamma")

    assert record == {"id": "gamma", "name": "Gamma", "status": "planned"}
    assert ProjectState(project_path).get_milestone("gamma") == record
    assert ProjectState(project_path).validate() is True


def test_create_task_in_newly_created_milestone(work, project_path):
    work.create_milestone("gamma", "Gamma")
    work.create_task("task-003", milestone="gamma", title="Third")

    state = ProjectState(project_path)

    assert state.tasks_in_milestone("gamma") == [state.get_task("task-003")]


def test_update_task(work, project_path):
    record = work.update_task(
        "task-002",
        status="in_progress",
        title="Second revised",
        assigned_to="worker",
    )

    assert record["status"] == "in_progress"
    assert record["title"] == "Second revised"
    assert record["assigned_to"] == "worker"

    stored = ProjectState(project_path).get_task("task-002")

    assert stored == record
    assert stored["milestone"] == "beta"


def test_update_task_can_move_milestone(work, project_path):
    work.update_task("task-002", milestone="alpha")

    assert ProjectState(project_path).get_task("task-002")["milestone"] == "alpha"


def test_update_milestone(work, project_path):
    record = work.update_milestone("beta", name="Beta Two", status="completed")

    assert record == {"id": "beta", "name": "Beta Two", "status": "completed"}
    assert ProjectState(project_path).get_milestone("beta") == record


def test_duplicate_task_is_rejected(work, project_path):
    before = snapshot(project_path)

    with pytest.raises(DuplicateRecordError, match="Task already exists"):
        work.create_task("task-001", milestone="alpha", title="Clash")

    assert snapshot(project_path) == before
    assert ProjectState(project_path).validate() is True


def test_duplicate_milestone_is_rejected(work, project_path):
    before = snapshot(project_path)

    with pytest.raises(DuplicateRecordError, match="Milestone already exists"):
        work.create_milestone("alpha", "Clash")

    assert snapshot(project_path) == before


def test_unknown_milestone_reference_is_rejected(work, project_path):
    before = snapshot(project_path)

    with pytest.raises(
        InvalidFieldError,
        match="Task references unknown milestone: ghost",
    ):
        work.create_task("task-003", milestone="ghost", title="Third")

    assert snapshot(project_path) == before


def test_moving_task_to_unknown_milestone_is_rejected(work, project_path):
    before = snapshot(project_path)

    with pytest.raises(
        InvalidFieldError,
        match="Task references unknown milestone: ghost",
    ):
        work.update_task("task-002", milestone="ghost")

    assert snapshot(project_path) == before


@pytest.mark.parametrize(
    "status",
    ["finished-ish", "archived", "", None, "PLANNED"],
)
def test_invalid_task_status_is_rejected(work, project_path, status):
    before = snapshot(project_path)

    with pytest.raises(InvalidFieldError, match="Invalid task status"):
        work.create_task("task-003", milestone="alpha", title="T", status=status)

    with pytest.raises(InvalidFieldError, match="Invalid task status"):
        work.update_task("task-002", status=status)

    assert snapshot(project_path) == before


@pytest.mark.parametrize("status", ["blocked", "cancelled", "finished-ish", ""])
def test_invalid_milestone_status_is_rejected(work, project_path, status):
    before = snapshot(project_path)

    with pytest.raises(InvalidFieldError, match="Invalid milestone status"):
        work.create_milestone("gamma", "Gamma", status=status)

    with pytest.raises(InvalidFieldError, match="Invalid milestone status"):
        work.update_milestone("alpha", status=status)

    assert snapshot(project_path) == before


def test_milestone_status_vocabulary_is_narrower_than_task_vocabulary(
    work,
    project_path,
):
    for status in ("blocked", "cancelled"):
        with pytest.raises(InvalidFieldError):
            work.create_milestone(f"m-{status}", "M", status=status)


@pytest.mark.parametrize("title", ["", "   ", None, 7])
def test_required_text_fields_are_validated(work, project_path, title):
    before = snapshot(project_path)

    with pytest.raises(InvalidFieldError, match="Task title"):
        work.create_task("task-003", milestone="alpha", title=title)

    with pytest.raises(InvalidFieldError, match="Task id"):
        work.create_task("", milestone="alpha", title="Third")

    with pytest.raises(InvalidFieldError, match="Milestone name"):
        work.create_milestone("gamma", title)

    assert snapshot(project_path) == before


def test_unknown_update_fields_are_rejected(work, project_path):
    before = snapshot(project_path)

    with pytest.raises(InvalidFieldError, match="Cannot change task field"):
        work.update_task("task-002", statues="completed")

    with pytest.raises(InvalidFieldError, match="Cannot change milestone field"):
        work.update_milestone("alpha", statues="completed")

    assert snapshot(project_path) == before


def test_ids_cannot_be_rewritten(work, project_path):
    before = snapshot(project_path)

    with pytest.raises(InvalidFieldError, match="Cannot change task field 'id'"):
        work.update_task("task-002", id="task-999")

    with pytest.raises(
        InvalidFieldError,
        match="Cannot change milestone field 'id'",
    ):
        work.update_milestone("alpha", id="alpha-2")

    assert snapshot(project_path) == before


def test_missing_records_are_reported(work, project_path):
    before = snapshot(project_path)

    with pytest.raises(RecordNotFoundError, match="Task not found: ghost"):
        work.update_task("ghost", status="completed")

    with pytest.raises(RecordNotFoundError, match="Milestone not found: ghost"):
        work.update_milestone("ghost", status="completed")

    assert snapshot(project_path) == before


def test_work_errors_are_value_errors(work):
    with pytest.raises(ValueError):
        work.update_task("ghost", status="completed")

    with pytest.raises(WorkManagerError):
        work.create_milestone("alpha", "Clash")


def test_mutations_reject_malformed_existing_state(broken_path):
    work = WorkManager(broken_path)
    before = snapshot(broken_path)

    with pytest.raises(ValueError, match="unknown status 'finished-ish'"):
        work.create_task("task-009", milestone="alpha", title="New")

    with pytest.raises(ValueError, match="unknown status 'finished-ish'"):
        work.create_milestone("gamma", "Gamma")

    with pytest.raises(ValueError, match="unknown status 'finished-ish'"):
        work.update_task("task-001", status="completed")

    with pytest.raises(ValueError, match="unknown status 'finished-ish'"):
        work.update_milestone("alpha", status="completed")

    assert snapshot(broken_path) == before


def test_failed_write_leaves_original_state_byte_identical(
    work,
    project_path,
    monkeypatch,
):
    before = snapshot(project_path)

    def failing_replace(source, target):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(OSError, match="simulated rename failure"):
        work.create_task("task-003", milestone="alpha", title="Third")

    with pytest.raises(OSError, match="simulated rename failure"):
        work.create_milestone("gamma", "Gamma")

    assert snapshot(project_path) == before
    assert files_in(project_path) == EXPECTED_FILES
    assert ProjectState(project_path).validate() is True


def test_partial_dump_leaves_original_state_byte_identical(
    work,
    project_path,
    monkeypatch,
):
    before = snapshot(project_path)

    def failing_dump(document, file, **kwargs):
        file.write("tasks:\n- id: task-003\n  status: pla")
        file.flush()
        raise OSError("simulated disk full")

    monkeypatch.setattr(yaml, "safe_dump", failing_dump)

    with pytest.raises(OSError, match="simulated disk full"):
        work.update_milestone("beta", status="completed")

    assert snapshot(project_path) == before
    assert files_in(project_path) == EXPECTED_FILES


def test_mutations_use_atomic_replacement(work, project_path, monkeypatch):
    calls = []
    original_replace = os.replace

    def recording_replace(source, target):
        calls.append((Path(source), Path(target)))
        return original_replace(source, target)

    monkeypatch.setattr(os, "replace", recording_replace)

    work.create_milestone("gamma", "Gamma")
    work.create_task("task-003", milestone="gamma", title="Third")

    targets = [target.name for _, target in calls]

    assert targets == ["milestones.yaml", "tasks.yaml"]

    for source, target in calls:
        assert source.parent == target.parent
        assert not source.exists()


def test_successful_mutations_leave_no_temporary_file(work, project_path):
    work.create_milestone("gamma", "Gamma")
    work.create_task("task-003", milestone="gamma", title="Third")
    work.update_task("task-003", status="completed")
    work.update_milestone("gamma", status="completed")

    assert files_in(project_path) == EXPECTED_FILES


def test_unrelated_records_are_left_intact(work, project_path):
    work.create_milestone("gamma", "Gamma")
    work.create_task("task-003", milestone="gamma", title="Third")
    work.update_task("task-002", status="blocked")

    state = ProjectState(project_path)

    assert state.get_milestone("alpha") == ALPHA
    assert state.get_task("task-001") == TASK_ONE
    assert state.get_milestone("beta") == BETA
    assert state.get_task("task-002")["id"] == "task-002"
    assert state.get_task("task-002")["status"] == "blocked"
    assert state.validate() is True


def test_each_mutation_touches_only_its_own_file(work, project_path):
    milestones_before = (project_path / "milestones.yaml").read_bytes()
    tasks_before = (project_path / "tasks.yaml").read_bytes()
    project_before = (project_path / "project.yaml").read_bytes()

    work.create_milestone("gamma", "Gamma")

    assert (project_path / "milestones.yaml").read_bytes() != milestones_before
    assert (project_path / "tasks.yaml").read_bytes() == tasks_before
    assert (project_path / "project.yaml").read_bytes() == project_before

    work.create_task("task-003", milestone="gamma", title="Third")

    assert (project_path / "tasks.yaml").read_bytes() != tasks_before
    assert (project_path / "project.yaml").read_bytes() == project_before


def test_unknown_top_level_keys_survive_mutations(tmp_path):
    project_path = write_project(
        tmp_path / "keyed-project",
        milestones=[ALPHA],
        tasks=[TASK_ONE],
    )

    (project_path / "tasks.yaml").write_text(
        "version: 3\ntasks:\n"
        "- id: task-001\n"
        "  milestone: alpha\n"
        "  title: First\n"
        "  status: completed\n",
        encoding="utf-8",
    )
    (project_path / "milestones.yaml").write_text(
        "version: 3\nmilestones:\n"
        "- id: alpha\n"
        "  name: Alpha\n"
        "  status: in_progress\n",
        encoding="utf-8",
    )

    work = WorkManager(project_path)
    work.create_milestone("gamma", "Gamma")
    work.update_task("task-001", status="cancelled")

    tasks_doc = yaml.safe_load(
        (project_path / "tasks.yaml").read_text(encoding="utf-8")
    )
    milestones_doc = yaml.safe_load(
        (project_path / "milestones.yaml").read_text(encoding="utf-8")
    )

    assert tasks_doc["version"] == 3
    assert tasks_doc["tasks"][0]["status"] == "cancelled"
    assert milestones_doc["version"] == 3
    assert len(milestones_doc["milestones"]) == 2


def test_field_order_matches_existing_records(work, project_path):
    work.create_task(
        "task-003",
        milestone="alpha",
        title="Third",
        assigned_to="master",
    )

    lines = (project_path / "tasks.yaml").read_text(
        encoding="utf-8"
    ).splitlines()
    start = lines.index("- id: task-003")

    keys = [line.split(":")[0].strip().lstrip("- ") for line in lines[start :]]

    assert keys[: len(TASK_FIELDS)] == list(TASK_FIELDS)


def test_project_state_remains_usable_after_mutations(work, project_path):
    work.create_milestone("gamma", "Gamma")
    work.create_task("task-003", milestone="gamma", title="Third")

    state = ProjectState(project_path)

    assert state.update_task_status("task-003", "in_progress")["status"] == (
        "in_progress"
    )
    assert state.get_task("task-003")["status"] == "in_progress"
    assert state.validate() is True
    assert len(state.tasks_in_milestone("gamma")) == 1


def test_project_manager_sees_work_manager_changes(tmp_path):
    root = tmp_path / "projects"
    project_path = write_project(
        root / "work",
        milestones=[ALPHA],
        tasks=[TASK_ONE],
    )

    work = WorkManager(project_path)
    work.create_milestone("beta", "Beta")
    work.create_task(
        "task-002",
        milestone="beta",
        title="Second",
        status="blocked",
    )

    overview = ProjectManager(root).overview()

    assert overview["project_count"] == 1
    assert overview["problem_count"] == 0

    project = overview["projects"][0]

    assert project["total_milestones"] == 2
    assert project["total_tasks"] == 2
    assert project["task_status_counts"]["blocked"] == 1


def test_work_manager_imports_nothing_model_or_agent_related():
    """The layer must stay model/agent independent.

    Checked structurally via the AST rather than by grepping the source, so
    that documentation about the constraint does not fail the check and a
    real dependency cannot hide in an unsearched spelling.
    """
    source = (
        Path(__file__).parent.parent / "core" / "work_manager.py"
    ).read_text(encoding="utf-8")

    imported = set()

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert imported == {"copy", "pathlib", "sys", "core"}
    assert "core" in imported
