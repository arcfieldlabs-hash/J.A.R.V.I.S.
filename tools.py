from __future__ import annotations

import json
import re
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
        }
        tool = tools.get(name)
        if tool is None:
            return ToolResult(False, f"Unknown tool: {name}")
        try:
            return tool(args, why=why)
        except Exception as exc:
            return ToolResult(False, f"{type(exc).__name__}: {exc}")

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
            command,
            shell=True,
            cwd=str(self.workspace),
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = "\n".join(part for part in [completed.stdout, completed.stderr] if part).strip()
        if not output:
            output = f"Command exited with code {completed.returncode} and no output."
        if len(output) > MAX_COMMAND_OUTPUT:
            output = output[:MAX_COMMAND_OUTPUT] + "\n...[output truncated]"
        return ToolResult(completed.returncode == 0, output)

    def web_search(self, args: dict[str, Any], *, why: str = "") -> ToolResult:
        query = str(args.get("query", "")).strip()
        limit = int(args.get("limit", 5) or 5)
        limit = max(1, min(limit, 8))
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

        lines = []
        for index, result in enumerate(results, start=1):
            lines.append(f"{index}. {result['title']}\n   {result['url']}")
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
        print("\nJARVIS wants permission to run a command:")
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
        classes = attrs_dict.get("class", "")
        if tag == "a" and "result__a" in classes:
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
