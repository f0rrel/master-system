"""Retries learn from earlier failures and stop when they would only repeat.

core.retry_feedback is deterministic: no test here calls a model.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, ".")

from core.daemon import exhaustion_note, work_for
from core.evidence import HistoryEvidence, spec_hash
from core.history import EventType, InMemoryHistoryStore
from core.opencode_backend import build_prompt
from core.retry_feedback import (AttemptFailure, identical_streak, repeated_failure,
                                 retry_limits, retry_note, task_failures)
from core.run_config import ConfigError, load_config

PROTECTED = {"kind": "protected_path", "path": "tests/test_levels.py"}
TASK = {"id": "t1", "title": "Levels", "acceptance": {"commands": ["npm test"]}}
SECRET = "sk-" + "a1b2c3d4e5f6a7b8c9d0e1f2"


def command_failed(excerpt="1 failed in 0.31s", command="npm test", exit_code=1):
    return {"kind": "command_failed", "command": command, "exit_code": exit_code,
            "timed_out": False, "output_excerpt": excerpt}


def attempt(history, n, *, outcome="finished", verdict="fail", findings=(PROTECTED,),
            summary="1 protected path(s) changed", profile=None, task=TASK, task_id="t1",
            reason=None, error_type=None, verification=True):
    attempt_id = f"{n:032x}"
    ids = dict(run_id="r", project_id="p", task_id=task_id, attempt_id=attempt_id)
    started = {"spec_hash": spec_hash(task)}
    if profile:
        started["worker_profile"] = profile
    history.append(type=EventType.ATTEMPT_STARTED, event_id=attempt_id, run_id="r",
                   project_id="p", task_id=task_id, payload=started)
    finished = {"outcome": outcome, "worker_reported_status": "success"}
    if reason:
        finished["reason"] = reason
    if error_type:
        finished.update(error_type=error_type, message="boom")
    history.append(type=EventType.ATTEMPT_FINISHED, **ids, payload=finished)
    if verification and verdict:
        history.append(type=EventType.VERIFICATION, **ids,
                       payload={"verdict": verdict, "summary": summary,
                                "findings": list(findings)})


def human_action(history, task_id="t1"):
    history.append(type=EventType.HUMAN_ACTION, run_id="r", project_id="p", task_id=task_id,
                   payload={"action": "edit"})


# --- fingerprints ---------------------------------------------------------------------

def test_the_same_failure_with_different_timings_has_one_fingerprint():
    history = InMemoryHistoryStore()
    attempt(history, 1, findings=[command_failed("FAILED test_x - 3 failed in 0.31s\nabc12345def")])
    attempt(history, 2, findings=[command_failed("FAILED test_x - 3 failed in 9.87s\n99f00ba1c2d")])
    first, second = task_failures(history, "p", "t1")
    assert first.fingerprint == second.fingerprint


def test_different_failing_output_has_a_different_fingerprint():
    history = InMemoryHistoryStore()
    attempt(history, 1, findings=[command_failed("FAILED test_levels - boom")])
    attempt(history, 2, findings=[command_failed("FAILED test_stars - something else")])
    first, second = task_failures(history, "p", "t1")
    assert first.fingerprint != second.fingerprint


def test_finding_order_does_not_change_the_fingerprint():
    other = {"kind": "outside_allowed_paths", "path": "docs/x.md"}
    history = InMemoryHistoryStore()
    attempt(history, 1, findings=[PROTECTED, other])
    attempt(history, 2, findings=[other, PROTECTED])
    first, second = task_failures(history, "p", "t1")
    assert first.fingerprint == second.fingerprint


# --- repeated identical failures ------------------------------------------------------

def test_identical_failures_in_a_row_stop_retries():
    history = InMemoryHistoryStore()
    attempt(history, 1)
    assert repeated_failure(history, "p", "t1", 2) is None  # one failure is not a repeat
    attempt(history, 2)
    stopped = repeated_failure(history, "p", "t1", 2)
    assert stopped is not None and stopped.kind == "verification_failed"
    assert identical_streak(task_failures(history, "p", "t1")) == 2


def test_the_limit_is_configurable_and_zero_or_one_disables_it():
    history = InMemoryHistoryStore()
    for n in (1, 2):
        attempt(history, n)
    assert repeated_failure(history, "p", "t1", 3) is None
    assert repeated_failure(history, "p", "t1", 1) is None
    assert repeated_failure(history, "p", "t1", 0) is None
    attempt(history, 3)
    assert repeated_failure(history, "p", "t1", 3) is not None


def test_different_failures_across_attempts_are_not_a_repeat():
    history = InMemoryHistoryStore()
    attempt(history, 1)
    attempt(history, 2, findings=[command_failed()], summary="1 of 1 command(s) failed")
    attempt(history, 3, outcome="timed_out", verdict=None)
    failures = task_failures(history, "p", "t1")
    assert [f.kind for f in failures] == ["verification_failed", "verification_failed",
                                          "timed_out"]
    assert identical_streak(failures) == 1
    assert repeated_failure(history, "p", "t1", 2) is None


def test_a_different_failure_in_the_middle_breaks_the_streak():
    history = InMemoryHistoryStore()
    attempt(history, 1)
    attempt(history, 2, findings=[command_failed()])
    attempt(history, 3)
    assert identical_streak(task_failures(history, "p", "t1")) == 1


def test_a_different_worker_profile_starts_a_new_streak():
    history = InMemoryHistoryStore()
    attempt(history, 1, profile="free")
    attempt(history, 2, profile="free")
    attempt(history, 3, profile="paid")
    failures = task_failures(history, "p", "t1")
    assert identical_streak(failures) == 1
    assert repeated_failure(history, "p", "t1", 2) is None


def test_timeouts_repeat_too():
    history = InMemoryHistoryStore()
    for n in (1, 2):
        attempt(history, n, outcome="timed_out", verdict=None)
    stopped = repeated_failure(history, "p", "t1", 2)
    assert stopped is not None and stopped.kind == "timed_out"


# --- what is and is not a failure of the task ------------------------------------------

def test_a_human_action_starts_over():
    history = InMemoryHistoryStore()
    attempt(history, 1)
    attempt(history, 2)
    human_action(history)
    assert task_failures(history, "p", "t1") == []
    assert retry_note(history, "p", "t1") is None
    attempt(history, 3)
    assert len(task_failures(history, "p", "t1")) == 1


def test_failures_under_an_older_spec_are_not_evidence_about_the_current_one():
    history = InMemoryHistoryStore()
    attempt(history, 1)
    attempt(history, 2)
    changed = {**TASK, "acceptance": {"commands": ["npm run lint"]}}
    assert task_failures(history, "p", "t1", TASK) != []
    assert task_failures(history, "p", "t1", changed) == []
    assert repeated_failure(history, "p", "t1", 2, changed) is None
    assert retry_note(history, "p", "t1", changed) is None


def test_limits_stalls_passes_and_other_tasks_are_not_failures():
    history = InMemoryHistoryStore()
    attempt(history, 1, outcome="limited", verdict=None)
    attempt(history, 2, outcome="stalled", verdict=None)
    attempt(history, 3, verdict="pass", findings=(), summary="ok")
    attempt(history, 4, task_id="t2")
    assert task_failures(history, "p", "t1") == []


def test_an_attempt_that_passed_after_a_rebase_failure_is_not_a_failure():
    history = InMemoryHistoryStore()
    attempt(history, 1, verdict="pass", findings=(), summary="ok")
    history.append(type=EventType.VERIFICATION, run_id="r", project_id="p", task_id="t1",
                   attempt_id=f"{1:032x}",
                   payload={"verdict": "fail", "rebased_from": "abc", "findings": []})
    assert task_failures(history, "p", "t1") == []


def test_an_unfinished_or_interrupted_attempt_is_not_counted():
    history = InMemoryHistoryStore()
    history.append(type=EventType.ATTEMPT_STARTED, event_id=f"{1:032x}", run_id="r",
                   project_id="p", task_id="t1", payload={"spec_hash": spec_hash(TASK)})
    assert task_failures(history, "p", "t1") == []


# --- missing or malformed error details -------------------------------------------------

@pytest.mark.parametrize("findings,summary", [
    ([], None),
    ([], ""),
    (["not a mapping", 42, None], "weird"),
    ([{"kind": "command_failed"}], "no details at all"),
    ([{"kind": "command_failed", "command": None, "output_excerpt": None}], None),
    ([{"unexpected": {"nested": ["x"]}}], "unknown kind"),
])
def test_malformed_findings_still_give_a_stable_failure(findings, summary):
    history = InMemoryHistoryStore()
    attempt(history, 1, findings=findings, summary=summary)
    attempt(history, 2, findings=findings, summary=summary)
    first, second = task_failures(history, "p", "t1")
    assert first.fingerprint == second.fingerprint
    assert isinstance(retry_note(history, "p", "t1"), str)


def test_a_verification_that_could_not_run_is_its_own_kind():
    history = InMemoryHistoryStore()
    attempt(history, 1, verdict="unable_to_verify", findings=[],
            summary="the worktree does not match the committed result")
    attempt(history, 2, verification=False, verdict=None)
    kinds = [f.kind for f in task_failures(history, "p", "t1")]
    assert kinds == ["verification_unavailable", "no_result"]


def test_a_verifier_that_raised_has_no_verdict_but_is_still_a_failure():
    history = InMemoryHistoryStore()
    attempt(history, 1, verdict=None, verification=False)
    history.append(type=EventType.VERIFICATION, run_id="r", project_id="p", task_id="t1",
                   attempt_id=f"{1:032x}", payload={"verdict": None, "error_type": "OSError"})
    assert [f.kind for f in task_failures(history, "p", "t1")] == ["verification_unavailable"]


def test_a_worker_error_is_a_failure_with_its_error_type():
    history = InMemoryHistoryStore()
    attempt(history, 1, outcome="error", verdict=None, error_type="OpenCodeBackendError")
    (failure,) = task_failures(history, "p", "t1")
    assert failure.kind == "worker_error" and "OpenCodeBackendError" in failure.summary


# --- the note the next attempt is given -------------------------------------------------

def test_the_retry_note_says_what_failed_and_that_the_checks_are_fixed():
    history = InMemoryHistoryStore()
    attempt(history, 1)
    note = retry_note(history, "p", "t1")
    assert "tests/test_levels.py" in note
    assert "protected path" in note
    assert "fixed" in note and "same way" not in note


def test_the_retry_note_demands_a_new_approach_after_a_repeat():
    history = InMemoryHistoryStore()
    attempt(history, 1)
    attempt(history, 2)
    note = retry_note(history, "p", "t1")
    assert "exactly the same way" in note and "change what you do" in note


def test_the_retry_note_carries_the_failing_output_without_secrets():
    history = InMemoryHistoryStore()
    excerpt = f"AssertionError: expected 3 stars\nDEEPSEEK_API_KEY={SECRET}\nusing {SECRET}"
    attempt(history, 1, findings=[command_failed(excerpt)])
    note = retry_note(history, "p", "t1")
    assert "AssertionError: expected 3 stars" in note
    assert SECRET not in note and "<redacted>" in note


def test_the_retry_note_is_bounded():
    history = InMemoryHistoryStore()
    attempt(history, 1, findings=[command_failed("\n".join(f"line {i} " + "x" * 200
                                                         for i in range(500)))])
    assert len(retry_note(history, "p", "t1")) < 4000


def test_the_note_is_part_of_the_workers_prompt_and_absent_without_failures():
    history = InMemoryHistoryStore()
    assert retry_note(history, "p", "t1") is None
    assert "IMPORTANT" not in build_prompt({"id": "t1", "title": "T"}, {})
    attempt(history, 1)
    prompt = build_prompt({"id": "t1", "title": "T"},
                          {"previous_failure": retry_note(history, "p", "t1")})
    assert "tests/test_levels.py" in prompt


# --- the daemon's view: exhausted tasks ---------------------------------------------------

def status_for(task=TASK, state="in_progress"):
    return {"project_id": "p", "milestones": [], "tasks": [{**task, "status": state}]}


def test_work_for_stops_a_task_that_keeps_failing_identically():
    history = InMemoryHistoryStore()
    attempt(history, 1)
    attempt(history, 2)
    status = status_for()
    assert work_for(status, history, 3, 2) == ([], ["t1"])
    # Off (the default for callers that do not pass it): the old failure cap applies.
    assert work_for(status, history, 3) == (["t1"], [])
    assert work_for(status, history, 3, 0) == (["t1"], [])


def test_work_for_keeps_retrying_different_failures_until_the_old_cap():
    history = InMemoryHistoryStore()
    attempt(history, 1)
    attempt(history, 2, findings=[command_failed()])
    status = status_for()
    assert work_for(status, history, 3, 2) == (["t1"], [])
    attempt(history, 3, outcome="timed_out", verdict=None)
    assert work_for(status, history, 3, 2) == ([], ["t1"])  # three failures: the old cap


def test_a_changed_task_is_retried_after_an_identical_failure_streak():
    history = InMemoryHistoryStore()
    attempt(history, 1)
    attempt(history, 2)
    changed = {**TASK, "acceptance": {"commands": ["npm run lint"]}}
    assert work_for(status_for(changed), history, 3, 2) == (["t1"], [])


def test_exhaustion_note_names_the_repeat_and_the_old_cap():
    history = InMemoryHistoryStore()
    attempt(history, 1)
    attempt(history, 2)
    task = status_for()["tasks"][0]
    note = exhaustion_note(history, "p", task, 3, 2)
    assert "same way 2 times" in note and "protected path" in note
    assert exhaustion_note(history, "p", task, 3, 0) == (
        "failed 3 attempts; change its description or acceptance")


# --- the in-session limit ---------------------------------------------------------------------

def test_history_evidence_refuses_a_repeat_only_when_enabled():
    history = InMemoryHistoryStore()
    attempt(history, 1)
    attempt(history, 2)
    assert HistoryEvidence(history).repeated_failure("p", "t1", TASK) is None
    on = HistoryEvidence(history, max_identical_failures=2)
    assert on.repeated_failure("p", "t1", TASK) is not None


# --- configuration -------------------------------------------------------------------------------

def config_with(tmp_path, body):
    path = tmp_path / "config.toml"
    path.write_text(body)
    return load_config(path)


def test_the_limit_defaults_to_two_and_can_be_turned_off(tmp_path):
    assert config_with(tmp_path, "").run.max_identical_failures == 2
    assert config_with(tmp_path, "[run]\nmax_identical_failures = 0\n"
                       ).run.max_identical_failures == 0


@pytest.mark.parametrize("value", ["-1", "true", '"two"', "1.5"])
def test_a_bad_limit_is_a_config_error(tmp_path, value):
    with pytest.raises(ConfigError):
        config_with(tmp_path, f"[run]\nmax_identical_failures = {value}\n")


def test_worker_tiers_get_their_escalation_before_an_identical_stop(tmp_path):
    plain = config_with(tmp_path, "")
    assert retry_limits(plain) == {"max_failures": 3, "max_identical": 2}
    ladder = config_with(tmp_path, '[worker]\nladder = ["a", "b"]\n'
                         '[worker.profiles.a]\nmodel = "x/a"\n[worker.profiles.b]\n'
                         'model = "x/b"\n')
    limits = retry_limits(ladder)
    assert limits["max_identical"] == 3  # a tier gets FAILURES_PER_TIER (2) tries first
    off = config_with(tmp_path, "[run]\nmax_identical_failures = 0\n")
    assert retry_limits(off)["max_identical"] == 0
