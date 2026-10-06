"""Task types: skill docs, allowed paths (verifier) and allowed tools (worker config)."""

import json
from pathlib import Path

import pytest
import yaml

from conftest import attach_repository, git, make_repo, workspace_for
from core.acceptance_verifier import AcceptanceVerifier
from core.execution import ExecutionBackend, ExecutionResult
from core.history import EventType, InMemoryHistoryStore
from core.lessons import LessonStore
from core.master import Master
from core.opencode_backend import build_prompt
from core.planner import draft_problems, planner_settings, system_prompt, task_records
from core.task_orchestrator import TaskOrchestrator
from core.task_types import (ALL_TOOLS, TYPES, frozen_allowed_paths, opencode_config,
                             skill_text, type_settings)
from core.verification import VerificationResult

PROJECT = {"task_types": {"visual": {"allowed_paths": ["www/css/*", "www/assets/*"],
                                     "tools": ["read", "edit", "nonsense"],
                                     "skill": "docs/skills/visual.md"}}}


def test_types_have_defaults_and_project_overrides():
    assert TYPES == ("developer", "visual", "logic", "docs")
    assert type_settings({}, "developer")["allowed_paths"] == ["*"]
    assert type_settings({}, "docs")["allowed_paths"] == ["*.md", "docs/*"]
    assert "bash" not in type_settings({}, "docs")["tools"]
    visual = type_settings(PROJECT, "visual")
    assert visual == {"type": "visual", "allowed_paths": ["www/css/*", "www/assets/*"],
                      "tools": ["read", "edit"], "skill": "docs/skills/visual.md"}
    assert frozen_allowed_paths(visual) == ["www/css/*", "www/assets/*"]
    assert frozen_allowed_paths(type_settings({}, "logic")) is None
    with pytest.raises(ValueError):
        type_settings({}, "designer")


def test_the_opencode_config_disables_and_denies_other_tools():
    config = json.loads(opencode_config(["read", "edit"]))
    assert config["tools"]["bash"] is False and config["tools"]["webfetch"] is False
    assert "read" not in config["tools"] and "edit" not in config["tools"]
    assert config["permission"] == {"bash": "deny", "webfetch": "deny"}
    assert json.loads(opencode_config(["read"]))["permission"]["edit"] == "deny"
    assert set(json.loads(opencode_config([]))["tools"]) == set(ALL_TOOLS)


def test_skill_text_joins_the_generic_and_the_project_doc(tmp_path):
    repo = make_repo(tmp_path / "r", {"docs/skills/visual.md": "Use the tile palette.\n"})
    project = {**PROJECT, "repository": str(repo), "base_branch": "main"}
    text = skill_text(project, type_settings(project, "visual"))
    assert text.startswith("# Skill: visual") and text.endswith("Use the tile palette.")
    assert len(skill_text(project, type_settings(project, "visual"), budget=50)) < 70


def test_the_verifier_fails_changes_outside_the_allowed_paths(tmp_path):
    repo = make_repo(tmp_path / "r", {"www/css/a.css": "a\n", "www/js/a.js": "a\n"})
    base = git(repo, "rev-parse", "HEAD")
    (repo / "www" / "css" / "a.css").write_text("b\n")
    (repo / "www" / "js" / "a.js").write_text("b\n")
    git(repo, "commit", "-qam", "both")
    result = git(repo, "rev-parse", "HEAD")
    task = {"acceptance": {"commands": ["true"], "allowed_paths": ["www/css/*"]}}
    verdict = AcceptanceVerifier().verify(task, {}, workspace=workspace_for(
        repo, base_sha=base, result_sha=result))
    assert verdict.verdict == "fail"
    assert list(verdict.findings) == [{"kind": "outside_allowed_paths", "path": "www/js/a.js"}]
    assert "outside the task type" in verdict.summary
    task["acceptance"]["allowed_paths"] = ["www/*"]
    assert AcceptanceVerifier().verify(task, {}, workspace=workspace_for(
        repo, base_sha=base, result_sha=result)).verdict == "pass"


def draft(**extra):
    task = {"id": "t-1", "title": "T", "size": "small", "type": "visual",
            "description": "d", "manual_check": "1. x", "files": ["www/css/a.css"],
            "tests": [{"path": "tests/tasks/t-1.spec.js", "content": "x"}],
            "test_commands": ["npx playwright test tests/tasks/t-1.spec.js"], **extra}
    return {"epic": {"id": "epic-e", "title": "E"}, "tasks": [task]}


