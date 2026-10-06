"""Oversized tasks: sizing, splits, recovery after a cut-off, and reopen's fresh start."""

import io
import json

import pytest
import yaml

from core.evidence import HistoryEvidence
from core.history import EventType, InMemoryHistoryStore
from core.master import Master
from core.opencode_backend import build_prompt
from core.splits import SplitStep, SplitStore, cut_offs_since_human, recovery_note
from core.task_size import size_findings
from core.telegram import TelegramState
from core.telegram_bot import split_buttons
from core.work_manager import InvalidFieldError

# --- size ---


def task(**extra):
    return {"id": "t-1", "title": "T", "description": "Add one helper.", "estimate_lines": 60,
            "files": ["src/a.js"], **extra}


def test_a_small_task_passes_and_big_ones_are_flagged_with_a_split():
    assert size_findings(task()) == []
    assert "no estimate_lines" in size_findings(task(estimate_lines=None))[0]
    assert "split it into 3 tasks" in size_findings(task(estimate_lines=400))[0]
    six = task(description="Draw six SVG monster faces, one per tile type.")
    assert "creates about 6 things at once" in size_findings(six)[0]
    assert size_findings(task(description="Keep the six --tile-1..6 variables; the ml-14 "
                                          "test checks this; 20 moves; 400 ms.")) == []
    assert "changes 5 files" in size_findings(task(files=list("abcde")))[0]
    assert size_findings(task(estimate_lines=180), max_lines=200) == []


# --- splitting a task ---


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "projects"
    (root / "app").mkdir(parents=True)
    (root / "app" / "project.yaml").write_text(yaml.safe_dump(
        {"id": "app", "name": "App", "status": "active"}))
    (root / "app" / "milestones.yaml").write_text(
        "milestones:\n  - {id: epic, name: Epic, status: planned}\n")
    (root / "app" / "tasks.yaml").write_text(yaml.safe_dump({"tasks": [
        {"id": "a-1", "milestone": "epic", "title": "Big", "status": "blocked",
         "acceptance": {"commands": ["true"]}},
        {"id": "a-2", "milestone": "epic", "title": "Uses big", "status": "planned",
         "depends_on": ["a-1"], "acceptance": {"commands": ["true"]}}]}))
    return root


def record(task_id, deps=()):
    return {"id": task_id, "milestone": "epic", "title": task_id, "status": "planned",
            "acceptance": {"commands": ["true"]}, **({"depends_on": list(deps)} if deps else {})}


def test_split_task_replaces_in_place_and_rewires_dependents(root):
    master = Master(root)
    master.split_task("app", "a-1", [record("a-3"), record("a-4", ["a-3"]),
                                     record("a-5", ["a-4"])])
    tasks = master.status("app")["tasks"]
    assert [t["id"] for t in tasks] == ["a-1", "a-3", "a-4", "a-5", "a-2"]
    assert tasks[0]["status"] == "cancelled" and tasks[0]["replaced_by"] == ["a-3", "a-4",
                                                                             "a-5"]
    assert tasks[-1]["depends_on"] == ["a-3", "a-4", "a-5"]
    with pytest.raises(InvalidFieldError, match="only open or blocked"):
        master.split_task("app", "a-1", [record("a-6")])
    with pytest.raises(InvalidFieldError, match="stay in the task's epic"):
        master.split_task("app", "a-2", [{**record("a-7"), "milestone": "other"}])


def test_set_task_size_is_validated(root):
    master = Master(root)
    assert master.set_task_size("app", "a-2", "hard")["size"] == "hard"
    with pytest.raises(InvalidFieldError):
        master.set_task_size("app", "a-2", "huge")


# --- cut-offs: retry incrementally, then split ---


def cut_off(history, task_id="a-2", n=1):
    for i in range(n):
        attempt = f"{len(history.events()) + i:032x}"
        history.append(type=EventType.ATTEMPT_STARTED, run_id="r", project_id="app",
                       task_id=task_id, event_id=attempt, payload={})
        history.append(type=EventType.ATTEMPT_FINISHED, run_id="r", project_id="app",
                       task_id=task_id, attempt_id=attempt,
                       payload={"outcome": "stalled", "stall": {"finish_reason": "length"}})


def test_after_a_cut_off_the_next_attempt_writes_incrementally():
    history = InMemoryHistoryStore()
    assert recovery_note(history, "app", "a-2") is None
    cut_off(history)
    note = recovery_note(history, "app", "a-2")
    assert "write incrementally" in note
    prompt = build_prompt({"id": "a-2", "title": "T"}, {"type": "logic", "recovery": note})
    assert prompt.index("write incrementally") < prompt.index("Work in small steps")
    history.append(type=EventType.HUMAN_ACTION, run_id="r", project_id="app", task_id="a-2",
                   payload={"action": "reopen"})
    assert cut_offs_since_human(history, "app", "a-2") == 0


