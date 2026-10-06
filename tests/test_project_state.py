import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest
import yaml

from core.project_state import (
    VALID_MILESTONE_STATUSES,
    VALID_TASK_STATUSES,
    ProjectState,
)


PROJECT_PATH = Path(__file__).parent / "fixtures" / "projects" / "sample-project"
MODULE_PATH = Path(__file__).parent.parent / "core" / "project_state.py"


def write_project(
    project_path,
    project=None,
    milestones=None,
    tasks=None,
):
    project_path.mkdir(parents=True, exist_ok=True)

    files = {
        "project.yaml": project
        if project is not None
        else {
            "id": "generated-project",
            "name": "Generated Project",
            "status": "active",
        },
        "milestones.yaml": {"milestones": milestones or []},
        "tasks.yaml": {"tasks": tasks or []},
    }

    for filename, data in files.items():
        (project_path / filename).write_text(
            yaml.safe_dump(data),
            encoding="utf-8",
        )

    return ProjectState(project_path)


@pytest.fixture
def state():
    return ProjectState(PROJECT_PATH)


@pytest.fixture
def make_project(tmp_path):
    def factory(**kwargs):
        return write_project(tmp_path / "generated-project", **kwargs)

    return factory


@pytest.fixture
def sandbox(make_project):
    return make_project(
        project={
            "id": "sandbox-project",
            "name": "Sandbox Project",
            "status": "active",
        },
        milestones=[
            {"id": "alpha", "name": "Alpha", "status": "in_progress"},
            {"id": "beta", "name": "Beta", "status": "planned"},
        ],
        tasks=[
            {
                "id": "task-001",
                "milestone": "alpha",
                "title": "First",
                "status": "completed",
            },
            {
                "id": "task-002",
                "milestone": "alpha",
                "title": "Second",
                "status": "in_progress",
            },
            {
                "id": "task-003",
                "milestone": "beta",
                "title": "Third",
                "status": "blocked",
            },
        ],
    )


def test_load_project(state):
    project = state.project()

    assert project["id"] == "sample-project"
    assert project["name"] == "Sample Project"
    assert project["status"] == "active"


def test_get_existing_task(state):
    task = state.get_task("foundation-001")

    assert task is not None
    assert task["id"] == "foundation-001"
    assert task["milestone"] == "foundation"


def test_get_missing_task(state):
    task = state.get_task("does-not-exist")

    assert task is None


def test_update_valid_status(state):
    task = state.update_task_status(
        "foundation-002",
        "blocked",
    )

    assert task["status"] == "blocked"

    # Restore the real project state after the test.
    state.update_task_status(
        "foundation-002",
        "in_progress",
    )


def test_reject_invalid_status(state):
    with pytest.raises(ValueError):
        state.update_task_status(
            "foundation-002",
            "finished-ish",
        )


def test_reject_missing_task(state):
    with pytest.raises(ValueError):
        state.update_task_status(
            "does-not-exist",
            "in_progress",
        )


def test_get_existing_milestone(state):
    milestone = state.get_milestone("foundation")

    assert milestone is not None
    assert milestone["id"] == "foundation"
    assert milestone["name"] == "Foundation"
    assert milestone["status"] == "in_progress"


def test_get_missing_milestone(state):
    milestone = state.get_milestone("does-not-exist")

    assert milestone is None


def test_tasks_in_milestone(state):
    tasks = state.tasks_in_milestone("foundation")

    assert len(tasks) == 4
    assert [task["id"] for task in tasks] == [
        "foundation-001",
        "foundation-002",
        "foundation-003",
        "foundation-004",
    ]

    for task in tasks:
        assert task["milestone"] == "foundation"


def test_tasks_in_empty_milestone(state):
    tasks = state.tasks_in_milestone("orchestration")

    assert tasks == []


def test_tasks_in_missing_milestone(state):
    tasks = state.tasks_in_milestone("does-not-exist")

    assert tasks == []


def test_tasks_with_status(state):
    tasks = state.tasks_with_status("in_progress")

    assert [task["id"] for task in tasks] == [
        "foundation-001",
        "foundation-002",
    ]


