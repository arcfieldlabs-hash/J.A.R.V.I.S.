from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib import error, parse, request


MAX_FILE_BYTES = 120_000
MAX_COMMAND_OUTPUT = 12_000

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
    def __init__(self, *, workspace: Path) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)

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
            # Desktop control
            "activate_app": self.activate_app,
            "frontmost_app": self.frontmost_app,
            "screenshot": self.screenshot,
            "list_windows": self.list_windows,
            # Google via gog CLI (if installed)
            "google": self.google,
        }
        tool = tools.get(name)
        if tool is None:
            return ToolResult(False, f"Unknown tool: {name}")
        try:
            return tool(args, why=why)
        except Exception as exc:
            return ToolResult(False, f"{type(exc).__name__}: {exc}")

    # ── Core tools ──────────────────────────────────────────────

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

        url = "https://duckduckgo.com/html/?" + parse.urlencode({"q": query})
        req = request.Request(url, headers={"User-Agent": "Mozilla/5.0 JarvisLocal/1.0"})
        try:
            with request.urlopen(req, timeout=20) as response:
                html = response.read().decode("utf-8", errors="replace")
        except error.URLError as exc:
            return ToolResult(False, f"Web search failed: {exc}")

        parser = DuckDuckGoHTMLParser()
        parser.feed(html)
        results = parser.results[:limit]
        if not results:
            return ToolResult(False, "No search results were parsed.")

        lines = [f"{i}. {r['title']}\n   {r['url']}" for i, r in enumerate(results, 1)]
        return ToolResult(True, "\n".join(lines))

    def read_file(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        path = self._safe_path(str(args.get("path", "")).strip())
        if path is None:
            return ToolResult(False, "File path is outside the configured workspace.")
        if not path.exists():
            return ToolResult(False, f"File does not exist: {path}")
        if path.is_dir():
            return ToolResult(False, f"Path is a directory: {path}")

        data = path.read_bytes()
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

    # ── Desktop control ─────────────────────────────────────────

    def activate_app(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        """Bring an already-running app to the front."""
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
        """Capture the screen (or a window) into the workspace."""
        filename = str(args.get("filename", "screenshot.png")).strip() or "screenshot.png"
        if "/" in filename or "\\" in filename:
            filename = Path(filename).name
        dest = self.workspace / filename
        # -x = no sound, -t png
        completed = subprocess.run(
            ["screencapture", "-x", "-t", "png", str(dest)],
            capture_output=True, text=True, timeout=15,
        )
        if completed.returncode != 0:
            return ToolResult(False, completed.stderr.strip() or "Screenshot failed.")
        return ToolResult(True, f"Screenshot saved to {dest}")

    def list_windows(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        """List open windows (requires Accessibility permission)."""
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
        # osascript returns comma-separated list
        items = [x.strip() for x in raw.split(",") if x.strip()]
        return ToolResult(True, "\n".join(items[:40]) + ("\n..." if len(items) > 40 else ""))

    # ── Google via gog CLI ──────────────────────────────────────

    def google(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        """Run a gog CLI command for Gmail / Calendar / Drive / Docs / Sheets / Contacts.

        Requires the `gog` binary (https://github.com/teru-0529/gog or brew install).
        Example args: {"command": "gmail list --unread --limit 5"}
                      {"command": "calendar list --today"}
                      {"command": "drive search 'quarterly report'"}
        """
        if not shutil.which("gog"):
            return ToolResult(
                False,
                "gog CLI not found. Install it (e.g. `brew install teru-0529/tap/gog` or from GitHub), "
                "then run `gog auth login` to connect your Google accounts.",
            )
        command = str(args.get("command", "")).strip()
        if not command:
            return ToolResult(
                False,
                "Missing gog command. Examples: 'gmail list --unread', 'calendar list --today', "
                "'drive search report', 'docs list', 'contacts search Alice'.",
            )
        # Safety: only allow known subcommands
        first = command.split()[0].lower() if command else ""
        allowed = {"gmail", "calendar", "drive", "docs", "sheets", "contacts", "tasks", "auth", "help", "--help"}
        if first not in allowed and not first.startswith("-"):
            return ToolResult(False, f"Unsupported gog subcommand: {first}. Allowed: {', '.join(sorted(allowed))}")

        full = f"gog {command}"
        # Google actions that send/modify should ask permission
        mutating = any(kw in command.lower() for kw in ("send", "create", "delete", "update", "upload", "trash", "remove"))
        if mutating and not self._ask_permission(full, why or "Google action that may modify data"):
            return ToolResult(False, "User denied Google action.")

        completed = subprocess.run(
            full, shell=True, capture_output=True, text=True, timeout=60,
        )
        output = "\n".join(p for p in [completed.stdout, completed.stderr] if p).strip()
        if len(output) > MAX_COMMAND_OUTPUT:
            output = output[:MAX_COMMAND_OUTPUT] + "\n...[truncated]"
        if not output:
            output = f"gog exited with code {completed.returncode}"
        return ToolResult(completed.returncode == 0, output)

    # ── Helpers ─────────────────────────────────────────────────

    def _safe_path(self, user_path: str) -> Path | None:
        if not user_path:
            return None
        raw = Path(user_path).expanduser()
        candidate = raw if raw.is_absolute() else self.workspace / raw
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.workspace)
        except ValueError:
            return None
        return resolved

    def _looks_dangerous(self, command: str) -> bool:
        lowered = command.lower()
        return any(re.search(pattern, lowered) for pattern in DANGEROUS_COMMAND_PATTERNS)

    def _ask_permission(self, command: str, why: str) -> bool:
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
