import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis.extensions import ExtensionRegistry
from jarvis.selfdev import MAX_INSPECT_CHARS, SelfDevelopment


SOURCE = "def run(args, context):\n    return {'doubled': args['number'] * 2}\n"


class SelfDevelopmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "source"
        self.source.mkdir()
        (self.source / "__init__.py").write_text("")
        (self.source / "pyproject.toml").write_text("[project]\nname = 'fixture'\nversion = '1.0'\n")
        (self.source / "scalar.py").write_text("def value():\n    return 0\n")
        (self.source / "test_scalar.py").write_text(
            "import unittest\nfrom jarvis.scalar import value\n"
            "class ScalarTests(unittest.TestCase):\n"
            "    def test_staged_value(self):\n        self.assertEqual(value(), 2)\n"
        )
        self.data = self.base / "private"
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.registry = ExtensionRegistry(self.data / "extensions", self.workspace, self.data)
        self.approval = Mock(return_value=True)
        self.manager = SelfDevelopment(self.source, self.data, self.workspace, self.registry, self.approval)

    def extension(self, **overrides):
        payload = {
            "kind": "extension", "name": "ext_double", "description": "Double a number.",
            "parameters": {"type": "object", "properties": {"number": {"type": "integer"}},
                           "required": ["number"], "additionalProperties": False},
            "code": SOURCE, "sample_args": {"number": 3},
        }
        payload.update(overrides)
        return payload

    def core(self, content="def value():\n    return 2\n"):
        return {"kind": "core", "title": "Update scalar", "files": [{"path": "scalar.py", "content": content}]}

    def stage(self, payload=None):
        self.manager.set_enabled(True)
        return self.manager.propose(payload or self.extension())

    def _tested(self, payload=None):
        proposal = self.stage(payload)
        result = self.manager.test(proposal["id"])
        self.assertTrue(result["ok"], result)
        return proposal

    def applied(self, payload=None):
        proposal = self._tested(payload)
        self.assertTrue(self.manager.apply(proposal["id"])["ok"])
        return proposal

    def test_starts_off_and_inspects_without_execution(self):
        self.assertFalse(self.manager.enabled)
        read = self.manager.inspect("scalar.py")
        self.assertIn("return 0", read["content"])
        self.assertIn("scalar.py", [entry["path"] for entry in self.manager.inspect()["files"]])
        with self.assertRaisesRegex(ValueError, "off"):
            self.manager.propose(self.extension())
        self.approval.assert_not_called()

    def test_setting_persists_privately_and_requires_boolean(self):
        self.manager.set_enabled(True)
        restored = SelfDevelopment(self.source, self.data, self.workspace, self.registry)
        self.assertTrue(restored.enabled)
        self.assertEqual((self.data / "selfdev/settings.json").stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            restored.set_enabled("false")

    def test_staging_never_imports_or_installs_extension(self):
        marker = self.base / "executed"
        source = f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n" + SOURCE
        proposal = self.stage(self.extension(code=source))
        self.assertFalse(marker.exists())
        self.assertEqual(self.registry.tools(), [])
        self.assertEqual(proposal["status"], "staged")
        self.approval.assert_not_called()
        self.assertEqual(self.manager.review(proposal["id"])["code"], source)

    def test_review_contains_complete_diff_metadata_and_digest(self):
        proposal = self.stage()
        review = self.manager.review(proposal["id"])
        self.assertIn("def run", review["diff"])
        self.assertIn("Double a number", review["diff"])
        self.assertEqual(len(review["digest"]), 64)
        self.assertIn("not an operating-system sandbox", review["execution_warning"])
        listed = self.manager.list()[0]
        self.assertEqual(listed["id"], proposal["id"])
        self.assertNotIn("code", listed)

    def test_missing_approval_denies_tests_and_executes_nothing(self):
        proposal = self.stage()
        self.manager.permission_handler = None
        with patch.object(self.registry, "run_source") as run:
            with self.assertRaisesRegex(ValueError, "approval"):
                self.manager.test(proposal["id"])
        run.assert_not_called()
        self.assertEqual(self.manager.review(proposal["id"])["status"], "staged")

    def test_denial_does_not_test_or_apply(self):
        proposal = self.stage()
        self.approval.return_value = False
        with self.assertRaisesRegex(ValueError, "approval"):
            self.manager.test(proposal["id"])
        self.assertEqual(self.registry.tools(), [])

    def test_extension_requires_two_approvals_and_is_available_immediately(self):
        proposal = self._tested()
        with self.assertRaisesRegex(ValueError, "tests"):
            fresh = self.manager.propose(self.extension(name="ext_other"))
            self.manager.apply(fresh["id"])
        result = self.manager.apply(proposal["id"])
        self.assertFalse(result["restart_required"])
        self.assertEqual([entry["name"] for entry in self.registry.tools()], ["ext_double"])
        calls = self.approval.call_args_list
        self.assertEqual([call.args[0] for call in calls], ["selfdev.test", "selfdev.apply"])
        for call in calls:
            self.assertEqual(call.args[1]["digest"], proposal["digest"])
            self.assertEqual(call.args[1]["code"], SOURCE)
        run = self.registry.invoke("ext_double", {"number": 4}, lambda _action, _details: True)
        self.assertTrue(run["ok"], run)
        self.assertIn("8", run["content"])

    def test_invalid_sample_fails_test_and_cannot_install(self):
        proposal = self.stage(self.extension(sample_args={"number": "wrong"}))
        result = self.manager.test(proposal["id"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "test_failed")
        with self.assertRaisesRegex(ValueError, "tests"):
            self.manager.apply(proposal["id"])
        self.assertEqual(self.registry.tools(), [])

    def test_disabled_while_approval_pending_prevents_execution(self):
        proposal = self.stage()
        def disable(_action, _details):
            self.manager.set_enabled(False)
            return True
        self.manager.permission_handler = disable
        with patch.object(self.registry, "run_source") as run:
            with self.assertRaisesRegex(ValueError, "off"):
                self.manager.test(proposal["id"])
        run.assert_not_called()

    def test_disabled_after_test_prevents_install(self):
        proposal = self._tested()
        self.manager.set_enabled(False)
        with self.assertRaisesRegex(ValueError, "off"):
            self.manager.apply(proposal["id"])
        self.assertEqual(self.registry.tools(), [])

    def test_disabled_during_apply_approval_prevents_install(self):
        proposal = self._tested()
        self.manager.permission_handler = lambda _action, _details: self.manager.set_enabled(False) and True
        with self.assertRaisesRegex(ValueError, "off"):
            self.manager.apply(proposal["id"])
        self.assertEqual(self.registry.tools(), [])

    def test_changed_installed_base_rejects_test(self):
        proposal = self.stage()
        self.registry.install(proposal["metadata"], SOURCE)
        with self.assertRaisesRegex(ValueError, "changed"):
            self.manager.test(proposal["id"])

    def test_extension_change_during_apply_approval_rejects_install(self):
        proposal = self._tested()
        different = SOURCE.replace("* 2", "* 5")
        def change(_action, _details):
            self.registry.install(proposal["metadata"], different)
            return True
        self.manager.permission_handler = change
        with self.assertRaisesRegex(ValueError, "changed"):
            self.manager.apply(proposal["id"])
        self.assertEqual(self.registry.read("ext_double")["source"], different)

    def test_tampered_proposal_is_rejected(self):
        proposal = self.stage()
        path = self.data / "selfdev/proposals" / f"{proposal['id']}.json"
        record = json.loads(path.read_text())
        record["proposal"]["code"] += "\nraise RuntimeError('changed')\n"
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "changed"):
            self.manager.review(proposal["id"])
        self.assertEqual(self.manager.list(), [])

    def test_core_test_imports_proposed_copy_and_never_edits_original(self):
        proposal = self._tested(self.core())
        self.assertIn("Ran 1 test", self.manager.review(proposal["id"])["test_result"]["output"])
        self.assertIn("return 0", (self.source / "scalar.py").read_text())
        result = self.manager.apply(proposal["id"])
        self.assertTrue(result["restart_required"])
        self.assertIn("return 2", (self.source / "scalar.py").read_text())

    def test_unified_diff_stages_small_core_change_without_changing_original(self):
        patch_text = (
            "diff --git a/scalar.py b/scalar.py\n"
            "--- a/scalar.py\n+++ b/scalar.py\n"
            "@@ -1,2 +1,2 @@\n def value():\n-    return 0\n+    return 2\n"
        )
        proposal = self.stage({"kind": "core", "title": "Patch scalar", "patch": patch_text})
        self.assertIn("return 0", (self.source / "scalar.py").read_text())
        self.assertIn("+    return 2", proposal["diff"])
        self.assertTrue(self.manager.test(proposal["id"])["ok"])
        self.assertTrue(self.manager.apply(proposal["id"])["ok"])
        self.assertIn("return 2", (self.source / "scalar.py").read_text())

    def test_core_patches_reject_traversal_binary_renames_and_wrong_base(self):
        self.manager.set_enabled(True)
        for text in (
            "--- a/../outside.py\n+++ b/../outside.py\n@@ -1 +1 @@\n-a\n+b\n",
            "GIT binary patch\nliteral 3\nabc\n",
            "rename from scalar.py\nrename to other.py\n",
            "--- a/scalar.py\n+++ b/scalar.py\n@@ -1 +1 @@\n-does not exist\n+changed\n",
            "--- a/scalar.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-def value():\n",
        ):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    self.manager.propose({"kind": "core", "patch": text})
        self.assertIn("return 0", (self.source / "scalar.py").read_text())
        self.approval.assert_not_called()

    def test_switching_off_stops_a_running_core_test_and_keeps_source_unchanged(self):
        (self.source / "test_scalar.py").write_text(
            "import unittest,time\nclass WaitingTests(unittest.TestCase):\n"
            "    def test_wait(self):\n        time.sleep(20)\n"
        )
        proposal = self.stage(self.core())
        timer = threading.Timer(0.3, lambda: self.manager.set_enabled(False))
        timer.start()
        try:
            result = self.manager.test(proposal["id"])
        finally:
            timer.join(timeout=2)
        self.assertFalse(result["ok"])
        self.assertIn("switched off", result["test_result"]["output"])
        self.assertIn("return 0", (self.source / "scalar.py").read_text())

    def test_core_test_timeout_is_bounded(self):
        (self.source / "test_scalar.py").write_text(
            "import unittest,time\nclass WaitingTests(unittest.TestCase):\n"
            "    def test_wait(self):\n        time.sleep(20)\n"
        )
        proposal = self.stage(self.core())
        with patch("jarvis.selfdev.TEST_TIMEOUT", 0.2):
            result = self.manager.test(proposal["id"])
        self.assertFalse(result["ok"])
        self.assertIn("exceeded", result["test_result"]["output"])

    def test_core_failed_tests_cannot_apply(self):
        proposal = self.stage(self.core("def value():\n    return 9\n"))
        result = self.manager.test(proposal["id"])
        self.assertFalse(result["ok"])
        self.assertIn("AssertionError", result["test_result"]["output"])
        with self.assertRaisesRegex(ValueError, "tests"):
            self.manager.apply(proposal["id"])
        self.assertIn("return 0", (self.source / "scalar.py").read_text())

    def test_core_base_change_after_approval_is_rejected(self):
        proposal = self._tested(self.core())
        def change(_action, _details):
            (self.source / "scalar.py").write_text("def value():\n    return 7\n")
            return True
        self.manager.permission_handler = change
        with self.assertRaisesRegex(ValueError, "changed"):
            self.manager.apply(proposal["id"])
        self.assertIn("return 7", (self.source / "scalar.py").read_text())

    def test_core_dependency_change_invalidates_tested_source(self):
        proposal = self._tested(self.core())
        (self.source / "__init__.py").write_text("UPDATED = True\n")
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.manager.apply(proposal["id"])

    def test_core_partial_failure_restores_already_changed_files(self):
        payload = self.core()
        payload["files"].append({"path": "extra.py", "content": "VALUE = 3\n"})
        proposal = self._tested(payload)
        original_write = self.manager._atomic_write
        def fail_extra(path, raw, **kwargs):
            if path.name == "extra.py":
                raise OSError("disk full")
            return original_write(path, raw, **kwargs)
        with patch.object(self.manager, "_atomic_write", side_effect=fail_extra):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.manager.apply(proposal["id"])
        self.assertIn("return 0", (self.source / "scalar.py").read_text())
        self.assertFalse((self.source / "extra.py").exists())

    def test_application_status_write_failure_restores_core_and_extension(self):
        for payload in (self.core(), self.extension()):
            with self.subTest(kind=payload["kind"]):
                proposal = self._tested(payload)
                with patch.object(self.manager, "_save", side_effect=OSError("disk full")):
                    with self.assertRaisesRegex(OSError, "disk full"):
                        self.manager.apply(proposal["id"])
                self.assertEqual(self.manager.review(proposal["id"])["status"], "tested")
                self.assertIn("return 0", (self.source / "scalar.py").read_text())
                self.assertEqual(self.registry.tools(), [])

    def test_rollback_status_write_failure_preserves_applied_installation(self):
        proposal = self.applied(self.core())
        with patch.object(self.manager, "_save", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.manager.rollback(proposal["id"])
        self.assertEqual(self.manager.review(proposal["id"])["status"], "applied")
        self.assertIn("return 2", (self.source / "scalar.py").read_text())

    def test_core_rollback_restores_previous_files_and_removes_only_created_files(self):
        payload = self.core()
        payload["files"].append({"path": "extra.py", "content": "VALUE = 3\n"})
        proposal = self.applied(payload)
        self.manager.set_enabled(False)
        result = self.manager.rollback(proposal["id"])
        self.assertEqual(result["status"], "rolled_back")
        self.assertIn("return 0", (self.source / "scalar.py").read_text())
        self.assertFalse((self.source / "extra.py").exists())
        self.assertEqual(self.approval.call_args.args[0], "selfdev.rollback")

    def test_rollback_refuses_to_overwrite_a_later_edit(self):
        proposal = self.applied(self.core())
        (self.source / "scalar.py").write_text("CHANGED = True\n")
        with self.assertRaisesRegex(ValueError, "changed after"):
            self.manager.rollback(proposal["id"])
        self.assertEqual((self.source / "scalar.py").read_text(), "CHANGED = True\n")

    def test_extension_update_rollback_preserves_previous_metadata_and_code(self):
        previous = self.extension(description="Old tool", code=SOURCE.replace("* 2", "* 3"))
        self.registry.install({key: previous[key] for key in ("name", "description", "parameters")}, previous["code"])
        old = self.registry.snapshot("ext_double")
        proposal = self.applied()
        self.assertTrue(self.manager.rollback(proposal["id"])["ok"])
        self.assertEqual(self.registry.snapshot("ext_double"), old)

    def test_new_extension_rollback_removes_registration(self):
        proposal = self.applied()
        self.assertTrue(self.manager.rollback(proposal["id"])["ok"])
        self.assertEqual(self.registry.tools(), [])

    def test_traversal_symlinks_private_paths_and_unsupported_files_are_rejected(self):
        self.manager.set_enabled(True)
        outside = self.base / "outside.py"
        outside.write_text("OUTSIDE = True\n")
        (self.source / "linked.py").symlink_to(outside)
        for name in ("../outside.py", str(outside), ".git/config", ".venv/module.py", "linked.py", ".env", "photo.png"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.manager.propose({"kind": "core", "files": [{"path": name, "content": "VALUE = 2\n"}]})
        self.assertEqual(outside.read_text(), "OUTSIDE = True\n")

    def test_invalid_extension_and_python_fail_before_approval(self):
        self.manager.set_enabled(True)
        for payload in (self.extension(name="read_file"), self.extension(code="def broken(:"),
                        self.core("def broken(:"), {"kind": "core", "files": []}):
            with self.assertRaises((ValueError, SyntaxError)):
                self.manager.propose(payload)
        self.approval.assert_not_called()

    def test_inspection_is_bounded_and_validates_line(self):
        (self.source / "long.py").write_text("# line\n" * 5000)
        result = self.manager.inspect("long.py", 2)
        self.assertLessEqual(len(result["content"]), MAX_INSPECT_CHARS)
        self.assertTrue(result["truncated"])
        for line in (0, -1, True, "1"):
            with self.assertRaises(ValueError):
                self.manager.inspect("long.py", line)

    def test_authoritative_paths_are_protected_even_if_disabled(self):
        for path in (self.source / "tools.py", self.source / "web/index.html", self.source / "pyproject.toml",
                     self.source / ".git/config", self.data / "selfdev/settings.json", self.data / "extensions/ext_x.py"):
            self.assertTrue(self.manager.protect_path(path), path)
        for path in (self.source / "notes.txt", self.workspace / "snapshot.png", self.base / "external.py"):
            self.assertFalse(self.manager.protect_path(path), path)

    def test_replaced_private_directory_cannot_redirect_settings_or_records(self):
        moved = self.data / "saved-proposals"
        self.manager.proposals_dir.rename(moved)
        self.manager.proposals_dir.symlink_to(self.base)
        with self.assertRaisesRegex(ValueError, "private"):
            self.manager.set_enabled(True)
        self.assertFalse((self.base / "settings.json").exists())

    def test_handle_dispatch_and_invalid_id_cannot_read_arbitrary_records(self):
        self.assertTrue(self.manager.handle({"action": "inspect"})["ok"])
        self.assertEqual(self.manager.handle({"action": "list"})["proposals"], [])
        with self.assertRaises(ValueError):
            self.manager.handle({"action": "enable"})
        with self.assertRaises(ValueError):
            self.manager.review("../../settings")


if __name__ == "__main__":
    unittest.main()
