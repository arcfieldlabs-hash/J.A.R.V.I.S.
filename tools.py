from __future__ import annotations

import json
import ast
import math
import operator
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib import error, parse, request

from . import desktop as desktop_mod
from .memory import MemoryStore

try:
    from . import google_native as gnative
except Exception:  # pragma: no cover
    gnative = None  # type: ignore


MAX_FILE_BYTES = 120_000
MAX_COMMAND_OUTPUT = 12_000

CORE_TOOL_NAMES = (
    "open_app", "run_command", "web_search", "read_file", "write_file", "list_files",
    "system_status", "notify", "set_volume", "open_url", "activate_app", "frontmost_app",
    "screenshot", "list_windows", "desktop", "google", "google_native", "memory",
    "reminder", "calculate", "web_read", "browser", "research", "selfdev",
)

DANGEROUS_COMMAND_PATTERNS = [
    r"\brm\s+-[^\n]*r",
    r"\bsudo\b",
    r"\bdd\b",
    r"\bmkfs\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bdiskutil\s+erase",
    r"\bchmod\s+-R\s+777\b",
    r"\bchown\s+-R\b",
    r":\(\)\s*\{",
]


@dataclass
class ToolResult:
    ok: bool
    content: str

    def to_json(self) -> str:
        return json.dumps({"ok": self.ok, "content": self.content}, ensure_ascii=False)


