"""core.direction: the project's direction document, read from the base branch."""

import pytest
import yaml

from conftest import git, make_repo
from core.direction import Direction, direction_settings, direction_text, read_direction
from core.master import Master
from core.planner import planner_settings, task_records
from core.reasoning_engine import ReasoningEngine

TEXT = "# Direction\n\nBright and readable. Never change the board.\n"


@pytest.fixture
def repo(tmp_path):
    repo = make_repo(tmp_path / "repo", {"README.md": "x\n", "docs/DIRECTION.md": TEXT})
    git(repo, "branch", "develop")
    return repo


def project(repo, **extra):
    return {"id": "app", "repository": str(repo), "base_branch": "develop", **extra}


def test_settings_have_defaults_and_ignore_nonsense():
    assert direction_settings({}) == {"path": "docs/DIRECTION.md", "max_chars": 6000}
    assert direction_settings({"direction": {"path": "/VISION.md", "max_chars": 50}}) == \
        {"path": "VISION.md", "max_chars": 50}
    assert direction_settings({"direction": {"max_chars": -1}})["max_chars"] == 6000


def test_the_direction_is_read_from_the_base_branch(repo):
    direction = read_direction(project(repo))
    assert direction == Direction("docs/DIRECTION.md", TEXT.strip(), False)
    assert direction.block().startswith("PROJECT DIRECTION (docs/DIRECTION.md):\n# Direction")


def test_the_working_tree_and_other_branches_do_not_count(repo):
    (repo / "docs" / "DIRECTION.md").write_text("uncommitted\n")
    assert read_direction(project(repo)).text == TEXT.strip()
    git(repo, "checkout", "-q", "--", "docs/DIRECTION.md")
    git(repo, "checkout", "-q", "-b", "other")
    git(repo, "rm", "-q", "docs/DIRECTION.md")
    git(repo, "commit", "-qm", "drop")
    assert read_direction(project(repo, base_branch="other")) is None


def test_the_text_is_truncated_to_its_budget(repo):
    direction = read_direction(project(repo, direction={"max_chars": 12}))
    assert direction.truncated
    assert direction.text.startswith("# Direction")
    assert "truncated" in direction.text and "Never change" not in direction.text


def test_no_repository_or_file_means_no_direction(repo):
    assert read_direction({"id": "x"}) is None
    assert read_direction(project(repo, direction={"path": "docs/NONE.md"})) is None


def test_planned_tasks_protect_the_direction_document():
    settings = planner_settings({"planner": {"protected_paths": ["tests/*"]},
                                 "direction": {"path": "docs/VISION.md"}})
    draft = {"epic": {"id": "e", "title": "E"}, "tasks": [{
        "id": "t-1", "title": "T", "size": "small", "type": "developer",
        "description": "d", "manual_check": "1. x", "test_commands": ["true"],
        "tests": [{"path": "tests/tasks/t-1.test.js", "content": "x"}]}]}
    [record] = task_records(draft, settings)
    assert record["acceptance"]["protected_paths"][:2] == ["tests/*", "docs/VISION.md"]


def test_the_master_sees_the_direction(repo, tmp_path):
    root = tmp_path / "projects"
    (root / "app").mkdir(parents=True)
    (root / "app" / "project.yaml").write_text(yaml.safe_dump(
        {**project(repo), "name": "App", "status": "active"}))
    (root / "app" / "milestones.yaml").write_text("milestones: []\n")
    (root / "app" / "tasks.yaml").write_text("tasks: []\n")
    master = Master(root)
    assert direction_text(master, "app") == TEXT.strip()
    assert direction_text(master, "missing") is None
    engine = ReasoningEngine(provider=None, master=master,
                             direction_source=lambda pid: direction_text(master, pid))
    context = engine.context_for("app")
    assert context["direction"] == TEXT.strip()
    assert "judge every proposal against it" in engine.build_prompt("go", "app").lower()
