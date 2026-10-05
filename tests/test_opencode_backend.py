import sys
from pathlib import Path
import tempfile

sys.path.insert(0, ".")

from core.execution import ExecutionBackend, ExecutionResult
from core.master import Master
from core.execution_runner import TaskExecutionRunner
from core.opencode_backend import OpenCodeCliBackend


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


class FakeOpenCode:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls = []

    def run(self, cmd, cwd=None, capture_output=True, text=True, timeout=None):
        self.calls.append((cmd, cwd, timeout))
        class R:
            pass
        r = R()
        r.returncode = self.returncode
        r.stdout = self.stdout
        r.stderr = self.stderr
        return r


def test_backend_implements_interface(tmp_path):
    b = OpenCodeCliBackend(workdir=tmp_path)
    assert isinstance(b, ExecutionBackend)


def test_valid_task_reaches_opencode(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress", "title": "Do X"}])
    m = Master(root)
    fake = FakeOpenCode()
    b = OpenCodeCliBackend(workdir=tmp_path)
    b._opencode_bin = "opencode"  # placeholder
    # monkey patch subprocess
    import core.opencode_backend as ob
    old = ob.subprocess.run
    try:
        ob.subprocess.run = fake.run
        runner = TaskExecutionRunner(m, b)
        res = runner.execute("p", "t1")
        assert res.status == "success"
        assert fake.calls
    finally:
        ob.subprocess.run = old


def test_workdir_controlled(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    fake = FakeOpenCode()
    work = tmp_path / "ws"
    b = OpenCodeCliBackend(workdir=work)
    import core.opencode_backend as ob
    old = ob.subprocess.run
    try:
        ob.subprocess.run = fake.run
        runner = TaskExecutionRunner(m, b)
        res = runner.execute("p", "t1")
        assert fake.calls
        cmd, cwd, timeout = fake.calls[0]
        assert str(work.resolve()) == cwd
    finally:
        ob.subprocess.run = old


def test_success_maps_to_success(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    fake = FakeOpenCode(returncode=0)
    b = OpenCodeCliBackend(workdir=tmp_path)
    import core.opencode_backend as ob
    old = ob.subprocess.run
    try:
        ob.subprocess.run = fake.run
        runner = TaskExecutionRunner(m, b)
        res = runner.execute("p", "t1")
        assert res.status == "success"
    finally:
        ob.subprocess.run = old


def test_failure_maps_to_failed(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    fake = FakeOpenCode(returncode=5)
    b = OpenCodeCliBackend(workdir=tmp_path)
    import core.opencode_backend as ob
    old = ob.subprocess.run
    try:
        ob.subprocess.run = fake.run
        runner = TaskExecutionRunner(m, b)
        res = runner.execute("p", "t1")
        assert res.status == "failed"
    finally:
        ob.subprocess.run = old


def test_timeout_maps_to_failed(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    import subprocess
    import core.opencode_backend as ob

    def timeout_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0] if a else [], timeout=k.get("timeout"))

    b = OpenCodeCliBackend(workdir=tmp_path, timeout=1)
    old = ob.subprocess.run
    try:
        ob.subprocess.run = timeout_run
        runner = TaskExecutionRunner(m, b)
        res = runner.execute("p", "t1")
        assert res.status == "failed"
    finally:
        ob.subprocess.run = old


def test_backend_does_not_mutate_state(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    before = m.status("p")
    fake = FakeOpenCode()
    b = OpenCodeCliBackend(workdir=tmp_path)
    import core.opencode_backend as ob
    old = ob.subprocess.run
    try:
        ob.subprocess.run = fake.run
        runner = TaskExecutionRunner(m, b)
        runner.execute("p", "t1")
        after = m.status("p")
        assert before == after
    finally:
        ob.subprocess.run = old


def test_state_updates_never_applied(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    fake = FakeOpenCode()
    b = OpenCodeCliBackend(workdir=tmp_path)
    import core.opencode_backend as ob
    old = ob.subprocess.run
    try:
        ob.subprocess.run = fake.run
        runner = TaskExecutionRunner(m, b)
        res = runner.execute("p", "t1")
        assert isinstance(res.state_updates, dict)
        after = m.status("p")
        assert after["tasks"][0]["status"] == "in_progress"
    finally:
        ob.subprocess.run = old


def test_replaceable_mocked(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    m = Master(root)
    fake = FakeOpenCode(returncode=0)
    b = OpenCodeCliBackend(workdir=tmp_path)
    import core.opencode_backend as ob
    old = ob.subprocess.run
    try:
        ob.subprocess.run = fake.run
        runner = TaskExecutionRunner(m, b)
        res = runner.execute("p", "t1")
        assert res.status == "success"
    finally:
        ob.subprocess.run = old
