"""Configuration for ``core.run_cli``: one TOML file, standard library only.

Default location: ``$XDG_CONFIG_HOME/master-system/config.toml``
(``~/.config/master-system/config.toml``). A missing file means "use the
defaults". It holds no secrets: the Master's API key stays in ``run_cli``'s own
environment, and the worker's credential lives in the worker home.

```toml
[master]                          # the reasoning provider (D2: DeepSeek V4 Flash)
provider = "deepseek"             # deepseek | ollama | opencode
model = "deepseek-v4-flash"
# base_url = "https://api.deepseek.com"
timeout_s = 180

[worker]                          # the OpenCode CLI worker (D3: dedicated home)
opencode_bin = "~/.opencode/bin/opencode"
# model = "provider/model"        # passed as --model; unset = the worker home's default
home = "~/.local/share/master-system-worker"
extra_args = []
node_min_major = 22
playwright_browsers_path = "~/.cache/ms-playwright"

# Worker tiers (optional): cheapest first; a task moves up after 2 failed attempts.
# [worker]  ladder = ["tier0", "tier1", "tier2"]
# [worker.profiles.tier0]          # OpenCode's free default model
# home = "~/.local/share/master-system-worker"
# [worker.profiles.tier1]          # DeepSeek V4 Flash (a spend-limited key in this home)
# model = "deepseek/deepseek-v4-flash"
# home = "~/.local/share/master-system-worker-paid"
# [worker.profiles.tier2]
# model = "deepseek/deepseek-v4-pro"
# home = "~/.local/share/master-system-worker-paid"

[run]
max_steps = 20
max_retries = 1
max_attempts_per_task = 3
attempt_timeout_s = 1800
verification_timeout_s = 1800
# projects_root = "~/.config/master-system/projects"   # the default

[budget]                          # priced spend caps (USD)
daily_usd = 0.50
run_usd = 0.20

[daemon]                          # the background service (ms daemon)
interval_s = 300
ntfy_server = "https://ntfy.sh"   # the topic is in ~/.config/master-system/ntfy-topic

[planner]                         # ms chat: the planner model
provider = "deepseek"
model = "deepseek-v4-flash"
chat_usd = 0.30                   # cap per chat

[reviewer]                        # visual reviewer (vision model); provider = "none" to switch off
provider = "deepseek"
model = "deepseek-flash"
detail = "high"                   # or "low": 512 px images, cheaper

[images]                          # image generation; keys in master.env
providers = ["pollinations", "cloudflare"]

[github]                          # the GitHub App (docs/GITHUB-SETUP.md)
app_id = ""                       # its private key: ~/.config/master-system/github-app.pem

[prices]                          # USD per million tokens, by model name
"deepseek-v4-flash" = { input = 0.44, output = 1.32, cached_input = 0.0028 }
```

This module is part of the composition root, like ``reason_cli``: it may name
providers. Nothing in the orchestration layer reads it.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

__all__ = ["BudgetSettings", "ConfigError", "DaemonSettings", "GitHubSettings", "MasterConfig", "RunConfig",
           "RunSettings", "WorkerConfig",
           "default_config_path", "load_config"]

PROVIDERS = ("deepseek", "ollama", "opencode")


class ConfigError(ValueError):
    """The configuration file is unreadable or invalid."""


def default_config_path(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME")
    root = Path(base) if base and Path(base).is_absolute() else Path.home() / ".config"
    return root / "master-system" / "config.toml"


def _path(value) -> Path:
    return Path(os.path.expanduser(str(value)))


@dataclass(frozen=True)
class MasterConfig:
    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    base_url: Optional[str] = None
    timeout_s: float = 180


@dataclass(frozen=True)
class WorkerConfig:
    opencode_bin: Path = field(default_factory=lambda: _path("~/.opencode/bin/opencode"))
    model: Optional[str] = None
    home: Path = field(default_factory=lambda: _path("~/.local/share/master-system-worker"))
    extra_args: tuple = ()
    node_min_major: int = 22
    playwright_browsers_path: Optional[Path] = field(
        default_factory=lambda: _path("~/.cache/ms-playwright"))
    #: Worker tiers: name -> {"model": str|None, "home": Path}; empty = no tiers.
    profiles: Mapping[str, Mapping] = field(default_factory=dict)
    #: The escalation ladder, cheapest first (profile names).
    ladder: tuple = ()


@dataclass(frozen=True)
class RunSettings:
    max_steps: int = 20
    max_retries: int = 1
    max_attempts_per_task: int = 3
    attempt_timeout_s: float = 1800
    verification_timeout_s: float = 1800
    projects_root: Optional[Path] = None


@dataclass(frozen=True)
class BudgetSettings:
    #: Total priced spend (Master + planner + worker) allowed per local day.
    daily_usd: float = 0.50
    #: Master spend allowed per run.
    run_usd: float = 0.20


@dataclass(frozen=True)
class DaemonSettings:
    #: Seconds between the background service's cycles.
    interval_s: float = 300
    #: The ntfy server; the topic is in ~/.config/master-system/ntfy-topic.
    ntfy_server: str = "https://ntfy.sh"
    #: A notification after every run; off: one morning summary when the work runs out.
    batch_notifications: bool = False


@dataclass(frozen=True)
class PlannerSettings:
    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    base_url: Optional[str] = None
    timeout_s: float = 300
    #: Priced spend allowed per planner chat.
    chat_usd: float = 0.30
    #: Output tokens per planner reply (a whole epic with its tests is long).
    max_output_tokens: int = 16000


@dataclass(frozen=True)
class ReviewerSettings:
    """The visual reviewer's vision-capable model (core.visual_review)."""
    #: deepseek, or none to switch the reviewer off.
    provider: str = "deepseek"
    model: str = "deepseek-flash"
    base_url: Optional[str] = None
    timeout_s: float = 120
    #: Image detail sent to the model: low (512 px, cheaper) or high.
    detail: str = "high"


