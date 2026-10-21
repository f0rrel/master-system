"""core.redaction: secrets are removed from program output before it is stored or shown."""

import pytest

from core.redaction import redact, redact_json, redact_output

# Built from parts so that no real-looking credential sits in the source.
TOKENS = {
    "github app": "ghs_" + "A1b2C3d4E5f6G7h8I9j0",
    "github pat": "github_pat_" + "11ABCDEFG0abcdefghij_klmnopqrstuvwxyz",
    "api key": "sk-" + "abcdef1234567890abcd",
    "aws id": "AKIA" + "ABCDEFGHIJKLMNOP",
    "slack": "xoxb-" + "1234567890-abcdefghij",
    "jwt": "eyJ" + "hbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
    "bearer": "Bearer " + "abcdEFGH1234567890abcdEFGH",
}


@pytest.mark.parametrize("name", sorted(TOKENS))
def test_secret_shaped_strings_are_removed_from_output(name):
    secret = TOKENS[name]
    out = redact_output(f"fetching with {secret} failed\nretrying")
    assert secret.split()[-1] not in out
    assert "<redacted>" in out and "retrying" in out


def test_private_keys_are_removed_whole():
    key = ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\nQQ==\n"
           "-----END OPENSSH PRIVATE KEY-----")
    out = redact_output(f"before\n{key}\nafter")
    assert "b3BlbnNz" not in out and "before" in out and "after" in out


def test_environment_style_assignments_are_blanked():
    out = redact_output("DEEPSEEK_API_KEY=hunter2hunter2\nexport GH_TOKEN: 'abcd1234'\n"
                        'MY_PASSWORD="correct horse"\nPATH=/usr/bin')
    assert "hunter2" not in out and "abcd1234" not in out and "correct" not in out
    assert "PATH=/usr/bin" in out
    assert "DEEPSEEK_API_KEY=<redacted>" in out


def test_url_credentials_and_authorization_headers_are_removed():
    out = redact_output("git clone https://bot:s3cretpass@github.com/x/y.git\n"
                        "Authorization: Basic dXNlcjpwYXNz\nhost: example.com")
    assert "s3cretpass" not in out and "dXNlcjpwYXNz" not in out
    assert "https://<redacted>@github.com/x/y.git" in out and "host: example.com" in out


def test_ordinary_test_output_is_left_readable():
    text = ('    if key == "a":\n        monkey = 3\nE   AssertionError: expected 3 stars\n'
            "tokens = 5 passed in 0.2s")
    assert redact_output(text) == text


def test_known_secret_values_are_removed_wherever_they_are():
    assert "mysecretvalue" not in redact_output("x mysecretvalue y", secrets=["mysecretvalue"])
    # Too short to be a safe replacement target: ignored rather than mangling the text.
    assert redact_output("a b c", secrets=["a"]) == "a b c"


@pytest.mark.parametrize("value", [None, 42, b"bytes", ["a"], {"a": 1}, ""])
def test_redact_output_passes_non_text_through(value):
    assert redact_output(value) == value


def test_redact_json_walks_nested_structures_and_keeps_the_rest():
    secret = TOKENS["api key"]
    data = {"stderr_tail": f"boom {secret}", "n": 3, "ok": True, "none": None,
            "list": [f"{secret}", 1, {"deep": f"x {secret}"}], "tuple": (f"{secret}",)}
    out = redact_json(data)
    assert secret not in repr(out)
    assert out["n"] == 3 and out["ok"] is True and out["none"] is None
    assert data["stderr_tail"].endswith(secret)  # the input is not modified


def test_redact_still_blanks_secret_named_config_lines_for_diagnostics():
    assert redact("DEEPSEEK_API_KEY = abc123\nmodel = flash") == (
        "DEEPSEEK_API_KEY = <redacted>\nmodel = flash")
    from core.ms import redact as ms_redact
    assert ms_redact is redact