def test_tasks_with_status_no_matches(state):
    tasks = state.tasks_with_status("blocked")

    assert tasks == []


def test_tasks_with_status_rejects_unknown_status(state):
    with pytest.raises(ValueError):
        state.tasks_with_status("finished-ish")


def test_progress(state):
    summary = state.progress()

    assert summary["project_id"] == "sample-project"
    assert summary["project_name"] == "Sample Project"
    assert summary["total_tasks"] == 4
    assert summary["task_status_counts"] == {
        "planned": 2,
        "in_progress": 2,
        "blocked": 0,
        "completed": 0,
        "cancelled": 0,
    }
    assert summary["total_milestones"] == 8
    assert summary["milestone_status_counts"] == {
        "planned": 7,
        "in_progress": 1,
        "completed": 0,
    }


def test_progress_counts_are_consistent(state):
    summary = state.progress()
    task_counts = summary["task_status_counts"]
    milestone_counts = summary["milestone_status_counts"]

    assert set(task_counts) == VALID_TASK_STATUSES
    assert set(milestone_counts) == VALID_MILESTONE_STATUSES
    assert sum(task_counts.values()) == summary["total_tasks"]
    assert sum(milestone_counts.values()) == summary["total_milestones"]


def test_sandbox_queries(sandbox):
    assert sandbox.get_milestone("alpha")["name"] == "Alpha"

    assert [task["id"] for task in sandbox.tasks_in_milestone("alpha")] == [
        "task-001",
        "task-002",
    ]

    assert sandbox.tasks_with_status("completed") == [sandbox.get_task("task-001")]


def test_sandbox_progress(sandbox):
    summary = sandbox.progress()

    assert summary["project_id"] == "sandbox-project"
    assert summary["project_name"] == "Sandbox Project"
    assert summary["total_tasks"] == 3
    assert summary["task_status_counts"] == {
        "planned": 0,
        "in_progress": 1,
        "blocked": 1,
        "completed": 1,
        "cancelled": 0,
    }
    assert summary["total_milestones"] == 2
    assert summary["milestone_status_counts"] == {
        "planned": 1,
        "in_progress": 1,
        "completed": 0,
    }


VALID_MILESTONE = {"id": "alpha", "name": "Alpha", "status": "in_progress"}
VALID_TASK = {
    "id": "task-001",
    "milestone": "alpha",
    "title": "First",
    "status": "planned",
}


def test_validate_real_project(state):
    assert state.validate() is True


def test_validate_sandbox(sandbox):
    assert sandbox.validate() is True


def test_validate_rejects_duplicate_task_ids(make_project):
    project = make_project(
        milestones=[VALID_MILESTONE],
        tasks=[VALID_TASK, dict(VALID_TASK, title="Duplicate")],
    )

    with pytest.raises(ValueError, match="duplicate task id: task-001"):
        project.validate()


def test_validate_rejects_duplicate_milestone_ids(make_project):
    project = make_project(
        milestones=[VALID_MILESTONE, dict(VALID_MILESTONE, name="Copy")],
        tasks=[VALID_TASK],
    )

    with pytest.raises(ValueError, match="duplicate milestone id: alpha"):
        project.validate()


def test_validate_rejects_unknown_milestone_reference(make_project):
    project = make_project(
        milestones=[VALID_MILESTONE],
        tasks=[dict(VALID_TASK, milestone="does-not-exist")],
    )

    with pytest.raises(
        ValueError,
        match="task task-001 references unknown milestone 'does-not-exist'",
    ):
        project.validate()


def test_validate_rejects_unknown_task_status(make_project):
    project = make_project(
        milestones=[VALID_MILESTONE],
        tasks=[dict(VALID_TASK, status="finished-ish")],
    )

    with pytest.raises(
        ValueError,
        match="task task-001 has unknown status 'finished-ish'",
    ):
        project.validate()


