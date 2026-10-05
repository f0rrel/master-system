import sys
from pathlib import Path

sys.path.insert(0, ".")

from conftest import attach_repository
from core.execution import ExecutionResult, ExecutionBackend
from core.verification import VerificationResult, VerificationBackend
from core.master import Master
from core.task_orchestrator import TaskOrchestrator
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


class FakeExec(ExecutionBackend):
    def __init__(self, result=None):
        self.result = result or ExecutionResult(status="success", artifacts={"e": 1})
        self.calls = []
    def execute(self, task, context, workspace=None):
        self.calls.append((task, context))
        return self.result


class FakeVerify(VerificationBackend):
    def __init__(self, verdict="pass", summary="ok"):
        self.verdict = verdict
        self.summary = summary
        self.calls = []
    def verify(self, task, context, evidence=None, workspace=None):
        self.calls.append((task, context, evidence))
        return VerificationResult(verdict=self.verdict, summary=self.summary, evidence=evidence or {})


class Prov:
    def __init__(self, response):
        self._r = response
    def complete(self, prompt, schema=None):
        import json
        return self._r if isinstance(self._r, str) else json.dumps(self._r)


def test_success_pass_results_available(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    before = m.status("p")
    orch = TaskOrchestrator(m, FakeExec(), FakeVerify("pass"))
    res = orch.orchestrate("p", "t1")
    assert res["execution"]["status"] == "success"
    assert res["verification"]["verdict"] == "pass"
    after = m.status("p")
    assert before == after


def test_success_fail_master_decides(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    orch = TaskOrchestrator(m, FakeExec(), FakeVerify("fail", "bad"))
    res = orch.orchestrate("p", "t1")
    assert res["verification"]["verdict"] == "fail"
    e = ReasoningEngine(Prov({
        "decision": "needs_information",
        "reason": "qa failed",
        "operation": None
    }), m, ReasoningInterface(m))
    dec = e.reason("next", "p")
    assert dec._decision.decision == Decision.NEEDS_INFORMATION


def test_success_needs_human_master_decides(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    orch = TaskOrchestrator(m, FakeExec(), FakeVerify("needs_human"))
    res = orch.orchestrate("p", "t1")
    assert res["verification"]["verdict"] == "needs_human"
    e = ReasoningEngine(Prov({
        "decision": "request_approval",
        "reason": "needs human",
        "operation": {"operation": "inspect_project", "project_id": "p"}
    }), m, ReasoningInterface(m))
    dec = e.reason("next", "p")
    assert dec._decision.decision == Decision.REQUEST_APPROVAL


def test_execution_failure_master_decides(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    orch = TaskOrchestrator(m, FakeExec(ExecutionResult(status="failed", reason="x")), FakeVerify("fail"))
    res = orch.orchestrate("p", "t1")
    assert res["execution"]["status"] == "failed"
    e = ReasoningEngine(Prov({
        "decision": "wait",
        "reason": "exec failed",
        "operation": None
    }), m, ReasoningInterface(m))
    dec = e.reason("next", "p")
    assert dec._decision.decision == Decision.WAIT
