from __future__ import annotations

import http.client
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from jarvis.web import MAX_BODY_BYTES, MAX_PROMPT_CHARS, create_server, serve


class Assistant:
    def __init__(self) -> None:
        self.calls = []
        self.thread_ids = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def ask(self, prompt):
        self.calls.append(prompt)
        self.thread_ids.append(threading.get_ident())
        self.started.set()
        if not self.release.wait(2):
            raise RuntimeError("Test worker did not release")
        if prompt == "fail":
            raise RuntimeError("Model unavailable")
        return "Answer: " + prompt


class WebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.assistant = Assistant()
        self.original_permission = lambda command, why: True
        self.toolkit = SimpleNamespace(workspace=Path(self.directory.name), memory=None, permission_handler=self.original_permission)
        self.server = create_server(self.assistant, self.toolkit, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval":.01}, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.assistant.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        self.directory.cleanup()

    def request(self, method, path, body=None, headers=None, token=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        request_headers = {"X-Jarvis-Token":self.server.csrf_token} if token else {}
        if isinstance(body, dict) or isinstance(body, list):
            body = json.dumps(body)
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        try:
            connection.request(method, path, body, request_headers)
            response = connection.getresponse()
            data = response.read()
            return response.status, dict(response.getheaders()), data
        finally:
            connection.close()

    def wait_for_job(self, request_id):
        for _ in range(100):
            status, headers, body = self.request("GET", "/api/chat/" + request_id)
            self.assertEqual(status, 200)
            result = json.loads(body)
            if result["status"] != "pending":
                return result
            threading.Event().wait(.005)
        self.fail("Chat did not complete")

    def test_page_bootstrap_and_only_explicit_assets(self) -> None:
        status, headers, body = self.request("GET", "/", token=False)
        self.assertEqual(status, 200)
        self.assertIn(self.server.csrf_token.encode(), body)
        self.assertNotIn(b"__JARVIS_BOOTSTRAP__", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.request("GET", "/../tools.py")[0], 404)
        self.assertEqual(self.request("GET", "/web/index.html")[0], 404)
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_local_bootstrap_enables_backend_and_safely_serializes_voice(self) -> None:
        voice = "Sir's </script><script>window.injected=true</script>"
        toolkit = SimpleNamespace(workspace=Path(self.directory.name), memory=None, permission_handler=None)
        server = create_server(self.assistant, toolkit, port=0, voice=voice)
        try:
            page = server.page.decode("utf-8")
            match = re.search(r'<script id="jarvis-config"[^>]*>(.*?)</script>', page, re.DOTALL)
            self.assertIsNotNone(match)
            config = json.loads(match.group(1))
            self.assertTrue(config["backendEnabled"])
            self.assertEqual(config["csrfToken"], server.csrf_token)
            self.assertEqual(config["voice"], voice)
            self.assertNotIn(voice, page)
            self.assertEqual(page.count("<script"), 3)
        finally:
            server.server_close()

    def test_reject_foreign_host_and_origin_before_tool_use(self) -> None:
        for headers in [{"Host":"attacker.example"},{"Host":f"127.0.0.1:{self.server.server_port}.attacker.example"},{"Origin":"https://attacker.example"},{"Origin":"null"}]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", "/api/chat", {"prompt":"hello"}, headers)[0], 403)
        self.assertEqual(self.assistant.calls, [])
        local_origin = f"http://localhost:{self.server.server_port}"
        self.assertEqual(self.request("GET", "/", headers={"Origin":local_origin})[0], 200)
        self.assertEqual(self.request("OPTIONS", "/api/chat", headers={"Origin":"https://attacker.example"})[0], 403)

    def test_api_requires_session_token_including_status_and_results(self) -> None:
        self.assertEqual(self.request("POST", "/api/chat", {"prompt":"hello"}, token=False)[0], 403)
        self.assertEqual(self.request("GET", "/api/status", token=False)[0], 403)
        self.assertEqual(self.request("GET", "/api/chat/nonexistent", token=False)[0], 403)
        self.assertEqual(self.request("GET", "/api/status", headers={"X-Jarvis-Token":"wrong"})[0], 403)
        self.assertEqual(self.request("GET", "/api/status", headers={"X-Jarvis-Token":"é"})[0], 403)

    def test_status_metrics_are_structured(self) -> None:
        status, headers, body = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertIn("disk", state)
        self.assertIn("reminders", state)
        self.assertIsNone(state["gpu"])
        self.assertFalse(state["busy"])

    def test_validate_json_content_and_limits(self) -> None:
        cases = [
            ({"prompt":""},{},400),
            ({"prompt":7},{},400),
            ([],{},400),
            ("{",{"Content-Type":"application/json"},400),
            ("hello",{"Content-Type":"text/plain"},415),
            ({"prompt":"a"*(MAX_PROMPT_CHARS+1)},{},413),
            ("a"*(MAX_BODY_BYTES+1),{"Content-Type":"application/json"},413),
            ("",{"Content-Type":"application/json","Content-Length":"-1"},400),
            ("",{"Content-Type":"application/json","Transfer-Encoding":"chunked"},400),
        ]
        for body, headers, expected in cases:
            with self.subTest(expected=expected, headers=headers):
                self.assertEqual(self.request("POST", "/api/chat", body, headers)[0], expected)
        self.assertEqual(self.assistant.calls, [])

    def test_chat_serialization_and_worker_affinity(self) -> None:
        self.assistant.release.clear()
        status, headers, body = self.request("POST", "/api/chat", {"prompt":"first"})
        self.assertEqual(status, 202)
        request_id = json.loads(body)["request_id"]
        self.assertTrue(self.assistant.started.wait(1))
        self.assertEqual(self.request("POST", "/api/chat", {"prompt":"second"})[0], 429)
        self.assertEqual(self.assistant.calls, ["first"])
        self.assertTrue(json.loads(self.request("GET", "/api/status")[2])["busy"])
        self.assistant.release.set()
        self.assertEqual(self.wait_for_job(request_id)["reply"], "Answer: first")
        status, headers, body = self.request("POST", "/api/chat", {"prompt":"second"})
        self.assertEqual(status, 202)
        self.assertEqual(self.wait_for_job(json.loads(body)["request_id"])["reply"], "Answer: second")
        self.assertEqual(len(set(self.assistant.thread_ids)), 1)
        self.assertNotEqual(self.assistant.thread_ids[0], threading.get_ident())
        self.assertEqual(self.wait_for_job(request_id)["reply"], "Answer: first")

    def test_tool_errors_return_clear_job_error_and_allow_next_turn(self) -> None:
        status, headers, body = self.request("POST", "/api/chat", {"prompt":"fail"})
        result = self.wait_for_job(json.loads(body)["request_id"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "Model unavailable")
        self.assertFalse(json.loads(self.request("GET", "/api/status")[2])["busy"])

    def test_controlled_actions_denied_and_permission_handler_restored(self) -> None:
        self.assertFalse(self.toolkit.permission_handler("dangerous command", "why"))
        self.server.shutdown()
        self.server.server_close()
        self.assertIs(self.toolkit.permission_handler, self.original_permission)

    def test_invalid_user_ports_return_clear_error(self) -> None:
        for port in [0,-1,65536,True,"8765"]:
            with self.subTest(port=port):
                self.assertEqual(serve(self.assistant, self.toolkit, port=port), 1)


if __name__ == "__main__":
    unittest.main()