class FakeChat:
    def __init__(self, draft, ok=True):
        from types import SimpleNamespace

        self.state = SimpleNamespace(chat_id="app-chat-1", draft=None)
        self._draft, self._ok = draft, ok
        self.messages = []

    def turn(self, text):
        self.messages.append(text)
        self.state.draft = self._draft
        return {"reply": "Here is a split.", "questions": [], "draft_changed": True}

    def check(self):
        return {"ok": self._ok, "items": [], "size": {}, "size_ok": True}


class Notes:
    def __init__(self):
        self.sent = []

    def send(self, title, message, **kwargs):
        self.sent.append({"title": title, "message": message, **kwargs})
        return True


SPLIT = {"replaces": "a-2", "epic": {"id": "epic", "title": "Epic"}, "tasks": [
    {"id": "a-3", "title": "Skeleton and sword", "type": "logic", "estimate_lines": 90},
    {"id": "a-4", "title": "Urn and crown", "type": "logic", "estimate_lines": 70}]}


def test_two_cut_offs_draft_a_split_for_the_owner(root, tmp_path):
    master = Master(root)
    history = InMemoryHistoryStore()
    chats, notes = [], Notes()

    def planner(project_id, chat_id):
        chats.append(FakeChat(SPLIT))
        return chats[-1]

    step = SplitStep(master, history, tmp_path / "state", planner, notes, escalate=True)
    cut_off(history)
    assert step("app") == []  # one cut-off: retry incrementally first
    cut_off(history)
    [line] = step("app")
    assert "split drafted" in line
    assert "Split task a-2" in chats[0].messages[0] and '"replaces": "a-2"' in \
        chats[0].messages[0]
    [note] = notes.sent
    assert note["title"] == "app: needs you"
    assert "a-3 Skeleton and sword (logic, ~90 lines)" in note["message"]
    assert [label for label, _ in note["actions"]] == ["Approve", "Reject", "Escalate"]
    store = SplitStore(tmp_path / "state")
    assert store.waiting("app") == {"a-2": "waiting for your decision on a split"}
    assert step("app") == []  # not drafted twice


def test_without_a_draft_the_owner_is_pointed_to_the_chat(root, tmp_path):
    notes = Notes()

    class Asks(FakeChat):
        def turn(self, text):
            return {"reply": "Which part first?", "questions": ["Sword first?"],
                    "draft_changed": False}

    step = SplitStep(Master(root), InMemoryHistoryStore(), tmp_path, lambda p, c: Asks(None),
                     notes)
    step.draft("app", "a-2", "skeleton first")
    assert notes.sent[0]["actions"] is None and "Continue with ms chat app" in \
        notes.sent[0]["message"]


def test_split_buttons():
    assert [b[0] for b in split_buttons("app", "a-2")] == ["Approve", "Reject"]
    assert split_buttons("app", "a-2", escalate=True)[2][1]["decision"] == "escalate"


# --- reopen: a genuinely fresh start ---


def test_the_master_sees_only_attempts_after_the_last_human_change():
    history = InMemoryHistoryStore()
    for n in range(3):
        attempt = f"{n:032x}"
        history.append(type=EventType.ATTEMPT_STARTED, run_id="r", project_id="app",
                       task_id="a-1", event_id=attempt, payload={})
        history.append(type=EventType.ATTEMPT_FINISHED, run_id="r", project_id="app",
                       task_id="a-1", attempt_id=attempt, payload={"outcome": "finished"})
        history.append(type=EventType.VERIFICATION, run_id="r", project_id="app",
                       task_id="a-1", attempt_id=attempt, payload={"verdict": "fail"})
    evidence = HistoryEvidence(history).for_project("app", ["a-1"])
    assert evidence["execution_evidence"]["tasks"]["a-1"]["attempts_total"] == 3
    history.append(type=EventType.HUMAN_ACTION, run_id="r", project_id="app", task_id="a-1",
                   payload={"action": "reopen"})
    fresh = HistoryEvidence(history).for_project("app", ["a-1"])["execution_evidence"][
        "tasks"]["a-1"]
    assert fresh["attempts_total"] == 0 and fresh["latest_attempt"] is None
    assert fresh["attempts_before_last_human_change"] == 3


# --- approve anyway ---




def test_the_planner_refuses_oversized_drafts_until_approve_anyway(request):
    env = request.getfixturevalue("env")
    from test_planner import answer, draft

    big = draft(estimate_lines=600)
    chat = env["chat"]([answer(big)])
    chat.turn("go")
    result = chat.check()
    assert result["ok"] and result["size"] == {"t-2": [result["size"]["t-2"][0]]}
    from core.planner import DraftProblem, render_draft

    assert "[!!] too big: about 600 lines" in render_draft(chat.state.draft, result)
    with pytest.raises(DraftProblem, match="approve anyway"):
        chat.approve()
    assert chat.approve(size_override=True) == ["t-2"]
    [action] = env["history"].events(types=[EventType.HUMAN_ACTION])
    assert action.payload["size_override"] is True
