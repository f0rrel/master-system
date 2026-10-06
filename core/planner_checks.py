"""The planner's deterministic checks, and what "approve" does (see core.planner).

Checks run in a fresh worktree of the development branch, through
AttemptWorkspace (the allowlisted worker environment, no secrets, a deadline,
logs under the state dir), never in the owner's checkout.
"""

from __future__ import annotations

import shlex
import time
import uuid
from datetime import datetime, timedelta, timezone

from core.evidence import spec_hash
from core.history import EventType
from core.planner import ACTOR, DraftProblem, planner_settings
from core.run_lock import ProjectLock
from core.task_orchestrator import default_worktrees
from core.workspace import AttemptWorkspace, branch_tip, fast_forward

__all__ = ["make_approver", "make_checker"]

#: Markers meaning "a module is missing"; not a broken test when the module is one
#: the task itself will create.
MISSING_MODULE = ("Cannot find module", "ERR_MODULE_NOT_FOUND", "ModuleNotFoundError",
                  "No module named")


def _names_new_file(output: str, new_files) -> bool:
    """Whether a missing-module error is about one of the files the task will create."""
    import re
    from pathlib import PurePosixPath

    for line in output.splitlines():
        if not any(marker in line for marker in MISSING_MODULE):
            continue
        for name in new_files:
            path = PurePosixPath(name)
            if re.search(rf"(^|[/'\"\s.]){re.escape(path.stem)}({re.escape(path.suffix)})?"
                         r"(['\"\s]|$)", line):
                return True
    return False

IDENTITY = {"GIT_AUTHOR_NAME": "Master System planner",
            "GIT_AUTHOR_EMAIL": "master-system@localhost",
            "GIT_COMMITTER_NAME": "Master System planner",
            "GIT_COMMITTER_EMAIL": "master-system@localhost"}


