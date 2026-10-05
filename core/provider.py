"""The model-provider boundary: one abstract way to ask a model for text.

This module deliberately contains no model, no HTTP client, and no provider
name. It states the smallest contract that any reasoning backend has to
satisfy, so that swapping the backend never reaches Master or
ReasoningInterface:

    ReasoningEngine
          |
          v
    ReasoningProvider        <- this module, nothing provider-specific
          ^
          |
    OllamaProvider / DeepSeekProvider / OpenAIProvider / ...

Two decisions are worth defending.

The contract returns text, not actions
-------------------------------------
A provider is handed a prompt and returns a string. It is not given the
operation allowlist, a Master, a ReasoningInterface, or any way to run
anything. A provider therefore cannot cause a state change even if it is
hostile or simply wrong, because the only thing it can do is produce characters
that the reasoning engine will then parse as data. Making the boundary
"propose operations" rather than "return text" would hand every future provider
the power to name operations, which is exactly the authority this design keeps
in one place.

The optional schema is a hint, not a contract
---------------------------------------------
Some backends can constrain generation to a JSON schema; others cannot. Passing
the schema lets an adapter use that capability when it has one, while an
adapter that ignores it still works, with the engine's own parsing and
validation guaranteeing correctness either way. Correctness never depends on
the model cooperating.

Nothing here reads or writes project state. Providers are stateless with respect
to the work system: they cannot see it except through a prompt the engine
builds, and they cannot change it at all.
"""

import sys
from abc import ABC, abstractmethod
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

__all__ = ["ProviderError", "ReasoningProvider"]


class ProviderError(RuntimeError):
    """Raised when a reasoning backend cannot be reached or gives no usable text.

    This is a failed reasoning request, not a rejected operation. It means no
    proposal was produced, so there is nothing to approve and nothing that could
    have changed project state.
    """


class ReasoningProvider(ABC):
    """A source of text completions. Deliberately the smallest useful surface.

    Subclasses implement :meth:`complete` and nothing else is required. The
    engine is responsible for building the prompt, parsing the reply, and
    validating every proposed operation; a provider participates in none of
    that.
    """

    #: Short human-readable identifier, used in output and errors.
    name = "provider"

    #: Token usage of the most recent ``complete`` call, in the shape of
    #: :func:`core.usage.make_usage`, or None when the backend does not report
    #: it. Set even when the reply turns out to be unusable, because the call
    #: was still paid for. Accounting only; never interpreted.
    last_usage = None

    @abstractmethod
    def complete(self, prompt, schema=None):
        """Return the model's reply to prompt as text.

        schema, when given, is a JSON schema the reply is expected to satisfy.
        It is an advisory capability hint: an implementation may use it to
        constrain generation, and an implementation that cannot may ignore it.
        The engine does not trust the reply either way.
        """

    def __repr__(self):
        return f"<{type(self).__name__} {self.name}>"