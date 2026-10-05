"""Milestone 2 dry run, offline: run_cli end to end on a toy repository.

Everything is real except the Master's model: the OpenCode backend runs a
fake 'opencode' executable (it edits the worktree and prints the event stream
recorded from the real CLI), the AcceptanceVerifier runs the task's acceptance
commands, integration moves a real branch, and the report is built from
history alone.
"""

import io
import json
import shutil
from pathlib import Path

import pytest
import yaml

from conftest import git, make_repo
from core import run_cli
from core.provider import ReasoningProvider

FIXTURE = Path(__file__).parent / "fixtures" / "opencode_run_events.jsonl"


def act(operation):
    return json.dumps({"decision": "act", "reason": "next", "operation": operation})


class ScriptedMaster(ReasoningProvider):
    name = "scripted:dry-run"

    def __init__(self, replies):
        self.replies = list(replies)

    def complete(self, prompt, schema=None):
        self.last_usage = {"input_tokens": 2000, "cached_input_tokens": 1000,
                           "output_tokens": 80}
        return self.replies.pop(0)


@pytest.fixture
def toy(tmp_path, tmp_path_factory, monkeypatch):
    repo = make_repo(tmp_path_factory.mktemp("toy") / "repo", {
        "greet.py": "def greet(name):\n    return 'Hey ' + name\n",
        "tests/test_greet.py": "from greet import greet\n\n\ndef test():\n"
                               "    assert greet('Ada') == 'Hi Ada'\n",
    })
    git(repo, "remote", "add", "origin", "https://example.invalid/toy.git")
    git(repo, "remote", "set-url", "--push", "origin", "DISABLED-by-master-system")
    root = tmp_path / "projects"
    project = root / "toy"
    project.mkdir(parents=True)
    (project / "project.yaml").write_text(yaml.safe_dump({
        "id": "toy", "name": "Toy", "status": "active",
        "repository": str(repo), "base_branch": "main"}))
    (project / "milestones.yaml").write_text(yaml.safe_dump(
        {"milestones": [{"id": "m", "name": "M", "status": "in_progress"}]}))
    (project / "tasks.yaml").write_text(yaml.safe_dump({"tasks": [
        {"id": "fix-greet", "milestone": "m", "title": "Fix greet", "status": "planned"}]}))

    # A fake OpenCode CLI: fixes greet.py in the directory it is given, then
    # prints the event stream recorded from the real CLI.
    fake = tmp_path / "fake-opencode"
    fake.write_text("#!/bin/sh\n"
                    'while [ "$1" != "--dir" ]; do shift; done; cd "$2"\n'
                    "printf \"def greet(name):\\n    return 'Hi ' + name\\n\" > greet.py\n"
                    "env >&2\n"
                    f"cat {FIXTURE}\n")
    fake.chmod(0o755)
    config = tmp_path / "config.toml"
    config.write_text(f'[worker]\nopencode_bin = "{fake}"\nmodel = "toy/worker-model"\n'
                      f'home = "{tmp_path / "worker-home"}"\n\n'
                      '[prices]\n"dry-run" = { input = 0.44, output = 1.32, cached_input = 0.0028 }\n'
                      '"worker-model" = { input = 0.44, output = 1.32, cached_input = 0.0028 }\n')
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-never-reach-a-worker")
    return {"root": root, "repo": repo, "config": config, "state": tmp_path / "state"}


def cli(toy, *argv):
    out = io.StringIO()
    code = run_cli.main(["--config", str(toy["config"]), "--root", str(toy["root"]),
                         "--state-dir", str(toy["state"]), *argv], out=out)
    return code, out.getvalue()


def test_one_task_end_to_end_through_run_cli(toy, monkeypatch):
    from core.worker_env import find_node_bin
    if find_node_bin(22) is None:
        monkeypatch.setattr(run_cli, "build_worker_env",
                            lambda config: {"PATH": "/usr/bin:/bin", "HOME": "/tmp"})
    start = act({"operation": "update_task", "project_id": "toy", "task_id": "fix-greet",
                 "status": "in_progress"})
    run = act({"operation": "run_task", "project_id": "toy", "task_id": "fix-greet"})
    done = act({"operation": "update_task", "project_id": "toy", "task_id": "fix-greet",
                "status": "completed"})
    master = ScriptedMaster([start, run, done])
    monkeypatch.setattr(run_cli, "build_provider", lambda config: master)

    # The human defines "done", through run_cli so it is recorded.
    assert cli(toy, "task", "describe", "toy", "fix-greet",
               "--text", "greet() must say 'Hi <name>'.")[0] == 0
    assert cli(toy, "task", "set-acceptance", "toy", "fix-greet",
               "--command", "grep -q \"'Hi '\" greet.py", "--protect", "tests/*")[0] == 0

    code, out = cli(toy, "start", "toy", "--objective", "Fix greet", "--session", "dry")
    assert code == 0, out
    assert "no_actionable_work" in out

    code, out = cli(toy, "status", "toy")
    assert "fix-greet" in out and "completed" in out

    report = json.loads(cli(toy, "report", "dry", "--json")[1])
    [attempt] = report["attempts"]
    assert (attempt["outcome"], attempt["verdict"]) == ("finished", "pass")
    assert git(toy["repo"], "rev-parse", "main") != attempt["result_sha"]

    # Only a human integrates.
    code, out = cli(toy, "integrate", "toy", attempt["attempt_id"])
    assert code == 0, out
    assert git(toy["repo"], "rev-parse", "main") == attempt["result_sha"]
    assert "'Hi '" in (toy["repo"] / "greet.py").read_text()

    shutil.rmtree(toy["root"])  # the report needs history only
    report = json.loads(cli(toy, "report", "dry", "--json")[1])
    [trace] = report["completions"]
    assert trace["integrated_sha"] == attempt["result_sha"]
    assert trace["verdict"] == "pass"
    assert [h["type"] for h in report["human_touches"]] == ["human_action", "human_action",
                                                            "integration"]
    assert report["unexplained_spec_changes"] == []
    [master_usage] = report["master_usage"]
    assert master_usage["usage"]["input_tokens"] == 6000
    assert master_usage["cost_usd"] > 0
    [worker_usage] = report["worker_usage"]
    assert worker_usage["model"] == "toy/worker-model"
    assert worker_usage["usage"]["input_tokens"] == 6326 and worker_usage["claimed"]
    assert worker_usage["cost_usd"] > 0

    # The worker never saw the Master's key.
    from core.sqlite_history import SQLiteHistoryStore
    from core.history import EventType
    history = SQLiteHistoryStore(toy["state"] / "history.sqlite")
    finished = history.events(types=[EventType.ATTEMPT_FINISHED])[0].payload
    worker_env = Path(finished["processes"][0]["stderr_log"]["path"]).read_text()
    assert "must-never-reach-a-worker" not in worker_env
    assert "DEEPSEEK_API_KEY" not in worker_env
    if find_node_bin(22) is not None:
        assert f"HOME={toy['config'].parent / 'worker-home'}" in worker_env
