"""Publish a project's development branch and, optionally, its preview site (H-D4).

After each run the background service pushes the project's ``base_branch``
(``develop``) to GitHub. If the project has a ``site_dir``, it also rebuilds the
``gh-pages`` branch:

    /           the site directory of the release branch (``main``): the released version
    /develop/   the site directory of ``develop``: the preview to review

The site directory holds plain static files, so the site is assembled with git
plumbing in a temporary index, without a checkout or a build step. The release
branch is only read here, never pushed. Without ``site_dir`` there is no preview
site: only ``develop`` is pushed.

project.yaml::

    github:
      repo: example-user/example-app
      release_branch: main
      site_dir: public                       # optional: no preview site without it
      site_url: https://app.example.com/     # optional: default https://<owner>.github.io/<repo>/
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from core.history import EventType

__all__ = ["Publisher", "pages_url", "site_urls", "build_site_commit"]


def pages_url(repo: str) -> str:
    owner, name = repo.split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}/"


def site_urls(github) -> tuple:
    """(live URL, preview URL) of a project's published site, or (None, None) without one."""
    if not isinstance(github, dict) or not github.get("repo") or not github.get("site_dir"):
        return None, None
    base = str(github.get("site_url") or pages_url(github["repo"]))
    base = base if base.endswith("/") else base + "/"
    return base, base + "develop/"


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


def refuse_foreign_tip(repository, branch: str, project_id: str) -> Optional[str]:
    """Why ``branch`` must not be pushed (a tip the system did not set), or None."""
    from core.workspace import foreign_tip

    foreign = foreign_tip(repository, branch)
    if foreign is None:
        return None
    return (f"{branch} was not published: it is at {foreign[:12]}, which the system did not "
            f"set (a change made outside Master System). If you made it on purpose: "
            f"ms publish {project_id} --accept-tip")


class Publisher:
    """The service's after-run hook: push develop and the preview site when they changed."""

    def __init__(self, master, history, app_factory, askpass_dir: Path, git=None, push=None,
                 notifier=None):
        self._master = master
        self._notifier = notifier
        self._history = history
        self._app_factory = app_factory  # repo -> GitHubApp, or None when not set up
        self._askpass_dir = Path(askpass_dir)
        if git is None:
            from core.host import git
        if push is None:
            from core.github import push
        self._git = git
        self._push = push

    def _tell_once(self, project_id, sha, message) -> None:
        """A "needs you" notification, once per foreign tip."""
        marker = self._askpass_dir / f"publish-refused-{project_id}"
        try:
            if marker.read_text() == sha:
                return
        except OSError:
            pass
        if self._notifier is not None:
            self._notifier.send(f"{project_id}: needs you", message)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(sha)

    def accept_tip(self, project_id: str, actor: str = "owner") -> Optional[str]:
        """The owner accepts the base branch's current tip as if the system had set it."""
        from core.workspace import accept_tip, system_tip

        project = self._master.project_state(project_id).project()
        repository = Path(project["repository"]).expanduser()
        previous = system_tip(repository, project["base_branch"])
        sha = accept_tip(repository, project["base_branch"])
        self._history.append(
            type=EventType.HUMAN_ACTION, run_id=uuid.uuid4().hex, project_id=project_id,
            payload={"actor": actor, "action": "accept_base_tip",
                     "branch": project["base_branch"], "sha": sha, "previous": previous})
        return sha

    def preview_url(self, project_id: str) -> Optional[str]:
        return site_urls(self._github(project_id))[1]

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
        site_dir = github.get("site_dir")
        git = self._git
        develop_sha = git(["rev-parse", f"refs/heads/{develop}"], repository)
        release_sha = git(["rev-parse", f"refs/heads/{release}"], repository)
        published = self._history.events(project_id=project_id, types=[EventType.PUBLISHED])
        last = published[-1].payload if published else {}
        if last.get("develop_sha") == develop_sha and last.get("release_sha") == release_sha:
            return []
        refused = refuse_foreign_tip(repository, develop, project_id)
        if refused:
            self._tell_once(project_id, develop_sha, refused)
            return [refused]
        app = self._app_factory(github["repo"])
        if app is None:
            return ["Publishing is not set up yet (GitHub App): the preview link is not updated."]
        site = parent = None
        if site_dir:
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
        else:
            self._push(app, repository, [develop], self._askpass_dir)
        url, preview = site_urls(github)
        self._history.append(
            type=EventType.PUBLISHED, run_id=uuid.uuid4().hex, project_id=project_id,
            session_id=session_id,
            payload={"develop_branch": develop, "develop_sha": develop_sha,
                     "release_branch": release, "release_sha": release_sha,
                     "pages_sha": site or parent, "url": url, "actor": "system"})
        if preview is None:
            return [f"Pushed {develop} ({develop_sha[:12]})."]
        return [f"Preview updated: {preview} (develop {develop_sha[:12]})"]
