"""Worker provider limits: recognise them, wait when it is short, ask the owner otherwise.

A worker can fail for reasons that say nothing about the task: the provider
rate-limited it (HTTP 429, a used-up quota), the model disappeared or stopped
being free, the profile has no credential, or the provider failed before the
worker did any work. These are **limits**, not failures:

* the attempt ends with outcome ``limited``; it never counts against the task's
  attempt budget and costs nothing;
* the reset time is read from the provider's answer (``Retry-After``,
  ``x-ratelimit-reset``, "try again in 1 hour", "resets at 14:00") and recorded,
  or recorded as unknown;
* **known reset within ``max_auto_wait_minutes`` (default 120)**: the project
  pauses until the reset plus 2 minutes, then the task runs again on the same
  worker;
* **unknown reset**: the project pauses ``unknown_limit_wait_minutes`` (default 60)
  once; if the worker is still limited, it is treated like a long limit;
* **longer limits, a model that is gone or no longer free, a missing
  credential**: the project pauses and waits for the owner's choice
  (``ms limit <project> wait|free|paid``): wait for the reset, switch to the next
  free worker profile, or use the paid profile (it counts toward the daily cap).

Other projects are not affected. State lives in ``<state dir>/limits/<project>.json``.
Classification reads the worker's own output, so it is a heuristic.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

__all__ = ["classify_failure", "parse_reset", "LimitState", "KINDS", "ASK_KINDS",
           "RESET_MARGIN", "describe", "choice_profile", "probe_profile", "PROBE_ANSWER"]

KINDS = ("rate_limited", "model_unavailable", "not_configured", "provider_error")
#: Kinds that always wait for the owner: waiting would not help.
ASK_KINDS = ("model_unavailable", "not_configured")
RESET_MARGIN = timedelta(minutes=2)

_PATTERNS = (
    ("model_unavailable", re.compile(
        r"model[^\n]{0,40}(not found|does not exist|unavailable|not available|no longer"
        r"|deprecated|retired|disabled)|modelnotfound|providermodelnotfound|unknown model"
        r"|no longer (free|available)|not free|payment required|\b402\b", re.I)),
    ("rate_limited", re.compile(
        r"\b429\b|rate[ _-]?limit|too many requests|quota|insufficient[ _](balance|credit|funds)"
        r"|credits? (exhausted|exceeded|used up)|usage limit|limit (reached|exceeded)"
        r"|exceeded your|free (tier|usage) (limit|exhausted)|try again (later|in)"
        r"|retry[-_ ]after", re.I)),
    ("not_configured", re.compile(
        r"provider not found|api[ _-]?key|unauthori[sz]ed|\b401\b|\b403\b|authentication"
        r"|no credentials|not logged in", re.I)),
)


def _error_messages(stdout: str) -> list:
    """Error events of `opencode run --format json`, as JSON text."""
    messages = []
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "error":
            messages.append(json.dumps(event.get("error"), sort_keys=True))
    return messages


def classify_failure(stdout: str, stderr: str, returncode: Optional[int], steps: int,
                     now: Optional[datetime] = None) -> Optional[dict]:
    """``{"kind", "detail", "reset_at"}`` for a limit, or None for a real failure.

    ``steps``: worker steps finished before the failure. ``reset_at`` is an ISO time
    or ``"unknown"``.
    """
    errors = _error_messages(stdout)
    if returncode == 0 and not errors:
        return None
    text = "\n".join(errors + [(stderr or "")[-4000:]])
    kind = detail = None
    for name, pattern in _PATTERNS:
        match = pattern.search(text)
        if match:
            start = max(0, match.start() - 80)
            kind = name
            detail = " ".join(text[start:match.end() + 160].split())
            break
    if kind is None and errors and steps == 0:
        kind, detail = "provider_error", errors[-1][:300]
    if kind is None:
        return None
    reset = parse_reset(text, now)
    return {"kind": kind, "detail": detail,
            "reset_at": reset.isoformat(timespec="seconds") if reset else "unknown"}


_UNITS = {"s": 1, "sec": 1, "second": 1, "m": 60, "min": 60, "minute": 60, "h": 3600,
          "hr": 3600, "hour": 3600, "d": 86400, "day": 86400}


def parse_reset(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """When the provider says the limit ends, if it says so."""
    now = now or datetime.now(timezone.utc)
    text = str(text or "")
    # Retry-After: seconds, or an HTTP date.
    match = re.search(r"retry[-_ ]after\\?\"?\s*[:=]\s*\\?\"?([^\",}\n\\]+)", text, re.I)
    if match:
        value = match.group(1).strip()
        if value.isdigit():
            return now + timedelta(seconds=int(value))
        try:
            return parsedate_to_datetime(value).astimezone(timezone.utc)
        except (TypeError, ValueError):
            pass
    # x-ratelimit-reset: epoch seconds or seconds from now.
    match = re.search(r"ratelimit[-_]reset[a-z-]*\\?\"?\s*[:=]\s*\\?\"?(\d+(?:\.\d+)?)", text, re.I)
    if match:
        value = float(match.group(1))
        return (datetime.fromtimestamp(value, timezone.utc) if value > 1_000_000_000
                else now + timedelta(seconds=value))
    # "try again in 1 hour", "in 45 minutes", "in 1h30m", "in 90s".
    match = re.search(r"(?:try again|retry|available again|resets?)\s+in\s+((?:\d+(?:\.\d+)?\s*"
                      r"(?:seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)(?![a-z])"
                      r"[\s,and]*)+)", text, re.I)
    if match:
        total = 0.0
        for amount, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]+)", match.group(1), re.I):
            unit = unit.lower().rstrip("s") or "s"
            total += float(amount) * _UNITS.get(unit, _UNITS.get(unit[:1], 0))
        if total > 0:
            return now + timedelta(seconds=total)
    # "resets at 2026-10-06T14:00:00Z" or "resets at 14:00 (UTC)".
    match = re.search(r"(?:resets?|available again|try again)\s+(?:at|on)\s+"
                      r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)",
                      text, re.I)
    if match:
        try:
            value = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    match = re.search(r"(?:resets?|available again|try again)\s+at\s+(\d{1,2}):(\d{2})"
                      r"\s*(utc|gmt)?", text, re.I)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        base = now if match.group(3) else now.astimezone()
        when = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when <= base:
            when += timedelta(days=1)
        return when.astimezone(timezone.utc)
    return None


class LimitState:
    """One project's worker-limit state: automatic waits, the owner's choice, overrides.

    Phases: ``auto_wait`` (paused until ``until``), ``needs_choice`` (paused until the
    owner chooses), ``chosen_wait`` (the owner chose to wait until ``until``).
    An ``override`` (the owner chose ``free`` or ``paid``) names the profile to use
    instead of the limited one until ``override_until``.
    """

    #: Phases in which the owner may choose.
    CHOICE_PHASES = ("needs_choice", "chosen_wait", "auto_wait")

    def __init__(self, state_dir: Path, now=lambda: datetime.now(timezone.utc)):
        self._dir = Path(state_dir) / "limits"
        self._now = now

    # --- storage ---

    def _path(self, project_id: str) -> Path:
        return self._dir / f"{project_id}.json"

    def get(self, project_id: str) -> dict:
        try:
            return json.loads(self._path(project_id).read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, project_id: str, state: dict) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(project_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
        tmp.replace(path)

    # --- events ---

    def record_limit(self, project_id: str, profile: str, info: dict,
                     max_auto_wait_min: float = 120, unknown_wait_min: float = 60) -> dict:
        """A worker profile hit a limit: decide whether to wait or to ask the owner."""
        now = self._now()
        state = self.get(project_id)
        same = state.get("profile") == profile
        reset = None if info.get("reset_at") in (None, "unknown") else \
            datetime.fromisoformat(info["reset_at"])
        record = {"profile": profile, "kind": info.get("kind"), "detail": info.get("detail"),
                  "reset_at": info.get("reset_at") or "unknown",
                  "since": state.get("since") if same else now.isoformat(timespec="seconds"),
                  "unknown_waits": state.get("unknown_waits", 0) if same else 0,
                  "events": (state.get("events", []) if same else [])[-9:]
                  + [{"at": now.isoformat(timespec="seconds"), "kind": info.get("kind"),
                      "reset_at": info.get("reset_at") or "unknown"}]}
        if state.get("override") and state["override"] != profile:
            # An override for another limited profile still applies.
            record.update({k: state[k] for k in ("override", "override_until", "override_for")
                           if k in state})
        if same and state.get("choice") == "wait" and info.get("kind") not in ASK_KINDS:
            # The owner chose to wait: keep waiting, without asking again.
            until = (reset + RESET_MARGIN) if reset else now + timedelta(minutes=unknown_wait_min)
            record.update(phase="chosen_wait", choice="wait",
                          until=until.isoformat(timespec="seconds"))
        elif info.get("kind") in ASK_KINDS:
            record.update(phase="needs_choice", until=None)
        elif reset is not None and reset - now <= timedelta(minutes=max_auto_wait_min):
            record.update(phase="auto_wait",
                          until=(reset + RESET_MARGIN).isoformat(timespec="seconds"))
        elif reset is None and record["unknown_waits"] < 1:
            record.update(phase="auto_wait", unknown_waits=record["unknown_waits"] + 1,
                          until=(now + timedelta(minutes=unknown_wait_min))
                          .isoformat(timespec="seconds"))
        else:
            record.update(phase="needs_choice", until=None)
        self._save(project_id, record)
        return record

    def record_success(self, project_id: str, profile: str) -> None:
        """A worker profile worked again: its limit is over."""
        state = self.get(project_id)
        if state.get("profile") == profile and state.get("phase"):
            history = state.get("history", []) + [{k: state.get(k) for k in (
                "profile", "kind", "reset_at", "since")} | {"ended": self._now().isoformat(
                    timespec="seconds")}]
            keep = {k: state[k] for k in ("override", "override_until", "override_for")
                    if k in state}
            self._save(project_id, {**keep, "history": history[-20:]})

    def choose(self, project_id: str, choice: str, profile: Optional[str] = None) -> dict:
        """The owner's choice: ``wait``, or ``free``/``paid`` with the profile to use."""
        state = self.get(project_id)
        if state.get("phase") not in self.CHOICE_PHASES:
            raise ValueError("no worker limit waits for a choice in this project")
        now = self._now()
        reset = None if state.get("reset_at") in (None, "unknown") else \
            datetime.fromisoformat(state["reset_at"])
        if choice == "wait":
            until = (reset + RESET_MARGIN) if reset and reset > now else now + timedelta(hours=1)
            state.update(phase="chosen_wait", choice="wait",
                         until=until.isoformat(timespec="seconds"))
        elif choice in ("free", "paid"):
            if not profile:
                raise ValueError(f"no {choice} worker profile is configured")
            ends = reset if reset and reset > now else now + timedelta(hours=24)
            state.update(phase=None, choice=choice, until=None, override=profile,
                         override_for=state.get("profile"),
                         override_until=ends.isoformat(timespec="seconds"))
        else:
            raise ValueError("choose wait, free or paid")
        self._save(project_id, state)
        return state

    # --- questions ---

    def paused(self, project_id: str) -> Optional[str]:
        """Why the project waits now, or None."""
        state = self.get(project_id)
        phase = state.get("phase")
        if phase == "needs_choice":
            return "needs_choice"
        if phase in ("auto_wait", "chosen_wait") and state.get("until") \
                and datetime.fromisoformat(state["until"]) > self._now():
            return phase
        return None

    def override(self, project_id: str) -> Optional[str]:
        """The profile the owner chose to use instead of a limited one, while it applies."""
        state = self.get(project_id)
        until = state.get("override_until")
        if state.get("override") and until and datetime.fromisoformat(until) > self._now():
            return state["override"]
        return None


