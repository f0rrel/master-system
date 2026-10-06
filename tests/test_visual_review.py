"""The visual reviewer: screenshots of visual tasks, judged by a vision model."""

import json
from pathlib import Path

import pytest
import yaml

from conftest import make_repo, git, workspace_for
from core.history import EventType, InMemoryHistoryStore
from core.master import Master
from core.report import spend_since
from core.verification import VerificationResult
from core.visual_review import (VisualReviewVerifier, parse_review, review_prompt,
                                review_settings)

# A capture "script": writes one small file per requested screen.
CAPTURE = ("for s in $(echo {screens} | tr , ' '); do printf 'png-%s' $s > {out_dir}/$s.png; "
           "done")
SCREENS = [{"name": "home", "url": "index.html"}, {"name": "game", "url": "index.html"}]


class Inner:
    def __init__(self, verdict="pass"):
        self.verdict = verdict

    def verify(self, task, context, evidence=None, *, workspace):
        return VerificationResult(verdict=self.verdict, summary="all 1 command(s) passed",
                                  evidence={"result_sha": "abc"})


class Judge:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []
        self.last_usage = {"input_tokens": 3000, "output_tokens": 200}
        self.name = "deepseek:deepseek-flash"

    def judge(self, prompt, images):
        self.calls.append((prompt, images))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def ok(**extra):
    return json.dumps({"verdict": "ok", "readability": "ok", "change_visible": "yes",
                       "fits_style": "ok", "notes": "Looks good.", **extra})


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "projects"
    (root / "app").mkdir(parents=True)
    repo = make_repo(tmp_path / "repo", {"www/index.html": "<p>hi</p>\n"})
    (root / "app" / "project.yaml").write_text(yaml.safe_dump({
        "id": "app", "name": "App", "status": "active", "repository": str(repo),
        "base_branch": "main", "visual_review": {"screens": SCREENS, "capture": CAPTURE}}))
    (root / "app" / "milestones.yaml").write_text("milestones: []\n")
    (root / "app" / "tasks.yaml").write_text("tasks: []\n")
    workspace = workspace_for(repo, base_sha="base", result_sha="r" * 40)
    return {"master": Master(root), "workspace": workspace, "artifacts": tmp_path / "artifacts",
            "root": root}


def reviewer(setup, judge, inner=None):
    return VisualReviewVerifier(inner or Inner(), setup["master"], judge, setup["artifacts"],
                                prices={"deepseek-flash": {"input": 0.30, "output": 1.20}},
                                direction=lambda project: "Bright and readable.",
                                reviewer_label=judge.name)


TASK = {"id": "t-1", "type": "visual", "title": "Bigger tiles", "description": "Make tiles bigger.",
        "manual_check": "1. Look at the board."}
CONTEXT = {"project_id": "app", "task_id": "t-1"}


def test_an_ok_review_keeps_the_pass_and_stores_the_screenshots(setup):
    judge = Judge(ok())
    result = reviewer(setup, judge).verify(TASK, CONTEXT, {}, workspace=setup["workspace"])
    assert result.verdict == "pass"
    review = result.evidence["visual_review"]
    assert review["status"] == "reviewed" and review["verdict"] == "ok"
    shots = [Path(p) for p in review["screenshots"]]
    assert [p.name for p in shots] == ["home.png", "game.png"]
    assert shots[0].parent == setup["artifacts"] / "app" / "t-1" / ("r" * 12)
    assert shots[0].read_bytes() == b"png-home"
    [(prompt, images)] = judge.calls
    assert images == [b"png-home", b"png-game"]
    assert "Bigger tiles" in prompt and "Bright and readable." in prompt
    assert "390x844" in prompt and "home, game" in prompt
    assert review["cost_usd"] == pytest.approx(3000 * 0.30 / 1e6 + 200 * 1.20 / 1e6)