def test_validate_rejects_unknown_milestone_status(make_project):
    project = make_project(
        milestones=[dict(VALID_MILESTONE, status="blocked")],
        tasks=[VALID_TASK],
    )

    with pytest.raises(
        ValueError,
        match="milestone alpha has unknown status 'blocked'",
    ):
        project.validate()


def test_validate_rejects_task_without_id(make_project):
    project = make_project(
        milestones=[VALID_MILESTONE],
        tasks=[{"milestone": "alpha", "status": "planned"}],
    )

    with pytest.raises(ValueError, match="task is missing an 'id'"):
        project.validate()


def test_validate_rejects_task_without_status(make_project):
    project = make_project(
        milestones=[VALID_MILESTONE],
        tasks=[{"id": "task-001", "milestone": "alpha"}],
    )

    with pytest.raises(ValueError, match="task task-001 is missing a 'status'"):
        project.validate()


def test_validate_rejects_unassigned_task(make_project):
    project = make_project(
        milestones=[VALID_MILESTONE],
        tasks=[{"id": "task-001", "status": "planned"}],
    )

    with pytest.raises(
        ValueError,
        match="task task-001 is not assigned to a milestone",
    ):
        project.validate()


def test_validate_rejects_milestone_without_status(make_project):
    project = make_project(
        milestones=[{"id": "alpha", "name": "Alpha"}],
        tasks=[VALID_TASK],
    )

    with pytest.raises(
        ValueError,
        match="milestone alpha is missing a 'status'",
    ):
        project.validate()


def test_validate_rejects_project_without_name(make_project):
    project = make_project(project={"id": "no-name"})

    with pytest.raises(ValueError, match="project.yaml is missing 'name'"):
        project.validate()


def test_validate_reports_every_problem(make_project):
    project = make_project(
        milestones=[dict(VALID_MILESTONE, status="blocked")],
        tasks=[
            dict(VALID_TASK, id="task-001", status="finished-ish"),
            dict(VALID_TASK, id="task-001", milestone="ghost"),
        ],
    )

    with pytest.raises(ValueError) as error:
        project.validate()

    message = str(error.value)

    assert "duplicate task id: task-001" in message
    assert "unknown status 'finished-ish'" in message
    assert "references unknown milestone 'ghost'" in message
    assert "unknown status 'blocked'" in message
    assert message.count("\n") == 4


def test_validate_allows_empty_project(make_project):
    project = make_project(milestones=[], tasks=[])

    assert project.validate() is True


def test_progress_rejects_malformed_state(make_project):
    project = make_project(
        milestones=[VALID_MILESTONE],
        tasks=[dict(VALID_TASK, status="finished-ish")],
    )

    with pytest.raises(ValueError, match="unknown status 'finished-ish'"):
        project.progress()


def test_progress_of_empty_project(make_project):
    project = make_project(milestones=[], tasks=[])

    summary = project.progress()

    assert summary["total_tasks"] == 0
    assert summary["total_milestones"] == 0
    assert sum(summary["task_status_counts"].values()) == 0
    assert sum(summary["milestone_status_counts"].values()) == 0


