"""The one way an attempt starts an external process.

Every worker or verification process is launched here, so these guarantees
hold for all of them:

* **The project lock is held while it runs.** The lock's file descriptor is
  passed to the process (``pass_fds``). ``flock`` locks belong to the open file
  description, so the lock stays held while any process keeps it open, even if
  the loop that took it is killed with SIGKILL.
* **The deadline holds even if the loop dies.** The command runs under
  coreutils ``timeout --signal=TERM --kill-after=<grace>``, which sends SIGTERM
  to the whole process group at the deadline and SIGKILL after the grace
  period. This helper also kills the group itself if it is still waiting.
* **Finishing means the process finished.** Output goes to log files, not
  pipes, and the helper waits on the ``timeout`` process itself. A worker that
  exits while a background child it started is still running has finished;
  the child is then killed with the rest of the group. ``timed_out`` is true
  only if the deadline actually passed.
* **Nothing is left behind.** The process starts in a new session (its own
  process group). When it finishes, any member of that group still running is
  killed. A process that calls ``setsid`` itself escapes this (README gap N1).

``on_spawn(pid, pgid)`` is called right after launch, so the caller can record
the process before waiting on it.
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

__all__ = [
    "DEFAULT_GRACE_S",
    "MAX_EXCERPT_CHARS",
    "LogFile",
    "ProcessOutcome",
    "run_process",
]

#: Seconds between SIGTERM and SIGKILL at the deadline.
DEFAULT_GRACE_S = 30
#: How much of each log is kept in memory (its tail). The full log stays on disk.
MAX_EXCERPT_CHARS = 64 * 1024
#: How ``timeout`` ends when it ended the command: status 124 (after TERM) or
#: 137 (after KILL); or killed by its own group-wide signal, since it leads the
#: group it signals (-15, -9). Only counted once the deadline has passed.
_TIMEOUT_STATUSES = frozenset({124, 137, -signal.SIGTERM, -signal.SIGKILL})


@dataclass(frozen=True)
class LogFile:
    path: str
    sha256: str
    bytes: int

    def to_dict(self) -> dict:
        return {"path": self.path, "sha256": self.sha256, "bytes": self.bytes}


@dataclass(frozen=True)
class ProcessOutcome:
    returncode: Optional[int]
    #: The tail of each log (at most MAX_EXCERPT_CHARS characters).
    stdout: str
    stderr: str
    timed_out: bool
    pid: Optional[int]
    pgid: Optional[int]
    stdout_log: Optional[LogFile] = None
    stderr_log: Optional[LogFile] = None


def _kill_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _read_log(path: Path, excerpt_chars: int):
    data = path.read_bytes()
    log = LogFile(str(path), hashlib.sha256(data).hexdigest(), len(data))
    text = data.decode("utf-8", errors="replace")
    return log, text[-excerpt_chars:]


def _log_paths(log_dir: Path, name: str):
    log_dir.mkdir(parents=True, exist_ok=True)
    stem = name
    counter = 1
    while (log_dir / f"{stem}.stdout").exists():
        counter += 1
        stem = f"{name}-{counter}"
    return log_dir / f"{stem}.stdout", log_dir / f"{stem}.stderr"


def run_process(
    argv: Sequence[str],
    *,
    cwd,
    deadline: float,
    log_dir,
    log_name: str = "process",
    lock_fd: Optional[int] = None,
    grace: float = DEFAULT_GRACE_S,
    env: Optional[Mapping[str, str]] = None,
    on_spawn: Optional[Callable[[int, int], None]] = None,
    clock: Callable[[], float] = time.monotonic,
    excerpt_chars: int = MAX_EXCERPT_CHARS,
) -> ProcessOutcome:
    """Run argv in cwd until it exits or the deadline (a ``clock`` value) passes.

    stdout and stderr are written to ``<log_dir>/<log_name>.stdout`` and
    ``.stderr`` (a numeric suffix is added rather than overwrite a log).
    """
    if not argv:
        raise ValueError("argv must not be empty")
    remaining = deadline - clock()
    if remaining <= 0:
        return ProcessOutcome(None, "", "", True, None, None)

    stdout_path, stderr_path = _log_paths(Path(log_dir), log_name)
    wrapped = [
        "timeout",
        "--signal=TERM",
        f"--kill-after={grace:g}s",
        f"{remaining:.3f}s",
        *argv,
    ]
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        process = subprocess.Popen(
            wrapped,
            cwd=str(cwd),
            env=None if env is None else dict(env),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
            pass_fds=() if lock_fd is None else (lock_fd,),
        )
    pgid = process.pid  # a new session makes it the group leader

    killed = False
    try:
        if on_spawn is not None:
            on_spawn(process.pid, pgid)
        try:
            # ``timeout`` ends the command at the deadline; this is the backstop.
            process.wait(timeout=remaining + grace + 5)
        except subprocess.TimeoutExpired:
            killed = True
            _kill_group(pgid, signal.SIGTERM)
            try:
                process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                _kill_group(pgid, signal.SIGKILL)
                process.wait()
    except BaseException:
        _kill_group(pgid, signal.SIGKILL)
        process.wait()
        raise
    finally:
        # The process is over: nothing it started may keep running (or keep
        # holding the lock).
        _kill_group(pgid, signal.SIGKILL)

    deadline_passed = clock() >= deadline
    timed_out = deadline_passed and (killed or process.returncode in _TIMEOUT_STATUSES)
    stdout_log, stdout = _read_log(stdout_path, excerpt_chars)
    stderr_log, stderr = _read_log(stderr_path, excerpt_chars)
    return ProcessOutcome(
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        pid=process.pid,
        pgid=pgid,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
    )