def test_the_planner_requires_a_type_and_freezes_its_paths():
    settings = planner_settings(PROJECT)
    assert draft_problems(draft(), set(), set(), settings) == []
    assert any("type must be one of" in p
               for p in draft_problems(draft(type="artist"), set(), set(), settings))
    assert any("outside what a visual task may change" in p
               for p in draft_problems(draft(files=["www/js/x.js"]), set(), set(), settings))
    [record] = task_records(draft(), settings)
    assert record["type"] == "visual"
    assert record["acceptance"]["allowed_paths"] == ["www/css/*", "www/assets/*"]
    [record] = task_records(draft(type="logic", files=[]), settings)
    assert "allowed_paths" not in record["acceptance"]
    prompt = system_prompt("App", ["t-1"], settings)
    assert "visual (what the user sees" in prompt and "www/css/*" in prompt


def test_the_worker_prompt_carries_the_briefing():
    prompt = build_prompt(
        {"id": "t-1", "title": "T", "description": "Do it.",
         "acceptance": {"commands": ["true"], "allowed_paths": ["www/css/*"]}},
        {"type": "visual", "skill": "SKILL", "direction": "DIRECTION",
         "lessons": ["Colours live in tokens.css"]})
    assert "Task type: visual" in prompt and "SKILL" in prompt and "DIRECTION" in prompt
    assert "- Colours live in tokens.css" in prompt
    assert "  - www/css/*" in prompt and "LESSON: " in prompt
    plain = build_prompt({"id": "t-1", "title": "T"})
    assert "LESSON" not in plain and "Task type" not in plain


class Capture(ExecutionBackend):
    def __init__(self):
        self.calls = []

    def execute(self, task, context, workspace=None):
        self.calls.append({"context": context, "env": dict(workspace.env)})
        return ExecutionResult(status="success", artifacts={"summary": "done"})


class Pass:
    def verify(self, task, context, evidence=None, workspace=None):
        return VerificationResult(verdict="pass", summary="ok")


def typed_project(tmp_path, task_type="visual"):
    project = tmp_path / "projects" / "p"
    project.mkdir(parents=True)
    (project / "project.yaml").write_text(yaml.safe_dump(
        {"id": "p", "name": "P", "status": "active", **PROJECT}))
    (project / "milestones.yaml").write_text(
        "milestones:\n  - {id: m1, name: M1, status: in_progress}\n")
    (project / "tasks.yaml").write_text(yaml.safe_dump({"tasks": [
        {"id": "t1", "milestone": "m1", "title": "T", "status": "in_progress",
         "type": task_type}]}))
    attach_repository(project, {"docs/DIRECTION.md": "Be bright.\n",
                                "docs/skills/visual.md": "Project visual skill.\n"})
    return project


def test_the_orchestrator_briefs_the_worker_and_limits_its_tools(tmp_path):
    project = typed_project(tmp_path)
    LessonStore(project).propose("visual", ["Approved one"], "t0", "a0")
    LessonStore(project).decide(approve=["L-1"])
    LessonStore(project).propose("visual", ["Pending one"], "t0", "a0")
    backend, history = Capture(), InMemoryHistoryStore()
    orchestrator = TaskOrchestrator(Master(tmp_path / "projects"), backend, Pass(),
                                    history=history)
    orchestrator.orchestrate("p", "t1", run_id="r1")

    [call] = backend.calls
    briefing = call["context"]["briefing"]
    assert briefing["type"] == "visual" and briefing["direction"] == "Be bright."
    assert "Project visual skill." in briefing["skill"]
    assert briefing["lessons"] == ["Approved one"]  # never the pending one
    config = json.loads(call["env"]["OPENCODE_CONFIG_CONTENT"])
    assert config["tools"]["bash"] is False
    [started] = history.events(types=[EventType.ATTEMPT_STARTED])
    assert started.payload["task_type"] == "visual"
    assert started.payload["allowed_tools"] == ["read", "edit"]
    assert started.payload["lessons_used"] == 1


def test_an_untyped_task_keeps_the_old_behaviour(tmp_path):
    project = typed_project(tmp_path)
    data = yaml.safe_load((project / "tasks.yaml").read_text())
    del data["tasks"][0]["type"]
    (project / "tasks.yaml").write_text(yaml.safe_dump(data))
    backend = Capture()
    TaskOrchestrator(Master(tmp_path / "projects"), backend, Pass()).orchestrate("p", "t1")
    [call] = backend.calls
    assert "OPENCODE_CONFIG_CONTENT" not in call["env"]
    assert call["context"]["briefing"] == {"direction": "Be bright."}


def test_an_unknown_task_type_is_invalid_state(tmp_path):
    project = typed_project(tmp_path, task_type="artist")
    with pytest.raises(Exception, match="unknown type"):
        Master(tmp_path / "projects").status("p")
