"""The planner: the owner describes what they want; it drafts an epic of tasks with tests.

``ms chat <project>`` holds one planner chat. Each owner message is one *turn*:
the planner model answers with a reply, questions for the owner, and (when it
can) a complete draft: an epic and its tasks, each with a description, a size,
acceptance tests it writes, acceptance commands, protected paths and a
``manual_check``. The model can read files of the project's development branch
(read-only, through git) before answering. It never writes code and never
changes anything: only the owner's ``approve`` does (owner decision H-D6).

Before a draft can be approved it must pass deterministic checks in a fresh
worktree of the development branch (``check``):

* every test file is valid JavaScript (``node --check``);
* each task's own test commands FAIL on the current code, and not because the
  test itself is broken (no syntax error, missing module, or "no tests found");
* the project's base checks still pass with the new tests present.

``approve`` re-runs those checks under the project lock, commits the tests to
the development branch, writes the epic and its tasks (all or nothing), and
records one ``human_action`` per task (actor ``owner via planner``) with the
draft's hash. Nothing is queued before that.

project.yaml::

    planner:
      test_dir: tests/tasks
      setup: ["npm ci --prefer-offline --no-audit --no-fund"]
      base_checks: ["node --test tests/unit/*.test.js", "npx playwright test tests/smoke"]
      protected_paths: ["tests/*", "package.json", ...]
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from core.history import EventType

__all__ = ["PlannerChat", "DraftProblem", "draft_hash", "draft_problems", "render_draft",
           "planner_settings"]

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
SIZES = ("small", "medium", "hard")
MAX_TASKS = 8
MAX_TEST_CHARS = 20000
MAX_FILE_CHARS = 20000
BROKEN_TEST_MARKERS = ("SyntaxError", "Cannot find module", "ERR_MODULE_NOT_FOUND",
                       "No tests found", "Error: No test files found")
ACTOR = "owner via planner"

SCHEMA = {
    "type": "object",
    "required": ["reply", "questions", "read_files", "draft"],
    "properties": {
        "reply": {"type": "string"},
        "questions": {"type": "array", "items": {"type": "string"}},
        "read_files": {"type": "array", "items": {"type": "string"}},
        "draft": {"type": ["object", "null"]},
    },
}


class DraftProblem(ValueError):
    pass


def planner_settings(project: dict) -> dict:
    planner = project.get("planner") if isinstance(project.get("planner"), dict) else {}
    return {
        "test_dir": planner.get("test_dir", "tests/tasks").rstrip("/"),
        "setup": list(planner.get("setup") or []),
        "base_checks": list(planner.get("base_checks") or []),
        "protected_paths": list(planner.get("protected_paths") or ["tests/*"]),
    }


def draft_hash(draft) -> str:
    return hashlib.sha256(json.dumps(draft, sort_keys=True).encode()).hexdigest()


def draft_problems(draft, existing_task_ids, existing_milestone_ids, settings) -> list:
    """Why a draft cannot be checked or approved (empty list: it can)."""
    problems = []
    if not isinstance(draft, dict):
        return ["the draft is not an object"]
    epic = draft.get("epic") if isinstance(draft.get("epic"), dict) else {}
    if not ID_PATTERN.match(str(epic.get("id", ""))):
        problems.append("the epic needs an id of lowercase letters, digits and dashes")
    elif epic["id"] in existing_milestone_ids:
        problems.append(f"the epic id {epic['id']} is already used")
    if not str(epic.get("title", "")).strip():
        problems.append("the epic needs a title")
    tasks = draft.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return problems + ["the draft has no tasks"]
    if len(tasks) > MAX_TASKS:
        problems.append(f"at most {MAX_TASKS} tasks per epic")
    ids = [t.get("id") for t in tasks if isinstance(t, dict)]
    known = set(existing_task_ids) | set(ids)
    test_dir = settings["test_dir"] + "/"
    for task in tasks:
        if not isinstance(task, dict):
            problems.append("a task is not an object")
            continue
        tid = str(task.get("id", ""))
        where = f"task {tid or '?'}"
        if not ID_PATTERN.match(tid):
            problems.append(f"{where}: invalid id")
        elif tid in existing_task_ids or ids.count(tid) > 1:
            problems.append(f"{where}: the id is already used")
        for name in ("title", "description", "manual_check"):
            value = task.get(name)
            if not isinstance(value, str) or not value.strip():
                problems.append(f"{where}: missing {name}")
            elif len(value) > 4000:
                problems.append(f"{where}: {name} is longer than 4000 characters")
        if task.get("size") not in SIZES:
            problems.append(f"{where}: size must be small, medium or hard")
        for dep in task.get("depends_on") or []:
            if dep not in known or dep == tid:
                problems.append(f"{where}: unknown dependency {dep}")
        tests = task.get("tests") or []
        if not tests:
            problems.append(f"{where}: needs at least one test file")
        paths = []
        for test in tests:
            path = str((test or {}).get("path", ""))
            paths.append(path)
            if (not path.startswith(test_dir) or ".." in path or not path.endswith(".js")
                    or not re.match(r"^[\w./-]+$", path)):
                problems.append(f"{where}: test path {path!r} must be a .js file under {test_dir}")
            content = (test or {}).get("content")
            if not isinstance(content, str) or not content.strip():
                problems.append(f"{where}: test {path} is empty")
            elif len(content) > MAX_TEST_CHARS:
                problems.append(f"{where}: test {path} is too long")
        commands = task.get("test_commands") or []
        if not commands or len(commands) > 3:
            problems.append(f"{where}: needs 1 to 3 test_commands that run its tests")
        for command in commands:
            if not isinstance(command, str) or not any(p and p in command for p in paths):
                problems.append(f"{where}: the command {command!r} must run one of its test files")
    all_paths = [str(t.get("path")) for task in tasks if isinstance(task, dict)
                 for t in task.get("tests") or [] if isinstance(t, dict)]
    if len(all_paths) != len(set(all_paths)):
        problems.append("two tasks use the same test file path")
    return problems


def task_records(draft, settings) -> list:
    """The task records an approved draft becomes."""
    records = []
    for task in draft["tasks"]:
        record = {
            "id": task["id"], "milestone": draft["epic"]["id"], "title": task["title"].strip(),
            "status": "planned", "size": task["size"],
            "description": task["description"].strip(),
            "acceptance": {
                "commands": [*settings["setup"], *task["test_commands"],
                             *settings["base_checks"]],
                "protected_paths": list(settings["protected_paths"]),
            },
            "manual_check": task["manual_check"].strip(),
        }
        if task.get("depends_on"):
            record["depends_on"] = list(task["depends_on"])
        records.append(record)
    return records


def _wrap(text, width, indent):
    import textwrap

    out = []
    for paragraph in str(text or "").splitlines() or [""]:
        out += textwrap.wrap(paragraph, width=max(30, width - len(indent)),
                             break_long_words=False, break_on_hyphens=False) or [""]
    return [indent + line if line else "" for line in out]


def _labelled(label, text, width, indent="  "):
    """'Label:  text' with continuation lines aligned under the text."""
    pad = indent + " " * 22
    lines = _wrap(text, width, pad)
    if not lines:
        return [f"{indent}{label + ':':<22}"]
    lines[0] = f"{indent}{label + ':':<22}" + lines[0].lstrip()
    return lines


def render_draft(draft, check=None, width: int = 100) -> str:
    if not draft:
        return "  No draft yet."
    epic = draft.get("epic") or {}
    lines = [f"EPIC {epic.get('id')}: {epic.get('title')}"]
    if epic.get("description"):
        lines += _wrap(epic["description"], width, "  ")
    for task in draft.get("tasks") or []:
        header = f"── {task.get('id')}: {task.get('title')} "
        lines += ["", header + "─" * max(3, min(width, 60) - len(header))]
        size = str(task.get("size"))
        if task.get("depends_on"):
            size += f" (after {', '.join(task['depends_on'])})"
        lines += _labelled("Size", size, width)
        files = task.get("files") or []
        lines += _labelled("Files", ", ".join(files) if files else "(not given)", width)
        lines += _labelled("What it does", task.get("description", ""), width)
        lines += _labelled("How to check by hand", task.get("manual_check", ""), width)
        tests = [f"{t.get('path')} ({len(str(t.get('content', '')).splitlines())} lines)"
                 for t in task.get("tests") or []]
        lines += _labelled("Tests", "; ".join(tests) or "(none)", width)
        if task.get("test_commands"):
            lines += _labelled("Test commands", "; ".join(task["test_commands"]), width)
    if check:
        lines.append("\nChecks: " + ("all passed, ready to approve" if check["ok"]
                                     else "FAILED (see below)"))
        for item in check["items"]:
            if not item["ok"]:
                lines += _wrap(f"!! {item['what']}: {item['note']}", width, "  ")
    return "\n".join(lines)

SYSTEM = """You are the planner for the software project "{name}". Its owner tells you, in \
plain language, what they want. You turn that into an EPIC split into small TASKS that a \
coding agent can each finish in one attempt (a focused change, usually under ~200 lines), \
with acceptance TESTS that you write.

