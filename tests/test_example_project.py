"""The fictional example project shipped in examples/ is a valid, hands-off definition."""

from pathlib import Path

from core.evidence import spec_hash
from core.planner import planner_settings
from core.project_state import ProjectState

PROJECT = Path(__file__).resolve().parent.parent / "examples" / "projects" / "example-app"


def test_the_definition_is_valid_and_hands_off():
    state = ProjectState(PROJECT)
    state.validate()
    project = state.project()

    assert project["base_branch"] == "develop"
    assert project["auto_integrate"] is True
    assert project["github"]["release_branch"] == "main"


def test_every_task_has_a_human_spec_and_runs_its_own_test():
    tasks = ProjectState(PROJECT).tasks()
    assert tasks
    for task in tasks:
        assert task["description"].strip()
        assert task["manual_check"].startswith("1. ")
        acceptance = task["acceptance"]
        assert "tests/*" in acceptance["protected_paths"]
        assert any(f"tests/tasks/{task['id']}-" in c for c in acceptance["commands"])


def test_the_specs_are_distinct():
    tasks = ProjectState(PROJECT).tasks()
    assert len({spec_hash(t) for t in tasks}) == len(tasks)

def test_the_planner_settings_are_read_from_the_project():
    settings = planner_settings(ProjectState(PROJECT).project())
    assert settings["test_dir"] == "tests/tasks"
    assert settings["test_suffixes"] == [".test.js"]
    assert settings["syntax_check"] == "node --check {path}"
