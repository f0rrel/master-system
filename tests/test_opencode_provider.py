import ast
import json
import urllib.error
from pathlib import Path

import pytest

from core import opencode_provider
from core.opencode_provider import (
    DEFAULT_AGENT,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL_ID,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PROVIDER_ID,
    OpenCodeProvider,
)
from core.provider import ProviderError, ReasoningProvider

PROVIDER_SOURCE_PATH = Path(__file__).parent.parent / "core" / "opencode_provider.py"

SESSION_ID = "ses_test0000000000000000000"
SUBMIT_PATH = f"/session/{SESSION_ID}/prompt_async"
MESSAGES_PATH = f"/session/{SESSION_ID}/message"
ABORT_PATH = f"/session/{SESSION_ID}/abort"
MODEL_NAME = f"{DEFAULT_PROVIDER_ID}/{DEFAULT_MODEL_ID}"


class FakeResponse:
    def __init__(self, payload=b""):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def json_response(payload):
    return FakeResponse(json.dumps(payload).encode("utf-8"))


def user_message(text="a prompt"):
    return {
        "info": {"id": "msg_user", "role": "user", "time": {"created": 1}},
        "parts": [{"id": "prt_user", "type": "text", "text": text}],
    }


def assistant_message(text="the answer", completed=True, error=None, parts=None):
    info = {"id": "msg_assistant", "role": "assistant", "time": {"created": 2}}
    if completed:
        info["time"]["completed"] = 3
    if error is not None:
        info["error"] = error
    if parts is None:
        parts = [{"id": "prt_text", "type": "text", "text": text}]
    return {"info": info, "parts": parts}


def tool_part(tool="bash"):
    return {
        "id": "prt_tool",
        "type": "tool",
        "tool": tool,
        "state": {"status": "completed", "output": "leaked output"},
    }


def finished(text="the answer"):
    """What a healthy server reaches: our prompt, then a finished answer."""
    return [user_message(), assistant_message(text)]


def path_of(url):
    return "/" + url.split("://", 1)[-1].split("/", 1)[-1].split("?")[0]


@pytest.fixture
def server(monkeypatch):
    """Install a scripted stand-in for a running OpenCode server.

    ``messages`` is a list of replies to the polling endpoint, consumed in order
    with the last one repeating, so a test can describe a model that takes two
    polls to answer. ``raw`` and ``payloads`` override any single endpoint, for
    describing a server that answers with something this contract does not allow.
    """
    recorded = {
        "requests": [],
        "session": {"id": SESSION_ID, "title": "reasoning request"},
        "messages": [],
        "providers": {
            "all": [{"id": DEFAULT_PROVIDER_ID, "models": {DEFAULT_MODEL_ID: {}}}]
        },
        "raw": {},
        "payloads": {},
        "errors": {},
        "polls": 0,
    }

    def reply_for(path, method):
        if path in recorded["raw"]:
            return FakeResponse(recorded["raw"][path])
        if path in recorded["payloads"]:
            return json_response(recorded["payloads"][path])
        if method == "POST" and path == "/session":
            return json_response(recorded["session"])
        if path.endswith("/prompt_async"):
            return FakeResponse(b"")
        if method == "GET" and path.endswith("/message"):
            replies = recorded["messages"] or [finished()]
            index = min(recorded["polls"], len(replies) - 1)
            recorded["polls"] += 1
            return json_response(replies[index])
        if method == "GET" and path == "/provider":
            return json_response(recorded["providers"])
        return FakeResponse(b"")

    def fake_urlopen(request, timeout=None):
        recorded["requests"].append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "timeout": timeout,
                "body": json.loads(request.data.decode("utf-8"))
                if request.data
                else None,
                "headers": {
                    key.lower(): value
                    for key, value in request.header_items()
                },
            }
        )
        method = request.get_method()
        path = path_of(request.full_url)
        failure = recorded["errors"].get(path, recorded["errors"].get("*"))
        if failure is not None:
            raise failure
        return reply_for(path, method)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return recorded


@pytest.fixture
def clock(monkeypatch):
    """A clock the tests own, so waiting is instant and a deadline is exact.

    ``monotonic`` stands still until a poll asks to wait, and waiting is what moves
    it. A budget is therefore consumed one poll interval at a time, with no
    dependence on how fast the machine running the tests happens to be.
    """
    recorded = {"now": 0.0, "delays": []}

    def sleep(seconds):
        recorded["delays"].append(seconds)
        recorded["now"] += seconds

    monkeypatch.setattr(opencode_provider.time, "sleep", sleep)
    monkeypatch.setattr(
        opencode_provider.time,
        "monotonic",
        lambda: recorded["now"],
    )
    return recorded


