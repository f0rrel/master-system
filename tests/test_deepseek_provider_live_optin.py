"""Optional live test for DeepSeek provider.

This test is opt-in and skipped unless DEEPSEEK_LIVE_TEST is set.
It requires a real DEEPSEEK_API_KEY and will make a real API call.
"""

import os
import sys
from pathlib import Path

import pytest

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.deepseek_provider import DeepSeekProvider
from core.provider import ProviderError


@pytest.mark.skipif(
    not os.environ.get("DEEPSEEK_LIVE_TEST"),
    reason="Set DEEPSEEK_LIVE_TEST=1 and DEEPSEEK_API_KEY to run live test",
)
def test_live_deepseek_completion():
    provider = DeepSeekProvider()
    try:
        result = provider.complete("Reply with exactly: OK")
        assert "OK" in result
    except ProviderError as e:
        # If API key is missing or other issue, still fail appropriately
        raise
