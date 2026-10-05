import sys
from pathlib import Path
import tempfile

sys.path.insert(0, ".")

from core.execution import ExecutionBackend, ExecutionResult
from core.master import Master
from core.execution_runner import TaskExecutionRunner
from core.opencode_backend import OpenCodeCliBackend
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


def fake_opencode(tmp_path, body="echo done"):
    """An executable standing in for the opencode CLI."""
    script = tmp_path / "fake-opencode"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(0o755)
    return script


def run_through_runner(tmp_path, body="echo done", seconds=60):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress", "title": "Do X"}])
    m = Master(root)
    work = tmp_path / "ws"
    work.mkdir()
    backend = OpenCodeCliBackend(opencode_bin=fake_opencode(tmp_path, body))
    result = TaskExecutionRunner(m, backend).execute(
        "p", "t1", workspace=workspace_for(work, seconds=seconds, grace=1)
    )
    return m, work, result


def test_backend_implements_interface(tmp_path):
    b = OpenCodeCliBackend()
    assert isinstance(b, ExecutionBackend)


def test_valid_task_reaches_opencode(tmp_path):
    _, work, res = run_through_runner(tmp_path, 'echo "$@" > args')
    assert res.status == "success"
    assert "Title: Do X" in (work / "args").read_text()


def test_opencode_runs_in_the_given_workspace(tmp_path):
    _, work, res = run_through_runner(tmp_path, "pwd > where")
    assert (work / "where").read_text().strip() == str(work.resolve())


def test_success_maps_to_success(tmp_path):
    assert run_through_runner(tmp_path, "exit 0")[2].status == "success"


def test_failure_maps_to_failed(tmp_path):
    assert run_through_runner(tmp_path, "exit 5")[2].status == "failed"


def test_timeout_maps_to_failed(tmp_path):
    res = run_through_runner(tmp_path, "sleep 60", seconds=1)[2]
    assert res.status == "failed"
    assert res.artifacts["timeout"] is True


def test_backend_does_not_mutate_state(tmp_path):
    m, _, _ = run_through_runner(tmp_path, "echo 'status: completed' > tasks.yaml")
    assert m.status("p")["tasks"][0]["status"] == "in_progress"


def test_state_updates_never_applied(tmp_path):
    m, _, res = run_through_runner(tmp_path)
    assert isinstance(res.state_updates, dict)
    assert m.status("p")["tasks"][0]["status"] == "in_progress"
