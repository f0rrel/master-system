import ast
import json
import urllib.error
from pathlib import Path

import pytest

from core.ollama_provider import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    OllamaProvider,
)
from core.provider import ProviderError, ReasoningProvider

PROVIDER_SOURCE_PATH = Path(__file__).parent.parent / "core" / "ollama_provider.py"


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def sent(monkeypatch):
    """Capture the request the adapter would send and reply with a canned body."""
    captured = {}

    def install(reply=None, error=None, raw=None):
        def fake_urlopen(request, timeout=None):
            captured["request"] = request
            captured["timeout"] = timeout
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["body"] = (
                json.loads(request.data.decode("utf-8"))
                if request.data
                else None
            )
            if error is not None:
                raise error
            if raw is not None:
                return FakeResponse(raw)
            return FakeResponse(json.dumps(reply).encode("utf-8"))

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        return captured

    return install


def chat_reply(content):
    return {"model": DEFAULT_MODEL, "message": {"role": "assistant", "content": content}}


# --- the provider contract ----------------------------------------------


def test_the_adapter_is_a_reasoning_provider():
    assert isinstance(OllamaProvider(), ReasoningProvider)


def test_the_provider_base_class_cannot_be_instantiated():
    with pytest.raises(TypeError):
        ReasoningProvider()


def test_the_adapter_has_a_readable_name():
    assert OllamaProvider().name == f"ollama:{DEFAULT_MODEL}"
    assert "qwen" in repr(OllamaProvider())


def test_the_name_follows_the_configured_model():
    assert OllamaProvider(model="gemma4:e2b").name == "ollama:gemma4:e2b"


# --- completing a prompt -------------------------------------------------


def test_the_adapter_posts_the_prompt_to_the_chat_endpoint(sent):
    captured = sent(reply=chat_reply("hello"))

    OllamaProvider().complete("say hello")

    assert captured["url"] == f"{DEFAULT_BASE_URL}/api/chat"
    assert captured["body"]["messages"] == [
        {"role": "user", "content": "say hello"}
    ]


def test_the_adapter_asks_for_the_configured_model(sent):
    captured = sent(reply=chat_reply("hello"))

    OllamaProvider(model="qwen3:8b").complete("hi")

    assert captured["body"]["model"] == "qwen3:8b"


def test_streaming_is_disabled_so_one_complete_reply_arrives(sent):
    captured = sent(reply=chat_reply("hello"))

    OllamaProvider().complete("hi")

    assert captured["body"]["stream"] is False


def test_generation_is_pinned_to_temperature_zero(sent):
    captured = sent(reply=chat_reply("hello"))

    OllamaProvider().complete("hi")

    assert captured["body"]["options"]["temperature"] == 0


def test_the_reply_text_is_returned(sent):
    sent(reply=chat_reply("the answer"))

    assert OllamaProvider().complete("hi") == "the answer"


def test_a_schema_is_passed_as_a_generation_constraint(sent):
    captured = sent(reply=chat_reply("{}"))
    schema = {"type": "object", "required": ["operations"]}

    OllamaProvider().complete("hi", schema=schema)

    assert captured["body"]["format"] == schema


def test_no_schema_is_sent_when_none_is_given(sent):
    captured = sent(reply=chat_reply("{}"))

    OllamaProvider().complete("hi")

    assert "format" not in captured["body"]


# --- configuration -------------------------------------------------------


def test_the_base_url_is_configurable(sent):
    captured = sent(reply=chat_reply("hello"))

    OllamaProvider(base_url="http://192.168.1.5:11434/").complete("hi")

    assert captured["url"] == "http://192.168.1.5:11434/api/chat"


def test_the_timeout_is_configurable(sent):
    captured = sent(reply=chat_reply("hello"))

    OllamaProvider(timeout=5).complete("hi")

    assert captured["timeout"] == 5


def test_the_default_is_a_local_unauthenticated_runtime():
    assert DEFAULT_BASE_URL.startswith("http://localhost")
    assert ":" in DEFAULT_MODEL


# --- listing and availability -------------------------------------------


def test_models_can_be_listed(sent):
    sent(reply={"models": [{"name": "qwen3:8b"}, {"name": "gemma4:e2b"}]})

    assert OllamaProvider().list_models() == ["qwen3:8b", "gemma4:e2b"]


def test_a_missing_model_list_is_an_error(sent):
    sent(reply={"something": "else"})

    with pytest.raises(ProviderError):
        OllamaProvider().list_models()


def test_availability_is_true_when_the_model_is_served(sent):
    sent(reply={"models": [{"name": DEFAULT_MODEL}]})

    assert OllamaProvider().is_available() is True


def test_availability_is_false_for_an_unserved_model(sent):
    sent(reply={"models": [{"name": "some-other-model"}]})

    assert OllamaProvider().is_available() is False


def test_availability_is_false_when_the_runtime_is_down(sent):
    sent(error=urllib.error.URLError("connection refused"))

    assert OllamaProvider().is_available() is False


