"""core.release: notes from history, the release PR, and tagging after the owner's merge."""

import pytest
import yaml

from conftest import git, make_repo
from core.history import EventType, InMemoryHistoryStore
from core.master import Master
from core.release import Releaser

A1, A2 = "a" * 32, "b" * 32


@pytest.fixture
def env(tmp_path):
    repo = make_repo(tmp_path / "repo", {"www/index.html": "v1\n"})
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "-q", "--bare", str(repo), str(remote))
    git(repo, "branch", "develop")
    git(repo, "checkout", "-q", "develop")
    shas = []
    for name in ("one", "two"):
        (repo / "www" / f"{name}.js").write_text(name + "\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", name)
        shas.append(git(repo, "rev-parse", "HEAD"))
    git(repo, "checkout", "-q", "main")
    root = tmp_path / "projects"
    project = root / "ml"
    project.mkdir(parents=True)
    (project / "project.yaml").write_text(yaml.safe_dump({
        "id": "ml", "name": "ML", "status": "active", "repository": str(repo),
        "base_branch": "develop", "auto_integrate": True,
        "github": {"repo": "o/r", "release_branch": "main"}}))
    (project / "milestones.yaml").write_text(yaml.safe_dump(
        {"milestones": [{"id": "m", "name": "M", "status": "in_progress"}]}))
    (project / "tasks.yaml").write_text(yaml.safe_dump({"tasks": [
        {"id": "app-9", "milestone": "m", "title": "First thing", "status": "completed"},
        {"id": "app-10", "milestone": "m", "title": "Second thing", "status": "completed"}]}))
    history = InMemoryHistoryStore()
    for task_id, attempt, sha in (("app-9", A1, shas[0]), ("app-10", A2, shas[1])):
        common = dict(run_id="r", project_id="ml", task_id=task_id, attempt_id=attempt)
        history.append(type=EventType.ATTEMPT_STARTED, event_id=attempt, run_id="r",
                       project_id="ml", task_id=task_id,
                       payload={"manual_check": f"1. Open the game.\n2. See {task_id}."})
        history.append(type=EventType.ATTEMPT_FINISHED, **common, payload={
            "outcome": "finished", "result_sha": sha,
            "diffstat": {"files": 1, "insertions": 1, "deletions": 0}})
        history.append(type=EventType.VERIFICATION, **common, payload={
            "verdict": "pass", "evidence": {"commands_run": [{"command": "npm test"}]}})
        history.append(type=EventType.INTEGRATION, **common, payload={"result_sha": sha})
    app = FakeApp()
    pushes = []
    releaser = Releaser(Master(root), history, lambda name: app, askpass_dir=tmp_path / "s",
                        push=lambda app, repo, refspecs, askpass, **k: pushes.append(refspecs),
                        remote_url=lambda name: str(remote))
    return {"repo": repo, "remote": remote, "history": history, "app": app, "pushes": pushes,
            "releaser": releaser, "shas": shas}


class FakeApp:
    repo = "o/r"

    def __init__(self):
        self.calls = []
        self.pr = {"number": 7, "html_url": "https://github.com/o/r/pull/7", "state": "open"}

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.endswith("/releases"):
            return {"html_url": "https://github.com/o/r/releases/tag/v0.1"}
        return dict(self.pr)


def owner_merges(env):
    """What GitHub does when the owner merges develop into main (a merge commit)."""
    remote = env["remote"]
    git(env["repo"], "push", "-q", str(remote), "develop")
    work = env["remote"].parent / "merge"
    git(remote.parent, "clone", "-q", str(remote), str(work))
    git(work, "merge", "-q", "--no-ff", "origin/develop", "-m", "Merge pull request #7")
    git(work, "push", "-q", "origin", "main")
    env["app"].pr = {"number": 7, "state": "closed", "merged": True,
                     "merge_commit_sha": git(work, "rev-parse", "HEAD"),
                     "head": {"sha": git(work, "rev-parse", "origin/develop")}}


