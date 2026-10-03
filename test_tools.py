import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, mock_open, patch

from jarvis.tools import MAX_FILE_BYTES, ToolKit, clean_duckduckgo_url


class ToolKitTests(unittest.TestCase):
    def test_blocks_paths_outside_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            toolkit = ToolKit(workspace=Path(tmp))
            result = toolkit.read_file({"path": "../secret.txt"})
            self.assertFalse(result.ok)

    def test_write_and_read_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            toolkit = ToolKit(workspace=Path(tmp))
            written = toolkit.write_file(
                {"path": "notes/test.txt", "content": "hello", "mode": "overwrite"}
            )
            self.assertTrue(written.ok)
            read = toolkit.read_file({"path": "notes/test.txt"})
            self.assertTrue(read.ok)
            self.assertEqual(read.content, "hello")

    def test_storage_access_grant_and_revoke_apply_to_read_write_and_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workspace = base / "workspace"
            external = base / "external"
            external.mkdir()
            file = external / "notes.txt"
            file.write_text("private notes")
            toolkit = ToolKit(workspace=workspace)
            self.assertFalse(toolkit.read_file({"path": str(file)}).ok)
            self.assertFalse(toolkit.write_file({"path": str(file), "content": "changed"}).ok)
            self.assertFalse(toolkit.list_files({"path": str(external)}).ok)

            toolkit.set_full_disk_access(True)
            self.assertEqual(toolkit.read_file({"path": str(file)}).content, "private notes")
            self.assertTrue(toolkit.write_file({"path": str(file), "content": "changed"}).ok)
            self.assertIn("notes.txt", toolkit.list_files({"path": str(external)}).content)
            self.assertEqual(file.read_text(), "changed")

            toolkit.set_full_disk_access(False)
            self.assertFalse(toolkit.read_file({"path": str(file)}).ok)
            self.assertFalse(toolkit.write_file({"path": str(file), "content": "denied"}).ok)
            self.assertFalse(toolkit.list_files({"path": str(external)}).ok)
            self.assertEqual(file.read_text(), "changed")
            self.assertTrue(toolkit.write_file({"path": "workspace-note.txt", "content": "allowed"}).ok)

    def test_full_storage_access_can_be_enabled_at_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            file = base / "outside.txt"
            file.write_text("outside")
            toolkit = ToolKit(workspace=base / "workspace", full_disk_access=True)
            self.assertTrue(toolkit.full_disk_access)
            self.assertEqual(toolkit.read_file({"path": str(file)}).content, "outside")

    def test_storage_grant_must_be_a_boolean(self):
        with tempfile.TemporaryDirectory() as tmp:
            toolkit = ToolKit(workspace=Path(tmp))
            with self.assertRaises(ValueError):
                toolkit.set_full_disk_access("false")
            self.assertFalse(toolkit.full_disk_access)

    def test_symlink_outside_workspace_is_blocked_again_after_revocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            toolkit = ToolKit(workspace=base / "workspace")
            outside = base / "outside.txt"
            outside.write_text("outside")
            (toolkit.workspace / "link.txt").symlink_to(outside)
            self.assertFalse(toolkit.read_file({"path": "link.txt"}).ok)
            toolkit.set_full_disk_access(True)
            self.assertEqual(toolkit.read_file({"path": "link.txt"}).content, "outside")
            toolkit.set_full_disk_access(False)
            self.assertFalse(toolkit.read_file({"path": "link.txt"}).ok)

    def test_reads_at_most_file_limit_plus_truncation_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            toolkit = ToolKit(workspace=Path(tmp))
            (toolkit.workspace / "large.txt").touch()
            opened = mock_open(read_data=b"a" * (MAX_FILE_BYTES + 1))
            with patch("jarvis.tools.Path.open", opened):
                result = toolkit.read_file({"path": "large.txt"})
            self.assertTrue(result.ok)
            self.assertEqual(result.content, "a" * MAX_FILE_BYTES + "\n...[file truncated]")
            opened().read.assert_called_once_with(MAX_FILE_BYTES + 1)

    def test_full_storage_access_does_not_bypass_os_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            toolkit = ToolKit(workspace=Path(tmp), full_disk_access=True)
            (toolkit.workspace / "protected.txt").touch()
            with patch("jarvis.tools.Path.open", side_effect=PermissionError("Permission denied by macOS")):
                result = toolkit.execute("read_file", {"path": "protected.txt"})
            self.assertFalse(result.ok)
            self.assertIn("Permission denied by macOS", result.content)

    def test_full_storage_access_preserves_shell_permission_and_destructive_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            permission = Mock(return_value=False)
            toolkit = ToolKit(workspace=Path(tmp), full_disk_access=True, permission_handler=permission)
            with patch("jarvis.tools.subprocess.run") as run:
                self.assertFalse(toolkit.run_command({"command": "pwd"}).ok)
                self.assertFalse(toolkit.run_command({"command": "rm -rf /tmp/files"}).ok)
            permission.assert_called_once_with("pwd", "")
            run.assert_not_called()

    def test_system_status_uses_attached_live_monitor_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            toolkit = ToolKit(workspace=Path(tmp))
            metrics = {"cpu_percent": 12.5, "memory": {"percent": 40}, "gpu": None}
            toolkit.monitor = Mock()
            toolkit.monitor.snapshot.return_value = metrics
            with patch("jarvis.tools.subprocess.run") as run:
                result = toolkit.system_status({})
            self.assertTrue(result.ok)
            self.assertEqual(json.loads(result.content), metrics)
            run.assert_not_called()

    def test_native_screen_capture_can_be_disabled_for_selected_browser_sharing(self):
        with tempfile.TemporaryDirectory() as tmp:
            toolkit = ToolKit(workspace=Path(tmp))
            toolkit.screen_capture_allowed = False
            with patch("jarvis.tools.subprocess.run") as run:
                result = toolkit.execute("screenshot", {})
            self.assertFalse(result.ok)
            self.assertEqual(
                result.content, "Use the selected screen controls in the local orb to capture a snapshot.",
            )
            run.assert_not_called()

    def test_native_screen_capture_remains_available_by_default_for_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            toolkit = ToolKit(workspace=Path(tmp))
            self.assertTrue(toolkit.screen_capture_allowed)
            with patch("jarvis.tools.subprocess.run", return_value=Mock(returncode=0)) as run:
                result = toolkit.execute("screenshot", {"filename": "screen.png"})
            self.assertTrue(result.ok)
            self.assertEqual(run.call_args.args[0], [
                "screencapture", "-x", "-t", "png", str(toolkit.workspace / "screen.png"),
            ])

    def test_cleans_duckduckgo_redirect(self):
        url = clean_duckduckgo_url(
            "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fhello"
        )
        self.assertEqual(url, "https://example.com/hello")

    def test_google_arguments_are_never_executed_as_shell_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            toolkit = ToolKit(workspace=Path(tmp))
            with patch("jarvis.tools.shutil.which", return_value="/usr/local/bin/gog"), \
                    patch("jarvis.tools.subprocess.run") as run:
                run.return_value = Mock(returncode=0, stdout="No messages", stderr="")
                result = toolkit.execute("google", {"command": "gmail list ';' 'echo injected'"})
            self.assertTrue(result.ok)
            self.assertEqual(run.call_args.args[0], ["gog", "gmail", "list", ";", "echo injected"])
            self.assertNotIn("shell", run.call_args.kwargs)

    def test_browser_can_be_reopened_after_explicit_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            toolkit = ToolKit(workspace=Path(tmp))
            previous = Mock()
            toolkit._browser = previous
            self.assertTrue(toolkit.execute("browser", {"action": "close"}).ok)
            previous.close.assert_called_once()
            with patch("jarvis.browser.BrowserController") as controller:
                controller.return_value.dispatch.return_value = "Page loaded."
                result = toolkit.execute("browser", {"action": "navigate", "url": "https://example.com"})
            self.assertTrue(result.ok)
            controller.assert_called_once()
            toolkit.close()


if __name__ == "__main__":
    unittest.main()
