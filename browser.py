"""Optional persistent Chromium control, confined to a dedicated Playwright thread."""
from __future__ import annotations

from importlib import import_module as _import_module
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .research import MAX_PAGE_TEXT, validate_public_url


INSTALL_MESSAGE = (
    "Browser automation needs Playwright and Chromium. Run `pip install playwright`, "
    "then `python -m playwright install chromium`."
)


class BrowserController:
    def __init__(self, workspace: Path, approve: Callable[[str, str], bool]) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.approve = approve
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-browser")
        self._lock = threading.Lock()
        self._closed = False
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

    def dispatch(self, args: dict[str, Any], why: str = "") -> str:
        action = str(args.get("action", "")).strip().lower()
        if action not in {"navigate", "read", "click", "fill", "screenshot", "close"}:
            raise ValueError("Browser action must be navigate, read, click, fill, screenshot, or close.")
        if action == "close":
            self.close()
            return "Browser closed."
        values = dict(args)
        if action == "navigate":
            values["url"] = validate_public_url(str(args.get("url", "")))
        if action in {"click", "fill"}:
            selector = str(args.get("selector", "")).strip()
            if not selector or len(selector) > 1000:
                raise ValueError("Provide a browser selector of at most 1000 characters.")
            values["selector"] = selector
            if action == "fill":
                if "text" not in args:
                    raise ValueError("Provide text for the browser fill action.")
                values["text"] = str(args["text"])
                if len(values["text"]) > 10_000:
                    raise ValueError("Browser fill text exceeds 10000 characters.")
            if not self.approve(f"Browser {action}: {selector}", why):
                raise RuntimeError("User denied browser interaction.")
        if action == "screenshot":
            filename = str(args.get("filename", "browser.png")).strip()
            if not filename:
                raise ValueError("Provide a screenshot filename.")
            path = (self.workspace / filename).resolve()
            if not path.is_relative_to(self.workspace) or path == self.workspace:
                raise ValueError("Screenshot path is outside the configured workspace.")
            if path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
                raise ValueError("Screenshot filename must end in .png, .jpg, or .jpeg.")
            values["path"] = path
        with self._lock:
            if self._closed:
                raise RuntimeError("Browser controller is closed.")
            future = self._executor.submit(self._dispatch, action, values)
        return future.result()

    def _ensure_browser(self) -> None:
        if self._page is not None:
            if not self._page.is_closed():
                return
            self._page = self._context.new_page()
            return
        try:
            module = _import_module("playwright.sync_api")
        except ImportError as exc:
            raise RuntimeError(INSTALL_MESSAGE) from exc
        try:
            self._playwright = module.sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=False)
            self._context = self._browser.new_context(
                viewport={"width": 1280, "height": 800}, service_workers="block",
                accept_downloads=False,
            )
            self._context.set_default_timeout(15_000)
            self._context.set_default_navigation_timeout(15_000)
            self._context.route("**/*", self._route_request)
            # WebSockets are unnecessary for reading and can otherwise bypass routing.
            if hasattr(self._context, "route_web_socket"):
                self._context.route_web_socket("**/*", lambda websocket: websocket.close())
            self._page = self._context.new_page()
        except Exception as exc:
            try:
                self._close_browser()
            except Exception:
                pass  # Preserve the useful startup error if cleanup also fails.
            raise RuntimeError(f"Could not start Chromium: {exc}. {INSTALL_MESSAGE}") from exc

    @staticmethod
    def _route_request(route: Any) -> None:
        try:
            validate_public_url(route.request.url)
        except ValueError:
            route.abort("blockedbyclient")
        else:
            route.continue_()

    def _dispatch(self, action: str, args: dict[str, Any]) -> str:
        if action == "read" and (self._page is None or self._page.url == "about:blank"):
            raise RuntimeError("Navigate to a public page before reading browser content.")
        self._ensure_browser()
        if action == "navigate":
            self._page.goto(args["url"], wait_until="domcontentloaded", timeout=15_000)
            validate_public_url(self._page.url)
        elif action == "click":
            self._page.locator(args["selector"]).click(timeout=15_000)
        elif action == "fill":
            self._page.locator(args["selector"]).fill(args["text"], timeout=15_000)
        elif action == "screenshot":
            path = args["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            self._page.screenshot(path=str(path), full_page=False, timeout=15_000)
            return f"Saved browser screenshot to {path}."
        if self._page.url != "about:blank":
            validate_public_url(self._page.url)
        return json.dumps({
            "url": self._page.url, "title": self._page.title()[:500],
            "text": self._page.locator("body").inner_text(timeout=15_000)[:MAX_PAGE_TEXT],
        }, ensure_ascii=False)

    def _close_browser(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            try:
                if self._playwright is not None:
                    self._playwright.stop()
            finally:
                self._playwright = self._browser = self._context = self._page = None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            future = self._executor.submit(self._close_browser)
        try:
            future.result()
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)