def test_demo_does_not_modify_tasks_yaml():
    before = (PROJECT_PATH / "tasks.yaml").read_bytes()

    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), str(PROJECT_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (PROJECT_PATH / "tasks.yaml").read_bytes() == before

    for section in ("PROJECT", "VALIDATION", "MILESTONES", "PROGRESS"):
        assert section in result.stdout

    assert "sample-project" in result.stdout
    assert "Sample Project" in result.stdout


def test_demo_does_not_modify_any_project_file():
    before = {
        path.name: path.read_bytes()
        for path in PROJECT_PATH.iterdir()
        if path.is_file()
    }

    subprocess.run(
        [sys.executable, str(MODULE_PATH), str(PROJECT_PATH)],
        capture_output=True,
        text=True,
        check=True,
    )

    after = {
        path.name: path.read_bytes()
        for path in PROJECT_PATH.iterdir()
        if path.is_file()
    }

    assert after == before


READ_CALLS = [
    pytest.param(lambda state: state.project(), id="project"),
    pytest.param(lambda state: state.milestones(), id="milestones"),
    pytest.param(lambda state: state.tasks(), id="tasks"),
    pytest.param(
        lambda state: state.get_task("task-001"),
        id="get_task",
    ),
    pytest.param(
        lambda state: state.get_milestone("alpha"),
        id="get_milestone",
    ),
    pytest.param(
        lambda state: state.tasks_in_milestone("alpha"),
        id="tasks_in_milestone",
    ),
    pytest.param(
        lambda state: state.tasks_with_status("planned"),
        id="tasks_with_status",
    ),
    pytest.param(lambda state: state.progress(), id="progress"),
    pytest.param(lambda state: state.validate(), id="validate"),
]


@pytest.fixture
def malformed_tasks(make_project):
    return make_project(
        milestones=[VALID_MILESTONE],
        tasks=[dict(VALID_TASK, status="finished-ish")],
    )


@pytest.fixture
def malformed_milestones(make_project):
    return make_project(
        milestones=[dict(VALID_MILESTONE, status="blocked")],
        tasks=[VALID_TASK],
    )


@pytest.fixture
def malformed_project_metadata(make_project):
    return make_project(
        project={"id": "no-name"},
        milestones=[VALID_MILESTONE],
        tasks=[VALID_TASK],
    )


@pytest.mark.parametrize("read", READ_CALLS)
def test_reads_reject_malformed_tasks(malformed_tasks, read):
    with pytest.raises(ValueError, match="unknown status 'finished-ish'"):
        read(malformed_tasks)


@pytest.mark.parametrize("read", READ_CALLS)
def test_reads_reject_malformed_milestones(malformed_milestones, read):
    with pytest.raises(ValueError, match="unknown status 'blocked'"):
        read(malformed_milestones)


@pytest.mark.parametrize("read", READ_CALLS)
def test_reads_reject_malformed_project_metadata(
    malformed_project_metadata,
    read,
):
    with pytest.raises(ValueError, match="project.yaml is missing 'name'"):
        read(malformed_project_metadata)


@pytest.mark.parametrize("read", READ_CALLS)
def test_reads_accept_valid_state(sandbox, read):
    read(sandbox)


def test_valid_reads_return_expected_data(sandbox):
    assert sandbox.project()["id"] == "sandbox-project"
    assert len(sandbox.milestones()) == 2
    assert len(sandbox.tasks()) == 3
    assert sandbox.get_task("task-001")["title"] == "First"
    assert sandbox.get_milestone("beta")["name"] == "Beta"
    assert sandbox.get_task("nope") is None
    assert sandbox.get_milestone("nope") is None
    assert sandbox.tasks_with_status("blocked") == [
        sandbox.get_task("task-003")
    ]


@pytest.mark.parametrize(
    "read",
    [
        pytest.param(lambda state: state.project(), id="project"),
        pytest.param(lambda state: state.tasks(), id="tasks"),
        pytest.param(lambda state: state.get_task("task-001"), id="get_task"),
        pytest.param(
            lambda state: state.get_milestone("alpha"),
            id="get_milestone",
        ),
        pytest.param(
            lambda state: state.tasks_in_milestone("alpha"),
            id="tasks_in_milestone",
        ),
        pytest.param(
            lambda state: state.tasks_with_status("planned"),
            id="tasks_with_status",
        ),
        pytest.param(lambda state: state.progress(), id="progress"),
        pytest.param(lambda state: state.validate(), id="validate"),
    ],
)
def test_reads_load_each_yaml_file_exactly_once(sandbox, monkeypatch, read):
    opened = []
    original_open = Path.open

    def counting_open(self, *args, **kwargs):
        opened.append(self.name)
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)

    read(sandbox)

    assert sorted(opened) == [
        "milestones.yaml",
        "project.yaml",
        "tasks.yaml",
    ]


def test_update_task_status_rejects_malformed_state(malformed_tasks):
    path = malformed_tasks.project_path / "tasks.yaml"
    before = path.read_bytes()

    with pytest.raises(ValueError, match="unknown status 'finished-ish'"):
        malformed_tasks.update_task_status("task-001", "completed")

    assert path.read_bytes() == before


