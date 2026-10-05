"""The shared process helper: deadline, process group, inherited lock."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.run_lock import ProjectBusyError, ProjectLock
from core.worker_process import run_process

REPO = Path(__file__).resolve().parent.parent


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); it is not running.
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().split()[2] != "Z"
    except FileNotFoundError:
        return False


def wait_until(predicate, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_output_and_exit_status_are_returned(tmp_path):
    outcome = run_process(["sh", "-c", "echo out; echo err >&2; exit 3"], cwd=tmp_path, log_dir=tmp_path / "logs",
                          deadline=time.monotonic() + 10)

    assert (outcome.returncode, outcome.stdout, outcome.stderr) == (3, "out\n", "err\n")
    assert outcome.timed_out is False


def test_the_process_runs_in_the_given_directory(tmp_path):
    outcome = run_process(["pwd"], cwd=tmp_path, log_dir=tmp_path / "logs", deadline=time.monotonic() + 10)

    assert outcome.stdout.strip() == str(tmp_path.resolve())


def test_on_spawn_reports_pid_and_its_own_process_group(tmp_path):
    seen = []

    outcome = run_process(["true"], cwd=tmp_path, log_dir=tmp_path / "logs", deadline=time.monotonic() + 10,
                          on_spawn=lambda pid, pgid: seen.append((pid, pgid)))

    assert seen == [(outcome.pid, outcome.pgid)]
    assert outcome.pid == outcome.pgid != os.getpgid(0)


def test_past_the_deadline_the_whole_group_is_killed(tmp_path):
    started = time.monotonic()

    outcome = run_process(
        ["sh", "-c", "sleep 60 & echo $! > grandchild.pid; sleep 60"],
        cwd=tmp_path, log_dir=tmp_path / "logs", deadline=time.monotonic() + 1, grace=1,
    )

    assert outcome.timed_out is True
    assert time.monotonic() - started < 10
    grandchild = int((tmp_path / "grandchild.pid").read_text())
    assert wait_until(lambda: not alive(grandchild))


def test_a_worker_ignoring_sigterm_is_killed_after_the_grace_period(tmp_path):
    started = time.monotonic()

    outcome = run_process(["sh", "-c", "trap '' TERM; sleep 60"], cwd=tmp_path, log_dir=tmp_path / "logs",
                          deadline=time.monotonic() + 1, grace=1)

    assert outcome.timed_out is True
    assert time.monotonic() - started < 10


def test_a_worker_exiting_124_before_its_deadline_is_not_a_timeout(tmp_path):
    outcome = run_process(["sh", "-c", "exit 124"], cwd=tmp_path, log_dir=tmp_path / "logs",
                          deadline=time.monotonic() + 30)

    assert outcome.returncode == 124
    assert outcome.timed_out is False


def test_a_deadline_already_passed_starts_nothing(tmp_path):
    outcome = run_process(["touch", "ran"], cwd=tmp_path, log_dir=tmp_path / "logs", deadline=time.monotonic() - 1)

    assert outcome.timed_out is True and outcome.pid is None
    assert not (tmp_path / "ran").exists()


def test_background_leftovers_are_killed_when_the_worker_exits(tmp_path):
    outcome = run_process(
        ["sh", "-c", "sleep 60 >/dev/null 2>&1 & echo $! > left.pid"],
        cwd=tmp_path, log_dir=tmp_path / "logs", deadline=time.monotonic() + 30,
    )

    assert outcome.returncode == 0
    left = int((tmp_path / "left.pid").read_text())
    assert wait_until(lambda: not alive(left))


PARENT = '''
import os, sys, time
sys.path.insert(0, {repo!r})
from core.run_lock import ProjectLock
from core.worker_process import run_process
lock = ProjectLock({project!r}, holder="dying-loop").acquire()
# The "loop" dies the moment its worker is running: no unlock, no cleanup.
run_process(["sh", "-c", "echo started > marker; sleep 2"], cwd={work!r},
            log_dir={work!r} + "-logs",
            deadline=time.monotonic() + 30, lock_fd=lock.fileno(),
            on_spawn=lambda pid, pgid: os._exit(0))
'''


def test_the_worker_keeps_the_lock_after_the_loop_dies(tmp_path):
    project = tmp_path / "project"
    work = tmp_path / "work"
    project.mkdir()
    work.mkdir()

    subprocess.run([sys.executable, "-c", PARENT.format(
        repo=str(REPO), project=str(project), work=str(work))], check=True)

    assert wait_until(lambda: (work / "marker").exists())
    with pytest.raises(ProjectBusyError):
        ProjectLock(project).acquire()

    def free():
        try:
            ProjectLock(project).acquire().release()
            return True
        except ProjectBusyError:
            return False

    assert wait_until(free, timeout=15)


def test_without_a_lock_fd_nothing_extra_is_inherited(tmp_path):
    outcome = run_process(["sh", "-c", "ls /proc/self/fd | wc -l"], cwd=tmp_path, log_dir=tmp_path / "logs",
                          deadline=time.monotonic() + 10)

    assert int(outcome.stdout.strip()) <= 5


# --- finishing means the worker finished, not that its pipes closed ----------------


def test_a_worker_that_exits_leaving_a_background_child_is_finished_not_timed_out(tmp_path):
    started = time.monotonic()

    outcome = run_process(["bash", "-c", "sleep 30 & echo $! > bg.pid; echo done"],
                          cwd=tmp_path, log_dir=tmp_path / "logs",
                          deadline=time.monotonic() + 3, grace=1)

    assert time.monotonic() - started < 1.5
    assert outcome.timed_out is False
    assert outcome.returncode == 0
    assert outcome.stdout == "done\n"
    background = int((tmp_path / "bg.pid").read_text())
    assert wait_until(lambda: not alive(background), timeout=3)


def test_output_goes_to_log_files_with_their_hashes(tmp_path):
    import hashlib

    outcome = run_process(["sh", "-c", "echo out; echo err >&2"], cwd=tmp_path,
                          log_dir=tmp_path / "logs", log_name="worker",
                          deadline=time.monotonic() + 10)

    stdout_path = Path(outcome.stdout_log.path)
    assert stdout_path == tmp_path / "logs" / "worker.stdout"
    assert stdout_path.read_bytes() == b"out\n"
    assert outcome.stdout_log.sha256 == hashlib.sha256(b"out\n").hexdigest()
    assert outcome.stdout_log.bytes == 4
    assert Path(outcome.stderr_log.path).read_bytes() == b"err\n"


def test_a_second_process_does_not_overwrite_the_first_log(tmp_path):
    first = run_process(["echo", "one"], cwd=tmp_path, log_dir=tmp_path / "logs",
                        log_name="worker", deadline=time.monotonic() + 10)
    second = run_process(["echo", "two"], cwd=tmp_path, log_dir=tmp_path / "logs",
                         log_name="worker", deadline=time.monotonic() + 10)

    assert first.stdout_log.path != second.stdout_log.path
    assert Path(first.stdout_log.path).read_text() == "one\n"


def test_the_in_memory_excerpt_is_bounded_but_the_log_is_complete(tmp_path):
    outcome = run_process(["sh", "-c", "seq 1 20000"], cwd=tmp_path,
                          log_dir=tmp_path / "logs", deadline=time.monotonic() + 10,
                          excerpt_chars=100)

    assert len(outcome.stdout) == 100
    assert outcome.stdout.endswith("20000\n")
    assert Path(outcome.stdout_log.path).read_text().startswith("1\n2\n")
