"""Code development integrates with real tool dispatch and permission checks."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from jarvis.assistant import JarvisAssistant, _read_only_call
from jarvis.cli import build_parser, interactive_loop
from jarvis.tools import ToolKit


class SelfDevelopmentToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.toolkit = ToolKit(workspace=self.root / "workspace", data_dir=self.root / "private", full_disk_access=True)
        self.addCleanup(self.toolkit.close)

    def test_ordinary_startup_does_not_create_development_storage(self):
        self.assertIsNone(self.toolkit._development)
        self.assertEqual(self.toolkit.extension_catalog(), [])
        self.assertIn("selfdev", self.toolkit.available_tools)
        self.assertFalse((self.root / "private" / "extensions").exists())

    def test_full_access_cannot_write_source_directly_or_through_a_symlink(self):
        import jarvis.tools
        source = Path(jarvis.tools.__file__).resolve()
        self.assertFalse(self.toolkit.execute("write_file", {"path": str(source), "content": "replaced"}).ok)
        (self.toolkit.workspace / "shortcut.py").symlink_to(source)
        self.assertFalse(self.toolkit.execute("write_file", {"path": "shortcut.py", "content": "replaced"}).ok)
        self.assertIn("class ToolKit", source.read_text())

    def test_workspace_notes_remain_writable_but_extension_authority_is_protected(self):
        self.assertTrue(self.toolkit.execute("write_file", {"path": "notes.txt", "content": "requested note"}).ok)
        approval_file = self.root / "private" / "extensions" / "ext_fake.json"
        self.assertFalse(self.toolkit.execute("write_file", {"path": str(approval_file), "content": "{}"}).ok)
        self.assertFalse(approval_file.exists())

    def test_core_tool_dispatch_cannot_be_replaced_by_an_extension_catalog(self):
        self.toolkit._extensions = Mock()
        self.toolkit._extensions.catalog.return_value = [{"name": "calculate", "description": "override", "parameters": {}}]
        result = self.toolkit.execute("calculate", {"expression": "2+3"})
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "5")
        self.toolkit._extensions.invoke.assert_not_called()

    def test_disabled_extension_cannot_reach_runtime_or_approval(self):
        self.toolkit._extensions = Mock()
        self.toolkit._extensions.catalog.return_value = [{"name": "ext_double", "description": "double", "parameters": {}}]
        self.toolkit._development = types.SimpleNamespace(enabled=False)
        self.toolkit.development_permission_handler = Mock(return_value=True)
        result = self.toolkit.execute("ext_double", {"number": 7})
        self.assertFalse(result.ok)
        self.assertIn("Self-development is off", result.content)
        self.toolkit._extensions.invoke.assert_not_called()
        self.toolkit.development_permission_handler.assert_not_called()

    def test_unknown_extensions_cannot_reach_runtime(self):
        self.toolkit._extensions = Mock()
        self.toolkit._extensions.catalog.return_value = []
        self.assertFalse(self.toolkit.execute("ext_invented", {}).ok)
        self.toolkit._extensions.invoke.assert_not_called()

    def test_installed_extension_is_discovered_without_import_and_runs_only_after_review(self):
        marker = self.toolkit.workspace / "extension-ran.txt"
        code = ("from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('executed')\n"
                "def run(args, context):\n    return str(args['number'] * 2)\n")
        metadata = {"name": "ext_double", "description": "Double a number", "parameters": {"type": "object", "properties": {"number": {"type": "number"}}, "required": ["number"]}}
        self.toolkit.extensions.install(metadata, code)
        self.assertIn("ext_double", self.toolkit.available_tools)
        self.assertFalse(marker.exists(), "Discovery must not execute extension module code")
        self.toolkit.development.set_enabled(True)
        self.toolkit.development_permission_handler = Mock(return_value=False)
        self.assertFalse(self.toolkit.execute("ext_double", {"number": 7}).ok)
        self.assertFalse(marker.exists())
        review = Mock(return_value=True)
        self.toolkit.development_permission_handler = review
        result = self.toolkit.execute("ext_double", {"number": 7})
        self.assertTrue(result.ok, result.content)
        self.assertEqual(result.content, "14")
        self.assertTrue(marker.exists())
        details = review.call_args.args[1]
        self.assertEqual(details["code"], code)
        self.assertEqual(details["args"], {"number": 7})

    def test_proposed_extension_requires_test_and_apply_reviews_then_becomes_a_tool(self):
        self.toolkit.development.set_enabled(True)
        reviews = []

        def allow(action, details):
            reviews.append((action, details))
            return True

        self.toolkit.development_permission_handler = allow
        source = "def run(args, context):\n    return str(args['number'] * 2)\n"
        proposed = self.toolkit.execute("selfdev", {
            "action": "propose", "kind": "extension", "name": "ext_reviewed_double",
            "description": "Double a number", "parameters": {"type": "object", "properties": {"number": {"type": "number"}}, "required": ["number"]},
            "code": source, "sample_args": {"number": 7},
        })
        self.assertTrue(proposed.ok, proposed.content)
        proposal_id = json.loads(proposed.content)["id"]
        self.assertEqual(reviews, [], "Staging source must not execute or ask for approval")
        premature = self.toolkit.execute("selfdev", {"action": "apply", "id": proposal_id})
        self.assertFalse(premature.ok)
        self.assertNotIn("ext_reviewed_double", self.toolkit.available_tools)
        tested = self.toolkit.execute("selfdev", {"action": "test", "id": proposal_id})
        self.assertTrue(tested.ok, tested.content)
        self.assertNotIn("ext_reviewed_double", self.toolkit.available_tools)
        applied = self.toolkit.execute("selfdev", {"action": "apply", "id": proposal_id})
        self.assertTrue(applied.ok, applied.content)
        self.assertIn("ext_reviewed_double", self.toolkit.available_tools)
        result = self.toolkit.execute("ext_reviewed_double", {"number": 9})
        self.assertTrue(result.ok, result.content)
        self.assertEqual(result.content, "18")
        self.assertEqual(len(reviews), 3, "Test, apply, and invocation each require a distinct review")
        self.assertEqual(reviews[-1][1]["code"], source)

    def test_revoking_development_during_review_cancels_extension_execution(self):
        marker = self.toolkit.workspace / "revoked-ran.txt"
        code = ("from pathlib import Path\n"
                "def run(args, context):\n"
                f"    Path({str(marker)!r}).write_text('executed')\n"
                "    return 'done'\n")
        self.toolkit.extensions.install({"name": "ext_revoked", "description": "Record a reviewed action", "parameters": {"type": "object", "properties": {}}}, code)
        self.toolkit.development.set_enabled(True)

        def revoke_during_review(action, details):
            self.toolkit.development.set_enabled(False)
            return True

        self.toolkit.development_permission_handler = revoke_during_review
        self.assertFalse(self.toolkit.execute("ext_revoked", {}).ok)
        self.assertFalse(marker.exists())

    def test_terminal_review_shows_complete_code_and_arguments_before_denial(self):
        details = {"code": "def run(args, context):\n    return args['number'] * 2\n", "args": {"number": 7}, "code_sha256": "approved digest"}
        self.toolkit.permission_handler = Mock(return_value=False)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertFalse(self.toolkit._development_permission("Run extension ext_double", details))
        shown = output.getvalue()
        self.assertIn("return args['number'] * 2", shown)
        self.assertIn('"number": 7', shown)
        self.assertIn("approved digest", shown)
        self.toolkit.permission_handler.assert_called_once()

    def test_web_review_callback_is_dynamic_after_manager_initialization(self):
        manager = self.toolkit.development
        first = Mock(return_value=False)
        second = Mock(return_value=True)
        self.toolkit.development_permission_handler = first
        self.assertFalse(self.toolkit._development_permission("Test proposal", {"code": "pass"}))
        self.toolkit.development_permission_handler = second
        self.assertTrue(self.toolkit._development_permission("Test proposal", {"code": "pass"}))
        self.assertIs(manager, self.toolkit.development)
        first.assert_called_once()
        second.assert_called_once()

    def test_development_read_only_classification_distinguishes_inspection_from_actions(self):
        for action in ("inspect", "review", "list"):
            self.assertTrue(_read_only_call("selfdev", {"action": action}))
        for action in ("propose", "test", "apply", "rollback"):
            self.assertFalse(_read_only_call("selfdev", {"action": action}))

    def test_extension_catalog_is_exposed_to_model_with_bounded_context(self):
        self.toolkit._extensions = Mock()
        self.toolkit._extensions.catalog.return_value = [
            {"name": "ext_double", "description": "Double a number", "parameters": {"type": "object", "properties": {"number": {"type": "number"}}}},
            *[{"name": f"ext_large_{i}", "description": "x" * 5000, "parameters": {"properties": {f"field{n}": {"type": "string"} for n in range(100)}}} for i in range(100)],
        ]
        client = types.SimpleNamespace(num_ctx=2048, chat=Mock(return_value='{"reply":"Ready, Sir."}'))
        assistant = JarvisAssistant(client, self.toolkit)
        assistant.ask("What extensions are installed?")
        messages = client.chat.call_args.args[0]
        self.assertIn('"name": "ext_double"', messages[0]["content"])
        self.assertIn('"number": "number"', messages[0]["content"])
        self.assertNotIn("ext_large_99", messages[0]["content"])
        self.assertLessEqual(sum(len(message["content"]) for message in messages), 2048 * 3)

    def test_terminal_permissions_can_be_changed_without_model_tool_calls(self):
        args = build_parser().parse_args([])
        assistant = types.SimpleNamespace(toolkit=self.toolkit, ask=Mock())
        with patch("builtins.input", side_effect=[":develop on", ":permissions", ":develop off", ":quit"]), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(interactive_loop(assistant, args, False, False, "Daniel"), 0)
        self.assertFalse(self.toolkit.development.enabled)
        self.assertIn("self-development: on", output.getvalue())
        assistant.ask.assert_not_called()

    def test_self_develop_flag_does_not_enable_other_permissions(self):
        args = build_parser().parse_args(["--self-develop"])
        self.assertTrue(args.self_develop)
        self.assertFalse(args.full_access)


if __name__ == "__main__":
    unittest.main()
