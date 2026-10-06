"""New files under every task type; stalled workers; reopening a task."""

import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from conftest import attach_repository
from core.acceptance_verifier import AcceptanceVerifier
from core.daemon import failed_attempts_since_human, stalls_since_human, work_for
from core.execution import ExecutionBackend, ExecutionResult
from core.history import EventType, InMemoryHistoryStore
from core.master import Master
from core.ms import main as ms_main
from core.opencode_backend import build_prompt, parse_progress
from core.paths import default_projects_root
from core.task_orchestrator import TaskOrchestrator
from core.task_types import TYPES, frozen_allowed_paths, opencode_config, type_settings

#: Where each type creates its new file, inside its default allowed paths.
NEW_FILE = {"developer": "src/feature.js", "visual": "www/css/theme.css",
            "logic": "www/js/new-module.js", "docs": "docs/guide.md"}


class CreatesAFile(ExecutionBackend):
    """A worker that creates one new file, as OpenCode's write tool would."""

    def __init__(self, path):
        self.path, self.env = path, None

    def execute(self, task, context, workspace=None):
        self.env = dict(workspace.env)
        target = Path(workspace.path) / self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("export const created = true;\n")
        return ExecutionResult(status="success", artifacts={"summary": "created"})


def project(tmp_path, task):
    path = tmp_path / "projects" / "p"
    path.mkdir(parents=True)
    (path / "project.yaml").write_text("id: p\nname: P\nstatus: active\n")
    (path / "milestones.yaml").write_text("milestones:\n  - {id: m, name: M, status: planned}\n")
    (path / "tasks.yaml").write_text(yaml.safe_dump({"tasks": [
        {"id": "t1", "milestone": "m", "title": "T", "status": "in_progress", **task}]}))
    attach_repository(path)
    return Master(tmp_path / "projects")


@pytest.mark.parametrize("task_type", TYPES)
def test_a_worker_can_create_a_new_file_under_every_type(tmp_path, task_type):
    settings = type_settings({}, task_type)
    config = json.loads(opencode_config(settings["tools"]))
    assert config["tools"].get("write") is not False      # creating files is allowed
    assert config.get("permission", {}).get("edit") != "deny"
    acceptance = {"commands": [f"test -f {NEW_FILE[task_type]}"]}
    if frozen_allowed_paths(settings):
        acceptance["allowed_paths"] = frozen_allowed_paths(settings)
    master = project(tmp_path, {"type": task_type, "acceptance": acceptance})
    worker = CreatesAFile(NEW_FILE[task_type])
    result = TaskOrchestrator(master, worker, AcceptanceVerifier()).orchestrate("p", "t1")
    assert result["verification"]["verdict"] == "pass", result["verification"]
    assert "OPENCODE_CONFIG_CONTENT" in worker.env


@pytest.mark.skipif(os.environ.get("MS_LIVE_OPENCODE") != "1",
                    reason="live OpenCode test; set MS_LIVE_OPENCODE=1")
@pytest.mark.parametrize("task_type", TYPES)
def test_live_opencode_creates_a_new_file_under_every_type(tmp_path, task_type):
    """Real OpenCode, with each type's tool config, must be able to create a file."""
    opencode = Path.home() / ".opencode" / "bin" / "opencode"
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    target = NEW_FILE[task_type]
    env = {"HOME": str(Path.home() / ".local/share/master-system-worker"),
           "PATH": "/usr/local/bin:/usr/bin:/bin",
           "OPENCODE_CONFIG_CONTENT": opencode_config(type_settings({}, task_type)["tools"])}
    subprocess.run([str(opencode), "run", "--format", "json", "--dir", str(work),
                    f"Create the file {target} containing one line: created. Then reply DONE."],
                   env=env, capture_output=True, timeout=600)
    assert (work / target).is_file()


# --- stalled workers ---


class ChangesNothing(ExecutionBackend):
    def __init__(self, finish_reason="stop"):
        self.finish_reason = finish_reason

    def execute(self, task, context, workspace=None):
        return ExecutionResult(status="success", artifacts={
            "summary": "", "finish_reason": self.finish_reason, "reasoning_tokens": 32002,
            "last_tools": ["read: {\"filePath\": \"www/index.html\"}"]})


class Verifier:
    def __init__(self):
        self.calls = 0

    def verify(self, task, context, evidence=None, workspace=None):
        from core.verification import VerificationResult

        self.calls += 1
        return VerificationResult(verdict="fail", summary="")


TASK = {"acceptance": {"commands": ["false"]}}


def test_a_worker_cut_off_while_thinking_has_stalled(tmp_path):
    master = project(tmp_path, TASK)
    verifier, history = Verifier(), InMemoryHistoryStore()
    result = TaskOrchestrator(master, ChangesNothing("length"), verifier,
                              history=history).orchestrate("p", "t1", run_id="r")
    assert result["outcome"] == "stalled" and verifier.calls == 0
    [finished] = history.events(types=[EventType.ATTEMPT_FINISHED])
    stall = finished.payload["stall"]
    assert stall["reason"].startswith("cut off") and stall["reasoning_tokens"] == 32002
    assert stall["last_tools"] == ["read: {\"filePath\": \"www/index.html\"}"]
    assert failed_attempts_since_human(history, "p", "t1") == 0
    assert stalls_since_human(history, "p", "t1") == 1


