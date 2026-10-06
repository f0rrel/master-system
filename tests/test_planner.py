"""core.planner, core.planner_checks and `ms chat`: drafts, must-fail checks, approval."""

import io
import json
import os
import shutil

import pytest
import yaml

from conftest import git, make_repo
from core.history import EventType, InMemoryHistoryStore
from core.master import Master
from core.ms import chat_loop
from core.paths import RuntimePaths
from core.planner import (DraftProblem, PlannerChat, draft_problems, planner_settings,
                          system_prompt)
from core.planner_checks import make_approver, make_checker
from core.project_state import ProjectState

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

FAILING = ("const test = require('node:test');\nconst assert = require('node:assert');\n"
           "const { greet } = require('../../greet.js');\n"
           "test('greets', () => assert.strictEqual(greet('Ada'), 'Hi Ada'));\n")
PASSING = FAILING.replace("'Hi Ada'", "'Hey Ada'")
BROKEN = "const test = require('node:test');\ntest('x', () => {\n"


def draft(content=FAILING, **task_extra):
    task = {"id": "t-2", "title": "Say hi", "size": "small", "depends_on": [],
            "description": "greet() must say Hi.", "manual_check": "1. Open the page.",
            "tests": [{"path": "tests/tasks/t-2-greet.test.js", "content": content}],
            "test_commands": ["node --test tests/tasks/t-2-greet.test.js"]}
    task.update(task_extra)
    return {"epic": {"id": "epic-hi", "title": "Friendlier greetings", "description": "d"},
            "tasks": [task]}


class Scripted:
    def __init__(self, replies):
        self.replies = [json.dumps(r) if not isinstance(r, str) else r for r in replies]
        self.prompts = []

    def complete(self, prompt, schema=None):
        self.prompts.append(prompt)
        self.last_usage = {"input_tokens": 10000, "output_tokens": 1000}
        return self.replies.pop(0)


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo", {
        "greet.js": "exports.greet = (name) => 'Hey ' + name;\n",
        "README.md": "# Toy\nA toy project.\n", "tests/tasks/README.md": "pending tests\n"})
    git(repo, "branch", "develop")
    root = tmp_path / "projects"
    project = root / "toy"
    project.mkdir(parents=True)
    (project / "project.yaml").write_text(yaml.safe_dump({
        "id": "toy", "name": "Toy", "status": "active", "repository": str(repo),
        "base_branch": "develop",
        "planner": {"test_dir": "tests/tasks", "test_suffixes": [".test.js"],
                    "syntax_check": "node --check {path}",
                    "test_command_examples": ["node --test {path}"],
                    "setup": [], "base_checks": ["true"],
                                  "protected_paths": ["tests/*", "docs/DIRECTION.md"]}}))
    (project / "milestones.yaml").write_text(yaml.safe_dump(
        {"milestones": [{"id": "m1", "name": "M1", "status": "in_progress"}]}))
    (project / "tasks.yaml").write_text(yaml.safe_dump({"tasks": [
        {"id": "t-1", "milestone": "m1", "title": "First", "status": "completed"}]}))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    paths = RuntimePaths(state_dir=tmp_path / "state", worktrees_root=tmp_path / "worktrees")
    master = Master(root)
    history = InMemoryHistoryStore()
    worker_env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    checker = make_checker(master, paths, worker_env, timeout_s=120)
    approver = make_approver(master, history, paths, checker)

    def chat(replies, **kwargs):
        return PlannerChat("toy", master, history, Scripted(replies),
                           store_dir=tmp_path / "state" / "planner",
                           prices={"deepseek-v4-flash": {"input": 0.44, "output": 1.32}},
                           model_label="deepseek:deepseek-v4-flash", checker=checker,
                           approver=approver, **kwargs)

    return {"repo": repo, "project": project, "history": history, "chat": chat,
            "master": master}


def answer(draft_value=None, reply="ok", questions=(), read_files=()):
    return {"reply": reply, "questions": list(questions), "read_files": list(read_files),
            "draft": draft_value}