def test_availability_does_not_raise_when_there_is_no_runtime(sent):
    sent(error=OSError("no route to host"))

    assert OllamaProvider().is_available() is False


# --- failures become ProviderError --------------------------------------


def test_an_unreachable_runtime_is_reported_clearly(sent):
    sent(error=urllib.error.URLError("connection refused"))

    with pytest.raises(ProviderError) as caught:
        OllamaProvider().complete("hi")

    assert "Cannot reach Ollama" in str(caught.value)


def test_an_http_error_is_reported_with_its_status(sent):
    sent(error=urllib.error.HTTPError("u", 404, "Not Found", {}, None))

    with pytest.raises(ProviderError) as caught:
        OllamaProvider().complete("hi")

    assert "404" in str(caught.value)


def test_a_socket_error_is_reported_clearly(sent):
    sent(error=OSError("connection reset"))

    with pytest.raises(ProviderError):
        OllamaProvider().complete("hi")


def test_a_non_json_reply_is_rejected(sent):
    sent(raw=b"<html>not json</html>")

    with pytest.raises(ProviderError) as caught:
        OllamaProvider().complete("hi")

    assert "non-JSON" in str(caught.value)


def test_a_reply_without_a_message_is_rejected(sent):
    sent(reply={"model": DEFAULT_MODEL})

    with pytest.raises(ProviderError):
        OllamaProvider().complete("hi")


def test_a_reply_whose_content_is_not_text_is_rejected(sent):
    sent(reply={"message": {"content": {"text": "structured"}}})

    with pytest.raises(ProviderError):
        OllamaProvider().complete("hi")


def test_an_empty_reply_is_returned_rather_than_invented(sent):
    sent(reply=chat_reply(""))

    assert OllamaProvider().complete("hi") == ""


def test_a_provider_error_is_never_a_project_error():
    """A dead runtime is a failed reasoning request, not a rejected operation."""
    assert not issubclass(ProviderError, ValueError)
    assert issubclass(ProviderError, RuntimeError)


# --- boundary: local, unauthenticated, no secrets -----------------------


def imported_modules(source_path):
    tree = ast.parse(Path(source_path).read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_the_adapter_adds_no_third_party_dependency():
    assert imported_modules(PROVIDER_SOURCE_PATH) == {
        "json",
        "sys",
        "urllib.error",
        "urllib.request",
        "pathlib",
        "core.provider",
        "core.usage",
    }


def test_the_adapter_reads_no_credentials():
    """No token, no environment variable, no config file, no key in code.

    Prose is exempt: the module docstring explains that it reads no token, and
    matching on documentation would be matching on the opposite of the claim.
    Only real code identifiers and string literals are checked.
    """
    tree = ast.parse(PROVIDER_SOURCE_PATH.read_text(encoding="utf-8"))

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            first = node.body[0] if node.body else None
            if (
                first is not None
                and isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
            ):
                docstrings.add(id(first.value))

    identifiers = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            identifiers.add(node.arg)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            identifiers.add(node.value)

    joined = " ".join(identifiers).lower()
    for word in ("token", "api_key", "apikey", "authorization", "bearer", "secret"):
        assert word not in joined


def test_the_adapter_sends_no_authorization_header(sent):
    captured = sent(reply=chat_reply("hello"))

    OllamaProvider().complete("hi")

    headers = {
        key.lower() for key in captured["request"].headers
    }
    assert "authorization" not in headers
    assert "x-api-key" not in headers


def test_the_adapter_never_reads_project_state():
    modules = imported_modules(PROVIDER_SOURCE_PATH)

    assert "yaml" not in modules
    assert "core.project_state" not in modules
    assert "core.work_manager" not in modules
    assert "core.project_manager" not in modules
    assert "core.master" not in modules
    assert "core.reasoning" not in modules


def test_the_adapter_cannot_run_commands():
    """json.loads is the permitted parse; anything that executes is banned."""
    forbidden_attributes = {
        "system",
        "popen",
        "run",
        "Popen",
        "call",
        "check_output",
        "spawn",
        "execv",
        "import_module",
    }
    forbidden_names = {
        "eval",
        "exec",
        "compile",
        "__import__",
        "input",
        "open",
        "getattr",
        "setattr",
    }
    tree = ast.parse(PROVIDER_SOURCE_PATH.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Attribute):
                assert function.attr not in forbidden_attributes
            elif isinstance(function, ast.Name):
                assert function.id not in forbidden_names


def test_the_only_json_calls_are_dumps_and_loads():
    tree = ast.parse(PROVIDER_SOURCE_PATH.read_text(encoding="utf-8"))
    used = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "json"
    }

    assert used == {"dumps", "loads"}


def test_the_adapter_has_no_bare_except():
    tree = ast.parse(PROVIDER_SOURCE_PATH.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            assert not (
                node.type is None or getattr(node.type, "id", None) == "Exception"
            )