def provider(**overrides):
    settings = {"poll_interval": 0}
    settings.update(overrides)
    return OpenCodeProvider(**settings)


def sent_to(recorded, path):
    """Every recorded request for exactly one endpoint."""
    return [
        entry for entry in recorded["requests"] if path_of(entry["url"]) == path
    ]


# --- the provider contract ----------------------------------------------


def test_the_adapter_is_a_reasoning_provider():
    assert isinstance(OpenCodeProvider(), ReasoningProvider)


def test_the_adapter_has_a_readable_name():
    assert OpenCodeProvider().name == f"opencode:{MODEL_NAME}"
    assert "big-pickle" in repr(OpenCodeProvider())


def test_the_name_follows_the_configured_model():
    assert (
        OpenCodeProvider(provider_id="acme", model_id="small").name
        == "opencode:acme/small"
    )


# --- a successful request -----------------------------------------------


def test_the_reply_text_is_returned(server):
    server["messages"] = [finished("the answer")]

    assert provider().complete("say hello") == "the answer"


def test_a_session_is_opened_for_the_request(server):
    provider().complete("say hello")

    opened = sent_to(server, "/session")
    assert len(opened) == 1
    assert opened[0]["method"] == "POST"
    assert opened[0]["url"] == f"{DEFAULT_BASE_URL}/session"


def test_the_prompt_is_sent_to_the_session(server):
    provider().complete("plan a task")

    submitted = sent_to(server, SUBMIT_PATH)[0]
    assert submitted["body"]["parts"] == [{"type": "text", "text": "plan a task"}]


def test_the_prompt_is_submitted_asynchronously(server):
    """The synchronous streaming endpoint has no deadline, so it is not used."""
    provider().complete("say hello")

    endpoints = {
        (entry["method"], entry["url"].rsplit("/", 1)[-1])
        for entry in server["requests"]
    }
    assert ("POST", "prompt_async") in endpoints
    assert ("POST", "message") not in endpoints


def test_a_submission_that_answers_with_nothing_is_accepted(server):
    """prompt_async replies 204 No Content: that is success, not a missing body."""
    server["messages"] = [finished()]

    assert provider().complete("say hello") == "the answer"


def test_a_submission_is_not_asked_to_do_anything_else(server):
    provider().complete("say hello")

    assert sent_to(server, SUBMIT_PATH)[0]["body"] == {
        "model": {
            "providerID": DEFAULT_PROVIDER_ID,
            "modelID": DEFAULT_MODEL_ID,
        },
        "agent": DEFAULT_AGENT,
        "parts": [{"type": "text", "text": "say hello"}],
    }


# --- polling for completion ---------------------------------------------


def test_the_session_is_polled_until_the_model_finishes(server):
    server["messages"] = [
        [user_message()],
        [user_message(), assistant_message(completed=False)],
        finished("late answer"),
    ]

    assert provider().complete("say hello") == "late answer"
    assert server["polls"] == 3


def test_polling_stops_as_soon_as_the_answer_arrives(server):
    server["messages"] = [finished("first try")]

    provider().complete("say hello")

    assert server["polls"] == 1


def test_every_poll_reads_the_same_session(server):
    provider().complete("say hello")

    polls = sent_to(server, MESSAGES_PATH)
    assert polls
    expected = f"{DEFAULT_BASE_URL}/session/{SESSION_ID}/message"
    assert all(entry["url"] == expected for entry in polls)


def test_polling_waits_between_attempts(server, clock):
    server["messages"] = [[user_message()], finished()]

    provider(poll_interval=DEFAULT_POLL_INTERVAL).complete("say hello")

    assert clock["delays"] == [DEFAULT_POLL_INTERVAL]


def test_a_model_with_no_budget_times_out_rather_than_polling_forever(server, clock):
    server["messages"] = [[user_message()]]

    with pytest.raises(ProviderError):
        provider(total_timeout=0).complete("say hello")

    assert server["polls"] == 1


# --- timeout handling ---------------------------------------------------