def test_minutes_without_a_change_are_a_stall_but_a_quick_empty_attempt_is_not(tmp_path):
    master = project(tmp_path, TASK)
    quick = TaskOrchestrator(master, ChangesNothing(), Verifier()).orchestrate("p", "t1")
    assert quick["outcome"] == "finished"
    slow = TaskOrchestrator(master, ChangesNothing(), Verifier(),
                            stall_minutes=1e-9).orchestrate("p", "t1")
    assert slow["outcome"] == "stalled"


def test_three_stalls_wait_for_the_owner(tmp_path):
    master = project(tmp_path, TASK)
    history = InMemoryHistoryStore()
    for _ in range(3):
        TaskOrchestrator(master, ChangesNothing("length"), Verifier(),
                         history=history).orchestrate("p", "t1", run_id="r")
    assert work_for(master.status("p"), history) == ([], ["t1"])
    history.append(type=EventType.HUMAN_ACTION, run_id="r", project_id="p", task_id="t1",
                   payload={"actor": "owner", "action": "reopen"})
    assert work_for(master.status("p"), history) == (["t1"], [])


def test_the_report_shows_the_stall_excerpt(tmp_path):
    from core.report import build_report, render_report

    master = project(tmp_path, TASK)
    history = InMemoryHistoryStore()
    TaskOrchestrator(master, ChangesNothing("length"), Verifier(), history=history) \
        .orchestrate("p", "t1", run_id="r", session_id="s")
    text = render_report(build_report(history, "s"))
    assert "stalled worker: cut off" in text and "reasoning tokens 32002" in text
    assert 'last: read: {"filePath": "www/index.html"}' in text


def test_progress_parsing_and_the_write_early_instruction():
    stdout = "\n".join(json.dumps(e) for e in [
        {"type": "tool_use", "part": {"tool": "bash", "state": {"input": {"command": "ls"}}}},
        {"type": "step_finish", "part": {"reason": "tool-calls"}},
        {"type": "step_finish", "part": {"reason": "length"}}])
    assert parse_progress(stdout) == ("length", ['bash: {"command": "ls"}'])
    assert "Write or create the files you need early" in build_prompt({"id": "t", "title": "T"})


# --- reopening ---


def test_ms_reopen_puts_a_blocked_task_back_with_a_record():
    from core.paths import RuntimePaths
    from core.sqlite_history import SQLiteHistoryStore

    tasks_file = default_projects_root() / "sample-project" / "tasks.yaml"
    tasks = yaml.safe_load(tasks_file.read_text())
    tasks["tasks"][2]["status"] = "blocked"
    tasks_file.write_text(yaml.safe_dump(tasks))
    task_id = tasks["tasks"][2]["id"]
    out = io.StringIO()
    assert ms_main(["reopen", "sample-project", task_id, "--reason", "worker stalled"],
                   out=out) == 0
    assert "planned again" in out.getvalue()
    assert yaml.safe_load(tasks_file.read_text())["tasks"][2]["status"] == "planned"
    [event] = SQLiteHistoryStore(RuntimePaths.default().history_path).events(
        types=[EventType.HUMAN_ACTION])
    assert event.task_id == task_id
    assert event.payload["action"] == "reopen" and event.payload["reason"] == "worker stalled"
    assert event.payload["spec_hash_before"] == event.payload["spec_hash_after"]
    tasks["tasks"][2]["status"] = "completed"
    tasks_file.write_text(yaml.safe_dump(tasks))
    assert ms_main(["reopen", "sample-project", task_id], out=io.StringIO()) == 1


# --- cancelling ---


def test_ms_cancel_cancels_with_a_record_and_names_the_tasks_that_depend_on_it():
    from core.paths import RuntimePaths
    from core.sqlite_history import SQLiteHistoryStore

    tasks_file = default_projects_root() / "sample-project" / "tasks.yaml"
    tasks = yaml.safe_load(tasks_file.read_text())
    tasks["tasks"][3]["depends_on"] = ["foundation-003"]
    tasks_file.write_text(yaml.safe_dump(tasks))
    out = io.StringIO()

    assert ms_main(["cancel", "sample-project", "foundation-003", "--reason", "not needed"],
                   out=out) == 0

    assert "foundation-003 is cancelled" in out.getvalue()
    assert "foundation-004" in out.getvalue()  # its dependent is named, not changed
    after = {t["id"]: t for t in yaml.safe_load(tasks_file.read_text())["tasks"]}
    assert after["foundation-003"]["status"] == "cancelled"
    assert after["foundation-004"]["status"] == "planned"
    [event] = SQLiteHistoryStore(RuntimePaths.default().history_path).events(
        types=[EventType.HUMAN_ACTION])
    assert event.task_id == "foundation-003"
    assert event.payload["action"] == "cancel" and event.payload["reason"] == "not needed"
    assert event.payload["status_before"] == "planned"


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_ms_cancel_refuses_a_finished_task(status):
    tasks_file = default_projects_root() / "sample-project" / "tasks.yaml"
    tasks = yaml.safe_load(tasks_file.read_text())
    tasks["tasks"][2]["status"] = status
    tasks_file.write_text(yaml.safe_dump(tasks))
    out = io.StringIO()

    assert ms_main(["cancel", "sample-project", "foundation-003", "--reason", "x"],
                   out=out) == 1
    assert f"foundation-003 is already {status}" in out.getvalue()
