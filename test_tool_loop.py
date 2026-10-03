"""Behavioral checks for finishing turns after bounded, deduplicated tools."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.assistant import JarvisAssistant, OBSOLETE_TOOL_LIMIT_REPLY
from jarvis.tools import ToolKit


class FakeClient:
    def __init__(self, replies, *, num_ctx=2048):
        self.replies = list(replies)
        self.num_ctx = num_ctx
        self.messages = []
        self.reply_only = []

    def chat(self, messages, *, reply_only=False):
        self.messages.append([dict(message) for message in messages])
        self.reply_only.append(reply_only)
        if not self.replies:
            raise AssertionError("Assistant requested an unexpected model round")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply) if isinstance(reply, dict) else reply


class ToolLoopTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.workspace = root / "workspace"
        self.toolkit = ToolKit(workspace=self.workspace, data_dir=root / "data")
        self.addCleanup(self.toolkit.close)

    def assistant(self, replies, **options):
        client = FakeClient(replies)
        assistant = JarvisAssistant(client, self.toolkit, memory=self.toolkit.memory, **options)
        return assistant, client

    @staticmethod
    def append(content="A", *, path="activity.txt", why="Append the requested line"):
        return {"tool": "write_file", "args": {"path": path, "content": content, "mode": "append"}, "why": why}

    def test_duplicate_append_with_reordered_args_and_different_why_executes_once(self):
        first = self.append("A")
        duplicate = {
            "tool": "write_file",
            "args": {"mode": "append", "content": "A", "path": "activity.txt"},
            "why": "The model thinks it should repeat the same action",
        }
        assistant, client = self.assistant([first, duplicate, {"reply": "Appended it once, Sir."}])
        with patch.object(self.toolkit, "execute", wraps=self.toolkit.execute) as execute:
            reply = assistant.ask("Append A to activity.txt once.")
        self.assertEqual(reply, "Appended it once, Sir.")
        self.assertEqual((self.workspace / "activity.txt").read_text(), "A")
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(len(client.messages), 3)
        self.assertTrue(client.reply_only[-1])

    def test_same_append_is_permitted_again_on_the_next_user_turn(self):
        action = self.append("A")
        assistant, client = self.assistant(
            [action, {"reply": "Appended, Sir."}, action, {"reply": "Appended again, Sir."}],
            max_tool_rounds=1,
        )
        with patch.object(self.toolkit, "execute", wraps=self.toolkit.execute) as execute:
            self.assertEqual(assistant.ask("Append A."), "Appended, Sir.")
            self.assertEqual(assistant.ask("Append A again."), "Appended again, Sir.")
        self.assertEqual((self.workspace / "activity.txt").read_text(), "AA")
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(client.reply_only, [False, True, False, True])

    def test_distinct_args_allow_multiple_calls_to_the_same_tool(self):
        assistant, client = self.assistant(
            [self.append("A"), self.append("B"), {"reply": "Both requested lines are present, Sir."}],
            max_tool_rounds=2,
        )
        with patch.object(self.toolkit, "execute", wraps=self.toolkit.execute) as execute:
            reply = assistant.ask("Append A and then B to activity.txt.")
        self.assertEqual(reply, "Both requested lines are present, Sir.")
        self.assertEqual((self.workspace / "activity.txt").read_text(), "AB")
        self.assertEqual(execute.call_count, 2)
        self.assertTrue(client.reply_only[-1])

    def test_reply_only_model_tool_request_reports_the_completed_action(self):
        unexpected = {
            "tool": "memory", "args": {"action": "remember", "text": "An unrequested fallback fact"},
        }
        assistant, client = self.assistant([self.append("A"), unexpected], max_tool_rounds=1)
        with patch.object(self.toolkit, "execute", wraps=self.toolkit.execute) as execute:
            reply = assistant.ask("Append A to activity.txt.")
        self.assertEqual((self.workspace / "activity.txt").read_text(), "A")
        self.assertEqual(execute.call_count, 1)
        self.assertTrue(client.reply_only[-1])
        self.assertIn("Wrote", reply)
        self.assertIn("activity.txt", reply)
        self.assertNotIn("tool limit", reply.lower())
        self.assertEqual(self.toolkit.memory.recall(), [])
        self.assertEqual(self.toolkit.memory.recent_history()[-1]["content"], reply)

    def test_reply_only_model_failure_reports_the_side_effect_and_keeps_the_turn(self):
        prompt = "Append A to activity.txt."
        assistant, client = self.assistant([self.append("A"), RuntimeError("Ollama timed out")], max_tool_rounds=1)
        reply = assistant.ask(prompt)
        self.assertEqual((self.workspace / "activity.txt").read_text(), "A")
        self.assertTrue(client.reply_only[-1])
        self.assertIn("Wrote", reply)
        self.assertIn("activity.txt", reply)
        self.assertNotIn("tool limit", reply.lower())
        expected = [{"role": "user", "content": prompt}, {"role": "assistant", "content": reply}]
        self.assertEqual(assistant.history, expected)
        self.assertEqual(self.toolkit.memory.recent_history(), expected)

    def test_duplicate_then_malformed_final_response_never_repeats_the_write(self):
        action = self.append("A")
        assistant, client = self.assistant([action, action, {"unexpected": "shape"}])
        with patch.object(self.toolkit, "execute", wraps=self.toolkit.execute) as execute:
            reply = assistant.ask("Append A once.")
        self.assertEqual(execute.call_count, 1)
        self.assertEqual((self.workspace / "activity.txt").read_text(), "A")
        self.assertTrue(client.reply_only[-1])
        self.assertIn("Wrote", reply)
        self.assertIn("activity.txt", reply)

    def test_initial_model_error_rolls_back_before_any_tool_runs(self):
        self.toolkit.memory.save_turn("Previous question", "Previous reply")
        assistant, client = self.assistant([RuntimeError("Ollama unavailable")])
        before = [dict(message) for message in assistant.history]
        with patch.object(self.toolkit, "execute", wraps=self.toolkit.execute) as execute:
            with self.assertRaisesRegex(RuntimeError, "Ollama unavailable"):
                assistant.ask("An unfinished request")
        execute.assert_not_called()
        self.assertEqual(assistant.history, before)
        self.assertEqual(self.toolkit.memory.recent_history(), before)
        self.assertEqual(len(client.messages), 1)

    def test_finished_history_contains_only_user_and_reply_pairs(self):
        self.toolkit.memory.save_turn("Earlier question", "Earlier reply")
        prompt, reply = "Append a note.", "The note is appended, Sir."
        assistant, _ = self.assistant([self.append("A"), {"reply": reply}], max_tool_rounds=1)
        self.assertEqual(assistant.ask(prompt), reply)
        expected = [
            {"role": "user", "content": "Earlier question"},
            {"role": "assistant", "content": "Earlier reply"},
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": reply},
        ]
        self.assertEqual(assistant.history, expected)
        self.assertEqual(self.toolkit.memory.recent_history(), expected)
        self.assertNotIn("Tool result", json.dumps(assistant.history))

    def test_acknowledgement_does_not_receive_previous_tool_trajectory(self):
        assistant, client = self.assistant([self.append("A"), {"reply": "Appended, Sir."}, {"reply": "Of course, Sir."}])
        with patch.object(self.toolkit, "execute", wraps=self.toolkit.execute) as execute:
            assistant.ask("Append A once.")
            self.assertEqual(assistant.ask("Okay"), "Of course, Sir.")
        self.assertEqual(execute.call_count, 1)
        messages = client.messages[-1]
        self.assertEqual(messages[-1], {"role": "user", "content": "Okay"})
        self.assertNotIn("Recorded tool results", "\n".join(m["content"] for m in messages))
        self.assertEqual(messages[1:3], [{"role": "user", "content": "Append A once."}, {"role": "assistant", "content": "Appended, Sir."}])

    def test_obsolete_limit_replies_are_not_sent_to_model_or_deleted_from_storage(self):
        self.toolkit.memory.save_turn("The old request", OBSOLETE_TOOL_LIMIT_REPLY)
        assistant, client = self.assistant([{"reply": "Of course, Sir."}])
        self.assertEqual(assistant.ask("Okay"), "Of course, Sir.")
        self.assertNotIn(OBSOLETE_TOOL_LIMIT_REPLY, "\n".join(m["content"] for m in client.messages[0]))
        self.assertEqual(self.toolkit.memory.recent_history()[1]["content"], OBSOLETE_TOOL_LIMIT_REPLY)

    def test_read_after_edit_is_not_blocked_as_a_duplicate(self):
        (self.workspace / "activity.txt").write_text("Before")
        read = {"tool": "read_file", "args": {"path": "activity.txt"}}
        write = {"tool": "write_file", "args": {"path": "activity.txt", "content": "After", "mode": "overwrite"}}
        assistant, client = self.assistant([read, write, read, {"reply": "Verified the edit, Sir."}])
        with patch.object(self.toolkit, "execute", wraps=self.toolkit.execute) as execute:
            self.assertEqual(assistant.ask("Read activity.txt, replace it with After, then verify."), "Verified the edit, Sir.")
        self.assertEqual(execute.call_count, 3)
        evidence = json.loads(client.messages[-1][-1]["content"].split("\n", 1)[1])
        self.assertEqual(evidence[0]["content"], "Before")
        self.assertEqual(evidence[2]["content"], "After")

    def test_malformed_action_is_reported_without_crashing_dispatch(self):
        invalid = {"tool": "memory", "args": {"action": {"invalid": "shape"}}}
        assistant, _ = self.assistant([invalid, {"reply": "The memory request was invalid, Sir."}], max_tool_rounds=1)
        self.assertEqual(assistant.ask("Use memory."), "The memory request was invalid, Sir.")
        self.assertEqual(self.toolkit.memory.recall(), [])

    def test_final_context_retains_all_outcomes_with_large_results_in_small_budget(self):
        markers = ["FIRST_OUTCOME_MARKER", "SECOND_OUTCOME_MARKER", "THIRD_OUTCOME_MARKER"]
        for output in ("x" * 5000, '"\\\n' * 2000):
            with self.subTest(escaping=output.startswith('"')):
                for index, marker in enumerate(markers):
                    (self.workspace / f"note{index}.txt").write_text(marker + "\n" + output)
                actions = [
                    {"tool": "read_file", "args": {"path": f"note{index}.txt"}}
                    for index in range(3)
                ]
                goal = "Compare all three notes and preserve the facts from each."
                assistant, client = self.assistant(actions + [{"reply": "Compared all three, Sir."}], max_tool_rounds=3)
                self.assertEqual(assistant.ask(goal), "Compared all three, Sir.")
                self.assertTrue(client.reply_only[-1])
                final_context = "\n".join(message["content"] for message in client.messages[-1])
                for marker in markers:
                    self.assertIn(marker, final_context)
                evidence = json.loads(client.messages[-1][-1]["content"].partition("\n")[2])
                self.assertEqual(len(evidence), 3)
                self.assertTrue(all(result["tool"] == "read_file" and result["ok"] for result in evidence))
                for result, marker in zip(evidence, markers):
                    self.assertIn(marker, result["content"])
                for messages in client.messages:
                    self.assertIn({"role": "user", "content": goal}, messages)
                    self.assertLessEqual(sum(len(message["content"]) for message in messages), 2048 * 3)

    def test_zero_tool_budget_allows_reply_only_answer(self):
        assistant, client = self.assistant([{"reply": "Good evening, Sir."}], max_tool_rounds=0)
        with patch.object(self.toolkit, "execute", wraps=self.toolkit.execute) as execute:
            self.assertEqual(assistant.ask("Hello."), "Good evening, Sir.")
        execute.assert_not_called()
        self.assertEqual(client.reply_only, [True])

    def test_zero_tool_budget_malformed_response_has_safe_persisted_fallback(self):
        for response in ({"unexpected": "shape"}, self.append("A"), {"reply": None}):
            with self.subTest(response=response):
                assistant, client = self.assistant([response], max_tool_rounds=0)
                with patch.object(self.toolkit, "execute", wraps=self.toolkit.execute) as execute:
                    reply = assistant.ask("Answer without changing files.")
                execute.assert_not_called()
                self.assertTrue(isinstance(reply, str) and reply.strip())
                self.assertEqual(client.reply_only, [True])
                self.assertEqual(self.toolkit.memory.recent_history(limit=2)[-1]["content"], reply)
                self.assertFalse((self.workspace / "activity.txt").exists())


if __name__ == "__main__":
    unittest.main()
