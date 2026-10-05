"""The Milestone 2 target project definition (projects/match-legends)."""

from pathlib import Path

from core.evidence import spec_hash
from core.project_state import ProjectState

PROJECT = Path(__file__).resolve().parent.parent / "projects" / "match-legends"


def test_the_definition_is_valid_and_points_at_the_dedicated_clone():
    state = ProjectState(PROJECT)
    state.validate()
    project = state.project()

    assert project["repository"] == "~/AI/managed/match-legends"
    assert project["base_branch"] == "main"


def test_five_independent_tasks_each_with_a_human_spec():
    tasks = ProjectState(PROJECT).tasks()

    assert [t["id"] for t in tasks] == ["ml-1", "ml-2", "ml-3", "ml-4", "ml-5"]
    for task in tasks:
        assert task["status"] == "planned"
        assert "depends_on" not in task, f"{task['id']} must be independent"
        assert task["description"].strip()
        acceptance = task["acceptance"]
        assert "tests/*" in acceptance["protected_paths"]
        assert "package.json" in acceptance["protected_paths"]
        assert acceptance["commands"][0].startswith("npm ci")
        assert any(task["id"] in c for c in acceptance["commands"]), \
            f"{task['id']} must run its own pending test"
        assert "npx playwright test tests/smoke" in acceptance["commands"]
        assert not any(c.strip() == "npm test" for c in acceptance["commands"])


def test_no_task_runs_another_tasks_pending_test():
    for task in ProjectState(PROJECT).tasks():
        others = {f"ml-{n}" for n in range(1, 6)} - {task["id"]}
        for command in task["acceptance"]["commands"]:
            assert not any(f"tasks/{o}-" in command for o in others), (task["id"], command)


def test_the_specs_are_distinct():
    hashes = {spec_hash(t) for t in ProjectState(PROJECT).tasks()}
    assert len(hashes) == 5
