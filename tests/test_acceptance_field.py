"""Task acceptance criteria: validated by ProjectState, set only by humans."""

import json

import pytest
import yaml

from core import master as master_cli
from core.master import Master
from core.project_state import ProjectState
from core.reasoning import (
    InvalidOperationError,
    Operation,
    ReasoningInterface,
    ResultStatus,
)
from core.reasoning_engine import build_operation_schema
from core.run_lock import ProjectLock
from core.work_manager import InvalidFieldError

ACCEPTANCE = {"commands": ["python -m pytest -q"], "protected_paths": ["tests/*"]}


@pytest.fixture
def project(tmp_path):
    path = tmp_path / "alpha-project"
    path.mkdir()
    (path / "project.yaml").write_text(
        yaml.safe_dump({"id": "alpha", "name": "alpha", "status": "active"})
    )
    (path / "milestones.yaml").write_text(yaml.safe_dump(
        {"milestones": [{"id": "m1", "name": "M1", "status": "in_progress"}]}
    ))
    (path / "tasks.yaml").write_text(yaml.safe_dump({"tasks": [
        {"id": "t1", "milestone": "m1", "title": "T1", "status": "planned"}
    ]}))
    return path


def set_task_field(project, **fields):
    data = yaml.safe_load((project / "tasks.yaml").read_text())
    data["tasks"][0].update(fields)
    (project / "tasks.yaml").write_text(yaml.safe_dump(data))


def set_project_field(project, **fields):
    data = yaml.safe_load((project / "project.yaml").read_text())
    data.update(fields)
    (project / "project.yaml").write_text(yaml.safe_dump(data))


# --- validation -----------------------------------------------------------


def test_valid_acceptance_is_accepted(project):
    set_task_field(project, acceptance=ACCEPTANCE)

    assert ProjectState(project).get_task("t1")["acceptance"] == ACCEPTANCE


def test_protected_paths_are_optional(project):
    set_task_field(project, acceptance={"commands": ["make test"]})

    ProjectState(project).validate()


@pytest.mark.parametrize(
    "acceptance",
    [
        "pytest",
        {"commands": []},
        {"commands": "pytest"},
        {"commands": [""]},
        {"commands": ["pytest"], "protected_paths": "tests/*"},
        {"commands": ["pytest"], "protected_paths": ["/etc/passwd"]},
        {"commands": ["pytest"], "protected_paths": ["../outside"]},
        {"commands": ["pytest"], "extra": 1},
        {"protected_paths": ["tests/*"]},
    ],
)
def test_malformed_acceptance_makes_the_project_invalid(project, acceptance):
    set_task_field(project, acceptance=acceptance)

    with pytest.raises(ValueError, match="acceptance"):
        ProjectState(project).validate()


def test_a_repository_needs_an_absolute_path_and_a_base_branch(project):
    set_project_field(project, repository="/srv/toy", base_branch="main")
    ProjectState(project).validate()

    for fields in (
        {"repository": "relative/repo", "base_branch": "main"},
        {"repository": "/srv/toy"},
        {"repository": "/srv/toy", "base_branch": "has space"},
        {"repository": "/srv/toy", "base_branch": "-x"},
        {"base_branch": "main"},
    ):
        data = {"id": "alpha", "name": "alpha", "status": "active", **fields}
        (project / "project.yaml").write_text(yaml.safe_dump(data))
        with pytest.raises(ValueError, match="repository|base_branch"):
            ProjectState(project).validate()


# --- humans set it --------------------------------------------------------


def test_master_sets_and_clears_acceptance(project):
    master = Master(project.parent)

    master.set_task_acceptance("alpha", "t1", ACCEPTANCE)
    assert ProjectState(project).get_task("t1")["acceptance"] == ACCEPTANCE

    master.set_task_acceptance("alpha", "t1", None)
    assert "acceptance" not in ProjectState(project).get_task("t1")


def test_malformed_acceptance_is_refused_and_nothing_is_written(project):
    before = (project / "tasks.yaml").read_bytes()

    with pytest.raises(InvalidFieldError):
        Master(project.parent).set_task_acceptance("alpha", "t1", {"commands": []})

    assert (project / "tasks.yaml").read_bytes() == before


def test_the_cli_sets_acceptance(project):
    code = master_cli.main(["--root", str(project.parent), "set-acceptance", "alpha",
                            "t1", "--command", "python -m pytest -q",
                            "--protect", "tests/*"])

    assert code == 0
    assert ProjectState(project).get_task("t1")["acceptance"] == ACCEPTANCE


def test_the_cli_clears_acceptance(project):
    set_task_field(project, acceptance=ACCEPTANCE)

    code = master_cli.main(["--root", str(project.parent), "set-acceptance", "alpha",
                            "t1", "--clear"])

    assert code == 0
    assert "acceptance" not in ProjectState(project).get_task("t1")


def test_the_cli_refuses_while_the_project_is_locked(project, capsys):
    before = (project / "tasks.yaml").read_bytes()

    with ProjectLock(project, holder="session=s1"):
        code = master_cli.main(["--root", str(project.parent), "set-acceptance",
                                "alpha", "t1", "--command", "true"])

    assert code == 1
    assert "in use by another process" in capsys.readouterr().err
    assert (project / "tasks.yaml").read_bytes() == before


# --- models cannot set it -------------------------------------------------


def test_update_task_cannot_set_acceptance(project):
    interface = ReasoningInterface(Master(project.parent))
    before = (project / "tasks.yaml").read_bytes()

    # A mapping is not a scalar, so it is refused before reaching Master...
    with pytest.raises(InvalidOperationError):
        Operation.propose({"operation": "update_task", "project_id": "alpha",
                           "task_id": "t1", "acceptance": ACCEPTANCE})
    # ...and a scalar is refused by the domain layer as an unknown field.
    outcome = interface.execute(Operation.propose(
        {"operation": "update_task", "project_id": "alpha", "task_id": "t1",
         "acceptance": "true"}
    ))

    assert outcome.status is ResultStatus.DOMAIN_ERROR
    assert (project / "tasks.yaml").read_bytes() == before


def test_create_task_cannot_set_acceptance():
    with pytest.raises(InvalidOperationError):
        Operation.propose({"operation": "create_task", "project_id": "alpha",
                           "task_id": "t2", "milestone": "m1", "title": "T2",
                           "acceptance": "true"})


def test_acceptance_is_not_in_the_operation_vocabulary():
    schema = json.dumps(build_operation_schema())

    assert "acceptance" not in schema
    assert "set_task_acceptance" not in schema
