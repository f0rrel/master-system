"""One writer per project at a time, released by the OS if its holder dies.

An autonomous run and a human edit both rewrite the same YAML files, and a
read-modify-write from each at once loses one of them. :class:`ProjectLock` is
the single place that prevents that: an exclusive, non-blocking ``flock`` on
``<project>/.run.lock``. It is not a lease and needs no heartbeat -- the kernel
drops the lock the moment the process holding it exits, however it exits.

That is also what makes crash recovery decidable. A session recorded as
running whose project lock can be acquired was left by a process that no
longer exists, so it can be recovered; if the lock is held, someone is still
running and nothing is touched.

The holder description written into the file is for a human looking at it.
Nothing ever reads it to make a decision.

POSIX only, as ``fcntl`` is.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

__all__ = ["LOCK_FILENAME", "ProjectBusyError", "ProjectLock"]

LOCK_FILENAME = ".run.lock"


class ProjectBusyError(RuntimeError):
    """Raised when another process holds the project's mutation lock."""


class ProjectLock:
    """Exclusive mutation lock on one project directory. A context manager."""

    def __init__(self, project_path, holder: str = ""):
        self.path = Path(project_path) / LOCK_FILENAME
        self._holder = holder
        self._file = None

    def acquire(self) -> "ProjectLock":
        if self._file is not None:
            raise RuntimeError("lock already acquired by this object")
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            owner = handle.read().strip() or "unknown holder"
            handle.close()
            raise ProjectBusyError(
                f"project at {self.path.parent} is in use by another process "
                f"({owner}); no changes were made"
            ) from None
        except BaseException:
            handle.close()
            raise
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} {self._holder}".strip() + "\n")
        handle.flush()
        self._file = handle
        return self

    def fileno(self) -> int:
        """The locked file's descriptor, for passing to a worker process.

        A process that inherits it keeps the lock held after this process
        releases it or dies: ``release`` only closes this process's
        descriptor, so the lock ends when the last holder does.
        """
        if self._file is None:
            raise RuntimeError("lock is not held")
        return self._file.fileno()

    def release(self) -> None:
        """Close this process's descriptor. Deliberately no ``LOCK_UN``.

        ``flock`` belongs to the open file description, which worker processes
        share through inherited descriptors. ``LOCK_UN`` would release the lock
        for all of them, so a worker that escaped its process group would keep
        running unlocked while recovery raced it. Closing only drops this
        holder; the lock is released when the last holder closes or exits.
        """
        if self._file is None:
            return
        try:
            self._file.close()
        finally:
            self._file = None

    def __enter__(self) -> "ProjectLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
