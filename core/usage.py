"""Token and cost usage in one neutral shape, for accounting only.

Providers (the Master's model) and workers report usage in their own formats;
the adapters translate it into this shape. It is provenance: it is recorded in
history and summed by the report, and nothing ever branches on it.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional

__all__ = ["USAGE_KEYS", "add_usage", "from_counts", "make_usage"]

#: Token counts are integers; reported_cost_usd is what the service itself billed,
#: if it says (0 when unknown).
USAGE_KEYS = ("input_tokens", "cached_input_tokens", "cache_write_tokens",
              "output_tokens", "reasoning_tokens", "reported_cost_usd")


def _number(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return value


def make_usage(**values) -> dict:
    """A usage dict with every key present; unknown keys are refused."""
    unknown = set(values) - set(USAGE_KEYS)
    if unknown:
        raise ValueError(f"unknown usage keys {sorted(unknown)}")
    usage = {key: 0 for key in USAGE_KEYS}
    for key, value in values.items():
        usage[key] = _number(value)
    usage["reported_cost_usd"] = float(usage["reported_cost_usd"])
    return usage


def add_usage(items: Iterable[Optional[Mapping]]) -> dict:
    """Sum usage dicts (None and unknown keys are ignored)."""
    total = make_usage()
    for item in items:
        if not isinstance(item, Mapping):
            continue
        for key in USAGE_KEYS:
            total[key] += _number(item.get(key))
    total["reported_cost_usd"] = round(float(total["reported_cost_usd"]), 6)
    return total


def from_counts(input=0, output=0) -> dict:
    """Usage from plain prompt/completion counts (for backends that report only those)."""
    return make_usage(input_tokens=input, output_tokens=output)