Rules:
- When something is unclear or is the owner's decision (look and feel, where art or sounds \
come from, scope, priorities), ask in "questions" instead of guessing. Keep questions few \
and concrete; offer options.
- Prefer independent tasks; use depends_on only when one task truly needs another.
- Task ids: continue the project's numbering; the next free ids are {next_ids}. The epic id \
is a short slug like "epic-avatars".
- size: "small" (one function or one screen element), "medium" (several parts), "hard" \
(new mechanics, tricky logic). Be honest; it picks the coding model.
- description: what to build and where in the code, written for the coding agent. End it \
with: "Replace old code; don't leave the previous implementation behind as a fallback."
- tests: files under {test_dir}/ named "<task id>-<short-name>.test.js" (logic, node:test) \
or ".spec.js" (browser, Playwright), following the existing tests and helpers (read them \
with read_files first). Each test must FAIL on the current code and PASS once the task is \
done. Test observable behaviour, not implementation details.
- test_commands: 1-3 commands that run only this task's tests, e.g. \
"node --test {test_dir}/ml-9-x.test.js" or "npx playwright test {test_dir}/ml-9-x.spec.js". \
The project's setup and base checks are added automatically.
- manual_check: short numbered steps the owner follows on the preview link (a browser or \
phone) to see the result by hand.
- To look at code, list paths in "read_files" (at most 6 per turn, from the file list); you \
will get their contents and can answer again. Don't invent file contents.