from test_planner import env  # noqa: E402,F401  (the planner fixture)


def test_a_split_draft_is_approved_in_place(request):
    env = request.getfixturevalue("env")
    from test_planner import FAILING, answer, draft

    master = env["master"]
    master.add_planned_work("toy", {"id": "epic-x", "name": "X"}, [
        {"id": "t-5", "milestone": "epic-x", "title": "Too big", "status": "planned",
         "acceptance": {"commands": ["true"]}},
        {"id": "t-6", "milestone": "epic-x", "title": "After", "status": "planned",
         "depends_on": ["t-5"], "acceptance": {"commands": ["true"]}}])
    split = draft(FAILING)
    split["replaces"] = "t-5"
    split["epic"] = {"id": "epic-x", "title": "X"}
    chat = env["chat"]([answer(split)])
    chat.turn("split t-5")
    assert chat.problems() == []
    chat.check()
    assert chat.approve() == ["t-2"]
    tasks = {t["id"]: t for t in master.status("toy")["tasks"]}
    assert tasks["t-5"]["status"] == "cancelled" and tasks["t-5"]["replaced_by"] == ["t-2"]
    assert tasks["t-6"]["depends_on"] == ["t-2"]
    actions = [e.payload["action"] for e in env["history"].events(
        types=[EventType.HUMAN_ACTION])]
    assert "split" in actions and "planner_approved" in actions


def test_a_split_of_another_epic_or_a_done_task_is_refused(request):
    env = request.getfixturevalue("env")
    from test_planner import answer, draft

    split = draft()
    split["replaces"] = "t-1"  # completed
    chat = env["chat"]([answer(split)])
    chat.turn("split")
    assert any("not an open task" in p for p in chat.problems())


def test_ms_split_reject_and_escalate(tmp_path):
    from core.ms import main as ms_main
    from core.paths import RuntimePaths, default_projects_root

    tasks_file = default_projects_root() / "sample-project" / "tasks.yaml"
    tasks = yaml.safe_load(tasks_file.read_text())
    task_id = tasks["tasks"][2]["id"]
    store = SplitStore(RuntimePaths.default().state_dir)
    out = io.StringIO()
    assert ms_main(["split", "sample-project", task_id, "reject"], out=out) == 1
    assert "no split" in out.getvalue()
    store.save("sample-project", task_id, {"chat_id": "c1"})
    out = io.StringIO()
    assert ms_main(["split", "sample-project", task_id, "escalate"], out=out) == 1
    assert "needs worker tiers" in out.getvalue()
    out = io.StringIO()
    assert ms_main(["split", "sample-project", task_id, "reject"], out=out) == 0
    assert "blocked" in out.getvalue() and store.pending("sample-project") == {}
    assert yaml.safe_load(tasks_file.read_text())["tasks"][2]["status"] == "blocked"


def test_telegram_asks_to_confirm_a_split_approval(tmp_path):
    from test_telegram import OWNER, FakeAPI, FakeOps, paired, press

    from core.telegram_bot import TelegramBot

    state = TelegramState(tmp_path)
    api = FakeAPI()
    ops = FakeOps(state)
    ops.split = lambda action: (ops.calls.append(["split", action["decision"]])
                                or [{"text": "done"}])
    bot = TelegramBot(api, state, ops, log=lambda line: None)
    bot._actions["split"] = ops.split
    b, api, state, ops = paired((bot, api, state, ops))
    buttons = b._buttons(split_buttons("app", "a-2"))[0]
    b.handle(press(OWNER, buttons[0]["callback_data"]))
    assert "Replace a-2 with the drafted split?" in api.out[-1]["text"]
    b.handle(press(OWNER, api.out[-1]["buttons"][0][0]["callback_data"]))
    assert ops.calls[-1] == ["split", "approve"]
    b.handle(press(OWNER, b._buttons(split_buttons("app", "a-2"))[0][1]["callback_data"]))
    assert ops.calls[-1] == ["split", "reject"]  # reject needs no confirmation


def test_a_draft_with_problems_goes_back_to_the_planner_once(root, tmp_path):
    from core.planner import DraftProblem

    class Fixable(FakeChat):
        checks = 0

        def check(self):
            Fixable.checks += 1
            if Fixable.checks == 1:
                raise DraftProblem("the draft is incomplete:\n  - task a-3: bad file")
            return super().check()

    notes, chats = Notes(), []
    step = SplitStep(Master(root), InMemoryHistoryStore(), tmp_path,
                     lambda p, c: chats.append(Fixable(SPLIT)) or chats[-1], notes)
    step.draft("app", "a-2")
    assert "task a-3: bad file" in chats[-1].messages[-1]
    assert notes.sent[-1]["actions"] is not None
    assert SplitStore(tmp_path).get("app", "a-2")["check_ok"] is True
