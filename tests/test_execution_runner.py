import sys
from pathlib import Path

sys.path.insert(0, ".")

from core.execution import ExecutionResult
from core.execution_runner import TaskExecutionRunner, ExecutionError
from core.master import Master
from conftest import workspace_for


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


class RecordingBackend:
    def __init__(self, result=None):
        self.result = result or ExecutionResult(status="success", reason="ok")
        self.calls = []

    def execute(self, task, context, workspace=None):
        self.calls.append((dict(task), dict(context)))
        return self.result


def test_valid_task_reaches_backend_with_authoritative_context(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    b = RecordingBackend()
    runner = TaskExecutionRunner(m, b)
    res = runner.execute("p", "t1", workspace=workspace_for(tmp_path))
    assert res.status == "success"
    assert b.calls
    task, ctx = b.calls[0]
    assert task["id"] == "t1"
    assert ctx["project_id"] == "p"
    assert ctx["task_id"] == "t1"


def test_backend_swappable(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    b1 = RecordingBackend(ExecutionResult(status="success"))
    b2 = RecordingBackend(ExecutionResult(status="failed"))
    assert TaskExecutionRunner(m, b1).execute("p", "t1", workspace=workspace_for(tmp_path)).status == "success"
    assert TaskExecutionRunner(m, b2).execute("p", "t1", workspace=workspace_for(tmp_path)).status == "failed"


def test_nonexistent_task_rejected(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    b = RecordingBackend()
    runner = TaskExecutionRunner(m, b)
    try:
        runner.execute("p", "nope", workspace=workspace_for(tmp_path))
        assert False
    except ExecutionError:
        pass


def test_blocked_task_cannot_be_executed(tmp_path):
    root = make_project(tmp_path, [
        {"id": "t1", "status": "in_progress"},
        {"id": "t2", "status": "planned", "depends_on": ["t1"]},
    ])
    m = Master(root)
    b = RecordingBackend()
    runner = TaskExecutionRunner(m, b)
    try:
        runner.execute("p", "t2", workspace=workspace_for(tmp_path))
        assert False
    except ExecutionError:
        pass


def test_runner_does_not_mutate_project_state(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    before = m.status("p")
    b = RecordingBackend()
    runner = TaskExecutionRunner(m, b)
    runner.execute("p", "t1", workspace=workspace_for(tmp_path))
    after = m.status("p")
    assert before == after


def test_state_updates_returned_but_not_applied(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    res = ExecutionResult(status="success", reason="ok", state_updates={"task_id": "t1", "status": "completed"})
    b = RecordingBackend(result=res)
    runner = TaskExecutionRunner(m, b)
    out = runner.execute("p", "t1", workspace=workspace_for(tmp_path))
    assert out.state_updates == {"task_id": "t1", "status": "completed"}
    after = m.status("p")
    assert after["tasks"][0]["status"] == "in_progress"


def test_backend_result_returned(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    res = ExecutionResult(status="partial", reason="partial", artifacts={"log": "x"})
    b = RecordingBackend(result=res)
    runner = TaskExecutionRunner(m, b)
    out = runner.execute("p", "t1", workspace=workspace_for(tmp_path))
    assert out is res


def test_no_concrete_backend_dependency(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    b = RecordingBackend()
    runner = TaskExecutionRunner(m, b)
    runner.execute("p", "t1", workspace=workspace_for(tmp_path))
    # importing concrete backends not required


def test_planned_cannot_be_executed(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "planned"}])
    m = Master(root)
    b = RecordingBackend()
    runner = TaskExecutionRunner(m, b)
    try:
        runner.execute("p", "t1", workspace=workspace_for(tmp_path))
        assert False
    except ExecutionError:
        pass