def test_the_timeout_names_the_model_and_the_budget(server, clock):
    server["messages"] = [[user_message()]]

    with pytest.raises(ProviderError) as caught:
        provider(
            poll_interval=DEFAULT_POLL_INTERVAL, total_timeout=7
        ).complete("say hello")

    assert MODEL_NAME in str(caught.value)
    assert "7" in str(caught.value)


def test_a_timed_out_session_is_stopped(server, clock):
    server["messages"] = [[user_message()]]

    with pytest.raises(ProviderError):
        provider(total_timeout=0).complete("say hello")

    assert sent_to(server, ABORT_PATH)


def test_stopping_a_timed_out_session_cannot_replace_the_error(server, clock):
    """The timeout is the real failure; a server that cannot be told to stop is
    not news, and must not become the message a person reads."""
    server["messages"] = [[user_message()]]
    server["errors"][ABORT_PATH] = urllib.error.URLError("gone")

    with pytest.raises(ProviderError) as caught:
        provider(total_timeout=0).complete("say hello")

    assert "did not finish" in str(caught.value)


def test_a_timeout_is_a_failed_request_not_a_rejected_operation(server, clock):
    server["messages"] = [[user_message()]]

    with pytest.raises(ProviderError):
        provider(total_timeout=0).complete("say hello")

    assert not issubclass(ProviderError, ValueError)


# --- extracting the reply ------------------------------------------------


def test_text_from_several_assistant_messages_is_joined(server):
    server["messages"] = [
        [user_message(), assistant_message("first"), assistant_message("second")]
    ]

    assert provider().complete("say hello") == "first\nsecond"


def test_tool_output_is_never_returned(server):
    server["messages"] = [
        [
            user_message(),
            assistant_message(
                parts=[tool_part(), {"type": "text", "text": "only this"}]
            ),
        ]
    ]

    assert provider().complete("say hello") == "only this"


def test_an_answer_made_only_of_tool_calls_is_not_usable_text(server):
    server["messages"] = [[user_message(), assistant_message(parts=[tool_part()])]]

    with pytest.raises(ProviderError) as caught:
        provider().complete("say hello")

    assert "no assistant text" in str(caught.value)


def test_an_incomplete_answer_is_not_returned_early(server):
    server["messages"] = [
        [
            user_message(),
            assistant_message("half a thought", completed=False),
            assistant_message("the finished answer"),
        ]
    ]

    assert provider().complete("say hello") == "the finished answer"


def test_a_completed_session_with_no_text_is_an_error(server):
    server["messages"] = [[user_message(), assistant_message(text="")]]

    with pytest.raises(ProviderError) as caught:
        provider().complete("say hello")

    assert "no assistant text" in str(caught.value)


def test_an_answer_of_the_wrong_type_is_ignored(server):
    server["messages"] = [
        [
            user_message(),
            assistant_message(
                parts=[
                    {"type": "text", "text": {"nested": "object"}},
                    {"type": "text", "text": "   "},
                ]
            ),
        ]
    ]

    with pytest.raises(ProviderError):
        provider().complete("say hello")


def test_the_user_prompt_is_never_echoed_back_as_the_answer(server):
    server["messages"] = [[user_message("say hello"), assistant_message("hi")]]

    assert provider().complete("say hello") == "hi"


# --- a failed model call ------------------------------------------------


def test_an_assistant_error_is_reported_with_its_cause(server):
    server["messages"] = [
        [
            user_message(),
            assistant_message(
                error={
                    "name": "APIError",
                    "data": {
                        "message": "Error from provider: model is overloaded",
                        "statusCode": 503,
                    },
                },
            ),
        ]
    ]

    with pytest.raises(ProviderError) as caught:
        provider().complete("say hello")

    message = str(caught.value)
    assert "APIError" in message
    assert "503" in message
    assert "model is overloaded" in message
    assert MODEL_NAME in message


def test_an_error_without_details_is_still_reported(server):
    server["messages"] = [
        [user_message(), assistant_message(error={"name": "AbortedError"})]
    ]

    with pytest.raises(ProviderError) as caught:
        provider().complete("say hello")

    assert "AbortedError" in str(caught.value)


# --- HTTP and malformed replies -----------------------------------------


def test_an_unreachable_server_is_reported_clearly(server):
    server["errors"]["*"] = urllib.error.URLError("connection refused")

    with pytest.raises(ProviderError) as caught:
        provider().complete("say hello")

    assert "Cannot reach OpenCode" in str(caught.value)
    assert "opencode serve" in str(caught.value)