def test_a_turn_can_read_files_then_drafts_and_is_costed(env):
    chat = env["chat"]([answer(read_files=["greet.js"]),
                        answer(draft(), reply="Here is a plan.",
                               questions=["Should it also say bye?"])])
    result = chat.turn("I want greet to say Hi")

    assert result == {"reply": "Here is a plan.", "questions": ["Should it also say bye?"],
                      "draft_changed": True}
    second_prompt = chat._provider.prompts[1]
    assert "FILE greet.js:\nexports.greet" in second_prompt
    assert "t-2" in chat._provider.prompts[0]  # the next free id is offered
    turns = env["history"].events(types=[EventType.PLANNER_TURN])
    assert len(turns) == 2 and turns[0].payload["reasoner"] == "deepseek:deepseek-v4-flash"
    assert chat.state.cost_usd == pytest.approx(2 * (0.0044 + 0.00132))
    assert ProjectState(env["project"]).get_task("t-2") is None  # nothing queued


def test_the_chat_cap_stops_turns(env):
    chat = env["chat"]([answer()], max_cost_usd=0.001)
    chat.turn("hello")
    with pytest.raises(DraftProblem, match="cap"):
        chat.turn("again")


def test_draft_problems_catch_bad_drafts(env):
    settings = planner_settings({"planner": {"test_dir": "tests/tasks",
                                             "test_suffixes": [".test.js", ".spec.js"]}})
    assert draft_problems(draft(), {"t-1"}, {"m1"}, settings) == []
    bad = draft(size="huge", test_commands=["npm test"],
                tests=[{"path": "src/x.js", "content": "x"}])
    problems = draft_problems(bad, {"t-1"}, {"m1"}, settings)
    assert any("size" in p for p in problems)
    assert any("must be a .test.js or .spec.js file under tests/tasks/" in p for p in problems)
    assert any("must run one of its test files" in p for p in problems)
    assert any("already used" in p for p in draft_problems(draft(id="t-1"), {"t-1"}, {"m1"},
                                                            settings))


def test_a_good_test_fails_on_the_current_code_and_passes_the_check(env):
    chat = env["chat"]([answer(draft())])
    chat.turn("go")
    result = chat.check()
    assert result["ok"], result["items"]


def test_a_test_that_already_passes_is_refused(env):
    chat = env["chat"]([answer(draft(PASSING))])
    chat.turn("go")
    result = chat.check()
    assert not result["ok"]
    assert any("passes already" in i["note"] for i in result["items"])


def test_a_broken_test_is_refused(env):
    chat = env["chat"]([answer(draft(BROKEN))])
    chat.turn("go")
    result = chat.check()
    assert not result["ok"]
    assert any(not i["ok"] and "syntax check" in i["what"] for i in result["items"])


def test_approve_needs_a_passing_check_of_the_current_draft(env):
    chat = env["chat"]([answer(draft()), answer(draft(title="Say hello"))])
    chat.turn("go")
    with pytest.raises(DraftProblem, match="check"):
        chat.approve()
    chat.check()
    chat.turn("rename it")  # the draft changed: its check no longer counts
    with pytest.raises(DraftProblem, match="check"):
        chat.approve()


def test_approve_commits_the_tests_and_queues_the_tasks(env):
    chat = env["chat"]([answer(draft())])
    chat.turn("go")
    chat.check()
    assert chat.approve() == ["t-2"]

    repo = env["repo"]
    assert "Hi Ada" in git(repo, "show", "develop:tests/tasks/t-2-greet.test.js")
    assert "approved by the owner" in git(repo, "log", "-1", "--format=%B", "develop").lower()
    assert git(repo, "rev-parse", "main") != git(repo, "rev-parse", "develop")
    task = ProjectState(env["project"]).get_task("t-2")
    assert task["status"] == "planned" and task["size"] == "small"
    assert task["milestone"] == "epic-hi"
    assert task["acceptance"] == {"commands": ["node --test tests/tasks/t-2-greet.test.js",
                                               "true"],
                                  "protected_paths": ["tests/*", "docs/DIRECTION.md"]}
    assert task["manual_check"] == "1. Open the page."
    [action] = env["history"].events(types=[EventType.HUMAN_ACTION])
    assert action.payload["actor"] == "owner via planner"
    assert action.payload["action"] == "planner_approved"
    assert chat.state.status == "approved"


def test_the_chat_loop_drives_a_whole_conversation(env):
    chat = env["chat"]([answer(draft(), reply="Plan ready.", questions=["Bye too?"]),
                        answer(None, reply="Fine, no bye.")])
    lines = iter(["I want Hi", "no bye", "show", "check", "approve"])
    out = io.StringIO()
    assert chat_loop(chat, read=lambda prompt: next(lines), out=out) == 0
    text = out.getvalue()
    assert "\n  Plan ready.\n" in text and "   1.  Bye too?" in text
    assert "draft updated: 1 task(s)" in text
    assert "EPIC epic-hi: Friendlier greetings" in text
    assert "── t-2: Say hi ──" in text and "  Size:" in text and "How to check by hand:" in text
    assert "All checks passed" in text and "Queued t-2" in text
    assert "[this chat: $" in text


