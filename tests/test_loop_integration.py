import sys
from pathlib import Path

sys.path.insert(0, ".")

from core.execution import ExecutionResult, ExecutionBackend
from core.verification import VerificationResult, VerificationBackend
from core.master import Master
from core.task_orchestrator import TaskOrchestrator
from core.reasoning_engine import ReasoningEngine
from core.reasoning import ReasoningInterface, Decision
from core.evidence import HistoryEvidence
from core.history import InMemoryHistoryStore


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


class FakeExec(ExecutionBackend):
    def __init__(self, result=None):
        self.result = result or ExecutionResult(status="success", artifacts={"e": 1})
    def execute(self, task, context):
        return self.result


class FakeVerify(VerificationBackend):
    def __init__(self, verdict="pass", summary="ok"):
        self.verdict = verdict
        self.summary = summary
    def verify(self, task, context, evidence=None):
        return VerificationResult(verdict=self.verdict, summary=self.summary, findings=[{"issue": self.summary}] if self.summary else [], evidence=evidence or {})


class Prov:
    def __init__(self, response):
        self._r = response
    def complete(self, prompt, schema=None):
        import json
        return self._r if isinstance(self._r, str) else json.dumps(self._r)


def attempt(m, exec_backend, verifier):
    """Run one recorded attempt and return evidence read back from history."""
    history = InMemoryHistoryStore()
    TaskOrchestrator(m, exec_backend, verifier, history=history).orchestrate(
        "p", "t1", run_id="r1"
    )
    return HistoryEvidence(history)


def latest(ctx):
    return ctx["execution_evidence"]["tasks"]["t1"]["latest_attempt"]


def test_success_fail_changes_decision(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    before = m.status("p")
    evidence = attempt(m, FakeExec(), FakeVerify("fail", "qa-issue"))
    e = ReasoningEngine(Prov({"decision": "wait", "reason": "initial", "operation": None}), m, ReasoningInterface(m), evidence_source=evidence)
    ctx = e.context_for("p")
    assert latest(ctx) is not None
    assert latest(ctx)["verification"]["verdict"] == "fail"
    e2 = ReasoningEngine(Prov({"decision": "needs_information", "reason": "qa failed", "operation": None}), m, ReasoningInterface(m), evidence_source=evidence)
    dec = e2.reason("next", "p")
    assert dec._decision.decision == Decision.NEEDS_INFORMATION
    after = m.status("p")
    assert before == after


def test_success_pass(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    evidence = attempt(m, FakeExec(), FakeVerify("pass"))
    e = ReasoningEngine(Prov({"decision": "wait", "reason": "ok", "operation": None}), m, ReasoningInterface(m), evidence_source=evidence)
    ctx = e.context_for("p")
    assert latest(ctx)["verification"]["verdict"] == "pass"


def test_success_needs_human_loop(tmp_path):
    from core.reasoning import Decision
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    evidence = attempt(m, FakeExec(), FakeVerify("needs_human"))
    e = ReasoningEngine(Prov({"decision": "request_approval", "reason": "needs human", "operation": {"operation": "inspect_project", "project_id": "p"}}), m, ReasoningInterface(m), evidence_source=evidence)
    ctx = e.context_for("p")
    assert latest(ctx)["verification"]["verdict"] == "needs_human"
    dec = e.reason("next", "p")
    assert dec._decision.decision == Decision.REQUEST_APPROVAL


def test_execution_failure_loop(tmp_path):
    from core.reasoning import Decision
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    evidence = attempt(m, FakeExec(ExecutionResult(status="failed", reason="boom")), FakeVerify("fail"))
    e = ReasoningEngine(Prov({"decision": "wait", "reason": "exec failed", "operation": None}), m, ReasoningInterface(m), evidence_source=evidence)
    ctx = e.context_for("p")
    assert latest(ctx)["outcome"] == "failed"
    dec = e.reason("next", "p")
    assert dec._decision.decision == Decision.WAIT