def test_a_socket_error_is_reported_clearly(server):
    server["errors"]["*"] = OSError("connection reset")

    with pytest.raises(ProviderError):
        provider().complete("say hello")


def test_an_http_error_on_opening_a_session_is_reported(server):
    server["errors"]["/session"] = urllib.error.HTTPError("u", 500, "Boom", {}, None)

    with pytest.raises(ProviderError) as caught:
        provider().complete("say hello")

    assert "500" in str(caught.value)


def test_an_http_error_on_submitting_is_reported(server):
    path = f"/session/{SESSION_ID}/prompt_async"
    server["errors"][path] = urllib.error.HTTPError("u", 404, "Not Found", {}, None)

    with pytest.raises(ProviderError) as caught:
        provider().complete("say hello")

    assert "404" in str(caught.value)


def test_an_http_error_while_polling_is_reported(server):
    path = f"/session/{SESSION_ID}/message"
    server["errors"][path] = urllib.error.HTTPError("u", 400, "Bad Request", {}, None)

    with pytest.raises(ProviderError) as caught:
        provider().complete("say hello")

    assert "400" in str(caught.value)


def test_a_non_json_reply_is_rejected(server):
    server["raw"]["/session"] = b"<html>not json</html>"

    with pytest.raises(ProviderError) as caught:
        provider().complete("say hello")

    assert "non-JSON" in str(caught.value)


def test_a_reply_that_is_not_a_session_is_rejected(server):
    server["payloads"]["/session"] = [{"id": "not a session"}]

    with pytest.raises(ProviderError) as caught:
        provider().complete("say hello")

    assert "session id" in str(caught.value)


def test_a_session_reply_without_an_id_is_rejected(server):
    server["session"] = {"title": "reasoning request"}

    with pytest.raises(ProviderError) as caught:
        provider().complete("say hello")

    assert "session id" in str(caught.value)


def test_a_message_list_of_the_wrong_shape_is_rejected(server):
    server["payloads"][f"/session/{SESSION_ID}/message"] = {"messages": []}

    with pytest.raises(ProviderError) as caught:
        provider().complete("say hello")

    assert "message list" in str(caught.value)


def test_a_message_without_an_envelope_is_rejected(server):
    server["messages"] = [[{"role": "assistant"}]]

    with pytest.raises(ProviderError) as caught:
        provider().complete("say hello")

    assert "malformed message" in str(caught.value)


def test_an_empty_message_list_waits_rather_than_pretending(server, clock):
    server["messages"] = [[]]

    with pytest.raises(ProviderError) as caught:
        provider(total_timeout=0).complete("say hello")

    assert "did not finish" in str(caught.value)


# --- configuration ------------------------------------------------------


def test_the_base_url_is_configurable(server):
    OpenCodeProvider(base_url="http://192.168.1.5:4096/", poll_interval=0).complete(
        "say hello"
    )

    assert server["requests"][0]["url"] == "http://192.168.1.5:4096/session"


def test_the_base_url_is_stripped_of_a_trailing_slash():
    configured = OpenCodeProvider(base_url="http://127.0.0.1:4096/")

    assert configured.base_url == "http://127.0.0.1:4096"


def test_the_provider_and_the_model_are_chosen_independently(server):
    configured = OpenCodeProvider(
        provider_id="acme", model_id="small-1", poll_interval=0
    )

    configured.complete("say hello")

    assert sent_to(server, "/session")[0]["body"]["model"] == {
        "id": "small-1",
        "providerID": "acme",
    }
    assert sent_to(server, SUBMIT_PATH)[0]["body"]["model"] == {
        "providerID": "acme",
        "modelID": "small-1",
    }


def test_the_agent_is_named_on_the_session_and_the_prompt(server):
    OpenCodeProvider(agent="plan", poll_interval=0).complete("say hello")

    assert sent_to(server, "/session")[0]["body"]["agent"] == "plan"
    assert sent_to(server, SUBMIT_PATH)[0]["body"]["agent"] == "plan"


def test_no_agent_can_be_named_at_all(server):
    OpenCodeProvider(agent=None, poll_interval=0).complete("say hello")

    assert "agent" not in sent_to(server, "/session")[0]["body"]
    assert "agent" not in sent_to(server, SUBMIT_PATH)[0]["body"]


def test_each_request_carries_the_configured_timeout(server):
    OpenCodeProvider(timeout=5, poll_interval=0).complete("say hello")

    assert [entry["timeout"] for entry in server["requests"]] == [5, 5, 5]


