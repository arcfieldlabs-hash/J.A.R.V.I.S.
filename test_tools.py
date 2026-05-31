import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
