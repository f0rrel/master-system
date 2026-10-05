"""Filesystem persistence for :class:`core.work_session.WorkSession`.

One YAML document per session, written through the same atomic writer the
project state uses, so a session file is never observed half-written. This is
infrastructure: nothing in the orchestration layer knows sessions are files.
It satisfies :class:`core.work_session.SessionStore` and nothing more, and it
deliberately holds no scheduling, no retry, no locking and no indexing.
Concurrent writers are kept out one level up: :class:`core.session_runner.SessionRunner`
only writes a session while holding its project's
:class:`core.run_lock.ProjectLock`.

Broken sessions
---------------
A session that cannot be read is not hidden and not silently repaired. It is
excluded from listings and reported by :meth:`FileSessionStore.problems`, with
the same reasoning :class:`core.project_manager.ProjectManager` uses for a
broken project: one unreadable file must not make every other session
unreachable, but it must also not vanish without explanation.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from core.paths import RuntimePaths
from core.project_state import write_yaml_atomically
from core.work_session import (
    InvalidSessionError,
    WorkSession,
)


SESSION_SUFFIX = ".yaml"


class SessionStoreError(Exception):
    """Base error for the session persistence layer."""


class UnknownSessionError(SessionStoreError):
    """Raised when a session id does not resolve to a stored session."""


#: Expected data failures while reading one session. Anything else is a bug in
#: this layer and is allowed to surface, on the same principle as
#: PROJECT_READ_ERRORS: silently swallowing an unexpected error would hide
#: real faults behind a "broken file" message.
SESSION_READ_ERRORS = (OSError, ValueError, yaml.YAMLError)


class FileSessionStore:
    """SessionStore backed by one YAML file per session."""

    def __init__(self, root=None):
        self.root = Path(root) if root is not None else RuntimePaths.default().sessions_dir
        self.root.mkdir(parents=True, exist_ok=True)

    # --- locations -------------------------------------------------------

    def _path_for(self, session_id: str) -> Path:
        # WorkSession already refuses an id that could escape a directory, and
        # validate_session_id runs on construction, so by the time an id reaches
        # here it cannot contain a separator or a parent reference.
        return self.root / f"{session_id}{SESSION_SUFFIX}"

    # --- SessionStore ---------------------------------------------------

    def create(self, session: WorkSession) -> WorkSession:
        if not isinstance(session, WorkSession):
            raise TypeError(f"expected a WorkSession, got {type(session).__name__}")
        path = self._path_for(session.session_id)
        if path.exists():
            raise SessionStoreError(
                f"session {session.session_id!r} already exists; load and save it instead"
            )
        self._write(session)
        return session

    def load(self, session_id: str) -> WorkSession:
        path = self._path_for(session_id)
        if not path.is_file():
            raise UnknownSessionError(f"unknown session: {session_id}")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except SESSION_READ_ERRORS as error:
            raise InvalidSessionError(
                f"session {session_id!r} could not be read: {error}"
            ) from error
        try:
            return WorkSession.from_dict(data)
        except InvalidSessionError as error:
            raise InvalidSessionError(
                f"session {session_id!r} is not valid: {error}"
            ) from error

    def save(self, session: WorkSession) -> WorkSession:
        if not isinstance(session, WorkSession):
            raise TypeError(f"expected a WorkSession, got {type(session).__name__}")
        path = self._path_for(session.session_id)
        if not path.exists():
            raise UnknownSessionError(
                f"cannot save unknown session {session.session_id!r}; create it first"
            )
        self._write(session)
        return session

    def list_sessions(self, project_id: Optional[str] = None) -> tuple:
        """Return usable sessions, newest first.

        Sorted by creation time so "the session I started most recently" is the
        first result, which is what a later status view will want.
        """
        entries, _ = self._scan()
        if project_id is not None:
            entries = [e for e in entries if e["session"].project_id == project_id]
        return tuple(e["session"] for e in entries)

    def problems(self) -> tuple:
        """Return unusable session files as (session_id, reason) pairs."""
        _, problems = self._scan()
        return tuple(problems)

    # --- internals -------------------------------------------------------

    def _write(self, session: WorkSession) -> None:
        path = self._path_for(session.session_id)
        # write_yaml_atomically preserves the existing file's permissions, so it
        # needs the file to already exist. Creating an empty one first keeps
        # that helper as the single write path rather than duplicating its
        # flush/fsync/replace logic here.
        if not path.exists():
            path.touch()
        write_yaml_atomically(path, session.to_dict())

    def _scan(self) -> tuple:
        entries = []
        problems = []
        for path in sorted(self.root.glob(f"*{SESSION_SUFFIX}")):
            session_id = path.name[: -len(SESSION_SUFFIX)]
            try:
                session = self.load(session_id)
            except (UnknownSessionError, InvalidSessionError) as error:
                problems.append((session_id, str(error)))
                continue
            entries.append({"session_id": session_id, "path": path, "session": session})
        entries.sort(key=lambda e: (e["session"].created_at, e["session_id"]), reverse=True)
        return entries, problems