def test_prepare_opens_a_pr_with_notes_from_history(env):
    result = env["releaser"].prepare("ml")

    assert result["opened"] and result["version"] == "v0.1"
    assert result["tasks"] == ["app-9", "app-10"]
    [(method, path, body)] = env["app"].calls
    assert (method, path) == ("POST", "/repos/o/r/pulls")
    assert body["head"] == "develop" and body["base"] == "main"
    notes = body["body"]
    assert "## app-9: First thing" in notes and "## app-10: Second thing" in notes
    assert "2. See app-10." in notes and "Verified: pass (1 acceptance command(s))" in notes
    assert env["pushes"] == [["develop"]]
    [event] = env["history"].events(types=[EventType.RELEASE])
    assert event.payload["stage"] == "pr_opened" and event.payload["pr_number"] == 7


def test_a_second_prepare_updates_the_open_pr(env):
    env["releaser"].prepare("ml")
    result = env["releaser"].prepare("ml")
    assert result["version"] == "v0.1"
    assert env["app"].calls[-1][:2] == ("PATCH", "/repos/o/r/pulls/7")


def test_watch_waits_while_the_pr_is_open(env):
    env["releaser"].prepare("ml")
    assert env["releaser"].watch("ml") == []


def test_after_the_merge_main_is_fetched_tagged_and_released(env):
    env["releaser"].prepare("ml")
    owner_merges(env)

    lines = env["releaser"].watch("ml")

    repo = env["repo"]
    merge = env["app"].pr["merge_commit_sha"]
    assert git(repo, "rev-parse", "main") == merge
    assert git(repo, "rev-parse", "v0.1^{commit}") == merge
    assert env["pushes"][-1] == ["refs/tags/v0.1"]
    assert ("POST", "/repos/o/r/releases") == env["app"].calls[-1][:2]
    assert env["app"].calls[-1][2]["tag_name"] == "v0.1"
    assert lines[0].startswith("Released v0.1")
    assert env["releaser"].watch("ml") == []  # done once
    assert env["releaser"].prepare("ml")["opened"] is False  # nothing new to release
    assert env["releaser"].next_version("ml", repo) == "v0.2"


def test_a_merge_with_different_files_is_not_tagged(env):
    env["releaser"].prepare("ml")
    owner_merges(env)
    env["app"].pr["head"]["sha"] = env["shas"][0]  # GitHub reports another head
    lines = env["releaser"].watch("ml")
    assert "differ" in lines[0]
    assert "v0.1" not in git(env["repo"], "tag", "-l")


def test_a_closed_pr_releases_nothing(env):
    env["releaser"].prepare("ml")
    env["app"].pr = {"number": 7, "state": "closed", "merged": False}
    assert "closed without merging" in env["releaser"].watch("ml")[0]
    assert env["releaser"].open_pending("ml") is None


def test_a_local_main_ahead_of_github_does_not_count_as_released(env):
    repo = env["repo"]
    git(repo, "merge", "-q", "--ff-only", "develop")  # integrated locally, never pushed
    result = env["releaser"].prepare("ml")
    assert result["opened"] and result["tasks"] == ["app-9", "app-10"]


def test_prepare_commits_a_changelog_entry_to_develop(env):
    repo = env["repo"]
    before = git(repo, "rev-parse", "develop")
    env["releaser"].prepare("ml")
    changelog = git(repo, "show", "develop:CHANGELOG.md")
    assert changelog.startswith("# Changelog")
    assert "## v0.1 (" in changelog
    assert "- app-9: First thing" in changelog and "- app-10: Second thing" in changelog
    assert git(repo, "rev-parse", "develop^") == before
    env["releaser"].prepare("ml")  # updating the PR does not duplicate the entry
    assert git(repo, "show", "develop:CHANGELOG.md").count("## v0.1 (") == 1


def test_prepare_refuses_a_develop_the_system_did_not_set(env):
    from core.workspace import accept_tip

    accept_tip(env["repo"], "develop")
    git(env["repo"], "checkout", "-q", "develop")
    (env["repo"] / "www" / "sneak.js").write_text("x\n")
    git(env["repo"], "add", "-A")
    git(env["repo"], "commit", "-qm", "outside the system")
    git(env["repo"], "checkout", "-q", "main")
    before = git(env["repo"], "rev-parse", "develop")

    result = env["releaser"].prepare("ml")

    assert not result["opened"] and "--accept-tip" in result["message"]
    assert env["pushes"] == [] and env["app"].calls == []
    assert git(env["repo"], "rev-parse", "develop") == before  # no changelog commit
