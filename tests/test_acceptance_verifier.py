"""The acceptance verifier, against real worktrees and real commands."""

import json
import sys
import time

import pytest
import yaml

from conftest import attach_repository, git, make_repo, workspace_for
from core.acceptance_verifier import AcceptanceVerifier
from core.autonomous_loop import AutonomousLoop
from core.execution import ExecutionResult
from core.history import EventType, InMemoryHistoryStore
from core.master import Master
from core.provider import ReasoningProvider
from core.workspace import GitWorktrees

PYTEST = f"{sys.executable} -m pytest -q -p no:cacheprovider"
ACCEPTANCE = {"commands": [PYTEST], "protected_paths": ["tests/*"]}
FIXED_APP = "def greet(name):\n    return 'Hi ' + name\n"
REPO_FILES = {
    "app.py": "def greet(name):\n    return 'Hey ' + name\n",  # broken on purpose
    "tests/test_app.py": "from app import greet\n\n\ndef test_greet():\n"
                         "    assert greet('Ada') == 'Hi Ada'\n",
}


def attempt(tmp_path, change, task_acceptance=ACCEPTANCE, evidence=None, **verifier_kwargs):
    """Create a real worktree, apply change(path), snapshot, and verify it."""
    repo = make_repo(tmp_path / "repo", REPO_FILES)
    worktrees = GitWorktrees(tmp_path / "worktrees", (tmp_path / "projects",))
    _, base = worktrees.check_repository(repo, "main")
    attempt_id = "a" * 32
    path = worktrees.create(repo, worktrees.path_for("p", attempt_id), attempt_id, base)
    change(path)
    facts = worktrees.snapshot(path, base, attempt_id)
    workspace = workspace_for(path, base_sha=base, result_sha=facts["result_sha"])
    task = {"id": "t1", "title": "Fix greet"}
    if task_acceptance is not None:
        task["acceptance"] = task_acceptance
    given = facts if evidence is None else evidence
    return AcceptanceVerifier(**verifier_kwargs).verify(task, {}, given,
                                                        workspace=workspace), path


def fix_app(path):
    (path / "app.py").write_text(FIXED_APP)


def kinds(result):
    return [f["kind"] for f in result.findings]


def test_a_real_fix_passes(tmp_path):
    result, _ = attempt(tmp_path, fix_app)

    assert result.verdict == "pass"
    assert result.findings == ()
    assert result.evidence["commands_run"][0]["exit_code"] == 0


def test_no_acceptance_is_unable_to_verify(tmp_path):
    result, _ = attempt(tmp_path, fix_app, task_acceptance=None)

    assert result.verdict == "unable_to_verify"


def test_a_failing_command_fails_with_a_bounded_excerpt(tmp_path):
    result, _ = attempt(tmp_path, lambda path: None, excerpt_chars=300)

    assert result.verdict == "fail"
    finding = result.findings[0]
    assert finding["kind"] == "command_failed" and finding["exit_code"] != 0
    assert 0 < len(finding["output_excerpt"]) <= 300
    assert "1 failed" in finding["output_excerpt"]


def test_editing_a_protected_test_fails_even_when_the_commands_pass(tmp_path):
    def cheat(path):
        (path / "tests" / "test_app.py").write_text("def test_greet():\n    assert True\n")

    result, _ = attempt(tmp_path, cheat)

    assert result.verdict == "fail"
    assert kinds(result) == ["protected_path"]
    assert result.findings[0]["path"] == "tests/test_app.py"
    assert result.evidence["commands_run"][0]["exit_code"] == 0


def test_deleting_a_protected_file_fails(tmp_path):
    result, _ = attempt(tmp_path, lambda path: (path / "tests" / "test_app.py").unlink())

    assert "protected_path" in kinds(result)


def test_renaming_a_protected_file_fails(tmp_path):
    def rename(path):
        (path / "tests" / "test_app.py").rename(path / "elsewhere.py")

    result, _ = attempt(tmp_path, rename)

    assert result.evidence["protected_violations"] == ["tests/test_app.py"]