Answer with ONE JSON object only:
{{"reply": "what you say to the owner (short, plain words)",
  "questions": ["..."],
  "read_files": ["path", ...],
  "draft": null or {{"epic": {{"id": "...", "title": "...", "description": "..."}},
            "tasks": [{{"id": "...", "title": "...", "size": "small|medium|hard",
                        "depends_on": [], "files": ["files it will change"],
                        "description": "...", "manual_check": "...",
                        "tests": [{{"path": "...", "content": "..."}}],
                        "test_commands": ["..."]}}]}}}}
"draft" is the COMPLETE current draft whenever you change it (it replaces the previous one), \
or null to keep the previous one unchanged."""


@dataclass
class ChatState:
    chat_id: str
    project_id: str
    created_at: str
    messages: list = field(default_factory=list)
    draft: Optional[dict] = None
    check: Optional[dict] = None
    cost_usd: float = 0.0
    status: str = "open"  # open | approved | discarded


class PlannerChat:
    """One planner chat. Every collaborator is injectable for tests."""

    def __init__(self, project_id, master, history, provider, *, store_dir: Path,
                 prices, model_label: str, checker: Callable, approver: Callable,
                 git: Optional[Callable] = None, max_cost_usd: float = 0.10,
                 chat_id: Optional[str] = None):
        self.project_id = project_id
        self._master = master
        self._history = history
        self._provider = provider
        self._prices = prices
        self._model_label = model_label  # "provider:model", as decisions record it
        self._checker = checker
        self._approver = approver
        if git is None:
            from core.host import git
        self._git = git
        self.max_cost_usd = max_cost_usd
        self._store_dir = Path(store_dir)
        self.state = self._load(chat_id) if chat_id else ChatState(
            chat_id=f"{project_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
            project_id=project_id,
            created_at=datetime.now(timezone.utc).isoformat())

    # --- persistence ---

    def _path(self, chat_id) -> Path:
        return self._store_dir / f"{chat_id}.json"

    def _load(self, chat_id) -> ChatState:
        return ChatState(**json.loads(self._path(chat_id).read_text()))

    def save(self) -> None:
        self._store_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path(self.state.chat_id).with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state.__dict__, indent=2))
        tmp.replace(self._path(self.state.chat_id))

    # --- project context ---

    def _project(self):
        return self._master.project_state(self.project_id).project()

    def _repo(self):
        project = self._project()
        return Path(project["repository"]).expanduser(), project["base_branch"]

    def _tasks(self):
        return self._master.status(self.project_id)["tasks"]

    def next_ids(self, count=4) -> list:
        tasks = self._tasks()
        numbers, prefix = [], None
        for task in tasks:
            match = re.match(r"^([a-z]+)-(\d+)$", str(task.get("id")))
            if match:
                prefix = prefix or match.group(1)
                numbers.append(int(match.group(2)))
        prefix = prefix or "t"
        start = max(numbers, default=0) + 1
        return [f"{prefix}-{n}" for n in range(start, start + count)]

    def read_file(self, path: str) -> str:
        repo, branch = self._repo()
        if not re.match(r"^[\w./@+-]+$", path) or ".." in path:
            return "(refused: invalid path)"
        try:
            content = self._git(["show", f"refs/heads/{branch}:{path}"], repo)
        except RuntimeError:
            return "(no such file)"
        return content[:MAX_FILE_CHARS] + ("\n...(truncated)" if len(content) > MAX_FILE_CHARS
                                           else "")

    def _context(self) -> str:
        repo, branch = self._repo()
        try:
            files = self._git(["ls-tree", "-r", "--name-only", f"refs/heads/{branch}"],
                              repo).splitlines()
        except RuntimeError:
            files = []
        files = [f for f in files if not f.startswith(("android/", "node_modules/"))][:400]
        readme = self.read_file("README.md")[:6000]
        tasks = "\n".join(f"- {t['id']} [{t.get('status')}] {t.get('title')}"
                          for t in self._tasks())
        return (f"PROJECT FILES ({branch}):\n" + "\n".join(files)
                + f"\n\nREADME.md:\n{readme}\n\nEXISTING TASKS:\n{tasks or '(none)'}")

    # --- a turn ---

    def _price(self, usage) -> float:
        from core.report import _price, add_usage

        model = self._model_label.split(":", 1)[-1]
        cost = _price(add_usage([usage]), model, self._prices) if usage else None
        return cost or 0.0

    def turn(self, text: str) -> dict:
        """The owner says something; returns {"reply", "questions", "draft_changed"}."""
        if self.state.status != "open":
            raise DraftProblem(f"this chat is {self.state.status}")
        if self.state.cost_usd >= self.max_cost_usd:
            raise DraftProblem(f"this chat reached its cap of ${self.max_cost_usd:.2f}; "
                               "start a new chat or raise [planner] chat_usd")
        self.state.messages.append({"role": "owner", "text": text})
        project = self._project()
        settings = planner_settings(project)
        system = SYSTEM.format(name=project.get("name") or self.project_id,
                               next_ids=", ".join(self.next_ids()),
                               test_dir=settings["test_dir"])
        extra_files: list = []
        answer = None
        for _round in range(3):
            prompt = self._prompt(system, extra_files)
            raw = self._provider.complete(prompt, schema=SCHEMA)
            usage = getattr(self._provider, "last_usage", None)
            cost = self._price(usage)
            self.state.cost_usd = round(self.state.cost_usd + cost, 6)
            self._history.append(
                type=EventType.PLANNER_TURN, run_id=uuid.uuid4().hex,
                project_id=self.project_id,
                payload={"chat_id": self.state.chat_id, "usage": usage or {},
                         "reasoner": self._model_label, "cost_usd": cost})
            answer = _parse(raw)
            wanted = [p for p in answer.get("read_files") or [] if isinstance(p, str)][:6]
            already = {name for name, _ in extra_files}
            wanted = [p for p in wanted if p not in already]
            if not wanted or answer.get("draft"):
                break
            extra_files += [(p, self.read_file(p)) for p in wanted]
        changed = False
        if isinstance(answer.get("draft"), dict):
            self.state.draft = answer["draft"]
            self.state.check = None
            changed = True
        reply = str(answer.get("reply") or "").strip()
        questions = [str(q) for q in answer.get("questions") or [] if str(q).strip()]
        self.state.messages.append({"role": "planner", "text": reply, "questions": questions})
        self.save()
        return {"reply": reply, "questions": questions, "draft_changed": changed}

    def _prompt(self, system, extra_files) -> str:
        parts = [system, self._context()]
        for name, content in extra_files:
            parts.append(f"FILE {name}:\n{content}")
        if self.state.draft:
            parts.append("CURRENT DRAFT:\n" + json.dumps(self.state.draft, indent=1))
        conversation = "\n".join(
            f"{m['role'].upper()}: {m['text']}"
            + (("\n  (asked: " + " | ".join(m["questions"]) + ")") if m.get("questions") else "")
            for m in self.state.messages[-20:])
        parts.append("CONVERSATION:\n" + conversation)
        return "\n\n".join(parts)

    # --- check / approve / discard ---

    def problems(self) -> list:
        status = self._master.status(self.project_id)
        return draft_problems(self.state.draft, {t["id"] for t in status["tasks"]},
                              {m["id"] for m in status["milestones"]},
                              planner_settings(self._project()))

    def check(self) -> dict:
        problems = self.problems()
        if problems:
            raise DraftProblem("the draft is incomplete:\n  - " + "\n  - ".join(problems))
        result = self._checker(self.project_id, self.state.draft)
        result["draft_hash"] = draft_hash(self.state.draft)
        self.state.check = result
        self.save()
        return result

    def approve(self) -> list:
        problems = self.problems()
        if problems:
            raise DraftProblem("the draft is incomplete:\n  - " + "\n  - ".join(problems))
        check = self.state.check
        if not check or not check.get("ok") or check.get("draft_hash") != draft_hash(self.state.draft):
            raise DraftProblem("run `check` first: the current draft has not passed its checks")
        settings = planner_settings(self._project())
        records = task_records(self.state.draft, settings)
        self._approver(self.project_id, self.state.draft, records,
                       chat_id=self.state.chat_id, draft_hash=check["draft_hash"])
        self.state.status = "approved"
        self.save()
        return [r["id"] for r in records]

    def discard(self) -> None:
        self.state.status = "discarded"
        self.save()


def _parse(raw) -> dict:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    try:
        value = json.loads(text[text.find("{"):text.rfind("}") + 1])
    except ValueError:
        return {"reply": text[:2000], "questions": [], "read_files": [], "draft": None}
    return value if isinstance(value, dict) else {"reply": str(value), "draft": None}
