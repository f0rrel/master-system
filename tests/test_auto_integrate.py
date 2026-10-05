"""H-D1..H-D3: verified attempts are integrated into develop by the system, during the run."""

import json

import pytest
import yaml

from conftest import git
from core import run_cli
from core.history import EventType
from core.sqlite_history import SQLiteHistoryStore
from test_m2_dry_run import FIXTURE, ScriptedMaster, act, cli, toy  # noqa: F401


@pytest.fixture
def dev(toy, tmp_path, monkeypatch):  # noqa: F811
    from core.worker_env import find_node_bin
    if find_node_bin(22) is None:
        monkeypatch.setattr(run_cli, "build_worker_env",
                            lambda config: {"PATH": "/usr/bin:/bin", "HOME": "/tmp"})
    git(toy["repo"], "branch", "develop")
    project_yaml = toy["root"] / "toy" / "project.yaml"
    data = yaml.safe_load(project_yaml.read_text())
    data.update(base_branch="develop", auto_integrate=True)
    project_yaml.write_text(yaml.safe_dump(data))
    assert cli(toy, "task", "set-acceptance", "toy", "fix-greet",
               "--command", "grep -q \"'Hi '\" greet.py", "--protect", "tests/*")[0] == 0
    toy["fake"] = tmp_path / "fake-opencode"
    toy["main"] = git(toy["repo"], "rev-parse", "main")
    return toy


def fake_worker(toy, also_on_develop=None):
    """The fake OpenCode fixes greet.py; optionally someone else moves develop meanwhile."""
    moved = ""
    if also_on_develop:
        name, content = also_on_develop
        moved = (f'git -C {toy["repo"]} worktree add -q /tmp/ms-mover-$$ develop >/dev/null 2>&1\n'
                 f'printf "{content}" > /tmp/ms-mover-$$/{name}\n'
                 f'git -C /tmp/ms-mover-$$ add -A && git -C /tmp/ms-mover-$$ '
                 '-c user.name=o -c user.email=o@x commit -qm moved\n'
                 f'git -C {toy["repo"]} worktree remove --force /tmp/ms-mover-$$\n')
    toy["fake"].write_text("#!/bin/sh\n"
                           'while [ "$1" != "--dir" ]; do shift; done; cd "$2"\n'
                           "printf \"def greet(name):\\n    return 'Hi ' + name\\n\" > greet.py\n"
                           + moved + f"cat {FIXTURE}\n")
    toy["fake"].chmod(0o755)


def run(toy, monkeypatch, replies):
    master = ScriptedMaster(replies)
    monkeypatch.setattr(run_cli, "build_provider", lambda config: master)
    code, out = cli(toy, "start", "toy", "--objective", "Fix greet", "--session", "auto")
    assert code == 0, out
    history = SQLiteHistoryStore(toy["state"] / "history.sqlite")
    return out, history


def ops():
    start = act({"operation": "update_task", "project_id": "toy", "task_id": "fix-greet",
                 "status": "in_progress"})
    run_task = act({"operation": "run_task", "project_id": "toy", "task_id": "fix-greet"})
    done = act({"operation": "update_task", "project_id": "toy", "task_id": "fix-greet",
                "status": "completed"})
    return start, run_task, done


def task_status(toy):
    tasks = yaml.safe_load((toy["root"] / "toy" / "tasks.yaml").read_text())["tasks"]
    return tasks[0]["status"]


def test_a_passing_attempt_lands_on_develop_and_the_task_completes(dev, monkeypatch):
    fake_worker(dev)
    start, run_task, done = ops()
    _, history = run(dev, monkeypatch, [start, run_task, done])

    [integration] = history.events(types=[EventType.INTEGRATION])
    [finished] = history.events(types=[EventType.ATTEMPT_FINISHED])
    assert integration.payload["actor"] == "system"
    assert integration.payload["base_branch"] == "develop"
    assert integration.payload["method"] == "update_ref"
    assert git(dev["repo"], "rev-parse", "develop") == finished.payload["result_sha"]
    assert git(dev["repo"], "rev-parse", "main") == dev["main"]  # main never moves
    assert task_status(dev) == "completed"


def test_a_moved_develop_is_rebased_and_reverified(dev, monkeypatch):
    fake_worker(dev, also_on_develop=("notes.txt", "unrelated\\n"))
    start, run_task, done = ops()
    _, history = run(dev, monkeypatch, [start, run_task, done])

    [integration] = history.events(types=[EventType.INTEGRATION])
    assert integration.payload["method"] == "rebased_update_ref"
    reverified = [e for e in history.events(types=[EventType.VERIFICATION])
                  if "rebased_from" in e.payload]
    assert reverified and reverified[0].payload["actor"] == "system"
    assert reverified[0].payload["verdict"] == "pass"
    assert git(dev["repo"], "rev-parse", "develop") == integration.payload["result_sha"]
    assert (dev["repo"] / "notes.txt").exists() or git(
        dev["repo"], "show", "develop:notes.txt") == "unrelated"
    assert task_status(dev) == "completed"


def test_a_conflict_is_retried_from_the_new_develop(dev, monkeypatch):
    fake_worker(dev, also_on_develop=("greet.py", "def greet(name):\\n    return 'Yo ' + name\\n"))
    start, run_task, done = ops()
    out, history = run(dev, monkeypatch, [start, run_task, done, run_task, done])

    [refused] = history.events(types=[EventType.INTEGRATION_REFUSED])
    assert refused.payload["reason"] == "rebase_conflict"
    assert refused.payload["actor"] == "system"
    results = [e.payload for e in history.events(types=[EventType.OPERATION_RESULT])]
    assert any(r.get("reason") == "not_integrated" and r["status"] == "refused"
               for r in results)
    # The second attempt started from the new develop and landed on it.
    [integration] = history.events(types=[EventType.INTEGRATION])
    started = history.events(types=[EventType.ATTEMPT_STARTED])
    assert len(started) == 2
    assert git(dev["repo"], "rev-parse", "develop") == integration.payload["result_sha"]
    assert task_status(dev) == "completed"
    assert git(dev["repo"], "rev-parse", "main") == dev["main"]


def test_without_auto_integrate_nothing_is_integrated(toy, monkeypatch):  # noqa: F811
    from core.worker_env import find_node_bin
    if find_node_bin(22) is None:
        monkeypatch.setattr(run_cli, "build_worker_env",
                            lambda config: {"PATH": "/usr/bin:/bin", "HOME": "/tmp"})
    assert cli(toy, "task", "set-acceptance", "toy", "fix-greet",
               "--command", "grep -q \"'Hi '\" greet.py")[0] == 0
    start, run_task, done = ops()
    _, history = run(toy, monkeypatch, [start, run_task, done])
    assert history.events(types=[EventType.INTEGRATION]) == ()
    assert task_status(toy) == "completed"
