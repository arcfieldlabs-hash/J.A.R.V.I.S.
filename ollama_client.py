from __future__ import annotations

import json
from typing import Any
from urllib import error, request


class OllamaClient:
    def __init__(self, *, base_url: str, model: str, timeout: int = 120) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def chat(self, messages: list[dict[str, str]]) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": 0.3,
            },
        }
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama returned HTTP {exc.code}: {details}") from exc
        except error.URLError as exc:
            raise RuntimeError(
                f"Could not reach Ollama at {self.base_url}. Start Ollama, then try again."
            ) from exc

        message = data.get("message", {})
        content = message.get("content", "")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError(f"Ollama response did not include message content: {data}")
        return content.strip()
