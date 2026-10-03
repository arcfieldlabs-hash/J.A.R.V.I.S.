"""Exercise CLI entry points without microphones, GUI actions, or an LLM server."""

from contextlib import ExitStack
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from jarvis import cli
from jarvis.memory import MemoryStore


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.workspace = base / "workspace"
        self.data_dir = base / "private-data"
        self.paths = ["--workspace", str(self.workspace), "--data-dir", str(self.data_dir)]
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("sys.stdout", self.stdout))
        self.stack.enter_context(patch("sys.stderr", self.stderr))

    def fake_runtime(self):
        """Replace services while leaving CLI argument handling and loops real."""
        self.client_factory = self.stack.enter_context(patch("jarvis.cli.OllamaClient"))
        self.toolkit_factory = self.stack.enter_context(patch("jarvis.cli.ToolKit"))
        self.assistant_factory = self.stack.enter_context(patch("jarvis.cli.JarvisAssistant"))
        self.monitor_factory = self.stack.enter_context(patch("jarvis.monitoring.SystemMonitor"))
        self.toolkit = self.toolkit_factory.return_value
        self.assistant = self.assistant_factory.return_value
        self.assistant.ask.return_value = "Your calendar is open, Sir."
        self.monitor = self.monitor_factory.return_value

    def assert_runtime_closed(self):
        self.monitor.stop.assert_called_once_with()
        self.toolkit.close.assert_called_once_with()

    def test_listen_speaks_and_finishes_cooldown_before_listening_again(self):
        self.fake_runtime()
        listener_factory = self.stack.enter_context(patch("jarvis.voice.VoiceListener"))
        listener = listener_factory.return_value
        speech = self.stack.enter_context(patch("jarvis.cli.speak"))
        events = []
        inputs = iter((None, "open calendar", KeyboardInterrupt()))

        def listen():
            events.append("listen")
            result = next(inputs)
            if isinstance(result, BaseException):
                raise result
            return result

        def answer(prompt):
            events.append(("ask", prompt))
            return "Your calendar is open, Sir."

        listener.listen.side_effect = listen
        self.assistant.ask.side_effect = answer
        speech.side_effect = lambda text, voice: events.append(("speak", text, voice))
        listener.suspend_after_speech.side_effect = lambda: events.append("cooldown")

        result = cli.main(self.paths + [
            "--listen", "--wake-word", "computer", "--stt-model", "tiny",
            "--voice", "Samantha",
        ])

        self.assertEqual(result, 0)
        listener_factory.assert_called_once_with(model="tiny", wake_word="computer")
        self.assertEqual(events, [
            "listen", "listen", ("ask", "open calendar"),
            ("speak", "Your calendar is open, Sir.", "Samantha"),
            "cooldown", "listen",
        ])
        self.assertIn("Voice input stopped", self.stdout.getvalue())
        self.monitor.start.assert_called_once_with()
        self.assert_runtime_closed()

    def test_listen_setup_failure_reports_the_error_and_releases_services(self):
        self.fake_runtime()
        listener_factory = self.stack.enter_context(patch("jarvis.voice.VoiceListener"))
        listener_factory.side_effect = RuntimeError("Microphone permission is required")

        self.assertEqual(cli.main(self.paths + ["--listen"]), 1)
        self.assertIn("Microphone permission is required", self.stderr.getvalue())
        self.assert_runtime_closed()

    def test_web_route_preserves_speech_settings_and_releases_services(self):
        self.fake_runtime()
        serve = self.stack.enter_context(patch("jarvis.web.serve", return_value=0))

        self.assertEqual(cli.main(self.paths + [
            "--web", "--port", "9123", "--speak", "--voice", "Samantha",
        ]), 0)

        serve.assert_called_once_with(
            self.assistant, self.toolkit, port=9123, speak_answers=True, voice="Samantha"
        )
        self.monitor.start.assert_not_called()
        self.assert_runtime_closed()

    def test_web_failure_reports_the_error_and_releases_services(self):
        self.fake_runtime()
        serve = self.stack.enter_context(patch("jarvis.web.serve"))
        serve.side_effect = OSError("Port is already in use")

        self.assertEqual(cli.main(self.paths + ["--web"]), 1)
        self.assertIn("Port is already in use", self.stderr.getvalue())
        self.assert_runtime_closed()

    def test_one_shot_model_failure_reports_the_error_and_releases_services(self):
        self.fake_runtime()
        self.assistant.ask.side_effect = RuntimeError("Ollama is unavailable")

        self.assertEqual(cli.main(self.paths + ["--once", "hello"]), 1)
        self.assertIn("Ollama is unavailable", self.stderr.getvalue())
        self.assert_runtime_closed()

    def test_invalid_and_conflicting_flags_fail_before_services_start(self):
        self.fake_runtime()
        cases = (
            (["--web", "--listen"], "Choose one"),
            (["--once", "hello", "--web"], "Choose one"),
            (["--once", "hello", "--menubar"], "Choose one"),
            (["--timeout", "0"], "positive --timeout"),
            (["--timeout", "-1"], "positive --timeout"),
            (["--num-ctx", "511"], "at least 512"),
            (["--port", "0"], "between 1 and 65535"),
            (["--port", "65536"], "between 1 and 65535"),
        )
        for flags, message in cases:
            with self.subTest(flags=flags):
                self.stderr.seek(0)
                self.stderr.truncate(0)
                self.assertEqual(cli.main(self.paths + flags), 2)
                self.assertIn(message, self.stderr.getvalue())
        self.client_factory.assert_not_called()
        self.toolkit_factory.assert_not_called()
        self.monitor_factory.assert_not_called()

    def test_doctor_reports_model_readiness_without_starting_services(self):
        self.fake_runtime()
        diagnose = self.stack.enter_context(patch("jarvis.diagnostics.diagnose"))
        for ready, installed, expected_status in (
            (True, True, 0), (False, True, 1), (True, False, 1),
        ):
            with self.subTest(ready=ready, installed=installed):
                report = {"ollama": {"ready": ready, "model_installed": installed}}
                diagnose.return_value = report
                self.stdout.seek(0)
                self.stdout.truncate(0)
                status = cli.main(self.paths + [
                    "--doctor", "--model", "test-model", "--ollama-url", "http://localhost:11435",
                ])
                self.assertEqual(status, expected_status)
                self.assertEqual(json.loads(self.stdout.getvalue()), report)
        diagnose.assert_called_with(
            ollama_url="http://localhost:11435", model="test-model",
            workspace=self.workspace.resolve(), data_dir=self.data_dir.resolve(),
        )
        self.client_factory.assert_not_called()
        self.toolkit_factory.assert_not_called()
        self.monitor_factory.assert_not_called()

    def test_one_shot_restores_facts_and_persists_history_across_invocations(self):
        store = MemoryStore(self.data_dir)
        store.remember("The preferred editor is Neovim.")
        client = types.SimpleNamespace(num_ctx=2048, chat=Mock(return_value='{"reply":"Neovim, Sir."}'))
        self.stack.enter_context(patch("jarvis.cli.OllamaClient", return_value=client))
        self.stack.enter_context(patch("jarvis.monitoring.SystemMonitor"))

        self.assertEqual(cli.main(self.paths + ["--once", "Which editor do I prefer?"]), 0)
        first_messages = client.chat.call_args.args[0]
        self.assertIn("The preferred editor is Neovim.", first_messages[0]["content"])
        self.assertEqual(store.recent_history(), [
            {"role": "user", "content": "Which editor do I prefer?"},
            {"role": "assistant", "content": "Neovim, Sir."},
        ])

        client.chat.return_value = '{"reply":"You asked about your editor, Sir."}'
        self.assertEqual(cli.main(self.paths + ["--once", "What did I just ask?"]), 0)
        second_messages = client.chat.call_args.args[0]
        self.assertIn({"role": "user", "content": "Which editor do I prefer?"}, second_messages)
        self.assertIn({"role": "assistant", "content": "Neovim, Sir."}, second_messages)
        self.assertEqual(store.recent_history()[-2:], [
            {"role": "user", "content": "What did I just ask?"},
            {"role": "assistant", "content": "You asked about your editor, Sir."},
        ])
        self.assertEqual(len(store.recall()), 1, "Conversation history must not become saved facts")


if __name__ == "__main__":
    unittest.main()