def test_a_block_turns_the_pass_into_a_fail(setup):
    judge = Judge(ok(verdict="block", readability="problem", notes="Tiles blur together."))
    result = reviewer(setup, judge).verify(TASK, CONTEXT, {}, workspace=setup["workspace"])
    assert result.verdict == "fail"
    assert "visual review blocked it: Tiles blur together." in result.summary
    assert result.findings[-1]["kind"] == "visual_review"
    assert result.evidence["visual_review"]["verdict"] == "block"


def test_the_reviewer_never_passes_a_failure_and_skips_other_types(setup):
    judge = Judge(ok())
    failed = reviewer(setup, judge, Inner("fail")).verify(TASK, CONTEXT, {},
                                                          workspace=setup["workspace"])
    assert failed.verdict == "fail" and "visual_review" not in failed.evidence
    logic = reviewer(setup, judge).verify({**TASK, "type": "logic"}, CONTEXT, {},
                                          workspace=setup["workspace"])
    assert "visual_review" not in logic.evidence
    rebased = reviewer(setup, judge).verify(TASK, CONTEXT, {"rebased_from": "x"},
                                            workspace=setup["workspace"])
    assert "visual_review" not in rebased.evidence
    assert judge.calls == []


@pytest.mark.parametrize("answer, note", [
    ("not json", "unusable"),
    (RuntimeError("HTTP 503"), "the reviewer failed: HTTP 503"),
])
def test_an_unavailable_reviewer_neither_blocks_nor_passes(setup, answer, note):
    result = reviewer(setup, Judge(answer)).verify(TASK, CONTEXT, {},
                                                   workspace=setup["workspace"])
    assert result.verdict == "pass"
    assert result.evidence["visual_review"]["status"] == "unavailable"
    assert note in result.evidence["visual_review"]["note"]


def test_a_failed_capture_is_recorded_not_blocking(setup):
    project = setup["root"] / "app" / "project.yaml"
    data = yaml.safe_load(project.read_text())
    data["visual_review"]["capture"] = "exit 3"
    project.write_text(yaml.safe_dump(data))
    judge = Judge(ok())
    result = reviewer(setup, judge).verify(TASK, CONTEXT, {}, workspace=setup["workspace"])
    assert result.verdict == "pass" and judge.calls == []
    assert "capture failed (exit 3)" in result.evidence["visual_review"]["note"]


def test_a_task_can_name_its_screens(setup):
    judge = Judge(ok())
    result = reviewer(setup, judge).verify({**TASK, "screens": ["game"]}, CONTEXT, {},
                                           workspace=setup["workspace"])
    assert [Path(p).name for p in result.evidence["visual_review"]["screenshots"]] == \
        ["game.png"]
    unknown = reviewer(setup, judge).verify({**TASK, "screens": ["nope"]}, CONTEXT, {},
                                            workspace=setup["workspace"])
    assert unknown.evidence["visual_review"]["status"] == "unavailable"


def test_settings_and_parsing():
    assert review_settings({}) is None
    settings = review_settings({"github": {"site_dir": "www"},
                                "visual_review": {"screens": SCREENS}})
    assert settings["site_dir"] == "www" and (settings["width"], settings["height"]) == (390, 844)
    assert parse_review("x") is None
    assert parse_review('{"verdict": "maybe"}') is None
    assert parse_review('Sure: {"verdict": "block", "notes": "a  b"}')["notes"] == "a b"


def test_review_spend_counts_toward_the_daily_cap():
    history = InMemoryHistoryStore()
    history.append(type=EventType.VERIFICATION, run_id="r", project_id="app", task_id="t-1",
                   attempt_id="a1",
                   payload={"verdict": "fail", "evidence": {"visual_review": {
                       "reasoner": "deepseek:deepseek-flash",
                       "usage": {"input_tokens": 10000, "output_tokens": 1000}}}})
    spend = spend_since(history, "2000-01-01", {"deepseek-flash": {"input": 0.30,
                                                                   "output": 1.20}})
    assert spend["reviewer_usd"] == pytest.approx(0.0042)
    assert spend["total_usd"] == pytest.approx(0.0042)