def test_discard_queues_nothing(env):
    chat = env["chat"]([answer(draft())])
    lines = iter(["go", "discard"])
    out = io.StringIO()
    chat_loop(chat, read=lambda prompt: next(lines), out=out)
    assert "Nothing was queued" in out.getvalue()
    assert ProjectState(env["project"]).get_task("t-2") is None


def test_a_saved_chat_continues(env, tmp_path):
    chat = env["chat"]([answer(draft())])
    chat.turn("go")
    again = env["chat"]([], chat_id=chat.state.chat_id)
    assert again.state.draft == draft() and again.state.messages[0]["text"] == "go"


def test_a_triple_quote_block_is_one_message():
    from core.ms import read_message

    lines = iter(['"""', "line one", "", "line two", '"""'])
    assert read_message(lambda p: next(lines), pending=lambda: False) == "line one\n\nline two"


def test_a_paste_arriving_at_once_is_one_message():
    from core.ms import read_message

    lines = iter(["first", "second", "third"])
    waiting = iter([True, True, False])
    assert read_message(lambda p: next(lines), pending=lambda: next(waiting)) == \
        "first\nsecond\nthird"


def test_a_file_is_sent_as_the_first_message(env):
    chat = env["chat"]([answer(None, reply="Got the whole request.")])
    out = io.StringIO()
    lines = iter(["quit"])
    chat_loop(chat, read=lambda prompt: next(lines), out=out,
              first_message="many\nlines\nof request", pending=lambda: False)
    assert chat.state.messages[0]["text"] == "many\nlines\nof request"
    assert len(chat._provider.prompts) == 1


def test_replies_wrap_to_the_width_and_are_indented(env):
    from core.ms import _print_answer

    chat = env["chat"]([])
    out = io.StringIO()
    _print_answer({"reply": "word " * 60, "questions": ["q " * 70], "draft_changed": False},
                  chat, out)
    for line in out.getvalue().splitlines():
        assert len(line) <= 100
    reply_lines = [l for l in out.getvalue().splitlines() if l.startswith("  word")]
    assert len(reply_lines) >= 3
    assert "\x1b[2m" not in out.getvalue()  # not a terminal: no colour codes


def test_without_test_suffixes_any_file_under_the_test_dir_is_accepted():
    settings = planner_settings({"planner": {"test_dir": "tests/tasks"}})
    python = draft("def test_x():\n    assert False\n",
                   tests=[{"path": "tests/tasks/t-2-greet.py", "content": "x"}],
                   test_commands=["python -m pytest -q tests/tasks/t-2-greet.py"])
    assert draft_problems(python, {"t-1"}, {"m1"}, settings) == []
    outside = draft(tests=[{"path": "src/t-2.py", "content": "x"}],
                    test_commands=["python -m pytest src/t-2.py"])
    assert any("must be a file under tests/tasks/" in p
               for p in draft_problems(outside, {"t-1"}, {"m1"}, settings))


def test_the_prompt_follows_the_projects_test_conventions():
    python = planner_settings({"planner": {
        "test_dir": "tests/tasks", "test_suffixes": [".py"],
        "test_command_examples": ["python -m pytest -q {path}"],
        "test_guidance": "Use pytest fixtures from tests/conftest.py."}})
    prompt = system_prompt("Example App", ["app-3", "app-4"], python)
    assert '"python -m pytest -q tests/tasks/app-3-x.py"' in prompt
    assert '<suffix> is ".py"' in prompt
    assert "Use pytest fixtures from tests/conftest.py." in prompt
    for js_only in ("node", "Playwright", ".js", "phone"):
        assert js_only not in prompt
    web = planner_settings({"planner": {
        "test_suffixes": [".test.js", ".spec.js"],
        "test_command_examples": ["node --test {path}", "npx playwright test {path}"]}})
    prompt = system_prompt("Example App", ["app-3"], web)
    assert ('"node --test tests/tasks/app-3-x.test.js" or '
            '"npx playwright test tests/tasks/app-3-x.spec.js"') in prompt
    generic = system_prompt("Example App", ["app-3"], planner_settings({}))
    assert "the project's existing test runner" in generic
    assert "{" not in generic.split("Answer with ONE JSON")[0]


