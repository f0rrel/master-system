import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import main  # noqa: E402


def fake_response(payload=None, text="", http_error=None):
    response = mock.Mock()
    response.text = text
    if http_error is not None:
        response.raise_for_status.side_effect = http_error
    else:
        response.raise_for_status.return_value = None
    if isinstance(payload, Exception):
        response.json.side_effect = payload
    else:
        response.json.return_value = payload
    return response


def ok(content):
    return fake_response({"message": {"role": "assistant", "content": content}})


class TempHistoryCase(unittest.TestCase):
    """Redirects persistence at a throwaway directory for every test."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        self.history_path = self.data_dir / "conversation.json"
        for attribute, value in (
            ("HISTORY_PATH", self.history_path),
            ("DATA_DIR", self.data_dir),
        ):
            patcher = mock.patch.object(main, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_history(self, text):
        self.history_path.write_text(text, encoding="utf-8")

    def read_history(self):
        return json.loads(self.history_path.read_text(encoding="utf-8"))


class ChatTests(unittest.TestCase):
    def test_returns_assistant_content(self):
        with mock.patch.object(main.requests, "post", return_value=ok("Привет!")) as post:
            self.assertEqual(main.chat([{"role": "user", "content": "hi"}]), "Привет!")

        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["json"]["model"], main.MODEL)
        self.assertFalse(kwargs["json"]["stream"])
        self.assertEqual(kwargs["timeout"], main.TIMEOUT)

    def test_sends_full_history_in_order(self):
        messages = [
            {"role": "system", "content": main.SYSTEM_PROMPT},
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ]
        with mock.patch.object(main.requests, "post", return_value=ok("ok")) as post:
            main.chat(messages)
        self.assertEqual(post.call_args.kwargs["json"]["messages"], messages)

    def test_connection_error_is_wrapped(self):
        err = requests.exceptions.ConnectionError("refused")
        with mock.patch.object(main.requests, "post", side_effect=err):
            with self.assertRaises(main.OllamaError) as ctx:
                main.chat([])
        self.assertIn("ollama serve", str(ctx.exception))

    def test_timeout_is_wrapped(self):
        with mock.patch.object(
            main.requests, "post", side_effect=requests.exceptions.Timeout("slow")
        ):
            with self.assertRaises(main.OllamaError) as ctx:
                main.chat([])
        self.assertIn(str(main.TIMEOUT), str(ctx.exception))

    def test_missing_model_reports_pull_hint(self):
        http_error = requests.exceptions.HTTPError(response=mock.Mock(status_code=404))
        with mock.patch.object(
            main.requests, "post", return_value=fake_response(http_error=http_error)
        ):
            with self.assertRaises(main.OllamaError) as ctx:
                main.chat([])
        self.assertIn(f"ollama pull {main.MODEL}", str(ctx.exception))

    def test_other_http_error_reports_status(self):
        http_error = requests.exceptions.HTTPError(response=mock.Mock(status_code=500))
        with mock.patch.object(
            main.requests, "post", return_value=fake_response(http_error=http_error)
        ):
            with self.assertRaises(main.OllamaError) as ctx:
                main.chat([])
        self.assertIn("500", str(ctx.exception))

    def test_generic_request_error_is_wrapped(self):
        with mock.patch.object(
            main.requests, "post", side_effect=requests.exceptions.RequestException("boom")
        ):
            with self.assertRaises(main.OllamaError):
                main.chat([])

    def test_non_json_body_is_reported(self):
        with mock.patch.object(
            main.requests, "post", return_value=fake_response(ValueError("nope"), text="<html>")
        ):
            with self.assertRaises(main.OllamaError) as ctx:
                main.chat([])
        self.assertIn("not valid JSON", str(ctx.exception))

    def test_non_object_payload_is_reported(self):
        with mock.patch.object(main.requests, "post", return_value=fake_response(["a", "b"])):
            with self.assertRaises(main.OllamaError) as ctx:
                main.chat([])
        self.assertIn("expected a JSON object", str(ctx.exception))

    def test_missing_message_key_is_reported(self):
        with mock.patch.object(main.requests, "post", return_value=fake_response({"done": True})):
            with self.assertRaises(main.OllamaError) as ctx:
                main.chat([])
        self.assertIn("no 'message' object", str(ctx.exception))

    def test_non_string_content_is_reported(self):
        payload = {"message": {"content": {"text": "hi"}}}
        with mock.patch.object(main.requests, "post", return_value=fake_response(payload)):
            with self.assertRaises(main.OllamaError) as ctx:
                main.chat([])
        self.assertIn("not a string", str(ctx.exception))

    def test_empty_content_is_reported(self):
        with mock.patch.object(
            main.requests, "post", return_value=fake_response({"message": {"content": "  "}})
        ):
            with self.assertRaises(main.OllamaError) as ctx:
                main.chat([])
        self.assertIn("empty", str(ctx.exception))


class ConversationTests(TempHistoryCase):
    def run_loop(self, inputs, replies):
        out, err = io.StringIO(), io.StringIO()
        post = mock.Mock(side_effect=[ok(r) for r in replies])
        with mock.patch.object(main.requests, "post", post), mock.patch(
            "builtins.input", side_effect=inputs
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main.run_conversation()
        return code, out.getvalue(), err.getvalue(), post

    def test_multi_turn_history_accumulates(self):
        code, out, _, post = self.run_loop(
            ["Привет", "Как дела?", "/quit"], ["Привет!", "Хорошо."]
        )

        self.assertEqual(code, 0)
        self.assertIn("Sergei: Привет!", out)
        self.assertIn("Sergei: Хорошо.", out)

        first = post.call_args_list[0].kwargs["json"]["messages"]
        second = post.call_args_list[1].kwargs["json"]["messages"]

        self.assertEqual([m["role"] for m in first], ["system", "user"])
        self.assertEqual(first[0]["content"], main.SYSTEM_PROMPT)
        self.assertEqual(first[1]["content"], "Привет")

        self.assertEqual(
            [m["role"] for m in second], ["system", "user", "assistant", "user"]
        )
        self.assertEqual(second[2], {"role": "assistant", "content": "Привет!"})
        self.assertEqual(second[3], {"role": "user", "content": "Как дела?"})

    def test_quit_command_makes_no_request(self):
        code, _, _, post = self.run_loop(["/quit"], [])
        self.assertEqual(code, 0)
        post.assert_not_called()

    def test_eof_exits_cleanly(self):
        out = io.StringIO()
        with mock.patch.object(main.requests, "post"), mock.patch(
            "builtins.input", side_effect=EOFError
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main.run_conversation()
        self.assertEqual(code, 0)
        self.assertIn("До свидания!", out.getvalue())

    def test_ctrl_c_exits_cleanly(self):
        out = io.StringIO()
        with mock.patch.object(main.requests, "post"), mock.patch(
            "builtins.input", side_effect=KeyboardInterrupt
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main.run_conversation()
        self.assertEqual(code, 0)

    def test_blank_input_is_ignored(self):
        _, _, _, post = self.run_loop(["", "   ", "/quit"], [])
        post.assert_not_called()

    def test_reset_keeps_persona_and_drops_history(self):
        _, out, _, post = self.run_loop(
            ["Привет", "/reset", "Пока", "/quit"], ["Привет!", "Пока!"]
        )

        self.assertIn("Контекст очищен", out)
        after_reset = post.call_args_list[1].kwargs["json"]["messages"]
        self.assertEqual([m["role"] for m in after_reset], ["system", "user"])
        self.assertEqual(after_reset[0]["content"], main.SYSTEM_PROMPT)
        self.assertEqual(after_reset[1]["content"], "Пока")

    def test_help_makes_no_request(self):
        _, out, _, post = self.run_loop(["/help", "/quit"], [])
        self.assertIn("/reset", out)
        post.assert_not_called()

    def test_failed_turn_is_dropped_and_loop_continues(self):
        out, err = io.StringIO(), io.StringIO()
        post = mock.Mock(
            side_effect=[
                requests.exceptions.ConnectionError("refused"),
                ok("Привет!"),
            ]
        )
        with mock.patch.object(main.requests, "post", post), mock.patch(
            "builtins.input", side_effect=["первый", "второй", "/quit"]
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main.run_conversation()

        self.assertEqual(code, 0)
        self.assertIn("[error]", err.getvalue())
        self.assertIn("Sergei: Привет!", out.getvalue())

        retry = post.call_args_list[1].kwargs["json"]["messages"]
        self.assertEqual([m["role"] for m in retry], ["system", "user"])
        self.assertEqual(retry[1]["content"], "второй")


class LoadHistoryTests(TempHistoryCase):
    def test_missing_file_starts_fresh_without_warning(self):
        self.assertEqual(main.load_history(), ([], None))

    def test_empty_file_warns_and_starts_fresh(self):
        self.write_history("")
        turns, warning = main.load_history()
        self.assertEqual(turns, [])
        self.assertIn("empty", warning)

    def test_whitespace_only_file_warns(self):
        self.write_history("   \n\t ")
        turns, warning = main.load_history()
        self.assertEqual(turns, [])
        self.assertIn("empty", warning)

    def test_malformed_json_warns(self):
        self.write_history("{not json,,,")
        turns, warning = main.load_history()
        self.assertEqual(turns, [])
        self.assertIn("not valid JSON", warning)

    def test_json_null_warns(self):
        self.write_history("null")
        turns, warning = main.load_history()
        self.assertEqual(turns, [])
        self.assertIn("list of messages", warning)

    def test_json_object_instead_of_list_warns(self):
        self.write_history('{"messages": []}')
        turns, warning = main.load_history()
        self.assertEqual(turns, [])
        self.assertIn("list of messages", warning)

    def test_valid_history_is_returned(self):
        self.write_history(
            json.dumps(
                [
                    {"role": "user", "content": "Привет"},
                    {"role": "assistant", "content": "Привет!"},
                ],
                ensure_ascii=False,
            )
        )
        turns, warning = main.load_history()
        self.assertIsNone(warning)
        self.assertEqual(
            turns,
            [
                {"role": "user", "content": "Привет"},
                {"role": "assistant", "content": "Привет!"},
            ],
        )

    def test_system_entries_are_not_loaded_from_disk(self):
        self.write_history(
            json.dumps([{"role": "system", "content": "tampered persona"}])
        )
        turns, warning = main.load_history()
        self.assertEqual(turns, [])
        self.assertIn("ignored 1 invalid message", warning)

    def test_invalid_entries_are_skipped_and_valid_ones_kept(self):
        self.write_history(
            json.dumps(
                [
                    {"role": "user", "content": "ok"},
                    {"role": "tool", "content": "nope"},
                    {"role": "assistant", "content": 42},
                    {"role": "assistant", "content": "  "},
                    "not even a dict",
                    {"role": "assistant", "content": "ok too"},
                ]
            )
        )
        turns, warning = main.load_history()
        self.assertEqual(
            turns,
            [
                {"role": "user", "content": "ok"},
                {"role": "assistant", "content": "ok too"},
            ],
        )
        self.assertIn("ignored 4 invalid message(s)", warning)

    def test_unreadable_file_warns_without_raising(self):
        self.history_path.mkdir()
        turns, warning = main.load_history()
        self.assertEqual(turns, [])
        self.assertIn("could not read", warning)

    def test_system_prompt_is_never_loaded_from_disk(self):
        self.write_history(
            json.dumps([{"role": "user", "content": "hi"}], ensure_ascii=False)
        )
        with mock.patch.object(main.requests, "post", return_value=ok("ok")) as post:
            with mock.patch("builtins.input", side_effect=["ещё", "/quit"]):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                    io.StringIO()
                ):
                    main.run_conversation()
        sent = post.call_args_list[0].kwargs["json"]["messages"]
        self.assertEqual(sent[0], {"role": "system", "content": main.SYSTEM_PROMPT})


class SaveHistoryTests(TempHistoryCase):
    def test_round_trip(self):
        turns = [
            {"role": "user", "content": "Привет"},
            {"role": "assistant", "content": "Привет!"},
        ]
        self.assertIsNone(main.save_history(turns))
        self.assertEqual(self.read_history(), turns)

    def test_cyrillic_is_written_readably(self):
        main.save_history([{"role": "user", "content": "Привет"}])
        self.assertIn("Привет", self.history_path.read_text(encoding="utf-8"))

    def test_creates_data_directory_when_absent(self):
        target = self.data_dir / "nested" / "deeper"
        with mock.patch.object(main, "DATA_DIR", target), mock.patch.object(
            main, "HISTORY_PATH", target / "conversation.json"
        ):
            main.save_history([{"role": "user", "content": "hi"}])
        self.assertTrue((target / "conversation.json").exists())

    def test_leaves_no_temporary_file_behind(self):
        main.save_history([{"role": "user", "content": "hi"}])
        leftovers = [p.name for p in self.data_dir.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_write_failure_warns_without_raising(self):
        with mock.patch.object(Path, "write_text", side_effect=OSError("disk full")):
            warning = main.save_history([{"role": "user", "content": "hi"}])
        self.assertIn("could not save conversation", warning)

    def test_write_failure_leaves_previous_history_intact(self):
        main.save_history([{"role": "user", "content": "original"}])
        with mock.patch.object(Path, "write_text", side_effect=OSError("disk full")):
            main.save_history([{"role": "user", "content": "new"}])
        self.assertEqual(self.read_history(), [{"role": "user", "content": "original"}])


class ClearHistoryTests(TempHistoryCase):
    def test_deletes_existing_file(self):
        main.save_history([{"role": "user", "content": "hi"}])
        self.assertIsNone(main.clear_history())
        self.assertFalse(self.history_path.exists())

    def test_missing_file_is_not_an_error(self):
        self.assertIsNone(main.clear_history())

    def test_delete_failure_warns_without_raising(self):
        main.save_history([{"role": "user", "content": "hi"}])
        with mock.patch.object(Path, "unlink", side_effect=OSError("locked")):
            warning = main.clear_history()
        self.assertIn("could not delete", warning)
        self.assertTrue(self.history_path.exists())


class PersistenceInLoopTests(TempHistoryCase):
    def run_loop(self, inputs, replies):
        out, err = io.StringIO(), io.StringIO()
        post = mock.Mock(side_effect=[ok(r) for r in replies])
        with mock.patch.object(main.requests, "post", post), mock.patch(
            "builtins.input", side_effect=inputs
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main.run_conversation()
        return code, out.getvalue(), err.getvalue(), post

    def test_conversation_is_written_after_each_turn(self):
        self.run_loop(["Привет", "/quit"], ["Привет!"])
        self.assertEqual(
            self.read_history(),
            [
                {"role": "user", "content": "Привет"},
                {"role": "assistant", "content": "Привет!"},
            ],
        )

    def test_history_accumulates_across_turns(self):
        self.run_loop(["раз", "два", "/quit"], ["один", "три"])
        self.assertEqual(
            self.read_history(),
            [
                {"role": "user", "content": "раз"},
                {"role": "assistant", "content": "один"},
                {"role": "user", "content": "два"},
                {"role": "assistant", "content": "три"},
            ],
        )

    def test_system_prompt_is_not_persisted(self):
        self.run_loop(["Привет", "/quit"], ["Привет!"])
        self.assertNotIn("system", [m["role"] for m in self.read_history()])
        self.assertNotIn(
            "Russian language companion", self.history_path.read_text(encoding="utf-8")
        )

    def test_previous_session_is_resumed(self):
        self.write_history(
            json.dumps(
                [
                    {"role": "user", "content": "Меня зовут Bartosz"},
                    {"role": "assistant", "content": "Приятно познакомиться!"},
                ],
                ensure_ascii=False,
            )
        )
        _, out, _, post = self.run_loop(["А ты кто?", "/quit"], ["Я Сергей."])

        sent = post.call_args_list[0].kwargs["json"]["messages"]
        self.assertEqual(
            [m["role"] for m in sent],
            ["system", "user", "assistant", "user"],
        )
        self.assertEqual(sent[0]["content"], main.SYSTEM_PROMPT)
        self.assertEqual(sent[1]["content"], "Меня зовут Bartosz")
        self.assertEqual(sent[2]["content"], "Приятно познакомиться!")
        self.assertIn("Resumed 2 earlier message(s)", out)

    def test_resume_announces_only_when_history_exists(self):
        _, out, _, _ = self.run_loop(["/quit"], [])
        self.assertNotIn("Resumed", out)

    def test_failed_turn_is_not_persisted(self):
        out, err = io.StringIO(), io.StringIO()
        post = mock.Mock(
            side_effect=[requests.exceptions.ConnectionError("refused"), ok("Привет!")]
        )
        with mock.patch.object(main.requests, "post", post), mock.patch(
            "builtins.input", side_effect=["первый", "второй", "/quit"]
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            main.run_conversation()
        self.assertEqual(
            self.read_history(),
            [
                {"role": "user", "content": "второй"},
                {"role": "assistant", "content": "Привет!"},
            ],
        )

    def test_reset_clears_memory_and_file(self):
        self.run_loop(["Привет", "/reset", "/quit"], ["Привет!"])
        self.assertFalse(self.history_path.exists())

    def test_reset_makes_next_start_fresh(self):
        self.run_loop(["Привет", "/reset", "Пока", "/quit"], ["Привет!", "Пока!"])
        self.assertEqual(
            self.read_history(),
            [
                {"role": "user", "content": "Пока"},
                {"role": "assistant", "content": "Пока!"},
            ],
        )

    def test_reset_on_missing_file_is_quiet(self):
        code, out, err, _ = self.run_loop(["/reset", "/quit"], [])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Контекст очищен", out)

    def test_reset_warns_when_file_cannot_be_deleted(self):
        main.save_history([{"role": "user", "content": "hi"}])
        _, out, err, _ = self.run_loop(["/reset", "/quit"], [])
        self.assertIn("Контекст очищен", out)

    def test_malformed_history_still_allows_conversation(self):
        self.write_history("}}} broken {{{")
        code, out, err, post = self.run_loop(["Привет", "/quit"], ["Привет!"])

        self.assertEqual(code, 0)
        self.assertIn("not valid JSON", err)
        self.assertIn("Sergei: Привет!", out)
        sent = post.call_args_list[0].kwargs["json"]["messages"]
        self.assertEqual([m["role"] for m in sent], ["system", "user"])
        self.assertEqual(
            self.read_history(),
            [
                {"role": "user", "content": "Привет"},
                {"role": "assistant", "content": "Привет!"},
            ],
        )

    def test_save_failure_warns_but_conversation_continues(self):
        with mock.patch.object(main, "save_history", return_value="could not save: boom"):
            code, out, err, _ = self.run_loop(["Привет", "/quit"], ["Привет!"])
        self.assertEqual(code, 0)
        self.assertIn("could not save: boom", err)
        self.assertIn("Sergei: Привет!", out)


if __name__ == "__main__":
    unittest.main()