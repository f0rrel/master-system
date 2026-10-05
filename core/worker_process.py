"""The one way an attempt starts an external process.

Every worker or verification process is launched here, so three guarantees
hold for all of them:

* **The project lock is held while it runs.** The lock's file descriptor is
  passed to the process (``pass_fds``). ``flock`` locks belong to the open file
  description, so the lock stays held while any process keeps it open, even if
  the loop that took it is killed with SIGKILL.
* **The deadline holds even if the loop dies.** The command runs under
  coreutils ``timeout --signal=TERM --kill-after=<grace>``, which sends SIGTERM
  to the whole process group at the deadline and SIGKILL after the grace
  period. This helper also kills the group itself if it is still waiting.
* **Nothing is left behind.** The process starts in a new session (its own
  process group). When it finishes, any member of that group still running is
  killed. A process that calls ``setsid`` itself escapes this; containment is
  not sandboxing.

``on_spawn(pid, pgid)`` is called right after launch, so the caller can record
the process before waiting on it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence

__all__ = ["DEFAULT_GRACE_S", "ProcessOutcome", "run_process"]

#: Seconds between SIGTERM and SIGKILL at the deadline.
DEFAULT_GRACE_S = 30
#: How ``timeout`` ends when it ended the command: status 124 (after TERM) or
#: 137 (after KILL); or killed by its own group-wide signal, since it leads the
#: group it signals (-15, -9). Only counted once the deadline has passed.
_TIMEOUT_STATUSES = frozenset({124, 137, -signal.SIGTERM, -signal.SIGKILL})


@dataclass(frozen=True)
class ProcessOutcome:
    returncode: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool
    pid: Optional[int]
    pgid: Optional[int]


def _kill_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def run_process(
    argv: Sequence[str],
    *,
    cwd,
    deadline: float,
    lock_fd: Optional[int] = None,
    grace: float = DEFAULT_GRACE_S,
    env: Optional[Mapping[str, str]] = None,
    on_spawn: Optional[Callable[[int, int], None]] = None,
    clock: Callable[[], float] = time.monotonic,
) -> ProcessOutcome:
    """Run argv in cwd until it exits or the deadline (a ``clock`` value) passes."""
    if not argv:
        raise ValueError("argv must not be empty")
    remaining = deadline - clock()
    if remaining <= 0:
        return ProcessOutcome(None, "", "", True, None, None)

    wrapped = [
        "timeout",
        "--signal=TERM",
        f"--kill-after={grace:g}s",
        f"{remaining:.3f}s",
        *argv,
    ]
    process = subprocess.Popen(
        wrapped,
        cwd=str(cwd),
        env=None if env is None else dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
        pass_fds=() if lock_fd is None else (lock_fd,),
    )
    pgid = process.pid  # a new session makes it the group leader

    killed = False
    try:
        if on_spawn is not None:
            on_spawn(process.pid, pgid)
        try:
            # ``timeout`` should end things first; this is the backstop.
            stdout, stderr = process.communicate(timeout=remaining + grace + 5)
        except subprocess.TimeoutExpired:
            killed = True
            _kill_group(pgid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=grace)
            except subprocess.TimeoutExpired:
                _kill_group(pgid, signal.SIGKILL)
                stdout, stderr = process.communicate()
    except BaseException:
        _kill_group(pgid, signal.SIGKILL)
        process.wait()
        raise
    finally:
        # The attempt is over: nothing it started may keep running (or keep
        # holding the lock).
        _kill_group(pgid, signal.SIGKILL)

    timed_out = killed or (
        process.returncode in _TIMEOUT_STATUSES and clock() >= deadline
    )
    return ProcessOutcome(
        returncode=process.returncode,
        stdout=stdout or "",
        stderr=stderr or "",
        timed_out=timed_out,
        pid=process.pid,
        pgid=pgid,
    )
