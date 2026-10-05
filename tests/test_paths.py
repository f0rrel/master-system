"""Runtime state defaults to a directory outside the repository."""

from pathlib import Path

from core.paths import RuntimePaths
from core.session_store import FileSessionStore
from core.sqlite_history import BUSY_TIMEOUT_MS, SQLiteHistoryStore

REPO = Path(__file__).resolve().parent.parent


def inside(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    return path == root or root in path.parents


def test_xdg_data_home_is_used(tmp_path):
    paths = RuntimePaths.default({"XDG_DATA_HOME": str(tmp_path)})

    assert paths.state_dir == tmp_path / "master-system"
    assert paths.history_path == tmp_path / "master-system" / "history.sqlite"
    assert paths.sessions_dir == tmp_path / "master-system" / "sessions"
    assert paths.worktrees_root == tmp_path / "master-system-worktrees"


def test_without_xdg_data_home_the_fallback_is_local_share():
    paths = RuntimePaths.default({})

    assert paths.state_dir == Path.home() / ".local" / "share" / "master-system"


def test_a_relative_xdg_data_home_is_ignored():
    assert RuntimePaths.default({"XDG_DATA_HOME": "relative/dir"}) == RuntimePaths.default({})


def test_worktrees_are_not_inside_the_state_dir(tmp_path):
    paths = RuntimePaths.default({"XDG_DATA_HOME": str(tmp_path)})

    assert not inside(paths.worktrees_root, paths.state_dir)
    assert not inside(paths.state_dir, paths.worktrees_root)


def test_the_defaults_are_outside_the_repository():
    for env in ({}, None):
        paths = RuntimePaths.default(env)
        assert not inside(paths.state_dir, REPO)
        assert not inside(paths.worktrees_root, REPO)


def test_stores_default_to_the_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    history = SQLiteHistoryStore()
    sessions = FileSessionStore()

    assert history.path == tmp_path / "master-system" / "history.sqlite"
    assert sessions.root == tmp_path / "master-system" / "sessions"
    assert not inside(history.path, REPO) and not inside(sessions.root, REPO)


def test_sqlite_busy_timeout_is_set(tmp_path):
    store = SQLiteHistoryStore(tmp_path / "h.sqlite")

    assert store.busy_timeout_ms() == BUSY_TIMEOUT_MS > 0
