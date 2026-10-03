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
        self.allowed_tools = []

    def chat(self, messages, *, reply_only=False, allowed_tools=None):
        self.messages.append([dict(message) for message in messages])
        self.reply_only.append(reply_only)
        self.allowed_tools.append(allowed_tools)
        if not self.replies:
            raise AssertionError("Assistant requested an unexpected model round")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply) if isinstance(reply, dict) else reply


class ToolLoopTests(unittest.TestCase):
    FALSE_RESEARCH_REPLY = (
        "I will need to propose a tool for research and develop myself onto this device. "
        "The recorded tool results indicate that the 'research' tool is unknown. "
        "I will need to propose a tool such as 'google_native' or 'web_search' to proceed with the task. "
        "This may take approximately 30 seconds to 1 minute to propose and enable the tool. "
        "Once enabled, I will begin the research task. Estimated completion time for the research task "
        "is approximately 2-5 minutes, depending on the scope and complexity of the topics. "
        "I will notify you once the research task is completed. Please enable the 'google_native' tool to proceed, Sir."
    )
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

    def test_normal_model_rounds_receive_the_live_tool_catalog(self):
        assistant, client = self.assistant([
            {"tool": "calculate", "args": {"expression": "2+2"}},
            {"reply": "4, Sir."},
        ])
        self.assertEqual(assistant.ask("Calculate two plus two."), "4, Sir.")
        for names in client.allowed_tools:
            self.assertEqual(names, self.toolkit.available_tools)
            self.assertIn("research", names)
            self.assertIn("web_search", names)

    def test_quoted_false_research_prerequisite_is_corrected_before_persisting(self):
        assistant, _ = self.assistant([{"reply": self.FALSE_RESEARCH_REPLY}])
        with patch.object(self.toolkit, "execute", wraps=self.toolkit.execute) as execute:
            reply = assistant.ask("Research local voice assistants.")
        execute.assert_not_called()
        self.assertIn("Research is built in", reply)
        self.assertIn("No research job was started", reply)
        self.assertNotIn("Please enable", reply)
        self.assertNotIn("2-5 minutes", reply)
        self.assertEqual(self.toolkit.memory.recent_history()[-1]["content"], reply)
        self.assertIsNone(self.toolkit._development)

    def test_false_final_research_reply_preserves_a_completed_write(self):
        assistant, client = self.assistant([
            self.append("A"), {"reply": self.FALSE_RESEARCH_REPLY},
        ], max_tool_rounds=1)
        with patch.object(self.toolkit, "execute", wraps=self.toolkit.execute) as execute:
            reply = assistant.ask("Append A once, then tell me which research tools you have.")
        self.assertEqual(execute.call_count, 1)
        self.assertEqual((self.workspace / "activity.txt").read_text(), "A")
        self.assertIn("Research is built in", reply)
        self.assertIn("Wrote", reply)
        self.assertIn("activity.txt", reply)
        self.assertNotIn("Please enable", reply)
        self.assertTrue(client.reply_only[-1])
        self.assertIn("research", client.messages[-1][0]["content"])
        self.assertIn("no installation, development or Google access required", client.messages[-1][0]["content"])

    def test_duplicate_call_finalization_also_corrects_false_research_claim(self):
        action = self.append("A")
        assistant, _ = self.assistant([action, action, {"reply": self.FALSE_RESEARCH_REPLY}])
        reply = assistant.ask("Append A once and describe research support.")
        self.assertEqual((self.workspace / "activity.txt").read_text(), "A")
        self.assertIn("Research is built in", reply)
        self.assertIn("Wrote", reply)
        self.assertNotIn("Please enable", reply)

    def test_unknown_model_tool_name_does_not_reach_dispatch_or_google(self):
        assistant, client = self.assistant([
            {"tool": "research.start", "args": {"query": "local voice assistants"}},
        ])
        with patch.object(self.toolkit, "execute", wraps=self.toolkit.execute) as execute:
            reply = assistant.ask("Research local voice assistants.")
        execute.assert_not_called()
        self.assertEqual(len(client.messages), 1)
        self.assertIn("unregistered tool name", reply)
        self.assertIn("research, web_search", reply)
        self.assertIn("Google access and code development are not required", reply)

    def test_unknown_tool_after_success_preserves_the_actual_outcome(self):
        assistant, _ = self.assistant([
            self.append("A"), {"tool": "invented_research", "args": {}},
        ])
        with patch.object(self.toolkit, "execute", wraps=self.toolkit.execute) as execute:
            reply = assistant.ask("Append A, then research local voice assistants.")
        self.assertEqual(execute.call_count, 1)
        self.assertEqual((self.workspace / "activity.txt").read_text(), "A")
        self.assertIn("Wrote", reply)
        self.assertIn("unregistered tool name", reply)

    def test_old_false_research_reply_is_excluded_from_context_and_retained_in_storage(self):
        self.toolkit.memory.save_turn("Research a topic.", self.FALSE_RESEARCH_REPLY)
        assistant, client = self.assistant([{"reply": "What topic would you like researched, Sir?"}])
        assistant.ask("Can you do research?")
        context = "\n".join(message["content"] for message in client.messages[0])
        self.assertNotIn(self.FALSE_RESEARCH_REPLY, context)
        self.assertEqual(self.toolkit.memory.recent_history()[1]["content"], self.FALSE_RESEARCH_REPLY)

    def test_actual_research_failure_and_google_account_setup_are_not_rewritten(self):
        replies = [
            "The research tool returned an unknown research job ID, Sir. Please provide a valid ID.",
            "Research is available, but the web request timed out, Sir.",
            "Research is unavailable because the network is offline, Sir.",
            "The research tool is unavailable after the worker shuts down, Sir.",
            "The research is missing evidence for that claim, Sir.",
            "It previously said 'research tool is unknown', but research is now built in, Sir.",
            "Research is built in. Google Native needs credentials to read your Gmail, Sir.",
        ]
        for expected in replies:
            with self.subTest(reply=expected):
                assistant, _ = self.assistant([{"reply": expected}])
                self.assertEqual(assistant.ask("Explain the research or Google result."), expected)

    def test_builtin_research_starts_without_google_or_development_and_keeps_job_result(self):
        query = "local voice assistants"
        assistant, client = self.assistant([
            {"tool": "research", "args": {"action": "start", "query": query}},
            {"reply": self.FALSE_RESEARCH_REPLY},
        ], max_tool_rounds=1)
        with patch.object(self.toolkit, "search_results", return_value=[{"url": "https://example.com/article"}]), \
             patch("jarvis.research.fetch_page", return_value={"url": "https://example.com/article", "title": "Local assistants", "text": "Source excerpt."}), \
             patch.object(self.toolkit, "execute", wraps=self.toolkit.execute) as execute:
            reply = assistant.ask("Research " + query + ".")
            self.toolkit.research._executor.shutdown(wait=True)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(execute.call_args.args[0], "research")
        job = self.toolkit.research.list_jobs()[0]
        self.assertEqual(job["query"], query)
        self.assertEqual(job["status"], "completed")
        self.assertTrue(Path(job["path"]).is_file())
        self.assertIn("Source excerpt.", Path(job["path"]).read_text())
        self.assertIn(job["id"], reply)
        self.assertIn("Research is built in", reply)
        self.assertNotIn("Please enable", reply)
        self.assertNotIn("2-5 minutes", reply)
        self.assertFalse(self.toolkit.development.enabled)
        self.assertIn("research", client.messages[-1][0]["content"])

    def test_research_registry_facts_fit_small_context_with_long_saved_memory(self):
        actions = []
        for index in range(3):
            path = self.workspace / f"source{index}.txt"
            path.write_text(f"SOURCE_{index}\n" + "x" * 5000)
            actions.append({"tool": "read_file", "args": {"path": path.name}})
        assistant, client = self.assistant(actions + [{"reply": "Compared the sources, Sir."}])
        with patch.object(self.toolkit.memory, "recall", return_value=[{"text": "F" * 5000}]):
            assistant.ask("Compare these sources. " + "G" * 6000)
        for messages in client.messages:
            self.assertLessEqual(sum(len(message["content"]) for message in messages), 2048 * 3)
            self.assertTrue(any(message["content"].startswith("Compare these sources.") for message in messages))
        evidence = json.loads(client.messages[-1][-1]["content"].partition("\n")[2])
        self.assertEqual(len(evidence), 3)
        self.assertTrue(all(result["ok"] for result in evidence))


if __name__ == "__main__":
    unittest.main()