def test_update_task_status_rejects_malformed_milestones(
    malformed_milestones,
):
    path = malformed_milestones.project_path / "tasks.yaml"
    before = path.read_bytes()

    with pytest.raises(ValueError, match="unknown status 'blocked'"):
        malformed_milestones.update_task_status("task-001", "completed")

    assert path.read_bytes() == before


def test_update_task_status_reports_state_before_status(
    malformed_tasks,
):
    with pytest.raises(ValueError, match="Invalid project state"):
        malformed_tasks.update_task_status("task-001", "finished-ish")


def test_update_task_status_writes_valid_state(sandbox):
    task = sandbox.update_task_status("task-001", "cancelled")

    assert task["status"] == "cancelled"
    assert sandbox.get_task("task-001")["status"] == "cancelled"
    assert sandbox.tasks_with_status("cancelled") == [task]


def test_update_task_status_rejects_invalid_status(sandbox):
    before = (sandbox.project_path / "tasks.yaml").read_bytes()

    with pytest.raises(ValueError, match="Invalid task status: finished-ish"):
        sandbox.update_task_status("task-001", "finished-ish")

    assert (sandbox.project_path / "tasks.yaml").read_bytes() == before


def test_update_task_status_rejects_missing_task(sandbox):
    before = (sandbox.project_path / "tasks.yaml").read_bytes()

    with pytest.raises(ValueError, match="Task not found: does-not-exist"):
        sandbox.update_task_status("does-not-exist", "completed")

    assert (sandbox.project_path / "tasks.yaml").read_bytes() == before


def test_update_task_status_preserves_other_task_fields(sandbox):
    sandbox.update_task_status("task-001", "cancelled")

    task = sandbox.get_task("task-001")

    assert task["title"] == "First"
    assert task["milestone"] == "alpha"


def test_update_task_status_preserves_unknown_top_level_keys(
    make_project,
):
    project = make_project(
        milestones=[VALID_MILESTONE],
        tasks=[dict(VALID_TASK)],
    )

    path = project.project_path / "tasks.yaml"
    path.write_text(
        "version: 2\ntasks:\n- id: task-001\n  milestone: alpha\n"
        "  title: First\n  status: planned\n",
        encoding="utf-8",
    )

    project.update_task_status("task-001", "completed")

    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {
        "version": 2,
        "tasks": [
            {
                "id": "task-001",
                "milestone": "alpha",
                "title": "First",
                "status": "completed",
            }
        ],
    }


def project_files(project):
    return sorted(path.name for path in project.project_path.iterdir())


EXPECTED_PROJECT_FILES = [
    "milestones.yaml",
    "project.yaml",
    "tasks.yaml",
]


def test_update_task_status_replaces_the_file_atomically(sandbox, monkeypatch):
    calls = []
    original_replace = os.replace

    def recording_replace(source, target):
        calls.append((Path(source), Path(target)))
        return original_replace(source, target)

    monkeypatch.setattr(os, "replace", recording_replace)

    sandbox.update_task_status("task-001", "cancelled")

    assert len(calls) == 1

    source, target = calls[0]

    assert target == sandbox.project_path / "tasks.yaml"
    assert source != target
    assert source.parent == target.parent
    assert not source.exists()


def test_update_task_status_leaves_no_temporary_file(sandbox):
    sandbox.update_task_status("task-001", "cancelled")

    assert project_files(sandbox) == EXPECTED_PROJECT_FILES


def test_tasks_yaml_is_valid_after_update(sandbox):
    sandbox.update_task_status("task-001", "cancelled")

    document = yaml.safe_load(
        (sandbox.project_path / "tasks.yaml").read_text(encoding="utf-8")
    )

    assert document["tasks"][0]["status"] == "cancelled"
    assert sandbox.validate() is True


