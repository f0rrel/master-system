"""The background service: processes approved tasks without anyone starting runs.

One cycle (``Daemon.cycle``), every ``[daemon] interval_s`` seconds:

1. If the owner paused the service (``ms pause``), do nothing.
2. If today's priced spend reached ``[budget] daily_usd``, do nothing (and say so once).
3. For each project that is idle and has work, start one run as a separate
   process (``core.run_cli start ... --until-stopped``) with a per-run cap of
   ``min(run_usd, what is left today)``, wait for it, then run the
   ``after_run`` hooks (integration into develop, publishing).
4. Notify the owner about what changed: tasks done, tasks blocked, tasks that
   need a human, the daily cap.

Only projects that opted in (``auto_integrate: true`` in project.yaml) are
run; others (such as the system's own roadmap project) are never touched.

"Work" is a task the owner approved (every task in the project's files was
written or approved by a human; tasks Master creates wait for approval under
the policy) that is ``in_progress``, or ``planned`` with its dependencies
completed, and that has acceptance commands. A task that already failed
``max_failures`` attempts since the last human action on it is not retried:
it waits for the owner, so the service cannot spend money on it every day.

The run itself is the existing run_cli path, with every safety rule it has
(project lock, worktrees, verification, the completion gate, budgets).
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from core.history import EventType

__all__ = ["Daemon", "OBJECTIVE", "RunRequest", "failed_attempts_since_human",
           "load_env_file", "start_of_today_utc", "work_for"]

OBJECTIVE = (
    "Complete the ready tasks one at a time, in the order ready_tasks lists them (backlog "
    "priority order): start a task, run it, and mark it completed "
    "only when its latest attempt passed verification. Never request integration: the "
    "system integrates verified work itself. If a task fails verification on its third "
    "attempt, mark it blocked and move on to the next task. When no task is ready, stop."
)


def start_of_today_utc(now: Optional[datetime] = None) -> str:
    """Local midnight today, as the UTC ISO string history timestamps use."""
    now = (now or datetime.now()).astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).isoformat()


def load_env_file(path: Path) -> dict:
    """KEY=VALUE lines (``export`` and quotes allowed). Values are never logged."""
    values = {}
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def failed_attempts_since_human(history, project_id: str, task_id: str) -> int:
    """Finished attempts on the task without a pass, since the last human action on it."""
    events = history.events(project_id=project_id, task_id=task_id)
    count = 0
    passed = set()
    for e in events:
        if e.type is EventType.HUMAN_ACTION:
            count = 0
            passed = set()
        elif e.type is EventType.VERIFICATION and e.payload.get("verdict") == "pass":
            passed.add(e.attempt_id)
        elif e.type is EventType.ATTEMPT_FINISHED:
            count += 1
    # Attempts that later passed are not failures.
    return max(0, count - len(passed))


def work_for(status: dict, history, max_failures: int = 3):
    """(runnable task ids, exhausted task ids) for one project's status, in backlog order."""
    from core.backlog import ordered_tasks

    tasks = ordered_tasks(status.get("milestones") or [], status.get("tasks") or [])
    done = {t.get("id") for t in tasks if t.get("status") == "completed"}
    runnable, exhausted = [], []
    for task in tasks:
        state = task.get("status")
        if state not in ("planned", "in_progress"):
            continue
        if state == "planned" and any(d not in done for d in task.get("depends_on") or []):
            continue
        if not (task.get("acceptance") or {}).get("commands"):
            continue
        if failed_attempts_since_human(history, status["project_id"], task["id"]) >= max_failures:
            exhausted.append(task["id"])
        else:
            runnable.append(task["id"])
    return runnable, exhausted


@dataclass
class RunRequest:
    project_id: str
    session_id: str
    max_cost_usd: float


