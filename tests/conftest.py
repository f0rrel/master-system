"""Shared fixtures: isolated runtime state, isolated git, throwaway repositories."""

import subprocess
from pathlib import Path

import pytest

from core.paths import RuntimePaths


@pytest.fixture(autouse=True)
def _isolate_runtime_and_git(tmp_path_factory, monkeypatch):
    """No test may touch the owner's real state directory or git configuration."""
    data_home = tmp_path_factory.mktemp("xdg-data")
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key, value in {
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }.items():
        monkeypatch.setenv(key, value)


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), check=True,
                          capture_output=True, text=True).stdout.strip()


def make_repo(path: Path, files=None) -> Path:
    """A git repository on branch main with one commit."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    files = files or {
        "app.py": "def greet(name):\n    return 'Hi ' + name\n",
        "tests/test_app.py": "from app import greet\n\n\ndef test_greet():\n"
                             "    assert greet('Ada') == 'Hi Ada'\n",
    }
    for name, content in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    return path


@pytest.fixture
def git_repo(tmp_path):
    return make_repo(tmp_path / "repo")


@pytest.fixture
def runtime_paths(tmp_path):
    return RuntimePaths(state_dir=tmp_path / "state", worktrees_root=tmp_path / "worktrees")