@dataclass(frozen=True)
class ImagesSettings:
    """Image generation for visual tasks (core.images); keys live in master.env."""
    #: Providers tried in order: pollinations, cloudflare.
    providers: tuple = ("pollinations", "cloudflare")
    pollinations_model: str = "flux"
    cloudflare_model: str = "@cf/black-forest-labs/flux-1-schnell"
    timeout_s: float = 120


@dataclass(frozen=True)
class GitHubSettings:
    #: The GitHub App's numeric id ("App ID" on its settings page); empty = not set up.
    app_id: str = ""
    #: The app's private key; default ~/.config/master-system/github-app.pem.
    key_path: str = ""


@dataclass(frozen=True)
class RunConfig:
    master: MasterConfig = field(default_factory=MasterConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    run: RunSettings = field(default_factory=RunSettings)
    budget: BudgetSettings = field(default_factory=BudgetSettings)
    daemon: DaemonSettings = field(default_factory=DaemonSettings)
    github: GitHubSettings = field(default_factory=GitHubSettings)
    planner: PlannerSettings = field(default_factory=PlannerSettings)
    reviewer: ReviewerSettings = field(default_factory=ReviewerSettings)
    images: ImagesSettings = field(default_factory=ImagesSettings)
    #: model name -> {"input": usd_per_m, "output": usd_per_m, "cached_input": usd_per_m}
    prices: Mapping[str, Mapping[str, float]] = field(default_factory=lambda: {
        "deepseek-v4-flash": {"input": 0.44, "output": 1.32, "cached_input": 0.0028},
        "deepseek-v4-pro": {"input": 1.32, "output": 3.96, "cached_input": 0.0084},
        # Peak prices (off-peak is half): the conservative figure for caps.
        "deepseek-flash": {"input": 0.30, "output": 1.20, "cached_input": 0.006},
    })
    source: Optional[Path] = None


def _section(data, name, allowed):
    section = data.get(name, {})
    if not isinstance(section, dict):
        raise ConfigError(f"[{name}] must be a table")
    unknown = sorted(set(section) - set(allowed))
    if unknown:
        raise ConfigError(f"[{name}] has unknown keys {unknown}")
    return section


def _positive(section, name, key, default, kind=float):
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"[{name}] {key} must be a positive number")
    return kind(value)


