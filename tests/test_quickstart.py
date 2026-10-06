"""examples/quickstart: a tiny Node project a new user can run end to end, offline."""

import os
import subprocess
from pathlib import Path

import pytest

from conftest import git
from core.planner import planner_settings
from core.project_state import ProjectState

QUICKSTART = Path(__file__).resolve().parent.parent / "examples" / "quickstart"


def init(target):
    return subprocess.run(["sh", str(QUICKSTART / "init.sh"), str(target)],
                          capture_output=True, text=True)


def test_the_definition_is_valid_and_hands_off_without_github():
    state = ProjectState(QUICKSTART / "project")
    state.validate()
    project = state.project()
    assert project["auto_integrate"] is True and project["base_branch"] == "develop"
    assert "github" not in project
    assert state.tasks() == []
    settings = planner_settings(project)
    assert settings["base_checks"] == ["node --test tests/unit/*.test.js"]
    assert settings["test_command_examples"] == ["node --test {path}"]


def test_init_creates_a_develop_repository_and_only_prints_the_next_steps(tmp_path):
    config_home = Path(os.environ["XDG_CONFIG_HOME"])
    before = sorted(p.relative_to(config_home) for p in config_home.rglob("*"))
    target = tmp_path / "quickstart"

    done = init(target)

    assert done.returncode == 0, done.stderr
    assert git(target, "branch", "--show-current") == "develop"
    assert git(target, "status", "--porcelain") == ""
    files = git(target, "ls-files").split()
    assert {"package.json", ".gitignore", "src/greet.js", "src/words.js",
            "tests/unit/greet.test.js", "tests/unit/words.test.js"} <= set(files)
    assert "node_modules/" in (target / ".gitignore").read_text()
    assert "dependencies" not in (target / "package.json").read_text()
    for step in ("cp ", f"repository: {target}", "ms backlog quickstart add", "ms chat quickstart",
                 "ms daemon --once", "ms report"):
        assert step in done.stdout
    assert sorted(p.relative_to(config_home) for p in config_home.rglob("*")) == before


def test_init_refuses_a_non_empty_target(tmp_path):
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "file").write_text("x")
    assert init(tmp_path / "taken").returncode == 1


def test_the_base_check_passes_on_the_seed_code(tmp_path):
    from core.run_cli import build_worker_env, find_node_for
    from core.run_config import load_config

    config = load_config(tmp_path / "none.toml")
    if find_node_for(config) is None:
        pytest.skip("no Node >= 22 for workers on this machine")
    target = tmp_path / "quickstart"
    assert init(target).returncode == 0
    [check] = planner_settings(ProjectState(QUICKSTART / "project").project())["base_checks"]

    done = subprocess.run(["/bin/sh", "-c", check], cwd=target, capture_output=True,
                          text=True, env=build_worker_env(config, home=tmp_path / "wh"))

    assert done.returncode == 0, done.stdout + done.stderr
    assert "pass 3" in done.stdout