def _local_time(iso: Optional[str]) -> Optional[str]:
    if not iso or iso == "unknown":
        return None
    return datetime.fromisoformat(iso).astimezone().strftime("%H:%M")


def describe(project_id: str, state: dict, labels: Optional[dict] = None,
             now: Optional[datetime] = None) -> Optional[str]:
    """One plain line about the project's worker limit, or None when there is none."""
    labels = labels or {}
    phase = state.get("phase")
    if not phase:
        if state.get("override") and state.get("override_until"):
            label = labels.get(state["override"], state["override"])
            limited = labels.get(state.get("override_for"), state.get("override_for"))
            return (f"Using {label} instead of {limited} until "
                    f"{_local_time(state['override_until'])} (your choice).")
        return None
    name = labels.get(state.get("profile"), state.get("profile"))
    reset = _local_time(state.get("reset_at"))
    what = {"rate_limited": "rate-limited", "model_unavailable": "not available (model gone "
            "or no longer free)", "not_configured": "not set up (no credential)",
            "provider_error": "failing at its provider"}.get(state.get("kind"), "limited")
    if phase == "needs_choice":
        until = f" until ~{reset}" if reset else ""
        return (f"{name} is {what}{until}. Choose: ms limit {project_id} wait | free | paid")
    until = _local_time(state.get("until"))
    how = "your choice" if phase == "chosen_wait" else "automatic"
    return f"{name} is {what}; the project waits until {until} ({how}), then continues."


