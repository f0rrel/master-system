import sys
from pathlib import Path

sys.path.insert(0, ".")

from core.execution import ExecutionResult, ExecutionBackend
from core.execution_runner import TaskExecutionRunner
from core.verification import VerificationResult, VerificationBackend
from core.master import Master


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
        self.result = result or ExecutionResult(status="success", artifacts={"evidence": "ok"})
        self.calls = []
    def execute(self, task, context):
        self.calls.append((dict(task), dict(context)))
        return self.result


class FakeVerify(VerificationBackend):
    def __init__(self, verdict="pass", summary="ok"):
        self.verdict = verdict
        self.summary = summary
        self.calls = []
    def verify(self, task, context, evidence=None):
        self.calls.append((dict(task), dict(context), dict(evidence) if evidence else {}))
        return VerificationResult(verdict=self.verdict, summary=self.summary, evidence=evidence or {})


class FakeReasoningProvider:
    def __init__(self, response):
        self._r = response
    def complete(self, prompt, schema=None):
        import json
        return self._r if isinstance(self._r, str) else json.dumps(self._r)


def test_success_pass_loop(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    before = m.status("p")
    execb = FakeExec()
    runner = TaskExecutionRunner(m, execb)
    exec_res = runner.execute("p", "t1")
    assert exec_res.status == "success"
    ver = FakeVerify("pass")
    task = before["tasks"][0]
    vres = ver.verify(task, {"project_id": "p"}, exec_res.artifacts)
    assert vres.verdict == "pass"
    after = m.status("p")
    after = m.status("p")
    assert before == after


def test_success_fail_loop(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    before = m.status("p")
    execb = FakeExec()
    runner = TaskExecutionRunner(m, execb)
    exec_res = runner.execute("p", "t1")
    ver = FakeVerify("fail", "issues")
    task = before["tasks"][0]
    vres = ver.verify(task, {"project_id": "p"}, exec_res.artifacts)
    assert vres.verdict == "fail"
    after = m.status("p")
    assert before == after


def test_success_needs_human(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    before = m.status("p")
    execb = FakeExec()
    runner = TaskExecutionRunner(m, execb)
    exec_res = runner.execute("p", "t1")
    ver = FakeVerify("needs_human")
    task = before["tasks"][0]
    vres = ver.verify(task, {"project_id": "p"}, exec_res.artifacts)
    assert vres.verdict == "needs_human"
    after = m.status("p")
    assert before == after


def test_execution_failure_loop(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    before = m.status("p")
    execb = FakeExec(result=ExecutionResult(status="failed", reason="boom"))
    runner = TaskExecutionRunner(m, execb)
    exec_res = runner.execute("p", "t1")
    assert exec_res.status == "failed"
    ver = FakeVerify("fail")
    task = before["tasks"][0]
    vres = ver.verify(task, {"project_id": "p"}, exec_res.artifacts)
    assert vres.verdict == "fail"
    after = m.status("p")
    assert before == after