def test_every_command_runs_even_after_a_failure(tmp_path):
    acceptance = {"commands": ["exit 3", "echo second > ran.txt", "exit 0"]}

    result, path = attempt(tmp_path, fix_app, task_acceptance=acceptance)

    assert result.verdict == "fail"
    assert [c["exit_code"] for c in result.evidence["commands_run"]] == [3, 0, 0]
    assert (path / "ran.txt").exists()


def test_a_command_past_its_timeout_fails(tmp_path, monkeypatch):
    monkeypatch.setattr("core.workspace.DEFAULT_GRACE_S", 1)
    started = time.monotonic()

    result, _ = attempt(tmp_path, fix_app, task_acceptance={"commands": ["sleep 60"]},
                        command_timeout_s=1)

    assert time.monotonic() - started < 20
    assert result.verdict == "fail"
    assert result.findings[0]["timed_out"] is True


def test_a_worktree_that_differs_from_the_result_is_unable_to_verify(tmp_path):
    repo = make_repo(tmp_path / "repo", REPO_FILES)
    base = git(repo, "rev-parse", "main")
    (repo / "stray.txt").write_text("not committed\n")

    result = AcceptanceVerifier().verify(
        {"acceptance": ACCEPTANCE}, {}, {},
        workspace=workspace_for(repo, base_sha=base, result_sha=base),
    )

    assert result.verdict == "unable_to_verify"


def test_the_verifier_never_uses_evidence_for_locations(tmp_path):
    misleading = {"workdir": str(tmp_path / "nowhere"), "result_sha": "0" * 40,
                  "base_sha": "f" * 40}

    result, _ = attempt(tmp_path, fix_app, evidence=misleading)

    assert result.verdict == "pass"
    assert result.evidence["result_sha"] != "0" * 40


# --- A2 end to end: a worker cannot pass by editing protected tests -------------


class Scripted(ReasoningProvider):
    name = "scripted"

    def __init__(self, replies):
        self._replies = list(replies)
        self.prompts = []

    def complete(self, prompt, schema=None):
        self.prompts.append(prompt)
        return self._replies.pop(0)


class Cheats:
    """Makes the test pass by rewriting the test, not the code."""

    def execute(self, task, context, *, workspace):
        (workspace.path / "tests" / "test_app.py").write_text(
            "def test_greet():\n    assert True\n"
        )
        return ExecutionResult(status="success", reason="all tests pass now")


def test_a_worker_that_rewrites_a_protected_test_gets_fail_and_cannot_complete(tmp_path):
    project = tmp_path / "projects" / "alpha-project"
    project.mkdir(parents=True)
    (project / "project.yaml").write_text(
        yaml.safe_dump({"id": "alpha", "name": "alpha", "status": "active"}))
    (project / "milestones.yaml").write_text(yaml.safe_dump(
        {"milestones": [{"id": "m1", "name": "M1", "status": "in_progress"}]}))
    (project / "tasks.yaml").write_text(yaml.safe_dump({"tasks": [
        {"id": "t1", "milestone": "m1", "title": "Fix greet", "status": "in_progress",
         "acceptance": ACCEPTANCE}]}))
    attach_repository(project, files=REPO_FILES)
    act = lambda op: json.dumps({"decision": "act", "reason": "r", "operation": op})
    provider = Scripted([
        act({"operation": "run_task", "project_id": "alpha", "task_id": "t1"}),
        act({"operation": "update_task", "project_id": "alpha", "task_id": "t1",
             "status": "completed"}),
    ])
    history = InMemoryHistoryStore()
    master = Master(tmp_path / "projects")

    result = AutonomousLoop(master, provider, Cheats(), AcceptanceVerifier(),
                            history=history).run("alpha")

    verification = history.events(types=[EventType.VERIFICATION])[-1].payload
    assert verification["verdict"] == "fail"
    assert verification["findings"][0] == {"kind": "protected_path",
                                           "path": "tests/test_app.py"}
    assert result.approval_reason == "verification_fail"
    assert master.status("alpha")["tasks"][0]["status"] == "in_progress"