def load_config(path=None, env: Optional[Mapping[str, str]] = None) -> RunConfig:
    """Read the config file, or return the defaults if it does not exist."""
    path = Path(path) if path is not None else default_config_path(env)
    if not path.exists():
        return RunConfig()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"cannot read {path}: {error}") from error

    unknown = sorted(set(data) - {"master", "worker", "run", "prices", "budget", "daemon",
                                  "github", "planner"})
    if unknown:
        raise ConfigError(f"unknown sections {unknown} in {path}")

    m = _section(data, "master", ("provider", "model", "base_url", "timeout_s"))
    master = MasterConfig(
        provider=m.get("provider", MasterConfig.provider),
        model=m.get("model", MasterConfig.model),
        base_url=m.get("base_url"),
        timeout_s=_positive(m, "master", "timeout_s", MasterConfig.timeout_s),
    )
    if master.provider not in PROVIDERS:
        raise ConfigError(f"[master] provider must be one of {list(PROVIDERS)}")
    if not isinstance(master.model, str) or not master.model.strip():
        raise ConfigError("[master] model must be a non-empty string")

    w = _section(data, "worker", ("opencode_bin", "model", "home", "extra_args",
                                  "node_min_major", "playwright_browsers_path", "profiles",
                                  "ladder"))
    profiles = {}
    for name, value in (w.get("profiles") or {}).items():
        if not isinstance(value, dict) or set(value) - {"model", "home"}:
            raise ConfigError(f"[worker.profiles.{name}] takes only model and home")
        profiles[name] = {"model": value.get("model") or None,
                          "home": _path(value["home"]) if value.get("home") else None}
    ladder = tuple(w.get("ladder") or ())
    unknown_profiles = [name for name in ladder if name not in profiles]
    if unknown_profiles:
        raise ConfigError(f"[worker] ladder names unknown profiles {unknown_profiles}")
    defaults = WorkerConfig()
    extra_args = w.get("extra_args", [])
    if not isinstance(extra_args, list) or not all(isinstance(a, str) for a in extra_args):
        raise ConfigError("[worker] extra_args must be a list of strings")
    worker = WorkerConfig(
        opencode_bin=_path(w["opencode_bin"]) if "opencode_bin" in w else defaults.opencode_bin,
        model=w.get("model"),
        home=_path(w["home"]) if "home" in w else defaults.home,
        extra_args=tuple(extra_args),
        node_min_major=_positive(w, "worker", "node_min_major", 22, int),
        playwright_browsers_path=(
            _path(w["playwright_browsers_path"]) if "playwright_browsers_path" in w
            else defaults.playwright_browsers_path
        ),
        profiles=profiles,
        ladder=ladder,
    )

    r = _section(data, "run", ("max_steps", "max_retries", "max_attempts_per_task",
                               "attempt_timeout_s", "verification_timeout_s", "projects_root"))
    max_retries = r.get("max_retries", 1)
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ConfigError("[run] max_retries must be a non-negative integer")
    run = RunSettings(
        max_steps=_positive(r, "run", "max_steps", 20, int),
        max_retries=max_retries,
        max_attempts_per_task=_positive(r, "run", "max_attempts_per_task", 3, int),
        attempt_timeout_s=_positive(r, "run", "attempt_timeout_s", 1800),
        verification_timeout_s=_positive(r, "run", "verification_timeout_s", 1800),
        projects_root=_path(r["projects_root"]) if "projects_root" in r else None,
    )

    prices = dict(RunConfig().prices)
    for model, rates in data.get("prices", {}).items():
        if not isinstance(rates, dict) or not set(rates) <= {"input", "output", "cached_input"}:
            raise ConfigError(f"[prices] {model!r} must be a table of input/output/cached_input")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0
                   for v in rates.values()):
            raise ConfigError(f"[prices] {model!r} rates must be non-negative numbers")
        prices[model] = {k: float(v) for k, v in rates.items()}

    b = _section(data, "budget", ("daily_usd", "run_usd"))
    budget = BudgetSettings(
        daily_usd=_positive(b, "budget", "daily_usd", BudgetSettings.daily_usd),
        run_usd=_positive(b, "budget", "run_usd", BudgetSettings.run_usd),
    )
    d = _section(data, "daemon", ("interval_s", "ntfy_server", "batch_notifications"))
    batch = d.get("batch_notifications", DaemonSettings.batch_notifications)
    if not isinstance(batch, bool):
        raise ConfigError("[daemon] batch_notifications must be true or false")
    daemon = DaemonSettings(
        interval_s=_positive(d, "daemon", "interval_s", DaemonSettings.interval_s),
        ntfy_server=str(d.get("ntfy_server", DaemonSettings.ntfy_server)).rstrip("/"),
        batch_notifications=batch,
    )
    rv = _section(data, "reviewer", ("provider", "model", "base_url", "timeout_s", "detail"))
    if rv.get("provider", ReviewerSettings.provider) not in ("deepseek", "none"):
        raise ConfigError("[reviewer] provider must be deepseek or none")
    if rv.get("detail", ReviewerSettings.detail) not in ("low", "high"):
        raise ConfigError("[reviewer] detail must be low or high")
    reviewer = ReviewerSettings(
        provider=str(rv.get("provider", ReviewerSettings.provider)),
        model=str(rv.get("model", ReviewerSettings.model)),
        base_url=rv.get("base_url"),
        timeout_s=_positive(rv, "reviewer", "timeout_s", ReviewerSettings.timeout_s),
        detail=str(rv.get("detail", ReviewerSettings.detail)),
    )
    im = _section(data, "images", ("providers", "pollinations_model", "cloudflare_model",
                                   "timeout_s"))
    providers = tuple(im.get("providers", ImagesSettings.providers))
    if not set(providers) <= {"pollinations", "cloudflare"}:
        raise ConfigError("[images] providers may only list pollinations and cloudflare")
    images = ImagesSettings(
        providers=providers,
        pollinations_model=str(im.get("pollinations_model", ImagesSettings.pollinations_model)),
        cloudflare_model=str(im.get("cloudflare_model", ImagesSettings.cloudflare_model)),
        timeout_s=_positive(im, "images", "timeout_s", ImagesSettings.timeout_s),
    )

    g = _section(data, "github", ("app_id", "key_path"))
    github = GitHubSettings(app_id=str(g.get("app_id", "")).strip(),
                            key_path=str(g.get("key_path", "")).strip())

    pl = _section(data, "planner", ("provider", "model", "base_url", "timeout_s", "chat_usd",
                                    "max_output_tokens"))
    planner = PlannerSettings(
        provider=str(pl.get("provider", PlannerSettings.provider)),
        model=str(pl.get("model", PlannerSettings.model)),
        base_url=pl.get("base_url"),
        timeout_s=_positive(pl, "planner", "timeout_s", PlannerSettings.timeout_s),
        chat_usd=_positive(pl, "planner", "chat_usd", PlannerSettings.chat_usd),
        max_output_tokens=_positive(pl, "planner", "max_output_tokens",
                                    PlannerSettings.max_output_tokens, int),
    )

    return RunConfig(master=master, worker=worker, run=run, prices=prices,
                     budget=budget, daemon=daemon, github=github, planner=planner,
                     reviewer=reviewer, images=images, source=path)
