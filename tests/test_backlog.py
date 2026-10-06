"""Backlog epics: priorities, proposed epics, ordering, `ms backlog` and the planner."""

import io

import pytest
import yaml

from core.backlog import backlog_rows, ordered_tasks, render_backlog
from core.daemon import work_for
from core.history import InMemoryHistoryStore
from core.master import Master
from core.ms import main as ms_main, slug
from core.planner import draft_problems, planner_settings, plannable_epics
from core.reasoning_engine import ReasoningEngine
from core.work_manager import DuplicateRecordError, InvalidFieldError


def task(tid, milestone, status="planned", **extra):
    return {"id": tid, "milestone": milestone, "title": tid, "status": status,
            "acceptance": {"commands": ["true"]}, **extra}


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "projects"
    (root / "app").mkdir(parents=True)
    (root / "app" / "project.yaml").write_text(yaml.safe_dump(
        {"id": "app", "name": "App", "status": "active"}))
    (root / "app" / "milestones.yaml").write_text(yaml.safe_dump({"milestones": [
        {"id": "later", "name": "Later", "status": "planned", "priority": 5},
        {"id": "first", "name": "First", "status": "planned", "priority": 1},
        {"id": "loose", "name": "No priority", "status": "planned"}]}))
    (root / "app" / "tasks.yaml").write_text(yaml.safe_dump({"tasks": [
        task("l-1", "later"), task("n-1", "loose"), task("f-1", "first"),
        task("f-2", "first")]}))
    return root


def test_tasks_are_ordered_by_epic_priority_then_file_order(root):
    status = Master(root).status("app")
    assert [t["id"] for t in ordered_tasks(status["milestones"], status["tasks"])] == \
        ["f-1", "f-2", "l-1", "n-1"]
    assert work_for(status, InMemoryHistoryStore())[0] == ["f-1", "f-2", "l-1", "n-1"]


def test_the_master_sees_ready_tasks_in_backlog_order(root):
    context = ReasoningEngine(provider=None, master=Master(root)).context_for("app")
    assert [t["id"] for t in context["ready_tasks"]] == ["f-1", "f-2", "l-1", "n-1"]
    assert [m["id"] for m in context["milestones"]] == ["first", "later", "loose"]


def test_backlog_epics_are_proposed_and_get_the_next_priority(root):
    master = Master(root)
    epic = master.add_backlog_epic("app", "epic-tiles", "Tiles", "Six tiles.")
    assert epic == {"id": "epic-tiles", "name": "Tiles", "status": "proposed", "priority": 6,
                    "summary": "Six tiles."}
    with pytest.raises(DuplicateRecordError):
        master.add_backlog_epic("app", "epic-tiles", "Again")
    master.set_epic_priority("app", "epic-tiles", 0)
    rows = backlog_rows(master.status("app"))
    assert [r["id"] for r in rows][:2] == ["epic-tiles", "first"]
    assert rows[0]["number"] == 1 and rows[0]["status"] == "proposed"
    assert "proposed (not planned yet)" in render_backlog(rows)


def test_a_proposed_epic_changes_only_through_the_backlog_or_an_approved_plan(root):
    master = Master(root)
    master.add_backlog_epic("app", "epic-x", "X")
    with pytest.raises(InvalidFieldError, match="backlog"):
        master.update_milestone("app", "epic-x", status="in_progress")
    with pytest.raises(InvalidFieldError, match="backlog"):
        master.update_milestone("app", "first", status="proposed")
    with pytest.raises(InvalidFieldError, match="backlog"):
        master.create_milestone("app", "epic-y", "Y", status="proposed")
    with pytest.raises(InvalidFieldError, match="unapproved backlog epic"):
        master.create_task("app", "x-1", "epic-x", "sneaky")
    with pytest.raises(InvalidFieldError):
        master.set_epic_priority("app", "epic-x", -1)


def test_approving_a_plan_for_a_proposed_epic_keeps_its_place(root):
    master = Master(root)
    master.add_backlog_epic("app", "epic-x", "X", priority=2)
    assert plannable_epics(master.status("app")) == {"epic-x"}
    master.add_planned_work("app", {"id": "epic-x", "name": "X"}, [task("x-1", "epic-x")])
    epic = next(m for m in master.status("app")["milestones"] if m["id"] == "epic-x")
    assert epic["status"] == "planned" and epic["priority"] == 2
    assert plannable_epics(master.status("app")) == set()
    with pytest.raises(DuplicateRecordError):
        master.add_planned_work("app", {"id": "epic-x", "name": "X"}, [task("x-2", "epic-x")])


def test_draft_problems_for_backlog_drafts():
    settings = planner_settings({})
    ok = {"backlog": [{"id": "epic-a", "title": "A", "summary": "s", "priority": 3}]}
    assert draft_problems(ok, set(), {"m1"}, settings) == []
    bad = {"backlog": [{"id": "m1", "title": ""}, {"id": "Bad Id", "title": "x",
                                                    "priority": -2}]}
    problems = draft_problems(bad, set(), {"m1"}, settings)
    assert any("already used" in p for p in problems)
    assert any("missing title" in p for p in problems)
    assert any("invalid id" in p for p in problems)
    assert any("priority" in p for p in problems)
    plan = {"epic": {"id": "m1", "title": "M"}, "tasks": []}
    assert "the epic id m1 is already used" in draft_problems(plan, set(), {"m1"}, settings)
    assert "the epic id m1 is already used" not in \
        draft_problems(plan, set(), {"m1"}, settings, plannable_epics={"m1"})


def test_slug():
    assert slug("Search page!") == "epic-search-page"
    assert len(slug("x " * 60)) <= 40


def run_ms(*argv):
    out = io.StringIO()
    code = ms_main(list(argv), out=out)
    return code, out.getvalue()


def test_ms_backlog_adds_lists_and_reorders():
    code, out = run_ms("backlog", "sample-project", "add", "Search", "page",
                       "--summary", "Six tiles.")
    assert code == 0 and "Added epic-search-page" in out
    code, out = run_ms("backlog", "sample-project", "priority", "epic-search-page", "0")
    assert code == 0 and "priority 0" in out
    code, out = run_ms("backlog", "sample-project")
    assert out.splitlines()[1].strip().startswith("1. [p0] epic-search-page: Search page")
    assert 'plan epic 1 from the backlog' in out
    code, out = run_ms("backlog", "sample-project", "priority", "nope", "1")
    assert code == 1 and "error" in out
