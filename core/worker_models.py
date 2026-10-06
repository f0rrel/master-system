"""The weekly check that the configured free worker models still exist and are still free.

Free models come and go. Every ``[worker] model_check_days`` (default 7) the service
asks OpenCode for its current model list (``opencode models <provider> --verbose
--refresh``, run in each profile's worker home) and checks every profile that is not
``paid``: the model is still listed, still active, and still costs nothing. Problems
are sent to the owner as one notification; the result is kept in
``<state dir>/worker-models.json``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

__all__ = ["parse_models", "FreeModelCheck"]

_HEADER = re.compile(r"^[\w.-]+/[\w.:@+-]+$")


def parse_models(output: str) -> dict:
    """``provider/model`` -> metadata, from `opencode models --verbose` output."""
    models, name, block = {}, None, []

    def flush():
        if name is not None:
            try:
                models[name] = json.loads("\n".join(block))
            except ValueError:
                models[name] = {}

    for line in (output or "").splitlines():
        if _HEADER.match(line.strip()) and not line.startswith((" ", "{", "}")):
            flush()
            name, block = line.strip(), []
        elif name is not None:
            block.append(line)
    flush()
    return models


def _is_free(meta: dict) -> bool:
    cost = meta.get("cost") or {}
    return all(not cost.get(key) for key in ("input", "output"))


class FreeModelCheck:
    """A service step: at most once per ``model_check_days``, check the free profiles."""

    def __init__(self, config, state_dir: Path, list_models: Callable, notifier,
                 now=lambda: datetime.now(timezone.utc)):
        self._config = config
        self._path = Path(state_dir) / "worker-models.json"
        #: (provider, home) -> `opencode models <provider> --verbose` output
        self._list = list_models
        self._notifier = notifier
        self._now = now

    def last(self) -> dict:
        try:
            return json.loads(self._path.read_text())
        except (OSError, ValueError):
            return {}

    def due(self) -> bool:
        checked = self.last().get("checked_at")
        days = self._config.worker.model_check_days
        return not checked or self._now() - datetime.fromisoformat(checked) >= timedelta(
            days=days)

    def __call__(self) -> list:
        if not self.due():
            return []
        problems, checked = self.check()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({"checked_at": self._now().isoformat(
            timespec="seconds"), "checked": checked, "problems": problems}, indent=2))
        if problems:
            self._notifier.send("Worker models need you", "\n".join(problems)
                                + "\nChange [worker] workers or the profiles in config.toml.",
                                tags="warning", priority="high")
        return [f"free worker models checked: {len(checked)} ok"
                if not problems else f"free worker models: {'; '.join(problems)}"]

    def check(self) -> tuple:
        """(problems, models checked) for every free profile with a model."""
        profiles = self._config.worker.profiles
        listings: dict = {}
        problems, checked = [], []
        for name, profile in profiles.items():
            model = profile.get("model")
            if profile.get("paid") or not model or "/" not in model:
                continue
            provider = model.split("/", 1)[0]
            home = profile.get("home") or self._config.worker.home
            key = (provider, str(home))
            if key not in listings:
                listings[key] = parse_models(self._list(provider, home) or "")
            meta = listings[key].get(model)
            label = profile.get("label", name)
            if not listings[key]:
                problems.append(f"{label}: could not list {provider} models")
            elif meta is None:
                problems.append(f"{label} ({model}) is no longer offered")
            elif str(meta.get("status") or "active") not in ("active", "beta", "alpha"):
                problems.append(f"{label} ({model}) is {meta.get('status')}")
            elif not _is_free(meta):
                problems.append(f"{label} ({model}) is no longer free")
            else:
                checked.append(model)
        return problems, checked


def opencode_lister(opencode_bin) -> Callable:
    """List a provider's models with the OpenCode CLI, in the given worker home."""
    from core.host import run_capture

    def list_models(provider: str, home) -> Optional[str]:
        env = {"HOME": str(home), "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}
        code, out = run_capture([str(opencode_bin), "models", provider, "--verbose",
                                 "--refresh"], env)
        return out if code == 0 else None

    return list_models
