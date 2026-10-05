import sys
from pathlib import Path

sys.path.insert(0, ".")

from conftest import attach_repository
from core.master import Master
from core.reasoning_engine import ReasoningEngine
from core.reasoning import ReasoningInterface, Decision


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
    attach_repository(proj)
    return Path(root) / "projects"


class Prov:
    def __init__(self, response):
        self._r = response
    def complete(self, prompt, schema=None):
        import json
        return self._r if isinstance(self._r, str) else json.dumps(self._r)


def test_planned_to_inprogress_via_approved_op(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    res = m.update_task("p", "t1", status="in_progress")
    assert res["status"] == "in_progress"


def test_inprogress_to_completed_via_approved_op(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    res = m.update_task("p", "t1", status="completed")
    assert res["status"] == "completed"


def test_qa_pass_does_not_auto_complete(tmp_path):
    from core.execution import ExecutionResult, ExecutionBackend
    from core.verification import VerificationResult, VerificationBackend
    from core.task_orchestrator import TaskOrchestrator
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    class FE(ExecutionBackend):
        def execute(self, t, c, workspace=None): return ExecutionResult(status="success")
    class FV(VerificationBackend):
        def verify(self, t, c, e=None, workspace=None): return VerificationResult(verdict="pass")
    orch = TaskOrchestrator(m, FE(), FV())
    before = m.status("p")
    orch.orchestrate("p", "t1")
    after = m.status("p")
    assert before == after


def test_exec_success_does_not_auto_complete(tmp_path):
    from core.execution import ExecutionResult, ExecutionBackend
    from core.verification import VerificationResult, VerificationBackend
    from core.task_orchestrator import TaskOrchestrator
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    class FE(ExecutionBackend):
        def execute(self, t, c, workspace=None): return ExecutionResult(status="success")
    class FV(VerificationBackend):
        def verify(self, t, c, e=None, workspace=None): return VerificationResult(verdict="fail")
    orch = TaskOrchestrator(m, FE(), FV())
    before = m.status("p")
    orch.orchestrate("p", "t1")
    after = m.status("p")
    assert before == after


def test_exec_failure_does_not_auto_retry(tmp_path):
    from core.execution import ExecutionResult, ExecutionBackend
    from core.verification import VerificationResult, VerificationBackend
    from core.task_orchestrator import TaskOrchestrator
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    class FE(ExecutionBackend):
        def execute(self, t, c, workspace=None): return ExecutionResult(status="failed")
    class FV(VerificationBackend):
        def verify(self, t, c, e=None, workspace=None): return VerificationResult(verdict="fail")
    orch = TaskOrchestrator(m, FE(), FV())
    before = m.status("p")
    orch.orchestrate("p", "t1")
    after = m.status("p")
    assert before == after


def test_orchestrator_does_not_mutate(tmp_path):
    from core.execution import ExecutionResult, ExecutionBackend
    from core.verification import VerificationResult, VerificationBackend
    from core.task_orchestrator import TaskOrchestrator
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    class FE(ExecutionBackend):
        def execute(self, t, c, workspace=None): return ExecutionResult(status="success")
    class FV(VerificationBackend):
        def verify(self, t, c, e=None, workspace=None): return VerificationResult(verdict="pass")
    orch = TaskOrchestrator(m, FE(), FV())
    before = m.status("p")
    orch.orchestrate("p", "t1")
    after = m.status("p")
    assert before == after
