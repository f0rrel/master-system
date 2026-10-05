"""Tests for the project mutation lock and the human tools that honour it."""

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from core import master as master_cli
from core import reason_cli
from core.provider import ReasoningProvider
from core.run_lock import LOCK_FILENAME, ProjectBusyError, ProjectLock

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def project(tmp_path):
    path = tmp_path / "projects" / "alpha-project"
    path.mkdir(parents=True)
    (path / "project.yaml").write_text(
        yaml.safe_dump({"id": "alpha", "name": "alpha", "status": "active"})
    )
    (path / "milestones.yaml").write_text(yaml.safe_dump(
        {"milestones": [{"id": "m1", "name": "M1", "status": "in_progress"}]}
    ))
    (path / "tasks.yaml").write_text(yaml.safe_dump({"tasks": [
        {"id": "t1", "milestone": "m1", "title": "T1", "status": "planned",
         "assigned_to": "master"}
    ]}))
    return path


def data_of(path):
    return {p.name: p.read_bytes() for p in sorted(path.iterdir())
            if p.is_file() and p.name != LOCK_FILENAME}


# --- the lock itself ------------------------------------------------------


def test_a_second_holder_is_refused(project):
    with ProjectLock(project, holder="first"):
        with pytest.raises(ProjectBusyError, match="first"):
            ProjectLock(project, holder="second").acquire()


def test_the_lock_is_released_on_exit_and_on_error(project):
    with ProjectLock(project):
        pass
    with pytest.raises(RuntimeError):
        with ProjectLock(project):
            raise RuntimeError("boom")

    with ProjectLock(project):
        pass


def test_the_lock_file_is_not_a_project(project):
    from core.master import Master

    with ProjectLock(project):
        assert Master(project.parent).list_projects() == ["alpha"]


HOLDER = '''
import sys
sys.path.insert(0, {repo!r})
from core.run_lock import ProjectLock
lock = ProjectLock({path!r}, holder="other-process").acquire()
print("locked", flush=True)
sys.stdin.readline()
'''


def hold_in_another_process(path):
    child = subprocess.Popen(
        [sys.executable, "-c", HOLDER.format(repo=str(REPO), path=str(path))],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    assert child.stdout.readline().strip() == "locked"
    return child


def test_a_lock_held_by_another_process_is_refused_and_freed_when_it_dies(project):
    child = hold_in_another_process(project)
    try:
        with pytest.raises(ProjectBusyError, match="other-process"):
            ProjectLock(project).acquire()
    finally:
        child.kill()  # no clean release: the OS must drop the lock
        child.wait()

    with ProjectLock(project):
        pass


# --- core.master CLI ------------------------------------------------------


def test_a_human_mutation_is_refused_while_the_project_is_locked(project, capsys):
    before = data_of(project)
    with ProjectLock(project, holder="session=s1"):
        code = master_cli.main(["--root", str(project.parent), "update-task",
                                "alpha", "t1", "--status", "blocked"])

    assert code == 1
    assert "in use by another process" in capsys.readouterr().err
    assert data_of(project) == before


def test_reading_does_not_need_the_lock(project, capsys):
    with ProjectLock(project):
        code = master_cli.main(["--root", str(project.parent), "status", "alpha"])

    assert code == 0
    assert "t1" in capsys.readouterr().out


def test_a_human_mutation_works_when_the_project_is_free(project):
    code = master_cli.main(["--root", str(project.parent), "update-task",
                            "alpha", "t1", "--status", "blocked"])

    assert code == 0
    assert "status: blocked" in (project / "tasks.yaml").read_text()


# --- reason_cli -----------------------------------------------------------


class Stub(ReasoningProvider):
    name = "stub"

    def complete(self, prompt, schema=None):
        return json.dumps({"reasoning": "r", "operations": [
            {"operation": "update_task", "project_id": "alpha", "task_id": "t1",
             "status": "blocked"}
        ]})


def test_an_approved_proposal_is_refused_while_the_project_is_locked(
    project, monkeypatch
):
    monkeypatch.setattr(reason_cli, "build_provider", lambda args: Stub())
    before = data_of(project)
    err = io.StringIO()

    with ProjectLock(project, holder="session=s1"):
        code = reason_cli.main(
            argv=["block it", "--root", str(project.parent), "--project", "alpha"],
            read_line=lambda: "y", stdout=io.StringIO(), stderr=err,
        )

    assert code == reason_cli.EXIT_BUSY
    assert "in use by another process" in err.getvalue()
    assert data_of(project) == before
