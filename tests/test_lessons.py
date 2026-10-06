"""Shared project memory: proposed, approved in a batch, used only when approved."""

import io

import pytest

from core.lessons import LessonStore, extract_lessons
from core.ms import main as ms_main
from core.paths import default_projects_root


def test_lesson_lines_are_extracted_bounded_and_deduplicated():
    reply = ("Changed the tiles.\nLESSON: Colours live in tokens.css\n"
             "- lesson: colours live in TOKENS.css!\n* LESSON:   " + "x" * 400 + "\n"
             "LESSON: one\nLESSON: two\nnot a lesson: three")
    lessons = extract_lessons(reply)
    assert len(lessons) == 3
    assert lessons[-2:] == ["one", "two"]
    assert all(len(text) <= 300 for text in lessons)
    assert extract_lessons(None) == [] and extract_lessons("nothing") == []


def test_pending_lessons_are_never_used_until_approved(tmp_path):
    store = LessonStore(tmp_path)
    added = store.propose("visual", ["Use tokens", "Use tokens!", "Keep it short"], "t1", "a1")
    assert [r["id"] for r in added] == ["L-1", "L-2"]
    assert store.for_prompt("visual") == []
    assert store.propose("visual", ["use tokens"], "t2", "a2") == []  # duplicate
    store.decide(approve=["L-2"], reject=["L-1"])
    assert store.for_prompt("visual") == ["Keep it short"]
    assert store.for_prompt("logic") == []
    with pytest.raises(ValueError, match="not pending"):
        store.decide(approve=["L-1"])
    store.propose("all", ["Run npm ci first"], "t3", "a3")
    store.decide(approve=["L-3"])
    assert store.for_prompt("logic") == ["Run npm ci first"]
    assert store.for_prompt("visual") == ["Run npm ci first", "Keep it short"]
    assert store.for_prompt("visual", budget=20) == ["Run npm ci first"]


def run_ms(*argv):
    out = io.StringIO()
    return ms_main(list(argv), out=out), out.getvalue()


def test_ms_lessons_approves_in_a_batch():
    store = LessonStore(default_projects_root() / "sample-project")
    store.propose("visual", ["A", "B", "C"], "t1", "a1")
    code, out = run_ms("lessons", "sample-project")
    assert code == 0 and "3 pending, 0 approved" in out and "L-2  [visual] B" in out
    code, out = run_ms("lessons", "sample-project", "--approve", "L-1,L-3", "--reject", "rest")
    assert code == 0 and "Approved 2, rejected 1." in out and "0 pending, 2 approved" in out
    assert run_ms("lessons", "sample-project", "--approve", "L-9")[0] == 1


def test_only_a_verified_attempt_proposes_lessons(tmp_path):
    from test_task_types import typed_project

    from core.execution import ExecutionBackend, ExecutionResult
    from core.master import Master
    from core.task_orchestrator import TaskOrchestrator
    from core.verification import VerificationResult

    class Worker(ExecutionBackend):
        def execute(self, task, context, workspace=None):
            return ExecutionResult(status="success", artifacts={
                "summary": "Done.\nLESSON: Tiles are drawn in drawTile()"})

    class Verdict:
        def __init__(self, verdict):
            self.verdict = verdict

        def verify(self, task, context, evidence=None, workspace=None):
            return VerificationResult(verdict=self.verdict, summary="")

    project = typed_project(tmp_path)
    master = Master(tmp_path / "projects")
    failed = TaskOrchestrator(master, Worker(), Verdict("fail")).orchestrate("p", "t1")
    assert "lessons_proposed" not in failed
    assert LessonStore(project).pending() == []
    passed = TaskOrchestrator(master, Worker(), Verdict("pass")).orchestrate("p", "t1")
    assert passed["lessons_proposed"] == ["L-1"]
    [lesson] = LessonStore(project).pending()
    assert lesson["type"] == "visual" and lesson["text"] == "Tiles are drawn in drawTile()"
    assert lesson["attempt_id"] == passed["attempt_id"]