@dataclass
class Daemon:
    """One background service. Every collaborator is injectable for tests."""

    master: object
    history: object
    config: object
    state_dir: Path
    notifier: object
    #: Runs one session to completion; returns the process exit code.
    run: Callable[[RunRequest], int]
    #: Called after each run with (project_id, session_id); returns notification lines.
    after_run: list = field(default_factory=list)
    is_busy: Callable[[str], bool] = lambda project_id: False
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    max_failures: int = 3
    preview_url: Callable[[str], Optional[str]] = lambda project_id: None
    #: Called every cycle per project (even without a run), e.g. the release
    #: watcher; returns notification lines.
    watchers: list = field(default_factory=list)
    #: Called per idle hands-off project before choosing work (e.g. images for
    #: visual tasks, core.images.AssetStep); returns lines.
    prepare: list = field(default_factory=list)
    #: project_id -> {task id: why it waits for the owner}; such tasks are not run.
    waiting: Callable[[str], dict] = lambda project_id: {}

    # --- files the service and `ms` share ---

    @property
    def pause_flag(self) -> Path:
        return Path(self.state_dir) / "paused"

    @property
    def _memory_path(self) -> Path:
        return Path(self.state_dir) / "daemon-state.json"

    def _memory(self) -> dict:
        try:
            return json.loads(self._memory_path.read_text())
        except (OSError, ValueError):
            return {}

    def _remember(self, memory: dict) -> None:
        self._memory_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._memory_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(memory, indent=2, sort_keys=True))
        tmp.replace(self._memory_path)

    def _notify_once(self, memory, key, title, message, **kwargs) -> None:
        if memory.get("notified", {}).get(key):
            return
        self.notifier.send(title, message, **kwargs)
        memory.setdefault("notified", {})[key] = self.now().isoformat()

    # --- one cycle ---

    def spent_today(self) -> float:
        from core.report import spend_since

        spend = spend_since(self.history, start_of_today_utc(self.now()), self.config.prices,
                            self.config.master.model)
        return spend["total_usd"]

    def cycle(self) -> list:
        """Run everything due now. Returns what happened, one line per item."""
        log = []
        memory = self._memory()
        today = start_of_today_utc(self.now())[:10]
        if self.pause_flag.exists():
            return ["paused"]
        spent = self.spent_today()
        daily = self.config.budget.daily_usd
        if spent >= daily:
            self._notify_once(memory, f"cap:{today}", "Daily budget reached",
                              f"Spent ${spent:.3f} of ${daily:.2f} today. Work resumes "
                              "tomorrow, or raise [budget] daily_usd.", tags="money_with_wings")
            self._remember(memory)
            return [f"daily cap reached (${spent:.4f} of ${daily:.2f})"]

        for project_id in self.master.list_projects():
            for watcher in self.watchers:
                try:
                    lines = watcher(project_id) or []
                except Exception as error:
                    lines = [f"release check failed: {error}"]
                if lines:
                    self.notifier.send(f"{project_id}: release", "\n".join(lines),
                                       tags="rocket", click=self.preview_url(project_id))
                    log += [f"{project_id}: {line}" for line in lines]
            if not self._hands_off(project_id):
                continue
            if self.is_busy(project_id):
                log.append(f"{project_id}: busy")
                continue
            stalled = memory.get("stalled", {}).get(project_id)
            if stalled is not None and stalled == self._human_mark(project_id):
                log.append(f"{project_id}: stalled; waiting for the owner")
                continue
            memory.get("stalled", {}).pop(project_id, None)
            for step in self.prepare:
                try:
                    lines = step(project_id) or []
                except Exception as error:  # a failed step must not stop the service
                    lines = [f"preparing failed: {error}"]
                log += [f"{project_id}: {line}" for line in lines]
                for line in lines:
                    if "to pick from" in line or "failed" in line:
                        self._notify_once(memory, f"prepare:{project_id}:{line}",
                                          f"{project_id}: needs you", line,
                                          tags="frame_with_picture")
            before = self.master.status(project_id)
            runnable, exhausted = work_for(before, self.history, self.max_failures)
            waiting = self.waiting(project_id) or {}
            runnable = [t for t in runnable if t not in waiting]
            for task_id in exhausted:
                self._notify_once(
                    memory, f"exhausted:{project_id}:{task_id}:{self._last_human(project_id, task_id)}",
                    f"{project_id}: {task_id} needs you",
                    f"{task_id} failed {self.max_failures} attempts. Change its description "
                    "or acceptance (or the planner can), and it will be tried again.",
                    tags="raising_hand", priority="high")
            if not runnable:
                log.append(f"{project_id}: nothing to do")
                continue
            budget = round(min(self.config.budget.run_usd, daily - spent), 4)
            if budget <= 0:
                break
            session_id = f"{project_id}-auto-{self.now().strftime('%Y%m%d-%H%M%S')}"
            memory["current"] = {"project_id": project_id, "session_id": session_id,
                                 "started_at": self.now().isoformat()}
            self._remember(memory)
            code = self.run(RunRequest(project_id, session_id, budget))
            memory = self._memory()
            memory.pop("current", None)
            memory["last_session"] = session_id
            log.append(f"{project_id}: ran {session_id} (exit {code}, tasks {runnable})")
            extra = []
            for hook in self.after_run:
                try:
                    extra += hook(project_id, session_id) or []
                except Exception as error:  # a hook failure must not stop the service
                    extra.append(f"after-run step failed: {error}")
            progressed = self._report_changes(memory, project_id, session_id, before, code,
                                              extra)
            if not progressed:
                # The same run would happen again in five minutes: wait for a
                # human action (a task edit, a new task, a reopen) instead.
                memory.setdefault("stalled", {})[project_id] = self._human_mark(project_id)
                self.notifier.send(f"{project_id}: needs you",
                                   "The last run made no progress (no attempt, no task "
                                   "finished). The service waits until a task is changed or "
                                   "added; see `ms status` and `ms report`.",
                                   tags="raising_hand", priority="high")
            spent = self.spent_today()
            if spent >= daily:
                break
        self._remember(memory)
        return log

    def _human_mark(self, project_id) -> int:
        events = self.history.events(project_id=project_id, types=[EventType.HUMAN_ACTION])
        return events[-1].seq if events else 0

    def _hands_off(self, project_id) -> bool:
        """Only projects that opted in (project.yaml ``auto_integrate: true``) are run."""
        try:
            return self.master.project_state(project_id).project().get("auto_integrate") is True
        except Exception:
            return False

    def _last_human(self, project_id, task_id) -> int:
        events = self.history.events(project_id=project_id, task_id=task_id,
                                     types=[EventType.HUMAN_ACTION])
        return events[-1].seq if events else 0

    def _report_changes(self, memory, project_id, session_id, before, code, extra) -> None:
        after = self.master.status(project_id)
        old = {t["id"]: t.get("status") for t in before.get("tasks") or []}
        titles = {t["id"]: t.get("title") for t in after.get("tasks") or []}
        done = [t["id"] for t in after.get("tasks") or []
                if t.get("status") == "completed" and old.get(t["id"]) != "completed"]
        blocked = [t["id"] for t in after.get("tasks") or []
                   if t.get("status") == "blocked" and old.get(t["id"]) != "blocked"]
        link = self.preview_url(project_id)
        lines = [f"Done: {t} {titles.get(t) or ''}".rstrip() for t in done]
        lines += [f"Blocked: {t} {titles.get(t) or ''}".rstrip() for t in blocked]
        lines += extra
        if code not in (0, None):
            lines.append(f"The run ended with an error (exit {code}); see `ms status`.")
        lines.append(f"Spent today: ${self.spent_today():.3f}")
        if link:
            lines.append(f"Preview: {link}")
        title = f"{project_id}: batch done" if done else f"{project_id}: run finished"
        tags = "white_check_mark" if done and not blocked else ("warning" if blocked else "")
        self.notifier.send(title, "\n".join(lines), tags=tags, click=link,
                           priority="high" if blocked else "default")
        for task_id in blocked:
            memory.setdefault("notified", {})[f"blocked:{project_id}:{task_id}:{session_id}"] = \
                self.now().isoformat()
        attempts = self.history.events(session_id=session_id, types=[EventType.ATTEMPT_STARTED])
        changed = any(old.get(t["id"]) != t.get("status") for t in after.get("tasks") or [])
        return bool(attempts) or changed

    def serve(self, sleep: Callable[[float], None] = time.sleep,
              should_stop: Callable[[], bool] = lambda: False, out=None) -> None:
        """Cycle forever (until ``should_stop``)."""
        out = out or sys.stdout
        while not should_stop():
            try:
                for line in self.cycle():
                    print(f"{self.now().isoformat()[:19]} {line}", file=out, flush=True)
            except Exception as error:  # keep serving; the next cycle may succeed
                print(f"{self.now().isoformat()[:19]} cycle failed: {error!r}", file=out,
                      flush=True)
            sleep(self.config.daemon.interval_s)
