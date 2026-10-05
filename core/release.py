"""Releases: the only way ``main`` changes, and only by the owner's merge on GitHub.

``ms release <project>`` (or ``release`` in the chat):

1. writes release notes from history alone: every task integrated into the
   development branch whose commit is not yet in ``main``, with what changed,
   how to check it by hand, and its verification; plus the spend since the
   last release;
2. pushes ``develop`` and opens (or updates) a pull request ``develop`` ->
   ``main`` with the notes, through the GitHub App.

The owner reviews and merges it on GitHub (G2: a merge commit). On a later
service cycle ``watch`` sees the merge and:

3. fetches ``main``, checks that the merge commit's tree equals the merged
   head's tree (the released files are exactly the reviewed ones),
4. tags ``v0.<n>`` and creates a GitHub Release with the notes,
5. lets the publisher rebuild the site, so ``/`` serves the new release.

Every step is recorded as a ``release`` event.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Optional

from core.history import EventType

__all__ = ["Releaser", "release_notes"]


def released_ref(git, repo, release_branch) -> str:
    """What GitHub's release branch was at the last fetch (else the local branch).

    The dedicated clone's local release branch can be ahead of GitHub's (work
    integrated there before Milestone 3); only what GitHub serves is released.
    """
    ref = f"refs/ms-release/{release_branch}"
    try:
        git(["rev-parse", "--verify", "-q", ref], repo)
        return ref
    except RuntimeError:
        return f"refs/heads/{release_branch}"


def _shipped(git, repo, sha, release_branch) -> bool:
    try:
        git(["merge-base", "--is-ancestor", sha, released_ref(git, repo, release_branch)], repo)
        return True
    except RuntimeError:
        return False


def release_notes(history, project_id, repo, release_branch, git, version, *, prices=None,
                  since=None, titles=None) -> dict:
    """The notes for the next release, from history; {"tasks": [...], "markdown": str}."""
    latest = {}
    for e in history.events(project_id=project_id, types=[EventType.INTEGRATION]):
        latest[e.task_id] = e
    tasks = []
    for task_id, event in latest.items():
        sha = event.payload.get("result_sha")
        if not sha or _shipped(git, repo, sha, release_branch):
            continue
        attempt = history.events(project_id=project_id, attempt_id=event.attempt_id)
        started = next((e for e in attempt if e.type is EventType.ATTEMPT_STARTED), None)
        finished = next((e for e in attempt if e.type is EventType.ATTEMPT_FINISHED), None)
        verdicts = [e for e in attempt if e.type is EventType.VERIFICATION]
        diffstat = (finished.payload.get("diffstat") if finished else None) or {}
        tasks.append({
            "task_id": task_id,
            "title": ((titles or {}).get(task_id)
                      or (started.payload.get("task_title") if started else None) or task_id),
            "commit": sha,
            "files": diffstat.get("files"),
            "insertions": diffstat.get("insertions"),
            "deletions": diffstat.get("deletions"),
            "manual_check": started.payload.get("manual_check") if started else None,
            "verdict": verdicts[-1].payload.get("verdict") if verdicts else None,
            "commands": [c.get("command") for c in
                         ((verdicts[-1].payload.get("evidence") or {}).get("commands_run")
                          or [])] if verdicts else [],
        })
    lines = [f"# {version}", ""]
    if not tasks:
        lines.append("No new tasks since the last release.")
    for t in tasks:
        lines += [f"## {t['task_id']}: {t['title']}", ""]
        if t["files"] is not None:
            lines.append(f"- What changed: {t['files']} file(s), +{t['insertions']} "
                         f"-{t['deletions']} (commit `{t['commit'][:12]}`)")
        lines.append(f"- Verified: {t['verdict'] or 'unknown'}"
                     + (f" ({len(t['commands'])} acceptance command(s))" if t["commands"] else ""))
        if t["manual_check"]:
            lines += ["- How to check by hand:", ""]
            lines += [f"  {line}" for line in t["manual_check"].splitlines()]
        lines.append("")
    if prices is not None and since:
        from core.report import spend_since

        spend = spend_since(history, since, prices)
        lines.append(f"Cost since the last release: ${spend['total_usd']:.3f} "
                     f"(Master ${spend['master_usd']:.3f}, workers ${spend['worker_usd']:.3f}).")
    lines.append("")
    lines.append("Merging this pull request releases these changes: the system then tags "
                 f"{version}, creates a GitHub Release, and updates the live site.")
    return {"tasks": tasks, "markdown": "\n".join(lines)}


class Releaser:
    def __init__(self, master, history, app_factory, *, askpass_dir: Path, prices=None,
                 git=None, push=None, publisher=None, remote_url=None):
        self._master = master
        self._history = history
        self._app_factory = app_factory
        self._askpass_dir = Path(askpass_dir)
        self._prices = prices
        if git is None:
            from core.host import git
        if push is None:
            from core.github import push
        self._git = git
        self._push = push
        self._publisher = publisher
        self._remote_url = remote_url or (lambda repo_name: f"https://github.com/{repo_name}.git")

    def _setup(self, project_id):
        project = self._master.project_state(project_id).project()
        github = project.get("github") if isinstance(project.get("github"), dict) else None
        if not github or not github.get("repo"):
            raise ValueError(f"{project_id} has no github section in project.yaml")
        return (Path(project["repository"]).expanduser(), project["base_branch"],
                github.get("release_branch", "main"), github["repo"])

    def _events(self, project_id):
        return self._history.events(project_id=project_id, types=[EventType.RELEASE])

    def _record(self, project_id, **payload):
        self._history.append(type=EventType.RELEASE, run_id=uuid.uuid4().hex,
                             project_id=project_id, payload={"actor": "system", **payload})

    def next_version(self, project_id, repo) -> str:
        numbers = [0]
        try:
            tags = self._git(["tag", "-l", "v0.*"], repo).split()
        except RuntimeError:
            tags = []
        recorded = [e.payload.get("version") for e in self._events(project_id)]
        for name in [*tags, *recorded]:
            match = re.match(r"^v0\.(\d+)$", str(name))
            if match:
                numbers.append(int(match.group(1)))
        return f"v0.{max(numbers) + 1}"

    def open_pending(self, project_id) -> Optional[dict]:
        """The release PR waiting for the owner's merge, if any."""
        state = {}
        for e in self._events(project_id):
            if e.payload.get("stage") == "pr_opened":
                state[e.payload["pr_number"]] = e.payload
            elif e.payload.get("stage") in ("published", "closed"):
                state.pop(e.payload.get("pr_number"), None)
        return list(state.values())[-1] if state else None

    def prepare(self, project_id) -> dict:
        """Write the notes, push develop, open or update the release PR."""
        repo, develop, release, repo_name = self._setup(project_id)
        try:
            self._git(["fetch", "-q", self._remote_url(repo_name),
                       f"+refs/heads/{release}:refs/ms-release/{release}"], repo)
        except RuntimeError:
            pass  # offline: judge by the last fetch
        pending = self.open_pending(project_id)
        version = pending["version"] if pending else self.next_version(project_id, repo)
        since = None
        published = [e for e in self._events(project_id) if e.payload.get("stage") == "published"]
        if published:
            since = published[-1].created_at
        titles = {t["id"]: t.get("title") for t in self._master.status(project_id)["tasks"]}
        notes = release_notes(self._history, project_id, repo, release, self._git, version,
                              prices=self._prices, since=since, titles=titles)
        if not notes["tasks"]:
            return {"opened": False, "message": "Nothing to release: every task in "
                    f"{develop} is already in {release}."}
        app = self._app_factory(repo_name)
        if app is None:
            return {"opened": False, "notes": notes["markdown"],
                    "message": "The GitHub App is not set up yet (docs/github-setup.md); "
                    "here are the notes."}
        self._push(app, repo, [develop], self._askpass_dir)
        title = f"Release {version}: " + ", ".join(t["task_id"] for t in notes["tasks"])[:200]
        if pending:
            pr = app.request("PATCH", f"/repos/{repo_name}/pulls/{pending['pr_number']}",
                             {"title": title, "body": notes["markdown"]})
        else:
            pr = app.request("POST", f"/repos/{repo_name}/pulls",
                             {"title": title, "head": develop, "base": release,
                              "body": notes["markdown"]})
        self._record(project_id, stage="pr_opened", version=version, pr_number=pr["number"],
                     pr_url=pr.get("html_url"), notes=notes["markdown"],
                     tasks=[t["task_id"] for t in notes["tasks"]])
        return {"opened": True, "version": version, "url": pr.get("html_url"),
                "tasks": [t["task_id"] for t in notes["tasks"]],
                "message": f"Release {version} is ready for you: review and merge "
                           f"{pr.get('html_url')} on GitHub."}

    def watch(self, project_id) -> list:
        """After the owner merges the release PR: tag, GitHub Release, republish."""
        pending = self.open_pending(project_id)
        if pending is None:
            return []
        repo, develop, release, repo_name = self._setup(project_id)
        app = self._app_factory(repo_name)
        if app is None:
            return []
        pr = app.request("GET", f"/repos/{repo_name}/pulls/{pending['pr_number']}")
        if pr.get("state") != "closed":
            return []
        if not pr.get("merged"):
            self._record(project_id, stage="closed", version=pending["version"],
                         pr_number=pending["pr_number"])
            return [f"Release {pending['version']} was closed without merging; nothing changed."]
        merge_sha, head_sha = pr["merge_commit_sha"], pr["head"]["sha"]
        git = self._git
        self._fetch(app, repo, release)
        tree = git(["rev-parse", f"{merge_sha}^{{tree}}"], repo)
        if tree != git(["rev-parse", f"{head_sha}^{{tree}}"], repo):
            self._record(project_id, stage="mismatch", version=pending["version"],
                         pr_number=pending["pr_number"], merge_sha=merge_sha, head_sha=head_sha)
            return [f"Release {pending['version']}: main's files differ from the reviewed "
                    "develop; not tagged. Check the merge on GitHub."]
        version = pending["version"]
        git(["tag", "-a", version, merge_sha, "-m", f"Release {version}"], repo,
            extra_env={"GIT_COMMITTER_NAME": "Master System",
                       "GIT_COMMITTER_EMAIL": "master-system@localhost"})
        self._push(app, repo, [f"refs/tags/{version}"], self._askpass_dir)
        created = app.request("POST", f"/repos/{repo_name}/releases",
                              {"tag_name": version, "name": version,
                               "body": pending.get("notes") or ""})
        self._record(project_id, stage="published", version=version,
                     pr_number=pending["pr_number"], merge_sha=merge_sha, head_sha=head_sha,
                     tree=tree, release_url=(created or {}).get("html_url"))
        lines = [f"Released {version}: {(created or {}).get('html_url') or ''}".rstrip()]
        if self._publisher is not None:
            lines += self._publisher(project_id)
        return lines

    def _fetch(self, app, repo, release) -> None:
        """Bring the merged release branch into the dedicated clone (fast-forward only)."""
        from core.workspace import branch_tip, fast_forward, is_ancestor

        url = self._remote_url(app.repo)
        self._git(["fetch", "-q", url, f"+refs/heads/{release}:refs/ms-release/{release}"], repo)
        new = self._git(["rev-parse", f"refs/ms-release/{release}"], repo)
        old = branch_tip(repo, release)
        if old != new and is_ancestor(repo, old, new):
            fast_forward(repo, release, old, new)
