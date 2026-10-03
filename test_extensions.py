import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis.extensions import (
    ExtensionRegistry, MAX_ARGS_BYTES, MAX_EXTENSIONS,
    validate_code, validate_metadata,
)


METADATA = {
    "name": "ext_echo",
    "description": "Return a supplied message and the active workspace.",
    "parameters": {
        "type": "object", "properties": {"message": {"type": "string"}},
        "required": ["message"], "additionalProperties": False,
    },
}
SOURCE = "def run(args, context):\n    return {'message': args['message'], 'workspace': context['workspace']}\n"


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.data_dir = self.base / "data"
        self.registry = ExtensionRegistry(self.data_dir / "extensions", self.workspace, self.data_dir)

    def tearDown(self):
        self.temporary.cleanup()

    def test_install_immediately_exposes_tool_and_runs_after_approval(self):
        installed = self.registry.install(METADATA, SOURCE)
        self.assertTrue(installed["ok"])
        self.assertEqual(installed["code_sha256"], hashlib.sha256(SOURCE.encode()).hexdigest())
        self.assertEqual(self.registry.tools(), [METADATA])
        self.assertEqual(self.registry.catalog(), [METADATA])
        permission = Mock(return_value=True)
        answer = self.registry.invoke("ext_echo", {"message": "hello"}, permission)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(json.loads(answer["content"]), {"message": "hello", "workspace": str(self.workspace)})
        action, details = permission.call_args.args
        self.assertEqual(action, "Run extension ext_echo")
        self.assertEqual(details["code"], SOURCE)
        self.assertEqual(details["args"], {"message": "hello"})
        self.assertEqual(details["code_sha256"], installed["code_sha256"])

    def test_install_and_discovery_never_import_plugin_in_parent(self):
        marker = self.workspace / "imported.txt"
        source = f"from pathlib import Path\nPath({str(marker)!r}).write_text('child')\n" + SOURCE
        self.registry.install(METADATA, source)
        restarted = ExtensionRegistry(self.registry.directory, self.workspace, self.data_dir)
        self.assertEqual(len(restarted.tools()), 1)
        self.assertFalse(marker.exists())
        denied = restarted.invoke("ext_echo", {"message": "hello"}, lambda *_: False)
        self.assertFalse(denied["ok"])
        self.assertFalse(marker.exists())
        answer = restarted.invoke("ext_echo", {"message": "hello"}, lambda *_: True)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(marker.read_text(), "child")

    def test_approval_required_on_every_invocation(self):
        self.registry.install(METADATA, SOURCE)
        permission = Mock(side_effect=[True, False])
        first = self.registry.invoke("ext_echo", {"message": "one"}, permission)
        second = self.registry.invoke("ext_echo", {"message": "two"}, permission)
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(permission.call_count, 2)

    def test_no_permission_handler_denies_execution(self):
        self.registry.install(METADATA, SOURCE)
        answer = self.registry.invoke("ext_echo", {"message": "hello"}, None)
        self.assertFalse(answer["ok"])
        self.assertIn("not approved", answer["content"])

    def test_source_changed_after_approval_is_not_executed(self):
        self.registry.install(METADATA, SOURCE)
        marker = self.workspace / "bad.txt"
        def permission(*_):
            changed = f"def run(args, context):\n    open({str(marker)!r}, 'w').write('bad')\n"
            (self.registry.directory / "ext_echo.py").write_text(changed)
            return True
        answer = self.registry.invoke("ext_echo", {"message": "hello"}, permission)
        self.assertFalse(answer["ok"])
        self.assertIn("changed", answer["content"])
        self.assertFalse(marker.exists())

    def test_manifest_changed_with_matching_hash_during_approval_is_rejected(self):
        self.registry.install(METADATA, SOURCE)
        def permission(*_):
            self.registry.install({**METADATA, "description": "Different behavior."}, SOURCE)
            return True
        answer = self.registry.invoke("ext_echo", {"message": "hello"}, permission)
        self.assertFalse(answer["ok"])
        self.assertIn("changed during approval", answer["content"])

    def test_mutating_approval_details_cannot_change_executed_args(self):
        self.registry.install(METADATA, SOURCE)
        args = {"message": "approved"}
        def permission(_, details):
            details["args"]["message"] = "not approved"
            args["message"] = "caller mutation"
            return True
        answer = self.registry.invoke("ext_echo", args, permission)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(json.loads(answer["content"])["message"], "approved")

    def test_hash_mismatch_and_corrupt_manifests_are_ignored_on_restart(self):
        self.registry.install(METADATA, SOURCE)
        (self.registry.directory / "ext_echo.py").write_text(SOURCE + "# tampered\n")
        (self.registry.directory / "ext_broken.json").write_text("not json")
        (self.registry.directory / "ext_missing.json").write_text(json.dumps({**METADATA, "name": "ext_missing"}))
        restarted = ExtensionRegistry(self.registry.directory, self.workspace, self.data_dir)
        self.assertEqual(restarted.tools(), [])
        permission = Mock(return_value=True)
        self.assertFalse(restarted.invoke("ext_echo", {"message": "hello"}, permission)["ok"])
        permission.assert_not_called()

    def test_symlinks_and_invalid_names_do_not_escape_registry(self):
        external = self.base / "outside.py"
        external.write_text(SOURCE)
        (self.registry.directory / "ext_echo.py").symlink_to(external)
        with self.assertRaises(ValueError):
            self.registry.install(METADATA, SOURCE)
        self.assertEqual(external.read_text(), SOURCE)
        for name in ("../escape", "read_file", "ext_", "ext_A", "ext_a/b", "ext_" + "a" * 41):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_metadata({**METADATA, "name": name})

    def test_replaced_extension_directory_is_rejected(self):
        directory = self.registry.directory
        directory.rmdir()
        directory.symlink_to(self.workspace, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.registry.tools()

    def test_directory_and_files_are_private(self):
        self.registry.install(METADATA, SOURCE)
        self.assertEqual(self.registry.directory.stat().st_mode & 0o777, 0o700)
        for suffix in (".py", ".json"):
            self.assertEqual((self.registry.directory / ("ext_echo" + suffix)).stat().st_mode & 0o777, 0o600)

    def test_code_requires_plain_run_entry_and_compiles_without_execution(self):
        for source in ("x = 1", "def run(x):\n return x", "async def run(args, context):\n return 1",
                       "@print\ndef run(args, context):\n return 1", "def run(args, context=None):\n return 1",
                       "def run(args, context):\n return (", SOURCE + SOURCE):
            with self.subTest(source=source), self.assertRaises(ValueError):
                validate_code(source)

    def test_metadata_rejects_invalid_schema_and_digest(self):
        invalid = [
            {**METADATA, "parameters": {"type": "array"}},
            {**METADATA, "parameters": {"type": "object", "required": ["missing"]}},
            {**METADATA, "parameters": {"type": "object", "$ref": "http://example.test"}},
            {**METADATA, "parameters": {"type": "object", "properties": {"message": {"type": "invalid"}}}},
            {**METADATA, "code_sha256": "fake"},
            {**METADATA, "extra": True}, {**METADATA, "description": ""},
        ]
        for metadata in invalid:
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                validate_metadata(metadata)

    def test_argument_types_required_fields_and_limit_checked_before_approval(self):
        self.registry.install(METADATA, SOURCE)
        permission = Mock(return_value=True)
        for args in ([], {}, {"message": 3}, {"message": "a", "other": True},
                     {"message": "a" * MAX_ARGS_BYTES}):
            with self.subTest(args_type=type(args).__name__):
                self.assertFalse(self.registry.invoke("ext_echo", args, permission)["ok"])
        permission.assert_not_called()

    def test_staged_source_testing_does_not_install_extension(self):
        answer = self.registry.run_source(METADATA, SOURCE, {"message": "test"}, lambda *_: True)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.registry.tools(), [])
        self.assertIsNone(self.registry.snapshot("ext_echo"))

    def test_restore_previous_version_and_remove_new_install(self):
        self.registry.install(METADATA, SOURCE)
        original = self.registry.snapshot("ext_echo")
        new_source = "def run(args, context):\n    return 'new'\n"
        self.registry.install(METADATA, new_source)
        self.registry.restore("ext_echo", original)
        self.assertEqual(self.registry.read("ext_echo"), original)
        self.registry.restore("ext_echo", None)
        self.assertEqual(self.registry.tools(), [])
        self.assertIsNone(self.registry.snapshot("ext_echo"))

    def test_install_limits_number_of_tools(self):
        for index in range(MAX_EXTENSIONS):
            self.registry.install({**METADATA, "name": f"ext_tool_{index}"}, SOURCE)
        with self.assertRaises(ValueError):
            self.registry.install(METADATA, SOURCE)
        self.assertEqual(len(self.registry.tools()), MAX_EXTENSIONS)
        self.registry.install({**METADATA, "name": "ext_tool_0"}, SOURCE)

    def test_stdout_prints_cannot_corrupt_json_protocol(self):
        source = "import os\nprint('import log')\ndef run(args, context):\n    print('run log')\n    os.write(1, b'native log\\n')\n    return 'done'\n"
        answer = self.registry.run_source(METADATA, source, {"message": "test"}, lambda *_: True)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["content"], "done")
        self.assertIn("import log", answer["logs"])
        self.assertIn("run log", answer["logs"])
        self.assertIn("native log", answer["logs"])

    def test_timeout_stops_child_and_descendant(self):
        pid_file = self.workspace / "descendant.pid"
        source = (
            "import subprocess, sys, time\n"
            "def run(args, context):\n"
            "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"    open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
            "    time.sleep(60)\n"
        )
        with patch("jarvis.extensions.RUN_TIMEOUT", 0.5):
            answer = self.registry.run_source(METADATA, source, {"message": "test"}, lambda *_: True)
        self.assertFalse(answer["ok"])
        self.assertIn("timed out", answer["content"])
        self.assertTrue(pid_file.exists())
        pid = int(pid_file.read_text())
        # A killed process can briefly remain a zombie until the OS reaps it.
        import psutil
        try:
            self.assertIn(psutil.Process(pid).status(), (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD))
        except psutil.NoSuchProcess:
            pass

    def test_large_result_and_print_flood_are_bounded(self):
        result_source = "def run(args, context):\n    return 'x' * 100000\n"
        answer = self.registry.run_source(METADATA, result_source, {"message": "test"}, lambda *_: True)
        self.assertFalse(answer["ok"])
        self.assertIn("result exceeds", answer["content"])
        print_source = "import os\ndef run(args, context):\n    while True: os.write(1, b'x' * 8192)\n"
        started = time.monotonic()
        answer = self.registry.run_source(METADATA, print_source, {"message": "test"}, lambda *_: True)
        self.assertFalse(answer["ok"])
        self.assertIn("stderr exceeded", answer["content"])
        self.assertLessEqual(len(answer["logs"]), 16_384)
        self.assertLess(time.monotonic() - started, 3)

    def test_extension_error_is_actionable(self):
        source = "def run(args, context):\n    raise RuntimeError('Cannot complete this test')\n"
        answer = self.registry.run_source(METADATA, source, {"message": "test"}, lambda *_: True)
        self.assertFalse(answer["ok"])
        self.assertIn("RuntimeError: Cannot complete", answer["content"])


if __name__ == "__main__":
    unittest.main()
