from __future__ import annotations

import json
from typing import Any
from urllib import error, request


MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class OllamaClient:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout: int = 120,
        temperature: float = 0.3,
        num_ctx: int | None = None,
        num_predict: int | None = None,
        keep_alive: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if timeout <= 0:
            raise ValueError("Ollama timeout must be positive.")
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.num_ctx = num_ctx
        self.num_predict = num_predict
        self.keep_alive = keep_alive

    def chat(self, messages: list[dict[str, str]]) -> str:
        options: dict[str, Any] = {
            "temperature": self.temperature,
        }
        if self.num_ctx is not None:
            options["num_ctx"] = self.num_ctx
        if self.num_predict is not None:
            options["num_predict"] = self.num_predict

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": "json",
            "options": options,
        }
        if self.keep_alive is not None:
            payload["keep_alive"] = self.keep_alive

        return self._chat(payload)

    def describe_image(self, prompt: str, image: str, *, model: str = "moondream") -> str:
        """Describe one explicitly supplied image without changing text-chat history."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("An image question is required.")
        if len(prompt) > 2000:
            raise ValueError("Image questions must contain at most 2000 characters.")
        if not isinstance(image, str) or not image.strip():
            raise ValueError("An image is required.")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("A vision model is required.")

        payload = {
            "model": model.strip(),
            "messages": [{"role": "user", "content": prompt.strip(), "images": [image]}],
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_ctx": 2048,
                "num_predict": 512,
            },
            "keep_alive": "5m",
        }
        return self._chat(payload, vision_model=model.strip())

    def _chat(self, payload: dict[str, Any], *, vision_model: str | None = None) -> str:
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise RuntimeError("Ollama returned a response larger than 2 MiB.")
                data = json.loads(raw.decode("utf-8"))
        except error.HTTPError as exc:
            details = exc.read(2000).decode("utf-8", errors="replace")
            if vision_model is not None and exc.code == 404:
                raise RuntimeError(
                    f"The vision model is unavailable. Run 'ollama pull {vision_model}', "
                    f"then try again. Ollama returned HTTP 404: {details}"
                ) from exc
            raise RuntimeError(f"Ollama returned HTTP {exc.code}: {details}") from exc
        except error.URLError as exc:
            raise RuntimeError(
                f"Could not reach Ollama at {self.base_url}. Start Ollama, then try again."
            ) from exc
        except TimeoutError as exc:
            raise RuntimeError(f"Ollama timed out after {self.timeout} seconds. Try a smaller model or increase --timeout.") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError("Ollama returned an invalid JSON response.") from exc

        if not isinstance(data, dict) or not isinstance(data.get("message"), dict):
            raise RuntimeError("Ollama response did not include an assistant message.")
        message = data["message"]
        content = message.get("content", "")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError(f"Ollama response did not include message content: {data}")
        return content.strip()