def test_without_a_syntax_check_the_check_runs_only_the_commands(env):
    project_yaml = env["project"] / "project.yaml"
    data = yaml.safe_load(project_yaml.read_text())
    del data["planner"]["syntax_check"]
    project_yaml.write_text(yaml.safe_dump(data))
    chat = env["chat"]([answer(draft())])
    chat.turn("go")
    result = chat.check()
    assert result["ok"]
    assert not any("syntax" in i["what"] for i in result["items"])


# --- P1: a reply that promises to read files must not end the turn ---


def test_a_promise_to_read_named_files_reads_them_in_the_same_turn(env):
    chat = env["chat"]([answer(reply="I'll read greet.js first, then draft the tasks."),
                        answer(draft(), reply="Here is a plan.")])
    result = chat.turn("I want greet to say Hi")

    assert result["reply"] == "Here is a plan." and result["draft_changed"]
    assert len(chat._provider.prompts) == 2
    assert "FILE greet.js:\nexports.greet" in chat._provider.prompts[1]


def test_a_promise_without_files_is_re_asked_once_in_the_same_turn(env):
    chat = env["chat"]([answer(reply="Let me look at the code first."),
                        answer(read_files=["greet.js"]),
                        answer(draft(), reply="Here is a plan.")])
    result = chat.turn("I want greet to say Hi")

    assert result["reply"] == "Here is a plan." and result["draft_changed"]
    assert "promised" in chat._provider.prompts[1]
    assert "FILE greet.js:" in chat._provider.prompts[2]
    owner_visible = [m["text"] for m in chat.state.messages if m["role"] == "planner"]
    assert owner_visible == ["Here is a plan."]


def test_a_repeated_empty_promise_is_re_asked_only_once(env):
    chat = env["chat"]([answer(reply="Let me check the files."),
                        answer(reply="I will check the files now.")])
    result = chat.turn("hello")

    assert len(chat._provider.prompts) == 2
    assert result["reply"] == "I will check the files now."


def test_an_ordinary_reply_costs_one_call(env):
    chat = env["chat"]([answer(reply="Noted: greetings stay short.")])
    assert chat.turn("Keep greetings short")["reply"] == "Noted: greetings stay short."
    assert len(chat._provider.prompts) == 1


def test_the_last_read_round_asks_for_an_answer_without_more_reads(env):
    chat = env["chat"]([answer(read_files=["greet.js"]), answer(read_files=["README.md"]),
                        answer(read_files=["tests/tasks/README.md"]),
                        answer(questions=["Hi or Hello?"], reply="One question.")])
    result = chat.turn("I want greet to say Hi")

    assert result["questions"] == ["Hi or Hello?"]
    assert len(chat._provider.prompts) == 4
    assert "no more files" in chat._provider.prompts[3]


# --- backlog drafts ---


def test_the_planner_can_add_backlog_epics_without_a_check(env):
    backlog = {"backlog": [{"id": "epic-bye", "title": "Goodbyes", "summary": "Say bye.",
                            "priority": 2}], "epic": None, "tasks": []}
    chat = env["chat"]([answer(backlog, reply="Added to the backlog.")])
    chat.turn("Put goodbyes on the backlog")
    assert "BACKLOG EPICS TO ADD" in chat_render(chat)
    assert chat.approve() == ["epic-bye"]
    epic = next(m for m in env["master"].status("toy")["milestones"] if m["id"] == "epic-bye")
    assert epic == {"id": "epic-bye", "name": "Goodbyes", "status": "proposed",
                    "priority": 2, "summary": "Say bye."}
    [action] = env["history"].events(types=[EventType.HUMAN_ACTION])
    assert action.payload["action"] == "backlog_add"


def test_planning_a_backlog_epic_fills_it_with_tasks(env):
    env["master"].add_backlog_epic("toy", "epic-hi", "Friendlier greetings", priority=1)
    chat = env["chat"]([answer(draft())])
    chat.turn("plan epic 1 from the backlog")
    assert "1. epic-hi [proposed] Friendlier greetings" in chat._provider.prompts[0]
    assert chat.problems() == []
    chat.check()
    assert chat.approve() == ["t-2"]
    status = env["master"].status("toy")
    epic = next(m for m in status["milestones"] if m["id"] == "epic-hi")
    assert epic["status"] == "planned" and epic["priority"] == 1
    assert [t["id"] for t in status["tasks"] if t["milestone"] == "epic-hi"] == ["t-2"]


def chat_render(chat):
    from core.planner import render_draft

    return render_draft(chat.state.draft)
