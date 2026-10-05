import sys
from pathlib import Path
import tempfile
import shutil

sys.path.insert(0, ".")

from core.master import Master
from core.reasoning_engine import ReasoningEngine
from core.reasoning import ReasoningInterface


class DummyProvider:
    def __init__(self):
        self.calls = 0
        self.prompts = []
        self.schemas = []

    def complete(self, prompt, schema=None):
        self.calls += 1
        self.prompts.append(prompt)
        self.schemas.append(schema)
        return '{"decision":"wait","reason":"ok","operation":null}'


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


def test_planned_no_deps_ready_in_context(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "planned"}])
    m = Master(root)
    e = ReasoningEngine(DummyProvider(), m, ReasoningInterface(m))
    ctx = e.context_for("p")
    assert any(t["id"] == "t1" and t["readiness"] == "ready" for t in ctx["ready_tasks"])
    assert "t1" in str(ctx["ready_tasks"])


def test_planned_dep_completed_ready(tmp_path):
    root = make_project(
        tmp_path,
        [
            {"id": "t1", "status": "completed"},
            {"id": "t2", "status": "planned", "depends_on": ["t1"]},
        ],
    )
    m = Master(root)
    e = ReasoningEngine(DummyProvider(), m, ReasoningInterface(m))
    ctx = e.context_for("p")
    assert any(t["id"] == "t2" and t["readiness"] == "ready" for t in ctx["ready_tasks"])


def test_planned_dep_inprogress_blocked(tmp_path):
    root = make_project(
        tmp_path,
        [
            {"id": "t1", "status": "in_progress"},
            {"id": "t2", "status": "planned", "depends_on": ["t1"]},
        ],
    )
    m = Master(root)
    e = ReasoningEngine(DummyProvider(), m, ReasoningInterface(m))
    ctx = e.context_for("p")
    assert any(t["id"] == "t2" and t["readiness"] == "blocked" for t in ctx["blocked_tasks"])
    assert ctx["blocked_tasks"][0]["blocked_by"] == "t1"
    assert ctx["blocked_tasks"][0]["readiness_reason"]


def test_inprogress_separately(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    e = ReasoningEngine(DummyProvider(), m, ReasoningInterface(m))
    ctx = e.context_for("p")
    assert any(t["id"] == "t1" for t in ctx["in_progress_tasks"])


def test_completed_separately(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "completed"}])
    m = Master(root)
    e = ReasoningEngine(DummyProvider(), m, ReasoningInterface(m))
    ctx = e.context_for("p")
    assert any(t["id"] == "t1" for t in ctx["completed_tasks"])
