import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jarvis.browser import BrowserController


class FakePage:
    def __init__(self):
        self.url = "about:blank"
        self.calls = []
        self.thread_ids = []

    def is_closed(self):
        return False

    def record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        self.thread_ids.append(threading.get_ident())

    def goto(self, url, **kwargs):
        self.record("goto", url, **kwargs)
        self.url = url

    def title(self):
        self.record("title")
        return "Example"

    def locator(self, selector):
        self.record("locator", selector)
        return SimpleNamespace(
            click=lambda **kwargs: self.record("click", selector, **kwargs),
            fill=lambda text, **kwargs: self.record("fill", selector, text, **kwargs),
            inner_text=lambda **kwargs: "Page contents",
        )

    def screenshot(self, **kwargs):
        self.record("screenshot", **kwargs)


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.page = FakePage()
        self.context = Mock()
        self.context.new_page.return_value = self.page
        self.browser = Mock()
        self.browser.new_context.return_value = self.context
        self.runtime = Mock()
        self.runtime.chromium.launch.return_value = self.browser
        self.module = Mock()
        self.module.sync_playwright.return_value.start.return_value = self.runtime
        self.approve = Mock(return_value=True)
        self.controller = BrowserController(Path(self.tmp.name), self.approve)

    def tearDown(self):
        self.controller.close()
        self.tmp.cleanup()

    def test_retains_page_on_one_thread_across_calling_threads(self):
        with patch("jarvis.browser._import_module", return_value=self.module), \
                patch("jarvis.browser.validate_public_url", side_effect=lambda url: url):
            first = json.loads(self.controller.dispatch({"action": "navigate", "url": "https://example.com"}))
            with ThreadPoolExecutor(max_workers=1) as caller:
                second = json.loads(caller.submit(self.controller.dispatch, {"action": "read"}).result(timeout=3))
            self.controller.dispatch({"action": "fill", "selector": "#name", "text": "Jarvis"}, "Enter name")
            self.controller.dispatch({"action": "click", "selector": "#submit"}, "Submit form")
        self.assertEqual(first, second)
        self.context.new_page.assert_called_once()
        self.assertEqual(len(set(self.page.thread_ids)), 1)
        self.assertNotEqual(self.page.thread_ids[0], threading.get_ident())
        self.approve.assert_any_call("Browser fill: #name", "Enter name")
        self.approve.assert_any_call("Browser click: #submit", "Submit form")
        self.context.route.assert_called_once()
        self.context.route_web_socket.assert_called_once()

    def test_denied_interaction_does_not_start_browser(self):
        self.approve.return_value = False
        with patch("jarvis.browser._import_module") as load, \
                self.assertRaisesRegex(RuntimeError, "denied"):
            self.controller.dispatch({"action": "click", "selector": "#pay"}, "Pay")
        load.assert_not_called()
        self.assertEqual(self.page.calls, [])

    def test_missing_dependency_has_install_instructions(self):
        with patch("jarvis.browser._import_module", side_effect=ImportError("missing")), \
                patch("jarvis.browser.validate_public_url", side_effect=lambda url: url), \
                self.assertRaisesRegex(RuntimeError, "python -m playwright install chromium"):
            self.controller.dispatch({"action": "navigate", "url": "https://example.com"})

    def test_missing_chromium_cleans_up(self):
        self.runtime.chromium.launch.side_effect = RuntimeError("Executable does not exist")
        with patch("jarvis.browser._import_module", return_value=self.module), \
                patch("jarvis.browser.validate_public_url", side_effect=lambda url: url), \
                self.assertRaisesRegex(RuntimeError, "pip install playwright"):
            self.controller.dispatch({"action": "navigate", "url": "https://example.com"})
        self.runtime.stop.assert_called_once()

    def test_startup_error_survives_cleanup_failure(self):
        self.runtime.chromium.launch.side_effect = RuntimeError("Missing executable")
        self.runtime.stop.side_effect = RuntimeError("Cleanup failed")
        with patch("jarvis.browser._import_module", return_value=self.module), \
                patch("jarvis.browser.validate_public_url", side_effect=lambda url: url), \
                self.assertRaisesRegex(RuntimeError, "Missing executable"):
            self.controller.dispatch({"action": "navigate", "url": "https://example.com"})
        self.assertIsNone(self.controller._playwright)

    def test_screenshot_stays_in_workspace(self):
        for filename in ("../outside.png", "/tmp/outside.png", "image.txt"):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                self.controller.dispatch({"action": "screenshot", "filename": filename})
        with patch("jarvis.browser._import_module", return_value=self.module):
            result = self.controller.dispatch({"action": "screenshot", "filename": "images/browser.png"})
        self.assertIn(str(Path(self.tmp.name) / "images/browser.png"), result)
        screenshot = next(call for call in self.page.calls if call[0] == "screenshot")
        self.assertFalse(screenshot[2]["full_page"])

    def test_blocks_private_network_requests(self):
        route = Mock()
        route.request.url = "http://localhost/secrets"
        with patch("jarvis.browser.validate_public_url", side_effect=ValueError("private")):
            self.controller._route_request(route)
        route.abort.assert_called_once_with("blockedbyclient")
        route.continue_.assert_not_called()

    def test_close_is_idempotent_and_prevents_more_work(self):
        with patch("jarvis.browser._import_module", return_value=self.module), \
                patch("jarvis.browser.validate_public_url", side_effect=lambda url: url):
            self.controller.dispatch({"action": "navigate", "url": "https://example.com"})
            self.controller.close()
            self.controller.close()
        self.browser.close.assert_called_once()
        self.runtime.stop.assert_called_once()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.controller.dispatch({"action": "read"})

    def test_read_before_navigate_fails_without_browser_startup(self):
        with patch("jarvis.browser._import_module") as load, \
                self.assertRaisesRegex(RuntimeError, "Navigate"):
            self.controller.dispatch({"action": "read"})
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
