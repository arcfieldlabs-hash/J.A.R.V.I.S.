from __future__ import annotations

import json
import re
from typing import Any

from .ollama_client import OllamaClient
from .tools import ToolKit


# Compact system prompt keeps context smaller on 16GB machines.
SYSTEM_PROMPT = """You are JARVIS — Just A Rather Very Intelligent System — a private local assistant on this Mac, inspired by the Iron Man films.

Personality: polished British English, dry wit, address the user as "Sir". Concise, calm, never sycophantic. Anticipate needs briefly.

Return exactly one compact JSON object and no markdown.

No tool needed:
{"reply":"Your short, elegant answer, Sir."}

Tool needed:
{"tool":"tool_name","args":{...},"why":"brief reason"}

Tools:
- open_app, activate_app, frontmost_app, list_windows, screenshot
- desktop: {"action":"set_window|type|key|click", ...} (type/key/click need confirmation)
- run_command, web_search, read_file, write_file, list_files
- system_status, notify, set_volume, open_url
- google: {"command":"gmail list --unread --limit 5"}  (needs gog)
- google_native: {"action":"gmail_unread|calendar_today|drive_search", ...}

Rules: Prefer short replies. Never claim tool success until you see the result. No destructive shell commands. Ask one clarifying question if ambiguous.
"""


def parse_assistant_message(text: str) -> dict[str, Any]:
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
        max_tool_rounds: int = 3,
        history_limit: int = 12,
    ) -> None:
        self.client = client
        self.toolkit = toolkit
        self.tools_enabled = tools_enabled
        self.max_tool_rounds = max_tool_rounds
        self.history_limit = history_limit
        self.history: list[dict[str, str]] = []

    def ask(self, user_text: str) -> str:
        self.history.append({"role": "user", "content": user_text})

        for _ in range(self.max_tool_rounds):
            raw = self.client.chat(self._messages())
            action = parse_assistant_message(raw)

            if "reply" in action:
                reply = str(action.get("reply", "")).strip()
                self.history.append({"role": "assistant", "content": reply})
                self._trim_history()
                return reply

            if not self.tools_enabled:
                reply = "I'm afraid tool use is disabled for this session, Sir."
                self.history.append({"role": "assistant", "content": reply})
                self._trim_history()
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
                        "Answer the user in character. Request another tool only if essential."
                    ),
                }
            )

        reply = "I reached my tool limit for that request, Sir. Try one step at a time."
        self.history.append({"role": "assistant", "content": reply})
        self._trim_history()
        return reply

    def _trim_history(self) -> None:
        if len(self.history) > self.history_limit:
            self.history = self.history[-self.history_limit :]

    def _messages(self) -> list[dict[str, str]]:
        return [{"role": "system", "content": SYSTEM_PROMPT}, *self.history]
