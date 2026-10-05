"""Publish a project's development branch and its preview site (H-D4).

After each run the background service pushes the project's ``base_branch``
(``develop``) to GitHub and rebuilds the ``gh-pages`` branch:

    /           the site directory of the release branch (``main``): the released game
    /develop/   the site directory of ``develop``: the preview to play-test

Both are plain files (Match Legends' ``www/``), so the site is assembled with
git plumbing in a temporary index, without a checkout or a build step. The
release branch is only read here, never pushed.

project.yaml::

    github:
      repo: f0rrel/Match_Legends_mobile_game
      release_branch: main
      site_dir: www
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from core.history import EventType

__all__ = ["Publisher", "pages_url", "build_site_commit"]


def pages_url(repo: str) -> str:
    owner, name = repo.split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}/"


def build_site_commit(git, repository: Path, release_sha: str, develop_sha: str,
                      site_dir: str, parent: Optional[str]) -> Optional[str]:
    """A gh-pages commit with release at / and develop at /develop/; None if unchanged."""
    with tempfile.TemporaryDirectory(prefix="ms-pages-") as tmp:
        env = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
        git(["read-tree", "--empty"], repository, extra_env=env)
        git(["read-tree", f"{release_sha}:{site_dir}"], repository, extra_env=env)
        git(["read-tree", f"--prefix=develop/", f"{develop_sha}:{site_dir}"], repository,
            extra_env=env)
        blob = git(["hash-object", "-w", "--stdin"], repository, input_text="")
        git(["update-index", "--add", "--cacheinfo", f"100644,{blob},.nojekyll"], repository,
            extra_env=env)
        tree = git(["write-tree"], repository, extra_env=env)
    if parent and git(["rev-parse", f"{parent}^{{tree}}"], repository) == tree:
        return None
    message = (f"Publish release {release_sha[:12]} at / and develop {develop_sha[:12]} "
               "at /develop/")
    ident = {"GIT_AUTHOR_NAME": "Master System", "GIT_AUTHOR_EMAIL": "master-system@localhost",
             "GIT_COMMITTER_NAME": "Master System",
             "GIT_COMMITTER_EMAIL": "master-system@localhost"}
    args = ["commit-tree", tree, "-m", message] + (["-p", parent] if parent else [])
    return git(args, repository, extra_env=ident)


class Publisher:
    """The service's after-run hook: push develop and the preview site when they changed."""

    def __init__(self, master, history, app_factory, askpass_dir: Path, git=None, push=None):
        self._master = master
        self._history = history
        self._app_factory = app_factory  # repo -> GitHubApp, or None when not set up
        self._askpass_dir = Path(askpass_dir)
        if git is None:
            from core.host import git
        if push is None:
            from core.github import push
        self._git = git
        self._push = push

    def preview_url(self, project_id: str) -> Optional[str]:
        github = self._github(project_id)
        return pages_url(github["repo"]) + "develop/" if github else None

    def _github(self, project_id):
        try:
            project = self._master.project_state(project_id).project()
        except Exception:
            return None
        github = project.get("github")
        return github if isinstance(github, dict) and github.get("repo") else None

    def __call__(self, project_id: str, session_id: Optional[str] = None) -> list:
        github = self._github(project_id)
        if github is None:
            return []
        project = self._master.project_state(project_id).project()
        repository = Path(project["repository"]).expanduser()
        develop = project["base_branch"]
        release = github.get("release_branch", "main")
        site_dir = github.get("site_dir", "www")
        git = self._git
        develop_sha = git(["rev-parse", f"refs/heads/{develop}"], repository)
        release_sha = git(["rev-parse", f"refs/heads/{release}"], repository)
        published = self._history.events(project_id=project_id, types=[EventType.PUBLISHED])
        last = published[-1].payload if published else {}
        if last.get("develop_sha") == develop_sha and last.get("release_sha") == release_sha:
            return []
        app = self._app_factory(github["repo"])
        if app is None:
            return ["Publishing is not set up yet (GitHub App): the preview link is not updated."]
        try:
            parent = git(["rev-parse", "--verify", "-q", "refs/heads/gh-pages"], repository)
        except RuntimeError:
            parent = None
        site = build_site_commit(git, repository, release_sha, develop_sha, site_dir, parent)
        if site:
            git(["update-ref", "refs/heads/gh-pages", site] + ([parent] if parent else []),
                repository)
        self._push(app, repository, [develop, "gh-pages"], self._askpass_dir,
                   force_refs=("gh-pages",))
        url = pages_url(github["repo"])
        notes = self._ensure_pages(app, github["repo"])
        self._history.append(
            type=EventType.PUBLISHED, run_id=uuid.uuid4().hex, project_id=project_id,
            session_id=session_id,
            payload={"develop_branch": develop, "develop_sha": develop_sha,
                     "release_branch": release, "release_sha": release_sha,
                     "pages_sha": site or parent, "url": url, "actor": "system"})
        return [f"Preview updated: {url}develop/ (develop {develop_sha[:12]})", *notes]

    @staticmethod
    def _ensure_pages(app, repo) -> list:
        """Turn on GitHub Pages from gh-pages the first time (the app has Pages: write)."""
        request = getattr(app, "request", None)
        if request is None:
            return []
        from core.github import GitHubError

        try:
            request("GET", f"/repos/{repo}/pages")
            return []
        except GitHubError as error:
            if " 404 " not in f" {error} ":
                return [f"Could not check GitHub Pages: {error}"]
        try:
            request("POST", f"/repos/{repo}/pages",
                    {"source": {"branch": "gh-pages", "path": "/"}})
            return ["GitHub Pages turned on; the first build takes a minute or two."]
        except GitHubError as error:
            return [f"Could not turn on GitHub Pages: {error}"]