def make_checker(master, paths, worker_env, timeout_s: float = 1800):
    def check(project_id, draft, lock_fd=None):
        project = master.project_state(project_id).project()
        settings = planner_settings(project)
        repo = master.project_state(project_id).project()["repository"]
        from pathlib import Path

        repo = Path(repo).expanduser()
        branch = project["base_branch"]
        tip = branch_tip(repo, branch)
        worktrees = default_worktrees(master, paths)
        name = f"planner-{uuid.uuid4().hex[:12]}"
        path = worktrees.free_path(project_id, name)
        worktrees.create(repo, path, name, tip, branch=f"planner/{path.name}")
        for task in draft["tasks"]:
            for test in task["tests"]:
                target = path / test["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(test["content"])
        deadline_at = (datetime.now(timezone.utc) + timedelta(seconds=timeout_s)).isoformat(
            timespec="seconds")
        workspace = AttemptWorkspace(
            path=path, base_sha=tip, deadline=time.monotonic() + timeout_s,
            deadline_at=deadline_at, log_dir=paths.state_dir / "logs" / project_id / name,
            phase="planner-check", env=worker_env, lock_fd=lock_fd)
        items = []

        def run(command):
            outcome = workspace.run(["/bin/sh", "-c", command])
            return outcome, (outcome.stdout or "") + (outcome.stderr or "")

        def item(what, ok, note=""):
            items.append({"what": what, "ok": ok, "note": note})
            return ok

        for command in settings["setup"]:
            outcome, _ = run(command)
            if not item(f"setup: {command}", outcome.returncode == 0 and not outcome.timed_out,
                        f"exit {outcome.returncode}"):
                return {"ok": False, "items": items, "base_sha": tip, "worktree": str(path)}
        for task in draft["tasks"]:
            if settings["syntax_check"]:
                for test in task["tests"]:
                    outcome, output = run(settings["syntax_check"].format(
                        path=shlex.quote(test["path"])))
                    item(f"{task['id']}: {test['path']} passes the syntax check",
                         outcome.returncode == 0, output.strip()[-300:])
            # Files the task will create: a test may fail because they do not exist yet.
            new_files = [f for f in task.get("files") or []
                         if isinstance(f, str) and not (path / f).exists()]
            for command in task["test_commands"]:
                outcome, output = run(command)
                broken = [m for m in settings["broken_test_markers"] if m in output
                          and not (m in MISSING_MODULE and _names_new_file(output, new_files))]
                if outcome.timed_out:
                    item(f"{task['id']}: {command}", False, "timed out")
                elif outcome.returncode == 0:
                    item(f"{task['id']}: {command}", False,
                         "passes already on the current code; it must fail until the task "
                         "is done")
                elif broken:
                    item(f"{task['id']}: {command}", False,
                         f"fails because the test itself is broken ({broken[0]}): "
                         + output.strip()[-300:])
                else:
                    item(f"{task['id']}: {command}", True, "fails on the current code, as it should")
        for command in settings["base_checks"]:
            outcome, output = run(command)
            item(f"base check: {command}", outcome.returncode == 0 and not outcome.timed_out,
                 output.strip()[-300:])
        return {"ok": all(i["ok"] for i in items), "items": items, "base_sha": tip,
                "worktree": str(path)}

    return check


def make_approver(master, history, paths, checker, git=None):
    if git is None:
        from core.host import git

    def approve(project_id, draft, records, *, chat_id, draft_hash, size_override=None):
        from pathlib import Path

        state = master.project_state(project_id)
        project = state.project()
        repo = Path(project["repository"]).expanduser()
        branch = project["base_branch"]
        with ProjectLock(state.project_path, holder=f"planner approve {chat_id}") as lock:
            result = checker(project_id, draft, lock_fd=lock.fileno()) if records else None
            if result is not None and not result["ok"]:
                raise DraftProblem("the checks failed on the latest code; run `check` again")
            for entry in draft.get("backlog") or []:
                master.add_backlog_epic(project_id, entry["id"], entry["title"].strip(),
                                        entry.get("summary"), entry.get("priority"))
                history.append(
                    type=EventType.HUMAN_ACTION, run_id=uuid.uuid4().hex,
                    project_id=project_id,
                    payload={"actor": ACTOR, "action": "backlog_add", "epic": entry["id"],
                             "chat_id": chat_id, "draft_hash": draft_hash})
            if not records:
                return None
            worktree = Path(result["worktree"])
            test_paths = [t["path"] for task in draft["tasks"] for t in task["tests"]]
            git(["add", "--", *test_paths], worktree)
            epic = draft["epic"]
            git(["commit", "-q", "-m",
                 f"{epic['id']}: acceptance tests for {epic['title']}\n\n"
                 f"Approved by the owner in planner chat {chat_id} (draft {draft_hash[:12]}).",
                 "--", *test_paths], worktree, extra_env=IDENTITY)
            commit = git(["rev-parse", "HEAD"], worktree)
            fast_forward(repo, branch, result["base_sha"], commit)
            if draft.get("replaces"):
                master.split_task(project_id, draft["replaces"], records)
                history.append(
                    type=EventType.HUMAN_ACTION, run_id=uuid.uuid4().hex,
                    project_id=project_id, task_id=draft["replaces"],
                    payload={"actor": ACTOR, "action": "split", "chat_id": chat_id,
                             "draft_hash": draft_hash,
                             "replaced_by": [r["id"] for r in records]})
            else:
                master.add_planned_work(project_id, {"id": epic["id"], "name": epic["title"]},
                                        records)
            for record in records:
                history.append(
                    type=EventType.HUMAN_ACTION, run_id=uuid.uuid4().hex,
                    project_id=project_id, task_id=record["id"],
                    payload={"actor": ACTOR, "action": "planner_approved", "chat_id": chat_id,
                             **({"size_override": True}
                                if size_override and record["id"] in size_override else {}),
                             "draft_hash": draft_hash, "tests_commit": commit,
                             "spec_hash_before": None, "spec_hash_after": spec_hash(record)})
        return commit

    return approve
