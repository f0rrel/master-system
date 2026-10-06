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


def approve(store, task_type, texts):
    """Approve texts (proposed at most three at a time, like an attempt's)."""
    ids = []
    for start in range(0, len(texts), 3):
        added = store.propose(task_type, texts[start:start + 3], "t0", "a0")
        store.decide(approve=[r["id"] for r in added])
        ids += [r["id"] for r in added]
    return ids


def test_selection_names_included_and_dropped_lessons(tmp_path):
    store = LessonStore(tmp_path)
    old, new = approve(store, "visual", ["o" * 150, "n" * 150])
    [other] = approve(store, "logic", ["logic only"])
    [shared] = approve(store, "all", ["s" * 100])
    chosen = store.select("visual", budget=300)
    assert [r["id"] for r in chosen["included"]] == [shared, new]
    assert [r["id"] for r in chosen["dropped"]] == [old]
    assert store.for_prompt("visual", budget=300) == ["s" * 100, "n" * 150]
    assert store.select("logic", budget=300)["dropped"] == []

    fits = store.fit(budget=300)
    assert fits[old] == ["visual"] and fits[new] == [] and fits[other] == []
    assert fits[shared] == []  # fits in the prompt of every type


def test_an_all_lesson_can_be_dropped_for_one_type_only(tmp_path):
    store = LessonStore(tmp_path)
    [shared] = approve(store, "all", ["s" * 100])
    approve(store, "visual", ["v" * 250])
    assert store.fit(budget=300)[shared] == ["visual"]


def test_attempt_started_records_the_ids_of_the_lessons_used(tmp_path):
    from test_task_types import Capture, Pass, typed_project

    from core.history import EventType, InMemoryHistoryStore
    from core.master import Master
    from core.task_orchestrator import TaskOrchestrator

    project = typed_project(tmp_path)
    store = LessonStore(project)
    approve(store, "visual", [c * 300 for c in "abcdefg"])  # 2,100: the oldest is dropped
    history = InMemoryHistoryStore()
    TaskOrchestrator(Master(tmp_path / "projects"), Capture(), Pass(),
                     history=history).orchestrate("p", "t1", run_id="r1")
    [started] = history.events(types=[EventType.ATTEMPT_STARTED])
    assert started.payload["lessons_used"] == 6
    assert started.payload["lessons_used_ids"] == ["L-7", "L-6", "L-5", "L-4", "L-3", "L-2"]


def history_with(attempts):
    """attempts: [(lesson ids or None, verdict or None, outcome)]."""
    from core.history import EventType, InMemoryHistoryStore

    history = InMemoryHistoryStore()
    for n, (ids, verdict, outcome) in enumerate(attempts):
        payload = {"lessons_used": len(ids or [])}
        if ids is not None:
            payload["lessons_used_ids"] = ids
        started = history.append(type=EventType.ATTEMPT_STARTED, run_id="r",
                                 project_id="sample-project", task_id="t1", payload=payload)
        attempt = started.attempt_id
        history.append(type=EventType.ATTEMPT_FINISHED, run_id="r", project_id="sample-project",
                       task_id="t1", attempt_id=attempt, payload={"outcome": outcome})
        if verdict:
            history.append(type=EventType.VERIFICATION, run_id="r",
                           project_id="sample-project", task_id="t1", attempt_id=attempt,
                           payload={"verdict": verdict})
    return history


def test_usefulness_counts_attempts_and_passes_from_history():
    from core.lessons import lesson_evidence

    history = history_with([
        (["L-1", "L-2"], "pass", "finished"),
        (["L-1"], "fail", "finished"),
        (["L-1"], None, "limited"),       # the provider never ran it: not counted
        (None, "pass", "finished"),       # an old attempt without ids: not counted
    ])
    evidence = lesson_evidence(history, "sample-project")
    assert evidence["L-1"] == {"attempts": 2, "passed": 1}
    assert evidence["L-2"] == {"attempts": 1, "passed": 1}
    assert "L-3" not in evidence


def test_ms_lessons_shows_fit_usefulness_and_drops(monkeypatch):
    from core import ms
    from core.lessons import NEVER_PASSED_AFTER

    store = LessonStore(default_projects_root() / "sample-project")
    approve(store, "visual", [c * 300 for c in "abcdefgh"])
    history = history_with([(["L-8"], "fail", "finished")] * NEVER_PASSED_AFTER
                           + [(["L-7"], "pass", "finished")])
    monkeypatch.setattr(ms, "_lesson_history", lambda: history)
    code, out = run_ms("lessons", "sample-project")
    assert code == 0
    assert "L-8  [visual] in the prompt" in out
    assert f"used in {NEVER_PASSED_AFTER} attempts, 0 passed; never in a passing attempt" in out
    assert "used in 1 attempt, 1 passed" in out
    assert "L-1  [visual] does not fit" in out
    assert ("2 approved lessons don't fit in the 2,000-character budget and aren't used; "
            "reject or shorten some") in out
    assert "The budget is fixed" in out


def test_ms_status_mentions_dropped_lessons_only_when_some_are_dropped():
    from core.master import Master
    from core.ms import owner_needs
    from core.run_config import load_config

    from core.history import InMemoryHistoryStore

    master = Master(default_projects_root())
    store = LessonStore(default_projects_root() / "sample-project")

    def needs():
        return [n for n in owner_needs(master, InMemoryHistoryStore(),
                                       load_config("/nonexistent"), "sample-project")
                if "lesson" in n]

    approve(store, "visual", ["a" * 300, "b" * 300])
    assert needs() == []
    approve(store, "logic", [c * 300 for c in "cdefghi"])
    [line] = needs()
    assert "1 approved lesson doesn't fit in the 2,000-character budget" in line
    assert "ms lessons sample-project" in line


def test_the_owner_can_reject_an_approved_lesson():
    store = LessonStore(default_projects_root() / "sample-project")
    approve(store, "visual", ["A", "B"])
    store.propose("visual", ["C"], "t1", "a1")
    code, out = run_ms("lessons", "sample-project", "--reject", "L-1")
    assert code == 0 and "rejected 1" in out
    assert [r["id"] for r in store.approved()] == ["L-2"]
    assert [r["id"] for r in store.pending()] == ["L-3"]
    assert "--reject L-2" in out or "--reject IDS" in out
    # "rest" still means the pending lessons only.
    code, out = run_ms("lessons", "sample-project", "--reject", "rest")
    assert [r["id"] for r in store.approved()] == ["L-2"] and store.pending() == []
