"""End-to-end tests that run the real application in separate processes.

These are the tests that actually prove history survives a restart: nothing is
mocked inside Sergei, each run is a fresh interpreter that re-imports the
module and reads from disk. A stub Ollama server stands in for the real one, so
no Ollama installation is required.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main  # noqa: E402


class StubOllama(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).received.append(body)
        last_user = [m for m in body["messages"] if m["role"] == "user"][-1]
        payload = json.dumps(
            {
                "message": {
                    "role": "assistant",
                    "content": f"ответ на {last_user['content']}",
                }
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class RestartTests(unittest.TestCase):
    def setUp(self):
        StubOllama.received = []

        self.server = HTTPServer(("127.0.0.1", 0), StubOllama)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        self.history_path = self.data_dir / "conversation.json"

    def run_sergei(self, stdin_text):
        env = dict(os.environ)
        env.update(
            {
                "OLLAMA_URL": f"http://127.0.0.1:{self.port}/api/chat",
                "OLLAMA_MODEL": "stub-model",
                "SERGEI_DATA_DIR": str(self.data_dir),
                "PYTHONIOENCODING": "utf-8",
            }
        )
        return subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "src" / "main.py")],
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            cwd=str(PROJECT_ROOT),
            env=env,
        )

    def read_history(self):
        return json.loads(self.history_path.read_text(encoding="utf-8"))

    def test_history_survives_restart(self):
        first = self.run_sergei("первая сессия\n/quit\n")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(
            self.read_history(),
            [
                {"role": "user", "content": "первая сессия"},
                {"role": "assistant", "content": "ответ на первая сессия"},
            ],
        )

        second = self.run_sergei("вторая сессия\n/quit\n")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("Resumed 2 earlier message(s)", second.stdout)

        sent = StubOllama.received[-1]["messages"]
        self.assertEqual(
            sent,
            [
                {"role": "system", "content": main.SYSTEM_PROMPT},
                {"role": "user", "content": "первая сессия"},
                {"role": "assistant", "content": "ответ на первая сессия"},
                {"role": "user", "content": "вторая сессия"},
            ],
        )
        self.assertEqual(
            self.read_history(),
            [
                {"role": "user", "content": "первая сессия"},
                {"role": "assistant", "content": "ответ на первая сессия"},
                {"role": "user", "content": "вторая сессия"},
                {"role": "assistant", "content": "ответ на вторая сессия"},
            ],
        )

    def test_persona_is_rebuilt_from_code_not_disk(self):
        self.history_path.write_text(
            json.dumps([{"role": "system", "content": "podrobno nie"}], ensure_ascii=False),
            encoding="utf-8",
        )
        self.run_sergei("привет\n/quit\n")
        sent = StubOllama.received[-1]["messages"]
        self.assertEqual(sent[0], {"role": "system", "content": main.SYSTEM_PROMPT})

    def test_system_prompt_is_never_written_to_disk(self):
        self.run_sergei("привет\n/quit\n")
        raw = self.history_path.read_text(encoding="utf-8")
        self.assertNotIn("system", raw)
        self.assertNotIn("Russian language companion", raw)

    def test_missing_file_starts_a_fresh_conversation(self):
        result = self.run_sergei("привет\n/quit\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Resumed", result.stdout)
        self.assertEqual([m["role"] for m in StubOllama.received[-1]["messages"]], ["system", "user"])

    def test_quitting_without_talking_creates_no_file(self):
        result = self.run_sergei("/quit\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.history_path.exists())
        self.assertEqual(StubOllama.received, [])

    def test_reset_clears_persisted_history_for_later_runs(self):
        self.run_sergei("первая сессия\n/quit\n")
        self.assertTrue(self.history_path.exists())

        second = self.run_sergei("/reset\n/quit\n")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("Контекст очищен", second.stdout)
        self.assertFalse(self.history_path.exists())

        third = self.run_sergei("новая сессия\n/quit\n")
        self.assertNotIn("Resumed", third.stdout)
        self.assertEqual(
            [m["role"] for m in StubOllama.received[-1]["messages"]], ["system", "user"]
        )

    def test_malformed_file_is_survived_and_replaced(self):
        self.history_path.write_text("{{{ not json", encoding="utf-8")
        result = self.run_sergei("привет\n/quit\n")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not valid JSON", result.stderr)
        self.assertEqual(
            self.read_history(),
            [
                {"role": "user", "content": "привет"},
                {"role": "assistant", "content": "ответ на привет"},
            ],
        )

    def test_empty_file_is_survived(self):
        self.history_path.write_text("", encoding="utf-8")
        result = self.run_sergei("привет\n/quit\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("empty", result.stderr)
        self.assertEqual(len(self.read_history()), 2)

    def test_history_of_wrong_shape_is_survived(self):
        self.history_path.write_text('{"messages": []}', encoding="utf-8")
        result = self.run_sergei("привет\n/quit\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("list of messages", result.stderr)
        self.assertEqual(len(self.read_history()), 2)

    def test_invalid_entries_are_dropped_and_valid_ones_kept(self):
        self.history_path.write_text(
            json.dumps(
                [
                    {"role": "user", "content": "старый"},
                    {"role": "assistant", "content": 7},
                    {"role": "assistant", "content": "старый ответ"},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        result = self.run_sergei("новый\n/quit\n")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ignored 1 invalid message(s)", result.stderr)
        self.assertEqual(
            [m["content"] for m in StubOllama.received[-1]["messages"][1:]],
            ["старый", "старый ответ", "новый"],
        )

    def test_unwritable_data_dir_does_not_break_the_conversation(self):
        self.assertEqual(self.run_sergei("привет\n/quit\n").returncode, 0)
        self.assertTrue(self.history_path.exists())

        env = dict(os.environ)
        env.update(
            {
                "OLLAMA_URL": f"http://127.0.0.1:{self.port}/api/chat",
                "OLLAMA_MODEL": "stub-model",
                "SERGEI_DATA_DIR": str(PROJECT_ROOT / "src" / "main.py"),
                "PYTHONIOENCODING": "utf-8",
            }
        )
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "src" / "main.py")],
            input="привет\n/quit\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            cwd=str(PROJECT_ROOT),
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ответ на привет", result.stdout)
        self.assertIn("could not save conversation", result.stderr)


if __name__ == "__main__":
    unittest.main()