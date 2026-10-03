from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch
from urllib import error

from jarvis.ollama_client import MAX_RESPONSE_BYTES, OllamaClient


class Response(io.BytesIO):
    def __init__(self, payload: bytes):
        super().__init__(payload)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read(size)


class OllamaClientTests(unittest.TestCase):
    def setUp(self):
        self.client = OllamaClient(
            base_url="http://127.0.0.1:11434/",
            model="llama3.2:3b",
            timeout=17,
            temperature=0.2,
            num_ctx=8192,
            num_predict=1024,
            keep_alive="10m",
        )

    def response(self, content="A blue mug on a desk."):
        return Response(json.dumps({"message": {"content": content}}).encode())

    def test_image_payload_uses_separate_model_and_bounded_generation(self):
        response = self.response("  A blue mug on a desk.  ")
        with patch("jarvis.ollama_client.request.urlopen", return_value=response) as urlopen:
            answer = self.client.describe_image(" What is this? ", "aW1hZ2U=", model="moondream")
        self.assertEqual(answer, "A blue mug on a desk.")
        req = urlopen.call_args.args[0]
        self.assertEqual(req.full_url, "http://127.0.0.1:11434/api/chat")
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 17})
        self.assertEqual(json.loads(req.data), {
            "model": "moondream",
            "messages": [{"role": "user", "content": "What is this?", "images": ["aW1hZ2U="]}],
            "stream": False,
            "options": {"temperature": 0.2, "num_ctx": 2048, "num_predict": 512},
            "keep_alive": "5m",
        })
        self.assertEqual(response.read_sizes, [MAX_RESPONSE_BYTES + 1])

    def test_ordinary_chat_preserves_options_and_contains_no_previous_image(self):
        messages = [{"role": "user", "content": "Hello"}]
        with patch("jarvis.ollama_client.request.urlopen", side_effect=[self.response(), self.response("Hello")]) as urlopen:
            self.client.describe_image("Describe this", "aW1hZ2U=")
            self.assertEqual(self.client.chat(messages), "Hello")
        self.assertEqual(json.loads(urlopen.call_args.args[0].data), {
            "model": "llama3.2:3b",
            "messages": messages,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2, "num_ctx": 8192, "num_predict": 1024},
            "keep_alive": "10m",
        })
        self.assertEqual(self.client.model, "llama3.2:3b")
        self.assertEqual(messages, [{"role": "user", "content": "Hello"}])

    def test_missing_vision_model_has_install_guidance_and_bounded_details(self):
        failure = error.HTTPError("http://127.0.0.1:11434/api/chat", 404, "Not found", {}, io.BytesIO(b"model not found" + b"x" * 3000))
        with patch("jarvis.ollama_client.request.urlopen", side_effect=failure):
            with self.assertRaisesRegex(RuntimeError, "ollama pull moondream") as raised:
                self.client.describe_image("Describe this", "aW1hZ2U=")
        self.assertIn("HTTP 404", str(raised.exception))
        self.assertLess(len(str(raised.exception)), 2200)

    def test_final_chat_constrains_response_to_reply_and_preserves_generation_options(self):
        messages = [
            {"role": "system", "content": "Summarize the completed work in a reply."},
            {"role": "user", "content": "Report what you found."},
        ]
        with patch("jarvis.ollama_client.request.urlopen", return_value=self.response('{"reply":"The work is complete."}')) as urlopen:
            self.assertEqual(self.client.chat(messages, reply_only=True), '{"reply":"The work is complete."}')
        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(payload["format"], {
            "type": "object",
            "properties": {"reply": {"type": "string"}},
            "required": ["reply"],
            "additionalProperties": False,
        })
        self.assertEqual(payload["messages"], messages)
        self.assertEqual(payload["options"], {"temperature": 0.2, "num_ctx": 8192, "num_predict": 1024})
        self.assertEqual(payload["keep_alive"], "10m")
        self.assertEqual(payload["model"], "llama3.2:3b")
        self.assertFalse(payload["stream"])
        self.assertNotIn("tools", payload)

    def test_reply_only_schema_does_not_change_later_tool_capable_chat(self):
        messages = [{"role": "user", "content": "Hello"}]
        with patch("jarvis.ollama_client.request.urlopen", side_effect=[self.response(), self.response()]) as urlopen:
            self.client.chat(messages, reply_only=True)
            self.client.chat(messages)
        first, second = [json.loads(call.args[0].data) for call in urlopen.call_args_list]
        self.assertIsInstance(first["format"], dict)
        self.assertEqual(second["format"], "json")

    def test_regular_chat_404_does_not_suggest_installing_vision_model(self):
        failure = error.HTTPError("http://127.0.0.1:11434/api/chat", 404, "Not found", {}, io.BytesIO(b"missing model"))
        with patch("jarvis.ollama_client.request.urlopen", side_effect=failure):
            with self.assertRaisesRegex(RuntimeError, "Ollama returned HTTP 404") as raised:
                self.client.chat([{"role": "user", "content": "Hello"}])
        self.assertNotIn("vision", str(raised.exception))

    def test_vision_network_failure_is_actionable(self):
        failures = [(TimeoutError(), "timed out after 17 seconds"), (error.URLError("offline"), "Start Ollama")]
        for failure, message in failures:
            with self.subTest(failure=failure):
                with patch("jarvis.ollama_client.request.urlopen", side_effect=failure):
                    with self.assertRaisesRegex(RuntimeError, message):
                        self.client.describe_image("Describe this", "aW1hZ2U=")

    def test_vision_rejects_malformed_responses(self):
        payloads = [b"not json", b"\xff", b"[]", b"{}", b'{"message":{"content":[]}}', b'{"message":{"content":" "}}']
        for payload in payloads:
            with self.subTest(payload=payload):
                with patch("jarvis.ollama_client.request.urlopen", return_value=Response(payload)):
                    with self.assertRaises(RuntimeError):
                        self.client.describe_image("Describe this", "aW1hZ2U=")

    def test_oversized_response_is_rejected_with_a_bounded_read(self):
        response = Response(b"x" * (MAX_RESPONSE_BYTES + 100))
        with patch("jarvis.ollama_client.request.urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "larger than 2 MiB"):
                self.client.describe_image("Describe this", "aW1hZ2U=")
        self.assertEqual(response.read_sizes, [MAX_RESPONSE_BYTES + 1])

    def test_invalid_image_requests_fail_before_network_access(self):
        cases = [
            ("", "aW1hZ2U=", "moondream"),
            ("  ", "aW1hZ2U=", "moondream"),
            (None, "aW1hZ2U=", "moondream"),
            ("x" * 2001, "aW1hZ2U=", "moondream"),
            ("Describe this", "", "moondream"),
            ("Describe this", " ", "moondream"),
            ("Describe this", None, "moondream"),
            ("Describe this", "aW1hZ2U=", ""),
            ("Describe this", "aW1hZ2U=", None),
        ]
        with patch("jarvis.ollama_client.request.urlopen") as urlopen:
            for prompt, image, model in cases:
                with self.subTest(prompt=prompt, image=image, model=model):
                    with self.assertRaises(ValueError):
                        self.client.describe_image(prompt, image, model=model)
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
