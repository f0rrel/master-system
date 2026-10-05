import sys
import json
from pathlib import Path

sys.path.insert(0, ".")

from core.master import Master
from core.reasoning_engine import ReasoningEngine
from core.reasoning import ReasoningInterface, Decision


class DummyProv:
    def __init__(self, response):
        self._r = response

    def complete(self, prompt, schema=None):
        return self._r if isinstance(self._r, str) else json.dumps(self._r)


def make_project(root, tasks):
    proj = Path(root) / "projects" / "p"
    proj.mkdir(parents=True)
    (proj / "project.yaml").write_text("id: p\nname: P\nstatus: active\n")
    (proj / "milestones.yaml").write_text(
        "milestones:\n  - id: m1\n    name: M1\n    status: in_progress\n"
    )
    lines = ["tasks:"]
    for t in tasks:
        lines.append(f"  - id: {t['id']}")
        lines.append(f"    milestone: {t.get('milestone','m1')}")
        lines.append(f"    title: {t.get('title', t['id'])}")
        lines.append(f"    status: {t['status']}")
        lines.append(f"    assigned_to: {t.get('assigned_to','master')}")
        if t.get("depends_on"):
            lines.append("    depends_on:")
            for d in t["depends_on"]:
                lines.append(f"      - {d}")
    (proj / "tasks.yaml").write_text("\n".join(lines) + "\n")
    return Path(root) / "projects"


def test_multiple_ready_tasks_selects_one(tmp_path):
    root = make_project(tmp_path, [
        {"id": "t1", "status": "planned"},
        {"id": "t2", "status": "planned"},
    ])
    m = Master(root)
    e = ReasoningEngine(DummyProv({
        "decision": "act",
        "reason": "pick t1",
        "operation": {"operation": "update_task", "project_id": "p", "task_id": "t1", "status": "in_progress"}
    }), m, ReasoningInterface(m))
    res = e.reason("next", "p")
    assert res._decision.decision == Decision.ACT


def test_blocked_not_selected(tmp_path):
    root = make_project(tmp_path, [
        {"id": "t1", "status": "in_progress"},
        {"id": "t2", "status": "planned", "depends_on": ["t1"]},
    ])
    m = Master(root)
    e = ReasoningEngine(DummyProv({
        "decision": "blocked",
        "reason": "deps pending",
        "operation": None
    }), m, ReasoningInterface(m))
    res = e.reason("next", "p")
    assert res._decision.decision == Decision.BLOCKED


def test_no_actionable_wait(tmp_path):
    root = make_project(tmp_path, [
        {"id": "t1", "status": "in_progress"},
    ])
    m = Master(root)
    e = ReasoningEngine(DummyProv({
        "decision": "wait",
        "reason": "nothing ready",
        "operation": None
    }), m, ReasoningInterface(m))
    res = e.reason("next", "p")
    assert res._decision.decision == Decision.WAIT


def test_inprogress_not_selected_as_new(tmp_path):
    root = make_project(tmp_path, [
        {"id": "t1", "status": "in_progress"},
    ])
    m = Master(root)
    e = ReasoningEngine(DummyProv({
        "decision": "needs_information",
        "reason": "pending work",
        "operation": None
    }), m, ReasoningInterface(m))
    res = e.reason("next", "p")
    assert res._decision.decision == Decision.NEEDS_INFORMATION


def test_completed_not_selected(tmp_path):
    root = make_project(tmp_path, [
        {"id": "t1", "status": "completed"},
    ])
    m = Master(root)
    e = ReasoningEngine(DummyProv({
        "decision": "request_approval",
        "reason": "no work",
        "operation": {"operation": "inspect_project", "project_id": "p"}
    }), m, ReasoningInterface(m))
    res = e.reason("next", "p")
    assert res._decision.decision == Decision.REQUEST_APPROVAL


def test_planned_with_complete_deps_ready(tmp_path):
    root = make_project(tmp_path, [
        {"id": "t1", "status": "completed"},
        {"id": "t2", "status": "planned", "depends_on": ["t1"]},
    ])
    m = Master(root)
    e = ReasoningEngine(DummyProv({
        "decision": "act",
        "reason": "do t2",
        "operation": {"operation": "update_task", "project_id": "p", "task_id": "t2", "status": "in_progress"}
    }), m, ReasoningInterface(m))
    res = e.reason("next", "p")
    assert res._decision.decision == Decision.ACT
