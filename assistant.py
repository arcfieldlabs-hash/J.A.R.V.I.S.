from __future__ import annotations

import json
import re
from typing import Any

from .ollama_client import OllamaClient
from .tools import ToolKit


SYSTEM_PROMPT = """You are JARVIS — Just A Rather Very Intelligent System — the sophisticated AI assistant from the Iron Man films, now running privately on this Mac.

Personality:
- Speak in polished, formal British English with dry wit and understated humor.
- Address the user as "Sir" (or "Madam" if the context clearly indicates otherwise).
- Be concise, elegant, and slightly sarcastic when appropriate. Never sycophantic.
- Anticipate needs and offer helpful next steps when it makes sense.
- Remain calm and professional even when delivering bad news or blocking unsafe actions.

You can chat normally, but you may also request tools when the user's request needs an action.
Return exactly one compact JSON object and no markdown, no extra text.

When no tool is needed:
{"reply":"Your elegant, witty answer, Sir."}

When a tool is needed:
{"tool":"tool_name","args":{"name":"value"},"why":"brief reason"}

Available tools:
- open_app: open a macOS application. args: {"name":"Visual Studio Code"}
- activate_app: bring a running app to the front. args: {"name":"Safari"}
- frontmost_app: report which app is currently focused. args: {}
- list_windows: list open windows (needs Accessibility permission). args: {}
- screenshot: capture the screen into the workspace. args: {"filename":"shot.png"}
- run_command: run a shell command after user approval. args: {"command":"pwd"}
- web_search: search the web. args: {"query":"search words","limit":5}
- read_file / write_file / list_files: safe workspace file operations
- system_status: battery, load, disk. args: {}
- notify: macOS notification. args: {"title":"Title","message":"Body"}
- set_volume: set output volume 0-100. args: {"level":40}
- open_url: open a URL. args: {"url":"https://example.com"}
- google: run a gog CLI command for Gmail/Calendar/Drive/Docs/Sheets/Contacts (requires gog installed + auth). args: {"command":"gmail list --unread --limit 5"}

Google examples (via google tool):
- "gmail list --unread --limit 5"
- "calendar list --today"
- "drive search quarterly report"
- "docs list"
- "contacts search Alice"

Rules:
- Ask a clarifying question if the request is ambiguous.
- Prefer workspace-relative file paths.
- Never claim a tool succeeded until you see the tool result.
- Do not request destructive shell commands.
- Keep replies concise unless the user asks for detail.
- When reporting status, search, or Google results, present them cleanly and offer a useful follow-up.
- If the user greets you or says "JARVIS", respond in character with a short status or witty greeting.
"""


def parse_assistant_message(text: str) -> dict[str, Any]:
    """Parse the model's JSON action, falling back to a plain reply."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {"reply": text.strip()}

    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return {"reply": text.strip()}

    if not isinstance(parsed, dict):
        return {"reply": text.strip()}
    return parsed


class JarvisAssistant:
    def __init__(
        self,
        client: OllamaClient,
        toolkit: ToolKit,
        *,
        tools_enabled: bool = True,
        max_tool_rounds: int = 4,
    ) -> None:
        self.client = client
        self.toolkit = toolkit
        self.tools_enabled = tools_enabled
        self.max_tool_rounds = max_tool_rounds
        self.history: list[dict[str, str]] = []

    def ask(self, user_text: str) -> str:
        self.history.append({"role": "user", "content": user_text})

        for _ in range(self.max_tool_rounds):
            raw = self.client.chat(self._messages())
            action = parse_assistant_message(raw)

            if "reply" in action:
                reply = str(action.get("reply", "")).strip()
                self.history.append({"role": "assistant", "content": reply})
                return reply

            if not self.tools_enabled:
                reply = "I'm afraid tool use is disabled for this session, Sir."
                self.history.append({"role": "assistant", "content": reply})
                return reply

            tool_name = str(action.get("tool", "")).strip()
            args = action.get("args", {})
            why = str(action.get("why", "")).strip()
            if not isinstance(args, dict):
                args = {}

            result = self.toolkit.execute(tool_name, args, why=why)
            self.history.append({"role": "assistant", "content": raw})
            self.history.append(
                {
                    "role": "user",
                    "content": (
                        f"Tool result for {tool_name}:\n"
                        f"{result.to_json()}\n\n"
                        "Use this result to answer the user in character. If another tool is needed, "
                        "return another JSON tool request."
                    ),
                }
            )

        reply = "I appear to have reached my tool limit for that request, Sir. Perhaps try one step at a time."
        self.history.append({"role": "assistant", "content": reply})
        return reply

    def _messages(self) -> list[dict[str, str]]:
        recent_history = self.history[-24:]
        return [{"role": "system", "content": SYSTEM_PROMPT}, *recent_history]