def test_update_does_not_touch_other_project_files(sandbox):
    watched = ("project.yaml", "milestones.yaml")
    before = {
        name: (sandbox.project_path / name).read_bytes() for name in watched
    }

    sandbox.update_task_status("task-001", "cancelled")

    after = {
        name: (sandbox.project_path / name).read_bytes() for name in watched
    }

    assert after == before


def test_failed_replace_leaves_original_byte_identical(sandbox, monkeypatch):
    path = sandbox.project_path / "tasks.yaml"
    before = path.read_bytes()

    def failing_replace(source, target):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(OSError, match="simulated rename failure"):
        sandbox.update_task_status("task-001", "cancelled")

    assert path.read_bytes() == before


def test_failed_replace_leaves_no_temporary_file(sandbox, monkeypatch):
    def failing_replace(source, target):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(OSError):
        sandbox.update_task_status("task-001", "cancelled")

    assert project_files(sandbox) == EXPECTED_PROJECT_FILES


def test_partial_write_leaves_original_byte_identical(sandbox, monkeypatch):
    path = sandbox.project_path / "tasks.yaml"
    before = path.read_bytes()

    def failing_dump(document, file, **kwargs):
        file.write("tasks:\n- id: task-001\n  status: canc")
        file.flush()
        raise OSError("simulated disk full")

    monkeypatch.setattr(yaml, "safe_dump", failing_dump)

    with pytest.raises(OSError, match="simulated disk full"):
        sandbox.update_task_status("task-001", "cancelled")

    assert path.read_bytes() == before


def test_partial_write_leaves_no_temporary_file(sandbox, monkeypatch):
    def failing_dump(document, file, **kwargs):
        file.write("tasks:\n- id: task-001\n  status: canc")
        file.flush()
        raise OSError("simulated disk full")

    monkeypatch.setattr(yaml, "safe_dump", failing_dump)

    with pytest.raises(OSError):
        sandbox.update_task_status("task-001", "cancelled")

    assert project_files(sandbox) == EXPECTED_PROJECT_FILES


def test_failed_replace_is_not_silently_swallowed(sandbox, monkeypatch):
    def failing_replace(source, target):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(OSError):
        sandbox.update_task_status("task-001", "cancelled")

    assert sandbox.get_task("task-001")["status"] == "completed"


def test_update_fsyncs_before_replacing_and_fsyncs_directory(
    sandbox,
    monkeypatch,
):
    events = []
    original_fsync = os.fsync
    original_replace = os.replace

    def recording_fsync(descriptor):
        events.append("fsync")
        return original_fsync(descriptor)

    def recording_replace(source, target):
        events.append("replace")
        return original_replace(source, target)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(os, "replace", recording_replace)

    sandbox.update_task_status("task-001", "cancelled")

    assert events == ["fsync", "replace", "fsync"]


def test_original_is_intact_until_the_atomic_swap(sandbox, monkeypatch):
    captured = {}
    original_replace = os.replace

    def recording_replace(source, target):
        captured["original"] = Path(target).read_bytes()
        captured["replacement"] = Path(source).read_bytes()
        return original_replace(source, target)

    monkeypatch.setattr(os, "replace", recording_replace)

    sandbox.update_task_status("task-001", "cancelled")

    assert yaml.safe_load(
        captured["original"].decode("utf-8")
    )["tasks"][0]["status"] == "completed"

    assert yaml.safe_load(
        captured["replacement"].decode("utf-8")
    )["tasks"][0]["status"] == "cancelled"


@pytest.mark.parametrize("mode", [0o644, 0o664, 0o600])
def test_update_preserves_file_permissions(sandbox, mode):
    path = sandbox.project_path / "tasks.yaml"
    path.chmod(mode)

    sandbox.update_task_status("task-001", "cancelled")

    assert stat.S_IMODE(path.stat().st_mode) == mode


def test_update_does_not_leave_private_temp_permissions(sandbox):
    path = sandbox.project_path / "tasks.yaml"
    path.chmod(0o664)

    sandbox.update_task_status("task-001", "cancelled")

    assert not stat.S_IMODE(path.stat().st_mode) == 0o600
