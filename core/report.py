"""The per-session report, generated from history alone.

Reads only a :class:`core.history.HistoryStore`: no project files, no session
files, no worktrees. So the report says what was recorded, and a gap in the
record shows up as a gap in the report.

For one session it gives:

* **runs**: start, end, how each ended, steps (decisions) per run;
* **attempts**: per task, the orchestrator's outcome, the verdict, duration;
* **wall-clock**: first run start to last run end, and the time runs were active;
* **tokens and cost**: the Master's usage per reasoner (including replies
  that were unusable) and the worker's *claimed* usage; cost from the price
  table, plus whatever cost the services reported themselves;
* **human touches**: every ``human_action`` and ``integration`` in the
  project, and any spec change that no recorded event (a human action or one
  of Master's own operations) explains;
* **completion traces**: each applied completion -> the attempt it relied
  on -> base and result SHAs -> verification -> integration.
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping, Optional

from core.evidence import spec_hash
from core.history import EventType, HistoryStore
from core.usage import add_usage

__all__ = ["build_report", "master_cost_usd", "render_report", "spend_since", "tier_stats"]

_RUN_ENDS = {EventType.RUN_STOPPED: "stopped", EventType.RUN_ERROR: "error",
             EventType.RUN_INTERRUPTED: "interrupted"}


def _time(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


def _seconds(start: Optional[str], end: Optional[str]) -> Optional[float]:
    if not start or not end:
        return None
    return round((_time(end) - _time(start)).total_seconds(), 1)


def _model_of(provenance: Optional[str]) -> Optional[str]:
    """``deepseek:deepseek-v4-flash`` -> ``deepseek-v4-flash``; ``a/b`` keeps both."""
    if not provenance:
        return None
    return provenance.split(":", 1)[-1]


def _price(usage: Mapping, model: Optional[str], prices: Mapping) -> Optional[float]:
    if not model:
        return None
    rates = prices.get(model) or prices.get(model.rsplit("/", 1)[-1])
    if rates is None:
        return None
    return round((usage.get("input_tokens", 0) * rates.get("input", 0)
                  + usage.get("cached_input_tokens", 0) * rates.get("cached_input",
                                                                     rates.get("input", 0))
                  + usage.get("output_tokens", 0) * rates.get("output", 0)) / 1_000_000, 6)


def master_cost_usd(history: HistoryStore, session_id: str, prices: Mapping,
                    default_model: Optional[str] = None) -> float:
    """What the Master's model calls in this session cost, from history.

    Includes unusable replies. A call whose model has no price raises
    ValueError: a cap that cannot price a call cannot be enforced.
    """
    total = 0.0
    for e in history.events(session_id=session_id,
                            types=[EventType.DECISION, EventType.RUN_STOPPED]):
        reasoner = e.payload.get("reasoner")
        for usage in (e.payload.get("usage"), e.payload.get("failed_call_usage")):
            if not usage or not any(usage.get(k) for k in ("input_tokens", "output_tokens",
                                                             "cached_input_tokens")):
                continue
            cost = _price(add_usage([usage]), _model_of(reasoner) or default_model, prices)
            if cost is None:
                raise ValueError(f"no price for the Master model of {reasoner!r}")
            total += cost
    return round(total, 6)


def spend_since(history: HistoryStore, since_iso: str, prices: Mapping,
                default_model: Optional[str] = None) -> dict:
    """Priced spend recorded at or after ``since_iso``, across all projects.

    Master and planner calls, and visual reviews, are priced from their usage. A worker's claimed
    usage is priced when its model has a price; otherwise the cost the service
    reported is used (0 for free models). Unpriced Master calls count as
    ``unpriced_calls`` so a cap can refuse to guess.
    """
    master = worker = reviewer = 0.0
    unpriced = 0
    for e in history.events(types=[EventType.DECISION, EventType.RUN_STOPPED,
                                   EventType.ATTEMPT_FINISHED, EventType.PLANNER_TURN,
                                   EventType.VERIFICATION, EventType.HUMAN_ACTION]):
        if e.created_at < since_iso:
            continue
        if e.type is EventType.HUMAN_ACTION:
            if e.payload.get("action") == "worker_probe":  # a probe is a worker call
                usage = e.payload.get("usage") or {}
                model = e.payload.get("model")
                cost = _price(usage, model, prices) if model else None
                worker += cost if cost is not None else float(usage.get("reported_cost_usd") or 0)
            continue
        if e.type is EventType.VERIFICATION:
            review = (e.payload.get("evidence") or {}).get("visual_review") or {}
            if review.get("usage"):
                cost = _price(add_usage([review["usage"]]), _model_of(review.get("reasoner")),
                              prices)
                if cost is None:
                    unpriced += 1
                else:
                    reviewer += cost
            continue
        if e.type is EventType.ATTEMPT_FINISHED:
            usage = e.payload.get("worker_reported_usage") or {}
            model = (e.payload.get("artifacts") or {}).get("model")
            cost = _price(usage, model, prices) if model else None
            worker += cost if cost is not None else float(usage.get("reported_cost_usd") or 0)
            continue
        for usage in (e.payload.get("usage"), e.payload.get("failed_call_usage")):
            if not usage:
                continue
            cost = _price(add_usage([usage]),
                          _model_of(e.payload.get("reasoner")) or default_model, prices)
            if cost is None:
                unpriced += 1
            else:
                master += cost
    return {"master_usd": round(master, 6), "worker_usd": round(worker, 6),
            "reviewer_usd": round(reviewer, 6),
            "total_usd": round(master + worker + reviewer, 6), "unpriced_calls": unpriced}


def _completes(payload) -> bool:
    operation = payload.get("operation") or {}
    arguments = operation.get("arguments") or {}
    return (operation.get("name") in ("update_task", "create_task")
            and arguments.get("status") == "completed")


def build_report(history: HistoryStore, session_id: str,
                 prices: Optional[Mapping] = None) -> dict:
    prices = prices or {}
    session_events = history.events(session_id=session_id)
    if not session_events:
        raise ValueError(f"no history for session {session_id!r}")
    project_id = session_events[0].project_id

    # --- runs ---------------------------------------------------------------
    runs = []
    for started in (e for e in session_events if e.type is EventType.RUN_STARTED):
        mine = [e for e in session_events if e.run_id == started.run_id]
        end = next((e for e in mine if e.type in _RUN_ENDS), None)
        runs.append({
            "run_id": started.run_id,
            "started_at": started.created_at,
            "ended_at": end.created_at if end else None,
            "ended": _RUN_ENDS[end.type] if end else "open",
            "stop_reason": end.payload.get("stop_reason") if end else None,
            "steps": sum(1 for e in mine if e.type is EventType.DECISION),
            "seconds": _seconds(started.created_at, end.created_at if end else None),
        })

    # --- attempts -----------------------------------------------------------
    attempts = []
    for started in (e for e in session_events if e.type is EventType.ATTEMPT_STARTED):
        mine = [e for e in session_events if e.attempt_id == started.attempt_id]
        finished = next((e for e in mine if e.type is EventType.ATTEMPT_FINISHED), None)
        interrupted = next((e for e in mine if e.type is EventType.ATTEMPT_INTERRUPTED), None)
        verdicts = [e for e in mine if e.type is EventType.VERIFICATION]
        outcome = (finished.payload.get("outcome") or finished.payload.get("status")
                   if finished else ("interrupted" if interrupted else "unfinished"))
        attempts.append({
            "attempt_id": started.attempt_id,
            "task_id": started.task_id,
            "outcome": outcome,
            "worker_reported_status": finished.payload.get("worker_reported_status")
            if finished else None,
            "verdict": verdicts[-1].payload.get("verdict") if verdicts else None,
            "base_sha": started.payload.get("base_sha"),
            "result_sha": finished.payload.get("result_sha") if finished else None,
            "spec_hash": started.payload.get("spec_hash"),
            "files_changed": (finished.payload.get("diffstat") or {}).get("files")
            if finished else None,
            "seconds": _seconds(started.created_at, finished.created_at if finished else None),
            "profile": started.payload.get("worker_profile"),
            "limit": finished.payload.get("limit") if finished else None,
            "stall": finished.payload.get("stall") if finished else None,
            "tampered": finished.payload.get("repository_tampered") if finished else None,
        })

    # --- tokens and cost ----------------------------------------------------
    master_by_reasoner: dict = {}
    for e in session_events:
        if e.type is EventType.DECISION:
            key = e.payload.get("reasoner") or "unknown"
            master_by_reasoner.setdefault(key, []).extend(
                [e.payload.get("usage"), e.payload.get("failed_call_usage")])
        elif e.type is EventType.RUN_STOPPED and e.payload.get("failed_call_usage"):
            master_by_reasoner.setdefault("unknown", []).append(e.payload["failed_call_usage"])
    master = []
    for reasoner, items in master_by_reasoner.items():
        usage = add_usage(items)
        master.append({"reasoner": reasoner, "usage": usage,
                       "cost_usd": _price(usage, _model_of(reasoner), prices)})

    worker_by_model: dict = {}
    for e in session_events:
        if e.type is EventType.ATTEMPT_FINISHED:
            model = (e.payload.get("artifacts") or {}).get("model") or "worker default model"
            worker_by_model.setdefault(model, []).append(e.payload.get("worker_reported_usage"))
    worker = []
    for model, items in worker_by_model.items():
        usage = add_usage(items)
        worker.append({"model": model, "usage": usage, "claimed": True,
                       "cost_usd": _price(usage, model, prices)})

    priced = [x["cost_usd"] for x in master + worker if x["cost_usd"] is not None]
    unpriced = [x.get("reasoner") or x.get("model") for x in master + worker
                if x["cost_usd"] is None and any(x["usage"].get(k) for k in
                                                 ("input_tokens", "output_tokens"))]

    # --- human touches and spec changes --------------------------------------
    project_events = history.events(project_id=project_id)
    human = [
        {"seq": e.seq, "at": e.created_at, "type": e.type.value, "task_id": e.task_id,
         "action": e.payload.get("action") or e.payload.get("method"),
         "actor": e.payload.get("actor")}
        for e in project_events
        if e.type in (EventType.HUMAN_ACTION, EventType.INTEGRATION)
    ]
    unexplained = []
    known: dict = {}
    for e in project_events:
        if e.type is EventType.HUMAN_ACTION:
            before = e.payload.get("spec_hash_before")
            if e.task_id in known and known[e.task_id] != before:
                unexplained.append({"task_id": e.task_id, "seq": e.seq, "before": "human_action"})
            known[e.task_id] = e.payload.get("spec_hash_after")
        elif (e.type is EventType.OPERATION_RESULT and e.payload.get("status") == "success"
              and e.payload.get("operation") in ("update_task", "create_task")
              and isinstance(e.payload.get("value"), dict)):
            # A recorded model operation explains its own change: the stored
            # record is the task as written.
            known[e.task_id] = spec_hash(e.payload["value"])
        elif e.type is EventType.ATTEMPT_STARTED and e.payload.get("spec_hash"):
            spec = e.payload["spec_hash"]
            if e.task_id in known and known[e.task_id] != spec:
                unexplained.append({"task_id": e.task_id, "seq": e.seq, "before": "attempt"})
            known[e.task_id] = spec

    # --- completion traces ----------------------------------------------------
    traces = []
    for decision in (e for e in session_events if e.type is EventType.DECISION):
        if not _completes(decision.payload) or decision.payload.get("gate_reason"):
            continue
        applied = any(e.type is EventType.OPERATION_RESULT and e.run_id == decision.run_id
                      and e.payload.get("step") == decision.payload.get("step")
                      and e.payload.get("status") == "success" for e in session_events)
        if not applied:
            continue
        attempt_id = decision.payload.get("gate_attempt_id")
        attempt = next((a for a in attempts if a["attempt_id"] == attempt_id), None)
        if attempt is None and attempt_id:
            attempt = next((a for a in build_attempts_any(history, project_id, attempt_id)), None)
        integrations = [e for e in project_events if e.type is EventType.INTEGRATION
                        and e.attempt_id == attempt_id]
        started = next((e for e in history.events(project_id=project_id, attempt_id=attempt_id,
                                                  types=[EventType.ATTEMPT_STARTED])), None) \
            if attempt_id else None
        traces.append({
            "task_id": decision.task_id,
            "manual_check": started.payload.get("manual_check") if started else None,
            "decision_seq": decision.seq,
            "attempt_id": attempt_id,
            "base_sha": attempt and attempt["base_sha"],
            "result_sha": attempt and attempt["result_sha"],
            "verdict": attempt and attempt["verdict"],
            "integrated_sha": integrations[-1].payload.get("result_sha") if integrations else None,
        })

    starts = [r["started_at"] for r in runs]
    ends = [r["ended_at"] for r in runs if r["ended_at"]]
    return {
        "session_id": session_id,
        "project_id": project_id,
        "runs": runs,
        "steps": sum(r["steps"] for r in runs),
        "attempts": attempts,
        "wall_clock_seconds": _seconds(min(starts), max(ends)) if starts and ends else None,
        "active_seconds": round(sum(r["seconds"] or 0 for r in runs), 1),
        "master_usage": master,
        "worker_usage": worker,
        "cost_usd": round(sum(priced), 6),
        "unpriced": unpriced,
        "reported_cost_usd": round(sum(x["usage"]["reported_cost_usd"]
                                       for x in master + worker), 6),
        "human_touches": human,
        "unexplained_spec_changes": unexplained,
        "completions": traces,
        "tiers": tier_stats(history, prices, session_id=session_id),
    }


def tier_stats(history, prices: Mapping, project_id=None, session_id=None) -> list:
    """Per worker tier: attempts, passes, success rate, worker cost, cost per pass."""
    rows: dict = {}
    for e in history.events(project_id=project_id, session_id=session_id,
                            types=[EventType.ATTEMPT_STARTED]):
        if "worker_profile" not in e.payload:
            continue
        events = history.events(project_id=e.project_id, attempt_id=e.attempt_id)
        finished = next((x for x in events if x.type is EventType.ATTEMPT_FINISHED), None)
        verdicts = [x for x in events if x.type is EventType.VERIFICATION
                    and "rebased_from" not in x.payload]
        key = (e.payload.get("worker_tier"), e.payload["worker_profile"])
        row = rows.setdefault(key, {"tier": key[0], "profile": key[1],
                                    "model": e.payload.get("worker_model") or "worker default model",
                                    "attempts": 0, "passes": 0, "cost_usd": 0.0})
        row["attempts"] += 1
        if verdicts and verdicts[-1].payload.get("verdict") == "pass":
            row["passes"] += 1
        usage = (finished.payload.get("worker_reported_usage") if finished else None) or {}
        model = e.payload.get("worker_model")
        cost = _price(usage, model, prices) if model and usage else None
        row["cost_usd"] += cost if cost is not None else float(usage.get("reported_cost_usd") or 0)
    out = []
    for row in sorted(rows.values(), key=lambda r: (r["tier"] is None, r["tier"])):
        row["cost_usd"] = round(row["cost_usd"], 6)
        row["success_rate"] = round(row["passes"] / row["attempts"], 3) if row["attempts"] else None
        row["cost_per_pass_usd"] = (round(row["cost_usd"] / row["passes"], 6)
                                    if row["passes"] else None)
        out.append(row)
    return out


def build_attempts_any(history, project_id, attempt_id):
    """An attempt outside the session (a pass from an earlier session)."""
    events = history.events(project_id=project_id, attempt_id=attempt_id)
    started = next((e for e in events if e.type is EventType.ATTEMPT_STARTED), None)
    if started is None:
        return []
    finished = next((e for e in events if e.type is EventType.ATTEMPT_FINISHED), None)
    verdicts = [e for e in events if e.type is EventType.VERIFICATION]
    return [{"attempt_id": attempt_id, "base_sha": started.payload.get("base_sha"),
             "result_sha": finished.payload.get("result_sha") if finished else None,
             "verdict": verdicts[-1].payload.get("verdict") if verdicts else None}]


def _tokens(usage) -> str:
    return (f"in {usage['input_tokens']:,} (+{usage['cached_input_tokens']:,} cached), "
            f"out {usage['output_tokens']:,}")


def render_report(report: dict) -> str:
    lines = [f"REPORT  session {report['session_id']}  project {report['project_id']}", ""]
    lines.append(f"Runs: {len(report['runs'])}   steps: {report['steps']}   "
                 f"wall-clock: {report['wall_clock_seconds']} s   "
                 f"active: {report['active_seconds']} s")
    for r in report["runs"]:
        lines.append(f"  {r['started_at'][:19]}  {r['ended']:<11} {r['stop_reason'] or '':<22} "
                     f"steps {r['steps']:<3} {r['seconds']} s")
    lines += ["", f"Attempts: {len(report['attempts'])}"]
    for a in report["attempts"]:
        lines.append(f"  {a['task_id']:<14} {a['outcome']:<11} verdict {a['verdict'] or '-':<17} "
                     f"files {a['files_changed']}  {a['seconds']} s  {a['attempt_id'][:12]}"
                     + (f"  worker {a['profile']}" if a.get("profile") else ""))
        if a.get("tampered"):
            lines.append("      REPOSITORY TAMPERED (restored; counted as a failure): "
                         + ", ".join(a["tampered"]))
        if a.get("stall"):
            stall = a["stall"]
            lines.append(f"      stalled worker: {stall.get('reason')} after "
                         f"{stall.get('minutes')} min (reasoning tokens "
                         f"{stall.get('reasoning_tokens')}; not counted as a failure)")
            for tool in stall.get("last_tools") or []:
                lines.append(f"        last: {tool}")
        if a.get("limit"):
            limit = a["limit"]
            lines.append(f"      worker limit: {limit.get('kind')}, reset "
                         f"{limit.get('reset_at') or 'unknown'} (not counted as a failure)")
    lines += ["", "Tokens and cost (USD; worker usage is the worker's own claim)"]
    for m in report["master_usage"]:
        lines.append(f"  master {m['reasoner']:<32} {_tokens(m['usage'])}  cost {m['cost_usd']}")
    for w in report["worker_usage"]:
        lines.append(f"  worker {w['model']:<32} {_tokens(w['usage'])}  cost {w['cost_usd']}"
                     "  (claimed)")
    lines.append(f"  total priced: ${report['cost_usd']}   reported by services: "
                 f"${report['reported_cost_usd']}"
                 + (f"   unpriced: {', '.join(report['unpriced'])}" if report["unpriced"] else ""))
    if report.get("tiers"):
        lines += ["", "Worker tiers"]
        for t in report["tiers"]:
            lines.append(f"  tier {t['tier']} {t['profile']:<8} {t['model']:<28} "
                         f"attempts {t['attempts']}  passed {t['passes']} "
                         f"({t['success_rate']:.0%})  cost ${t['cost_usd']}")
    lines += ["", f"Human touches: {len(report['human_touches'])}"]
    for h in report["human_touches"]:
        lines.append(f"  {h['at'][:19]}  {h['type']:<13} {h['task_id'] or '':<14} "
                     f"{h['action']}  ({h['actor']})")
    if report["unexplained_spec_changes"]:
        lines.append("  UNEXPLAINED spec changes (edits not made through run_cli):")
        for u in report["unexplained_spec_changes"]:
            lines.append(f"    task {u['task_id']} at event {u['seq']}")
    else:
        lines.append("  no unexplained spec changes")
    lines += ["", f"Completions: {len(report['completions'])}"]
    for c in report["completions"]:
        lines.append(f"  {c['task_id']:<14} decision #{c['decision_seq']} -> attempt "
                     f"{(c['attempt_id'] or '-')[:12]} -> {str(c['base_sha'])[:10]}.."
                     f"{str(c['result_sha'])[:10]} -> {c['verdict']} -> integrated "
                     f"{str(c['integrated_sha'])[:10] if c['integrated_sha'] else 'not yet'}")
        if c.get("manual_check"):
            lines.append("      how to check by hand:")
            lines.extend(f"        {line}" for line in c["manual_check"].splitlines())
    return "\n".join(lines) + "\n"