class ToolKit:
    def __init__(
        self, *, workspace: Path, data_dir: Path | None = None,
        permission_handler: Callable[[str, str], bool] | None = None,
        full_disk_access: bool = False,
    ) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.memory = MemoryStore(data_dir or self.workspace / ".jarvis")
        self.permission_handler = permission_handler
        self.full_disk_access = False
        self.set_full_disk_access(full_disk_access)
        self.screen_capture_allowed = True
        self.monitor = None
        self._browser = None
        self._research = None
        self._extensions = None
        self._development = None
        self.development_permission_handler: Callable[[str, dict[str, Any]], bool] | None = None

    @property
    def extensions(self):
        if self._extensions is None:
            from .extensions import ExtensionRegistry
            data_dir = self.memory.db_path.parent
            self._extensions = ExtensionRegistry(
                data_dir / "extensions", self.workspace, data_dir,
            )
        return self._extensions

    @property
    def development(self):
        if self._development is None:
            from .selfdev import SelfDevelopment
            self._development = SelfDevelopment(
                source_root=Path(__file__).resolve().parent,
                data_dir=self.memory.db_path.parent,
                workspace=self.workspace,
                registry=self.extensions,
                permission_handler=self._development_permission,
            )
        return self._development

    def extension_catalog(self) -> list[dict[str, Any]]:
        # Avoid creating private development storage during ordinary startup.
        if self._extensions is None and not (self.memory.db_path.parent / "extensions").exists():
            return []
        return self.extensions.catalog()

    @property
    def available_tools(self) -> tuple[str, ...]:
        return (*CORE_TOOL_NAMES, *(entry["name"] for entry in self.extension_catalog()))

    def _development_permission(self, action: str, details: dict[str, Any]) -> bool:
        if self.development_permission_handler is not None:
            return bool(self.development_permission_handler(action, details))
        print("\nJARVIS code review:")
        print(json.dumps(details, ensure_ascii=False, indent=2))
        return self._ask_permission(action, "Review the complete code and arguments above before allowing it.")

    def set_full_disk_access(self, enabled: bool) -> None:
        """Allow file tools to use paths the operating system permits.

        This switches Jarvis's workspace restriction; it does not grant macOS
        privacy permissions, administrator access, or permission to run shell
        commands. Disabling it restores the workspace restriction immediately.
        """
        if not isinstance(enabled, bool):
            raise ValueError("Full storage access must be enabled or disabled with a boolean.")
        self.full_disk_access = enabled

    @property
    def research(self):
        if self._research is None:
            from .research import ResearchManager
            self._research = ResearchManager(self.workspace, self.search_results)
        return self._research

    def close(self) -> None:
        if self._research is not None:
            self._research.close()
            self._research = None
        if self._browser is not None:
            self._browser.close()
            self._browser = None

    def execute(self, name: str, args: dict[str, Any], *, why: str = "") -> ToolResult:
        tools = {
            "open_app": self.open_app,
            "run_command": self.run_command,
            "web_search": self.web_search,
            "read_file": self.read_file,
            "write_file": self.write_file,
            "list_files": self.list_files,
            "system_status": self.system_status,
            "notify": self.notify,
            "set_volume": self.set_volume,
            "open_url": self.open_url,
            # Desktop
            "activate_app": self.activate_app,
            "frontmost_app": self.frontmost_app,
            "screenshot": self.screenshot,
            "list_windows": self.list_windows,
            "desktop": self.desktop,
            # Google
            "google": self.google,
            "google_native": self.google_native,
            "memory": self.memory_tool,
            "reminder": self.reminder,
            "calculate": self.calculate,
            "web_read": self.web_read,
            "browser": self.browser,
            "research": self.research_tool,
            "selfdev": self.selfdev_tool,
        }
        tool = tools.get(name)
        try:
            if tool is None:
                if name not in {entry["name"] for entry in self.extension_catalog()}:
                    return ToolResult(False, f"Unknown tool: {name}")
                if not self.development.enabled:
                    return ToolResult(False, "Self-development is off. Enable it in Permissions or with :develop on before running an extension.")
                def approve_extension(action, details):
                    return (self.development.enabled
                            and self._development_permission(action, details)
                            and self.development.enabled)
                result = self.extensions.invoke(name, args, approve_extension)
                return ToolResult(bool(result.get("ok")), str(result.get("content", "")))
            return tool(args, why=why)
        except Exception as exc:
            return ToolResult(False, f"{type(exc).__name__}: {exc}")

    def selfdev_tool(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        result = self.development.handle(args)
        return ToolResult(bool(result.get("ok")), json.dumps(result, ensure_ascii=False))

    # ── Core ────────────────────────────────────────────────────

    def open_app(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        app_name = str(args.get("name", "")).strip()
        if not app_name:
            return ToolResult(False, "Missing app name.")
        completed = subprocess.run(["open", "-a", app_name], capture_output=True, text=True)
        if completed.returncode != 0:
            return ToolResult(False, completed.stderr.strip() or f"Could not open {app_name}.")
        return ToolResult(True, f"Opened {app_name}.")

    def run_command(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        command = str(args.get("command", "")).strip()
        if not command:
            return ToolResult(False, "Missing command.")
        if self._looks_dangerous(command):
            return ToolResult(False, "Blocked a command that looked destructive.")
        if not self._ask_permission(command, why):
            return ToolResult(False, "User denied command execution.")

        completed = subprocess.run(
            command, shell=True, cwd=str(self.workspace),
            capture_output=True, text=True, timeout=60,
        )
        output = "\n".join(part for part in [completed.stdout, completed.stderr] if part).strip()
        if not output:
            output = f"Command exited with code {completed.returncode} and no output."
        if len(output) > MAX_COMMAND_OUTPUT:
            output = output[:MAX_COMMAND_OUTPUT] + "\n...[output truncated]"
        return ToolResult(completed.returncode == 0, output)

    def web_search(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        query = str(args.get("query", "")).strip()
        limit = max(1, min(int(args.get("limit", 5) or 5), 8))
        if not query:
            return ToolResult(False, "Missing search query.")

        try:
            results = self.search_results(query, limit)
        except (error.URLError, TimeoutError) as exc:
            return ToolResult(False, f"Web search failed: {exc}")
        if not results:
            return ToolResult(False, "No search results were parsed.")

        lines = [f"{i}. {r['title']}\n   {r['url']}" for i, r in enumerate(results, 1)]
        return ToolResult(True, "\n".join(lines))

    def search_results(self, query: str, limit: int = 5) -> list[dict[str, str]]:
        url = "https://duckduckgo.com/html/?" + parse.urlencode({"q": query[:500]})
        req = request.Request(url, headers={"User-Agent": "Mozilla/5.0 JarvisLocal/1.0"})
        with request.urlopen(req, timeout=20) as response:
            html = response.read(1_000_000).decode("utf-8", errors="replace")
        parser = DuckDuckGoHTMLParser()
        parser.feed(html)
        return parser.results[:max(1, min(limit, 8))]

    def memory_tool(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        action = str(args.get("action", "recall")).lower()
        if action == "remember":
            memory_id = self.memory.remember(args.get("text", ""))
            return ToolResult(True, f"Remembered fact {memory_id}.")
        if action == "recall":
            facts = self.memory.recall(args.get("query", ""), args.get("limit", 5))
            return ToolResult(True, json.dumps(facts, ensure_ascii=False))
        if action == "forget":
            removed = self.memory.forget(args["id"])
            return ToolResult(removed, "Forgot that fact." if removed else "No fact with that ID.")
        return ToolResult(False, "memory actions: remember(text), recall(query), forget(id).")

    def reminder(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        action = str(args.get("action", "list")).lower()
        if action == "add":
            result = self.memory.add_reminder(args.get("text", ""), args.get("due_at", ""))
            return ToolResult(True, json.dumps(result))
        if action == "list":
            return ToolResult(True, json.dumps(self.memory.list_reminders()))
        if action == "cancel":
            removed = self.memory.cancel_reminder(args["id"])
            return ToolResult(removed, "Cancelled reminder." if removed else "No pending reminder with that ID.")
        return ToolResult(False, "reminder actions: add(text,due_at ISO8601 with timezone), list, cancel(id).")

    def calculate(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        expression = str(args.get("expression", "")).strip()
        if not expression or len(expression) > 200:
            return ToolResult(False, "Provide an arithmetic expression of at most 200 characters.")
        tree = ast.parse(expression, mode="eval")
        if len(list(ast.walk(tree))) > 80:
            return ToolResult(False, "Expression is too complex.")
        binary = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
                  ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
                  ast.Mod: operator.mod, ast.Pow: operator.pow}

        def evaluate(node):
            if isinstance(node, ast.Constant) and type(node.value) in (int, float):
                value = node.value
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                value = evaluate(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
            elif isinstance(node, ast.BinOp) and type(node.op) in binary:
                left, right = evaluate(node.left), evaluate(node.right)
                if isinstance(node.op, ast.Pow) and abs(right) > 100:
                    raise ValueError("Exponent must be within -100 to 100.")
                value = binary[type(node.op)](left, right)
            else:
                raise ValueError("Only numbers and arithmetic operators are supported.")
            if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e100:
                raise ValueError("Result is outside the supported numeric range.")
            return value

        return ToolResult(True, str(evaluate(tree.body)))

    def web_read(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        from .research import fetch_page
        return ToolResult(True, json.dumps(fetch_page(str(args.get("url", ""))), ensure_ascii=False))

    def browser(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        if args.get("action") == "close":
            if self._browser is not None:
                self._browser.close()
                self._browser = None
            return ToolResult(True, "Browser closed.")
        if args.get("action") == "screenshot":
            destination = (self.workspace / str(args.get("filename", "browser.png"))).resolve()
            if self.development.protect_path(destination):
                return ToolResult(False, "Screenshot output cannot overwrite Jarvis source or development approvals.")
        if self._browser is None:
            from .browser import BrowserController
            self._browser = BrowserController(self.workspace, self._ask_permission)
        return ToolResult(True, self._browser.dispatch(args, why))

    def research_tool(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        action = str(args.get("action", "start")).lower()
        if action == "start":
            result = self.research.start(str(args.get("query", "")))
        elif action == "status":
            result = self.research.status(str(args.get("id", "")))
        elif action == "list":
            result = self.research.list_jobs()
        else:
            return ToolResult(False, "research actions: start(query), status(id), list.")
        return ToolResult(True, json.dumps(result, ensure_ascii=False))

    def read_file(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        path = self._safe_path(str(args.get("path", "")).strip())
        if path is None:
            return ToolResult(False, "File path is outside the configured workspace.")
        if not path.exists():
            return ToolResult(False, f"File does not exist: {path}")
        if path.is_dir():
            return ToolResult(False, f"Path is a directory: {path}")

        with path.open("rb") as file:
            data = file.read(MAX_FILE_BYTES + 1)
        truncated = len(data) > MAX_FILE_BYTES
        text = data[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
        if truncated:
            text += "\n...[file truncated]"
        return ToolResult(True, text)

    def write_file(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        path = self._safe_path(str(args.get("path", "")).strip())
        content = str(args.get("content", ""))
        mode = str(args.get("mode", "overwrite")).strip().lower()
        if path is None:
            return ToolResult(False, "File path is outside the configured workspace.")
        if mode not in {"overwrite", "append"}:
            return ToolResult(False, "Mode must be overwrite or append.")
        if self.development.protect_path(path):
            return ToolResult(False, "Jarvis source and development approvals are protected. Use selfdev propose, review, test, and apply for approved changes.")

        path.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append":
            with path.open("a", encoding="utf-8") as file:
                file.write(content)
        else:
            path.write_text(content, encoding="utf-8")
        return ToolResult(True, f"Wrote {len(content)} characters to {path}.")

    def list_files(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        path = self._safe_path(str(args.get("path", ".")).strip() or ".")
        if path is None:
            return ToolResult(False, "Path is outside the configured workspace.")
        if not path.exists():
            return ToolResult(False, f"Path does not exist: {path}")
        if not path.is_dir():
            return ToolResult(False, f"Path is not a directory: {path}")

        entries = []
        for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            suffix = "/" if child.is_dir() else ""
            entries.append(f"{child.name}{suffix}")
            if len(entries) >= 80:
                entries.append("...[truncated]")
                break
        return ToolResult(True, "\n".join(entries) or "(empty)")

    def system_status(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        if self.monitor is not None:
            try:
                return ToolResult(True, json.dumps(self.monitor.snapshot(), ensure_ascii=False))
            except Exception:
                # Keep the existing lightweight fallback if sampling fails.
                pass
        parts: list[str] = []
        try:
            batt = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5)
            if batt.returncode == 0:
                lines = [ln.strip() for ln in batt.stdout.splitlines() if ln.strip()]
                parts.append("Battery: " + (" | ".join(lines[-2:]) if lines else "unknown"))
        except Exception:
            parts.append("Battery: unavailable")

        try:
            load = subprocess.run(["uptime"], capture_output=True, text=True, timeout=5)
            if load.returncode == 0:
                parts.append("Load: " + load.stdout.strip())
        except Exception:
            parts.append("Load: unavailable")

        try:
            df = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=5)
            if df.returncode == 0:
                lines = df.stdout.strip().splitlines()
                if len(lines) >= 2:
                    parts.append("Disk (/): " + lines[1])
        except Exception:
            parts.append("Disk: unavailable")

        return ToolResult(True, "\n".join(parts) if parts else "Could not gather system status.")

    def notify(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        title = str(args.get("title", "JARVIS")).strip() or "JARVIS"
        message = str(args.get("message", "")).strip()
        if not message:
            return ToolResult(False, "Missing notification message.")
        title_esc = title.replace("\\", "\\\\").replace('"', '\\"')
        msg_esc = message.replace("\\", "\\\\").replace('"', '\\"')
        script = f'display notification "{msg_esc}" with title "{title_esc}"'
        completed = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
        if completed.returncode != 0:
            return ToolResult(False, completed.stderr.strip() or "Notification failed.")
        return ToolResult(True, f"Notification shown: {title} — {message}")

    def set_volume(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        try:
            level = int(args.get("level", 50))
        except (TypeError, ValueError):
            return ToolResult(False, "Volume level must be an integer 0-100.")
        level = max(0, min(100, level))
        completed = subprocess.run(
            ["osascript", "-e", f"set volume output volume {level}"],
            capture_output=True, text=True, timeout=5,
        )
        if completed.returncode != 0:
            return ToolResult(False, completed.stderr.strip() or "Could not set volume.")
        return ToolResult(True, f"System volume set to {level}%.")

    def open_url(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        url = str(args.get("url", "")).strip()
        if not url:
            return ToolResult(False, "Missing URL.")
        if not url.startswith(("http://", "https://", "file://")):
            url = "https://" + url
        completed = subprocess.run(["open", url], capture_output=True, text=True)
        if completed.returncode != 0:
            return ToolResult(False, completed.stderr.strip() or f"Could not open {url}.")
        return ToolResult(True, f"Opened {url}.")

    # ── Desktop ─────────────────────────────────────────────────

    def activate_app(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        name = str(args.get("name", "")).strip()
        if not name:
            return ToolResult(False, "Missing app name.")
        script = f'tell application "{name}" to activate'
        completed = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
        if completed.returncode != 0:
            return ToolResult(False, completed.stderr.strip() or f"Could not activate {name}.")
        return ToolResult(True, f"Activated {name}.")

    def frontmost_app(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        script = 'tell application "System Events" to get name of first application process whose frontmost is true'
        completed = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
        if completed.returncode != 0:
            return ToolResult(False, completed.stderr.strip() or "Could not determine frontmost app.")
        return ToolResult(True, completed.stdout.strip() or "unknown")

    def screenshot(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        if not self.screen_capture_allowed:
            return ToolResult(
                False, "Use the selected screen controls in the local orb to capture a snapshot.",
            )
        filename = str(args.get("filename", "screenshot.png")).strip() or "screenshot.png"
        if "/" in filename or "\\" in filename:
            filename = Path(filename).name
        dest = (self.workspace / filename).resolve()
        if self.development.protect_path(dest):
            return ToolResult(False, "Screenshot output cannot overwrite Jarvis source or development approvals.")
        completed = subprocess.run(
            ["screencapture", "-x", "-t", "png", str(dest)],
            capture_output=True, text=True, timeout=15,
        )
        if completed.returncode != 0:
            return ToolResult(False, completed.stderr.strip() or "Screenshot failed.")
        return ToolResult(True, f"Screenshot saved to {dest}")

    def list_windows(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        script = '''
tell application "System Events"
    set winList to {}
    repeat with proc in (every process whose background only is false)
        try
            set appName to name of proc
            repeat with w in (every window of proc)
                set end of winList to (appName & ": " & name of w)
            end repeat
        end try
    end repeat
    return winList
end tell
'''
        completed = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
        if completed.returncode != 0:
            return ToolResult(
                False,
                completed.stderr.strip()
                or "Could not list windows. Grant Accessibility permission to Terminal/Python in System Settings → Privacy & Security → Accessibility.",
            )
        raw = completed.stdout.strip()
        if not raw:
            return ToolResult(True, "(no windows found)")
        items = [x.strip() for x in raw.split(",") if x.strip()]
        return ToolResult(True, "\n".join(items[:40]) + ("\n..." if len(items) > 40 else ""))

    def desktop(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        """Advanced desktop actions: set_window, type, key, click.

        These can affect the UI; confirmation is required for type/key/click.
        """
        action = str(args.get("action", "")).strip().lower()
        if not action:
            return ToolResult(
                False,
                "desktop requires 'action'. Supported: set_window, type, key, click.",
            )

        if action in {"type", "key", "click"}:
            summary = f"desktop {action} {args}"
            if not self._ask_permission(summary, why or "Desktop input simulation"):
                return ToolResult(False, "User denied desktop input action.")

        ok, content = desktop_mod.dispatch(action, args)
        return ToolResult(ok, content)

    # ── Google ──────────────────────────────────────────────────

    def google(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        """Prefer gog CLI when available."""
        if not shutil.which("gog"):
            # Fall through hint toward native
            return ToolResult(
                False,
                "gog CLI not found. Install it, or use the google_native tool after "
                "pip install -r requirements-optional.txt and placing credentials.json in ~/.jarvis/google/.",
            )
        command = str(args.get("command", "")).strip()
        if not command:
            return ToolResult(
                False,
                "Missing gog command. Examples: 'gmail list --unread', 'calendar list --today', "
                "'drive search report'.",
            )
        first = command.split()[0].lower() if command else ""
        allowed = {"gmail", "calendar", "drive", "docs", "sheets", "contacts", "tasks", "auth", "help", "--help"}
        if first not in allowed and not first.startswith("-"):
            return ToolResult(False, f"Unsupported gog subcommand: {first}.")

        full = f"gog {command}"
        mutating = any(kw in command.lower() for kw in ("send", "create", "delete", "update", "upload", "trash", "remove"))
        if mutating and not self._ask_permission(full, why or "Google action that may modify data"):
            return ToolResult(False, "User denied Google action.")

        completed = subprocess.run(["gog", *shlex.split(command)], capture_output=True, text=True, timeout=60)
        output = "\n".join(p for p in [completed.stdout, completed.stderr] if p).strip()
        if len(output) > MAX_COMMAND_OUTPUT:
            output = output[:MAX_COMMAND_OUTPUT] + "\n...[truncated]"
        if not output:
            output = f"gog exited with code {completed.returncode}"
        return ToolResult(completed.returncode == 0, output)

    def google_native(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        """Native Google API actions (no gog CLI). Requires optional packages + OAuth setup."""
        if gnative is None or not gnative.available():
            return ToolResult(
                False,
                "Native Google libraries not installed. "
                "pip install -r requirements-optional.txt then place OAuth credentials at "
                "~/.jarvis/google/credentials.json",
            )
        action = str(args.get("action", "")).strip().lower()
        if not action:
            return ToolResult(
                False,
                "google_native requires 'action'. Supported: gmail_unread, calendar_today, drive_search.",
            )
        try:
            content = gnative.run_action(action, args)
            return ToolResult(True, content)
        except Exception as exc:
            return ToolResult(False, str(exc))

    # ── Helpers ─────────────────────────────────────────────────

    def _safe_path(self, user_path: str) -> Path | None:
        if not user_path:
            return None
        raw = Path(user_path).expanduser()
        candidate = raw if raw.is_absolute() else self.workspace / raw
        resolved = candidate.resolve()
        if self.full_disk_access:
            return resolved
        try:
            resolved.relative_to(self.workspace)
        except ValueError:
            return None
        return resolved

    def _looks_dangerous(self, command: str) -> bool:
        lowered = command.lower()
        return any(re.search(pattern, lowered) for pattern in DANGEROUS_COMMAND_PATTERNS)

    def _ask_permission(self, command: str, why: str) -> bool:
        if self.permission_handler is not None:
            return bool(self.permission_handler(command, why))
        print("\nJARVIS requires your authorisation:")
        if why:
            print(f"Reason: {why}")
        print(f"Command: {command}")
        try:
            answer = input("Allow? [y/N] ").strip().lower()
        except EOFError:
            return False
        return answer in {"y", "yes"}


class DuckDuckGoHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._capturing = False
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        if tag == "a" and "result__a" in attrs_dict.get("class", ""):
            self._capturing = True
            self._href = attrs_dict.get("href", "")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._capturing:
            return
        title = " ".join("".join(self._text).split())
        url = clean_duckduckgo_url(self._href)
        if title and url:
            self.results.append({"title": title, "url": url})
        self._capturing = False
        self._href = ""
        self._text = []


def clean_duckduckgo_url(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    parsed = parse.urlparse(href)
    query = parse.parse_qs(parsed.query)
    redirected = query.get("uddg", [""])[0]
    return redirected or href