def test_the_default_is_a_local_opencode_server():
    assert DEFAULT_BASE_URL == "http://127.0.0.1:4096"
    assert DEFAULT_PROVIDER_ID
    assert DEFAULT_MODEL_ID


def test_the_default_agent_is_the_one_that_cannot_edit():
    """The model here only ever plans, so the agent that cannot edit is used."""
    assert DEFAULT_AGENT == "plan"


def test_an_empty_provider_or_model_is_refused():
    with pytest.raises(ValueError):
        OpenCodeProvider(provider_id="")
    with pytest.raises(ValueError):
        OpenCodeProvider(model_id="   ")


def test_a_negative_poll_interval_is_refused():
    with pytest.raises(ValueError):
        OpenCodeProvider(poll_interval=-1)


# --- listing and availability -------------------------------------------


def test_models_can_be_listed(server):
    server["providers"] = {
        "all": [
            {"id": "acme", "models": {"small": {}, "large": {}}},
            {"id": "other", "models": []},
            {"id": "empty", "models": None},
            "nonsense",
        ]
    }

    assert OpenCodeProvider(poll_interval=0).list_models() == [
        "acme/large",
        "acme/small",
    ]


def test_a_missing_provider_list_is_an_error(server):
    server["providers"] = {"connected": True}

    with pytest.raises(ProviderError):
        OpenCodeProvider(poll_interval=0).list_models()


def test_availability_is_true_when_the_model_is_served(server):
    assert OpenCodeProvider(poll_interval=0).is_available() is True


def test_availability_is_false_for_an_unserved_model(server):
    unserved = OpenCodeProvider(model_id="absent", poll_interval=0)

    assert unserved.is_available() is False


def test_availability_is_false_when_the_server_is_down(server):
    server["errors"]["*"] = urllib.error.URLError("connection refused")

    assert OpenCodeProvider(poll_interval=0).is_available() is False


def test_availability_does_not_raise_when_there_is_no_server(server):
    server["errors"]["*"] = OSError("no route to host")

    assert OpenCodeProvider(poll_interval=0).is_available() is False


# --- boundary: text only, local, no credentials -------------------------


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
        "time",
        "urllib.error",
        "urllib.request",
        "pathlib",
        "core.provider",
        "core.usage",
    }


def test_the_adapter_never_reads_project_state():
    modules = imported_modules(PROVIDER_SOURCE_PATH)

    for forbidden in (
        "yaml",
        "core.project_state",
        "core.work_manager",
        "core.project_manager",
        "core.master",
        "core.reasoning",
        "core.reasoning_engine",
    ):
        assert forbidden not in modules


def test_the_adapter_cannot_run_commands():
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
    forbidden_names = {"eval", "exec", "compile", "__import__", "input", "open"}
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


def test_the_adapter_sends_no_authorization_header(server):
    provider().complete("say hello")

    for entry in server["requests"]:
        assert "authorization" not in entry["headers"]
        assert "x-api-key" not in entry["headers"]


def test_the_adapter_never_configures_opencode_tools(server):
    """Tool execution is not part of this adapter's authority model."""
    provider().complete("say hello")

    for entry in sent_to(server, SUBMIT_PATH):
        assert "tools" not in entry["body"]
        assert "permission" not in entry["body"]


def test_a_schema_hint_is_accepted_and_changes_nothing(server):
    schema = {"type": "object", "required": ["operations"]}

    without = provider().complete("say hello")
    with_schema = provider().complete("say hello", schema=schema)

    assert with_schema == without
    assert "format" not in sent_to(server, SUBMIT_PATH)[0]["body"]

# --- token usage (accounting) -------------------------------------------------


def test_usage_is_summed_from_the_assistant_messages(server):
    answer = assistant_message("the answer")
    answer["info"]["tokens"] = {"input": 900, "output": 40, "reasoning": 10,
                                "cache": {"read": 2048, "write": 0}}
    answer["info"]["cost"] = 0.0021
    server["messages"] = [[user_message(), answer]]
    adapter = provider()

    adapter.complete("a prompt")

    assert adapter.last_usage == {"input_tokens": 900, "cached_input_tokens": 2048,
                                  "cache_write_tokens": 0, "output_tokens": 40,
                                  "reasoning_tokens": 10, "reported_cost_usd": 0.0021}
