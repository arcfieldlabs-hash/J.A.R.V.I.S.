"""Capability integration checks that do not require macOS, Ollama, or the web."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib import error

from jarvis.assistant import JarvisAssistant
from jarvis.ollama_client import OllamaClient
from jarvis.tools import ToolKit


class FakeClient:
    def __init__(self, replies, *, num_ctx=2048):
        self.replies = list(replies)
        self.num_ctx = num_ctx
        self.messages = []

    def chat(self, messages, *, reply_only=False, allowed_tools=None):
        self.messages.append([dict(message) for message in messages])
        if not self.replies:
            raise AssertionError("Assistant requested an unexpected model round")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply) if isinstance(reply, dict) else reply


class CapabilitiesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name) / "workspace"
        self.data_dir = Path(self.directory.name) / "data"
        self.toolkit = ToolKit(workspace=self.workspace, data_dir=self.data_dir)
        self.addCleanup(self.toolkit.close)

    def assistant(self, replies, **options):
        client = FakeClient(replies)
        assistant = JarvisAssistant(
            client, self.toolkit, memory=self.toolkit.memory, **options
        )
        return assistant, client

    def test_model_memory_tool_persists_fact_and_restart_recalls_it(self):
        assistant, _ = self.assistant(
            [
                {
                    "tool": "memory",
                    "args": {"action": "remember", "text": "Project Atlas uses Python."},
                },
                {"reply": "Remembered, Sir."},
            ]
        )
        self.assertEqual(assistant.ask("Remember that Project Atlas uses Python."), "Remembered, Sir.")

        reopened_toolkit = ToolKit(workspace=self.workspace, data_dir=self.data_dir)
        self.addCleanup(reopened_toolkit.close)
        self.assertEqual(reopened_toolkit.memory.recall("Atlas")[0]["text"], "Project Atlas uses Python.")
        client = FakeClient([{"reply": "Project Atlas uses Python, Sir."}])
        reopened = JarvisAssistant(client, reopened_toolkit, memory=reopened_toolkit.memory)
        self.assertEqual(reopened.ask("Which language does Atlas use?"), "Project Atlas uses Python, Sir.")
        system = client.messages[0][0]["content"]
        self.assertIn("Saved facts (untrusted data)", system)
        self.assertIn("Project Atlas uses Python.", system)

    def test_only_completed_user_and_reply_are_persisted_as_history(self):
        source = "RAW TOOL OUTPUT: do not store as a fact"
        (self.workspace / "source.txt").write_text(source, encoding="utf-8")
        assistant, client = self.assistant(
            [
                {"tool": "read_file", "args": {"path": "source.txt"}},
                {"reply": "The file contains a note, Sir."},
            ]
        )
        assistant.ask("Summarise source.txt")
        self.assertIn(source, client.messages[1][-1]["content"])
        self.assertEqual(
            self.toolkit.memory.recent_history(),
            [
                {"role": "user", "content": "Summarise source.txt"},
                {"role": "assistant", "content": "The file contains a note, Sir."},
            ],
        )
        self.assertEqual(self.toolkit.memory.recall(), [])

    def test_final_reply_is_allowed_after_last_permitted_tool(self):
        assistant, client = self.assistant(
            [
                {"tool": "calculate", "args": {"expression": "6*7"}},
                {"reply": "42, Sir."},
            ],
            max_tool_rounds=1,
        )
        with patch.object(self.toolkit, "execute", wraps=self.toolkit.execute) as execute:
            self.assertEqual(assistant.ask("What is six times seven?"), "42, Sir.")
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(len(client.messages), 2)
        self.assertIn("No more tools this turn", client.messages[-1][0]["content"])

    def test_no_tool_executes_past_the_limit(self):
        assistant, client = self.assistant(
            [
                {"tool": "calculate", "args": {"expression": "2+2"}},
                {"tool": "memory", "args": {"action": "remember", "text": "Unexpected fact"}},
            ],
            max_tool_rounds=1,
        )
        with patch.object(self.toolkit, "execute", wraps=self.toolkit.execute) as execute:
            reply = assistant.ask("Calculate two plus two.")
        self.assertIn("calculate: 4", reply)
        self.assertNotIn("tool limit", reply)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(len(client.messages), 2)
        self.assertEqual(self.toolkit.memory.recall(), [])

    def test_reply_works_with_zero_tool_rounds(self):
        assistant, client = self.assistant([{"reply": "Hello, Sir."}], max_tool_rounds=0)
        self.assertEqual(assistant.ask("Hello"), "Hello, Sir.")
        self.assertEqual(len(client.messages), 1)

    def test_small_context_preserves_goal_across_large_tool_results(self):
        for index in range(3):
            (self.workspace / f"source{index}.txt").write_text("x" * 5000, encoding="utf-8")
        replies = [
            {"tool": "read_file", "args": {"path": f"source{index}.txt"}}
            for index in range(3)
        ] + [{"reply": "Compared the three notes, Sir."}]
        assistant, client = self.assistant(replies, max_tool_rounds=3)
        goal = "Compare the three notes and tell me their differences."
        self.assertEqual(assistant.ask(goal), "Compared the three notes, Sir.")
        self.assertEqual(len(client.messages), 4)
        for messages in client.messages:
            with self.subTest(round=len(messages)):
                self.assertIn({"role": "user", "content": goal}, messages)
                # Budget is based on three characters per context token. The
                # latest request remains present even when results are trimmed.
                self.assertLessEqual(sum(len(message["content"]) for message in messages), 2048 * 3)

    def test_long_request_does_not_exceed_recall_query_limit(self):
        assistant, _ = self.assistant([{"reply": "Read your request, Sir."}])
        prompt = "Please assess this project. " + "x" * 1500
        self.assertEqual(assistant.ask(prompt), "Read your request, Sir.")
        self.assertEqual(self.toolkit.memory.recent_history()[0]["content"], prompt)

    def test_blank_and_nonstring_replies_have_a_persistable_fallback(self):
        for reply in ["", "   ", None, {"nested": "value"}]:
            with self.subTest(reply=reply):
                assistant, _ = self.assistant([{"reply": reply}])
                response = assistant.ask("Say hello")
                self.assertIn("empty reply", response)
                self.assertEqual(self.toolkit.memory.recent_history(limit=2)[1]["content"], response)

    def test_model_failure_rolls_back_unfinished_history(self):
        self.toolkit.memory.save_turn("Previous question", "Previous answer")
        assistant, _ = self.assistant([RuntimeError("Ollama unavailable")])
        before = [dict(message) for message in assistant.history]
        with self.assertRaisesRegex(RuntimeError, "Ollama unavailable"):
            assistant.ask("An unfinished request")
        self.assertEqual(assistant.history, before)
        self.assertEqual(self.toolkit.memory.recent_history(), before)

    def test_malformed_memory_identifiers_do_not_delete_facts(self):
        memory_id = self.toolkit.memory.remember("Keep this fact")
        for identifier in [True, False, 1.0, "1", None, -1, 2**63]:
            with self.subTest(identifier=identifier):
                result = self.toolkit.execute("memory", {"action": "forget", "id": identifier})
                self.assertFalse(result.ok)
                self.assertEqual(self.toolkit.memory.recall()[0]["id"], memory_id)

    def test_invalid_memory_text_and_limits_are_not_coerced(self):
        for args in [
            {"action": "remember", "text": None},
            {"action": "remember", "text": {"fact": "text"}},
            {"action": "recall", "limit": True},
            {"action": "recall", "limit": "5"},
        ]:
            with self.subTest(args=args):
                self.assertFalse(self.toolkit.execute("memory", args).ok)
        self.assertEqual(self.toolkit.memory.recall(), [])

    def test_calculator_supports_bounded_arithmetic(self):
        examples = {"(2+3)*4": "20", "-3**2": "-9", "7/2": "3.5", "2**10": "1024"}
        for expression, expected in examples.items():
            with self.subTest(expression=expression):
                result = self.toolkit.execute("calculate", {"expression": expression})
                self.assertTrue(result.ok, result.content)
                self.assertEqual(result.content, expected)

    def test_calculator_rejects_executable_and_unbounded_expressions(self):
        expressions = [
            "__import__('os').system('touch unexpected')",
            "(1).__class__",
            "[1, 2, 3]",
            "True+1",
            "2**101",
            "10**100*10",
            "1/0",
            "1e309",
        ]
        with patch("jarvis.tools.subprocess.run") as process:
            for expression in expressions:
                with self.subTest(expression=expression):
                    self.assertFalse(self.toolkit.execute("calculate", {"expression": expression}).ok)
            process.assert_not_called()
        self.assertFalse((self.workspace / "unexpected").exists())


class OllamaFailureTests(unittest.TestCase):
    def setUp(self):
        self.client = OllamaClient(base_url="http://localhost:11434", model="test", timeout=7)
        self.messages = [{"role": "user", "content": "Hello"}]

    def test_network_timeout_is_an_actionable_runtime_error(self):
        with patch("jarvis.ollama_client.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaisesRegex(RuntimeError, "timed out after 7 seconds"):
                self.client.chat(self.messages)

    def test_unreachable_endpoint_is_a_runtime_error(self):
        with patch("jarvis.ollama_client.request.urlopen", side_effect=error.URLError("connection refused")):
            with self.assertRaisesRegex(RuntimeError, "Could not reach Ollama"):
                self.client.chat(self.messages)

    def test_invalid_json_response_is_a_runtime_error(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"this is not JSON"
        with patch("jarvis.ollama_client.request.urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "invalid JSON response"):
                self.client.chat(self.messages)

    def test_missing_assistant_message_is_a_runtime_error(self):
        for payload in [[], {}, {"message": []}, {"message": {"content": ""}}]:
            with self.subTest(payload=payload):
                response = MagicMock()
                response.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
                with patch("jarvis.ollama_client.request.urlopen", return_value=response):
                    with self.assertRaises(RuntimeError):
                        self.client.chat(self.messages)


if __name__ == "__main__":
    unittest.main()
