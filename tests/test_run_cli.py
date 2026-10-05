"""core.run_cli: the composition root for real runs."""

import io

import pytest
import yaml

from core import run_cli
from core.evidence import spec_hash
from core.history import EventType
from core.paths import RuntimePaths
from core.project_state import ProjectState
from core.run_lock import ProjectLock
from core.sqlite_history import SQLiteHistoryStore


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "projects"
    project = root / "alpha-project"
    project.mkdir(parents=True)
    (project / "project.yaml").write_text(
        yaml.safe_dump({"id": "alpha", "name": "alpha", "status": "active"}))
    (project / "milestones.yaml").write_text(yaml.safe_dump(
        {"milestones": [{"id": "m1", "name": "M1", "status": "in_progress"}]}))
    (project / "tasks.yaml").write_text(yaml.safe_dump({"tasks": [
        {"id": "t1", "milestone": "m1", "title": "T1", "status": "planned"}]}))
    state = tmp_path / "state"
    return {"root": root, "project": project, "state": state,
            "history": lambda: SQLiteHistoryStore(state / "history.sqlite")}


def cli(setup, *argv):
    out = io.StringIO()
    code = run_cli.main(["--config", str(setup["root"] / "no-config.toml"),
                         "--root", str(setup["root"]), "--state-dir", str(setup["state"]),
                         *argv], out=out)
    return code, out.getvalue()


def human_actions(setup):
    return [e for e in setup["history"]().events(types=[EventType.HUMAN_ACTION])]


def test_describing_a_task_is_written_and_recorded(setup):
    before = spec_hash(ProjectState(setup["project"]).get_task("t1"))

    code, out = cli(setup, "task", "describe", "alpha", "t1", "--text", "Add greet().")

    task = ProjectState(setup["project"]).get_task("t1")
    assert code == 0 and "recorded" in out
    assert task["description"] == "Add greet()."
    [event] = human_actions(setup)
    assert event.task_id == "t1"
    assert event.payload == {"actor": "human-cli", "action": "set_description",
                             "spec_hash_before": before, "spec_hash_after": spec_hash(task)}


def test_setting_and_clearing_acceptance_are_recorded(setup):
    assert cli(setup, "task", "set-acceptance", "alpha", "t1", "--command", "npm test",
               "--protect", "tests/*")[0] == 0
    assert ProjectState(setup["project"]).get_task("t1")["acceptance"] == {
        "commands": ["npm test"], "protected_paths": ["tests/*"]}
    assert cli(setup, "task", "set-acceptance", "alpha", "t1", "--clear")[0] == 0

    assert [e.payload["action"] for e in human_actions(setup)] == ["set_acceptance",
                                                                   "clear_acceptance"]
    assert "acceptance" not in ProjectState(setup["project"]).get_task("t1")


def test_an_edit_is_refused_while_a_run_holds_the_project(setup):
    with ProjectLock(setup["project"], holder="session=s1"):
        code, _ = cli(setup, "task", "describe", "alpha", "t1", "--text", "x")

    assert code == 1
    assert "description" not in ProjectState(setup["project"]).get_task("t1")
    assert human_actions(setup) == []


def test_an_invalid_edit_is_refused_and_not_recorded(setup):
    code, _ = cli(setup, "task", "describe", "alpha", "nope", "--text", "x")

    assert code == 1
    assert human_actions(setup) == []


@pytest.mark.parametrize("argv", [
    ["task", "describe", "alpha", "t1"],
    ["task", "describe", "alpha", "t1", "--text", "x", "--clear"],
    ["task", "set-acceptance", "alpha", "t1"],
    ["task", "set-acceptance", "alpha", "t1", "--clear", "--command", "x"],
])
def test_bad_usage_is_refused(setup, argv):
    assert cli(setup, *argv)[0] == 2
    assert human_actions(setup) == []


def test_state_defaults_to_the_xdg_state_dir(setup, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    out = io.StringIO()

    run_cli.main(["--root", str(setup["root"]), "task", "describe", "alpha", "t1",
                  "--text", "x"], out=out)

    history = SQLiteHistoryStore(RuntimePaths.default().history_path)
    assert len(history.events(types=[EventType.HUMAN_ACTION])) == 1