def choice_profile(choice: str, order, profiles: dict, limited: Optional[str]) -> Optional[str]:
    """The profile behind the owner's choice: the next free one, or the paid one."""
    if choice == "free":
        names = list(order)
        start = names.index(limited) + 1 if limited in names else 0
        for name in names[start:] + names[:start]:
            if name != limited and not profiles.get(name, {}).get("paid"):
                return name
        return None
    if choice == "paid":
        return next((n for n in list(order) + list(profiles)
                     if profiles.get(n, {}).get("paid") and n != limited), None)
    return None


#: The probe: a fixed prompt with a known short answer.
PROBE_ANSWER = "master-system-probe-ok"
PROBE_PROMPT = (f"This is a connection test. Do not use any tools. Reply with exactly this "
                f"text and nothing else: {PROBE_ANSWER}")
PROBE_TIMEOUT_S = 90


def _reported_model(stdout: str) -> Optional[str]:
    """``provider/model`` from the first event that names one (``providerID``/``modelID``)."""
    def find(value):
        if isinstance(value, dict):
            if isinstance(value.get("modelID"), str):
                provider = value.get("providerID")
                return f"{provider}/{value['modelID']}" if provider else value["modelID"]
            value = list(value.values())
        if isinstance(value, list):
            for item in value:
                found = find(item)
                if found:
                    return found
        return None

    for line in (stdout or "").splitlines():
        try:
            found = find(json.loads(line))
        except ValueError:
            continue
        if found:
            return found
    return None


