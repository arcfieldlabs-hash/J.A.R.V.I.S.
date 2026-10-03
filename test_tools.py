import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis.tools import ToolKit, clean_duckduckgo_url


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
