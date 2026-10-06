"""Worker tiers with escalation (Milestone 3 part 6).

The worker profiles form a ladder, cheapest first (``[worker] ladder``), for
example tier0 = OpenCode's free model, tier1 = DeepSeek V4 Flash, tier2 =
DeepSeek V4 Pro, each through OpenCode with its own worker home. The
orchestrator, never Master, picks the profile for each attempt from history:

* a task starts at the tier its human-set ``size`` asks for (small -> 0,
  medium -> 1, hard -> 2; none -> 0), capped to the ladder;
* after ``FAILURES_PER_TIER`` failed *semantic* attempts on its current tier
  (finished or timed out, and not verified ``pass``), it moves up one tier;
  errors and interrupted attempts are infrastructure, not the model's fault,
  and do not count;
* it never moves down: the current tier is the highest tier any of its
  attempts used (or the size's tier, if higher).

Every attempt records ``worker_profile``, ``worker_tier``, ``worker_model`` and,
when the tier changed, ``escalated_from``. Master's context never names them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from core.history import EventType

__all__ = ["FAILURES_PER_TIER", "SIZE_TIERS", "TierChoice", "TierSet", "choose_tier"]

FAILURES_PER_TIER = 2
SIZE_TIERS = {"small": 0, "medium": 1, "hard": 2}


@dataclass(frozen=True)
class TierChoice:
    tier: int
    profile: str
    escalated_from: Optional[int] = None


def choose_tier(history, project_id: str, task: Mapping, ladder) -> TierChoice:
    ladder = list(ladder)
    top = len(ladder) - 1
    start = min(SIZE_TIERS.get(task.get("size"), 0), top)
    attempts = {}
    for e in history.events(project_id=project_id, task_id=task.get("id"),
                            types=[EventType.ATTEMPT_STARTED, EventType.ATTEMPT_FINISHED,
                                   EventType.VERIFICATION]):
        if e.type is EventType.ATTEMPT_STARTED:
            tier = e.payload.get("worker_tier")
            attempts[e.attempt_id] = {"tier": tier if isinstance(tier, int) else None,
                                      "outcome": None, "verdict": None}
        elif e.attempt_id in attempts:
            if e.type is EventType.ATTEMPT_FINISHED:
                attempts[e.attempt_id]["outcome"] = e.payload.get("outcome")
            elif "rebased_from" not in e.payload:
                attempts[e.attempt_id]["verdict"] = e.payload.get("verdict")
    used = [a["tier"] for a in attempts.values() if a["tier"] is not None]
    current = min(max([start, *used]), top)
    failures = sum(1 for a in attempts.values()
                   if a["tier"] == current and a["outcome"] in ("finished", "timed_out")
                   and a["verdict"] != "pass")
    if failures >= FAILURES_PER_TIER and current < top:
        return TierChoice(current + 1, ladder[current + 1], escalated_from=current)
    return TierChoice(current, ladder[current])


@dataclass(frozen=True)
class TierSet:
    """The configured ladder: profile name -> backend, environment and model."""

    ladder: tuple
    backends: Mapping
    envs: Mapping
    models: Mapping
    #: Profiles in order of preference (the first is used; core.worker_limits).
    workers: tuple = ()
    #: profile -> whether it costs money (counts toward the daily cap).
    paid: Mapping = None
    #: profile -> a readable name ("Big Pickle").
    labels: Mapping = None
