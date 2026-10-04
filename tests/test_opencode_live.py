"""Live checks against a real OpenCode server.

Every other test in this suite mocks the network. These do not, so they are not
run by default: they need a running OpenCode server, a configured model, and the
provider's own quota. They exist so that the adapter can be verified against the
real API, not only against a script of what this repository believes the API
does.

Opt in explicitly::

    opencode serve --port 4096
    OPENCODE_LIVE=1 python -m pytest tests/test_opencode_live.py -v

Nothing is skipped quietly. With ``OPENCODE_LIVE=1`` set, a server that is not
running is a failure with instructions, not a skip: a live run that quietly
skipped every test would look exactly like a live run that passed.

Settings come from the environment so that a machine with a different model, a
different provider or a password-protected server can still run them:

``OPENCODE_LIVE``        set to anything to run these tests at all
``OPENCODE_BASE_URL``    server address, default ``http://127.0.0.1:4096``
``OPENCODE_PROVIDER_ID`` provider half of the model name, default ``opencode``
``OPENCODE_MODEL_ID``    model half of the model name, default ``big-pickle``
``OPENCODE_TOTAL_TIMEOUT`` seconds allowed for one reply, default ``120``

The prompts here are trivial on purpose. This is a test of the transport, not of
the model.
"""

import os

import pytest

from core.opencode_provider import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL_ID,
    DEFAULT_PROVIDER_ID,
    DEFAULT_TOTAL_TIMEOUT,
    OpenCodeProvider,
)

pytestmark = pytest.mark.integration

LIVE = os.environ.get("OPENCODE_LIVE") == "1"


def settings():
    return {
        "base_url": os.environ.get("OPENCODE_BASE_URL", DEFAULT_BASE_URL),
        "provider_id": os.environ.get("OPENCODE_PROVIDER_ID", DEFAULT_PROVIDER_ID),
        "model_id": os.environ.get("OPENCODE_MODEL_ID", DEFAULT_MODEL_ID),
        "total_timeout": float(
            os.environ.get("OPENCODE_TOTAL_TIMEOUT", DEFAULT_TOTAL_TIMEOUT)
        ),
    }


@pytest.fixture
def live():
    if not LIVE:
        pytest.skip("set OPENCODE_LIVE=1 to run the live OpenCode tests")
    return OpenCodeProvider(**settings())


def test_the_server_serves_the_configured_model(live):
    assert live.is_available(), (
        f"{live.name} is not served by {live.base_url}. "
        "Start OpenCode with 'opencode serve --port 4096', or set "
        "OPENCODE_BASE_URL, OPENCODE_PROVIDER_ID and OPENCODE_MODEL_ID."
    )


def test_the_configured_model_is_in_the_served_list(live):
    assert live.name.split(":", 1)[1] in live.list_models()


def test_a_prompt_comes_back_as_text(live):
    reply = live.complete("Reply with the single word: ready")

    assert isinstance(reply, str)
    assert "ready" in reply.lower()


def test_a_second_turn_also_returns_text(live):
    """Each ``complete`` opens its own session, so a second turn is a real request."""
    assert live.complete("Reply with the single word: ready")
    assert live.complete("Reply with the single word: ready")


def test_a_prompt_with_awkward_characters_survives_the_round_trip(live):
    """Quotation marks and non-ASCII text are JSON-encoded on the way out.

    A prompt is arbitrary user text, and the interesting failure would be a
    transport error on a quote rather than a wrong answer from the model.
    """
    reply = live.complete('Reply with exactly this: quote" and \'apostrophe — ünï')

    assert 'quote"' in reply
    assert "apostrophe" in reply