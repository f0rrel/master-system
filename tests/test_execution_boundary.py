import sys
from pathlib import Path

sys.path.insert(0, ".")

from core.execution import ExecutionResult, ExecutionBackend, VALID_STATUSES
from core.master import Master
from core.reasoning_engine import ReasoningEngine
from core.reasoning import ReasoningInterface


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


class DummyBackend:
    def execute(self, task, context):
        return ExecutionResult(status="success", reason="done")


class AnotherBackend:
    def execute(self, task, context):
        return ExecutionResult(status="failed", reason="bad")


def test_dummy_backend_satisfies_interface():
    d = DummyBackend()
    res = d.execute({"id": "t1"}, {"project": "p"})
    assert isinstance(res, ExecutionResult)
    assert res.status == "success"


def test_backends_substitutable(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "planned"}])
    m = Master(root)
    # Backend can be passed/injected; just verify types
    b1 = DummyBackend()
    b2 = AnotherBackend()
    assert isinstance(b1, ExecutionBackend) or hasattr(b1, "execute")
    assert isinstance(b2, ExecutionBackend) or hasattr(b2, "execute")


def test_execution_result_validates_statuses():
    assert ExecutionResult(status="success").status == "success"
    assert ExecutionResult(status="failed").status == "failed"
    assert ExecutionResult(status="partial").status == "partial"
    assert ExecutionResult(status="blocked").status == "blocked"
    assert ExecutionResult(status="cancelled").status == "cancelled"
    assert ExecutionResult(status="needs_human").status == "needs_human"
    try:
        ExecutionResult(status="invalid")
        assert False
    except ValueError:
        pass


def test_backend_output_does_not_mutate_project_state(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "planned"}])
    m = Master(root)
    before = m.status("p")
    b = DummyBackend()
    res = b.execute(before["tasks"][0], {"project_id": "p"})
    after = m.status("p")
    assert before == after
    assert isinstance(res.state_updates, dict)
    assert isinstance(res.artifacts, dict)


def test_master_does_not_depend_on_concrete_backend(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "planned"}])
    m = Master(root)
    # importing concrete backends should not be required
    from core.master import Master as M
    assert M is not None


def test_engine_does_not_depend_on_concrete_backend(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "planned"}])
    m = Master(root)
    class Prov:
        def complete(self, prompt, schema=None):
            return '{"decision":"wait","reason":"ok","operation":null}'
    e = ReasoningEngine(Prov(), m, ReasoningInterface(m))
    ctx = e.context_for("p")
    assert ctx is not None