def probe_profile(profile: dict, *, opencode_bin, extra_args, env: dict, log_dir,
                  run=None, timeout_s: float = PROBE_TIMEOUT_S) -> dict:
    """Ask a worker profile for :data:`PROBE_ANSWER`, the way an attempt would run it.

    Same OpenCode binary, the profile's model, its worker environment (``env``); an
    empty temporary directory outside every worktree and repository; every tool but
    ``bash`` disabled through the inline config (OpenCode's free tier refuses requests
    without ``bash``). Passes only if the run succeeds, the reply contains the answer
    and the model OpenCode reports, when it reports one, is the profile's.
    Returns ``{ok, kind, reason, model, reported_model, usage}``.
    """
    import tempfile
    import time

    from core.opencode_backend import OpenCodeCliBackend, parse_events
    from core.task_types import REQUIRED_TOOLS, opencode_config

    if run is None:
        from core.worker_process import run_process as run
    model = profile.get("model")
    backend = OpenCodeCliBackend(opencode_bin=opencode_bin, model=model, extra_args=extra_args)
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ms-probe-") as empty:
        outcome = run(backend.command(empty, PROBE_PROMPT), cwd=empty, log_dir=log_dir,
                      log_name="probe", deadline=time.monotonic() + timeout_s,
                      env={**env, "OPENCODE_CONFIG_CONTENT": opencode_config(REQUIRED_TOOLS)})
    text, usage, _ = parse_events(outcome.stdout)
    reported = _reported_model(outcome.stdout)
    result = {"ok": False, "kind": None, "reason": None, "model": model,
              "reported_model": reported, "usage": usage}
    limit = None if outcome.timed_out else classify_failure(
        outcome.stdout, outcome.stderr, outcome.returncode, usage.get("steps", 0))
    if outcome.timed_out:
        result["reason"] = f"it did not answer within {timeout_s:g} s"
    elif limit:
        result.update(kind=limit["kind"], reason=f"{limit['kind']}: {limit['detail']}")
    elif outcome.returncode != 0:
        result["reason"] = (f"OpenCode exited with code {outcome.returncode}: "
                            + " ".join((outcome.stderr or "")[-300:].split()))
    elif model and reported and reported != model and reported != model.split("/", 1)[-1]:
        result.update(kind="model_unavailable",
                      reason=f"OpenCode answered with {reported}, not {model}")
    elif PROBE_ANSWER not in (text or ""):
        result["reason"] = ("it did not give the expected answer; it replied: "
                            + " ".join((text or "(nothing)").split())[:200])
    else:
        result["ok"] = True
    return result
