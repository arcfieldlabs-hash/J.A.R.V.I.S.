"""An optional, loopback-only orb interface, using Python's standard library."""
from __future__ import annotations

import json
import secrets
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .monitoring import SystemMonitor


MAX_BODY_BYTES = 16_384
MAX_PROMPT_CHARS = 8_000


class JarvisWebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        assistant: Any,
        toolkit: Any,
        *,
        port: int,
        speak_answers: bool = False,
        voice: str = "Daniel",
    ) -> None:
        # Port 0 is allowed by this testable factory; serve() validates user ports.
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("Web port must be an integer between 1 and 65535.")
        super().__init__(("127.0.0.1", port), JarvisRequestHandler)
        actual_port = self.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"}
        self.allowed_origins = {"http://" + host for host in self.allowed_hosts}
        self.csrf_token = secrets.token_urlsafe(32)
        self.script_nonce = secrets.token_urlsafe(24)
        self.assistant = assistant
        self.toolkit = toolkit
        self._previous_permission_handler = getattr(toolkit, "permission_handler", None)
        toolkit.permission_handler = lambda command, why: False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-turn")
        self._turn_lock = threading.Lock()
        self._busy = False
        self._jobs: dict[str, Future[str]] = {}
        self._closed = False
        self.monitor = SystemMonitor(toolkit.workspace, memory=getattr(toolkit, "memory", None))
        bootstrap = json.dumps({
            "backendEnabled": True,
            "csrfToken": self.csrf_token,
            "speakAnswers": speak_answers,
            "voice": voice,
            "maxPromptChars": MAX_PROMPT_CHARS,
        }).replace("<", "\\u003c")
        template = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        self.page = template.replace("__JARVIS_BOOTSTRAP__", bootstrap).replace(
            "__JARVIS_NONCE__", self.script_nonce
        ).encode("utf-8")
        self.monitor.start()

    def submit_prompt(self, prompt: str) -> str | None:
        with self._turn_lock:
            if self._busy or self._closed:
                return None
            self._busy = True
            request_id = secrets.token_urlsafe(18)
            # Bound retained completed turns without evicting the current one.
            while len(self._jobs) >= 8:
                self._jobs.pop(next(iter(self._jobs)))
            future = self.executor.submit(self.assistant.ask, prompt)
            self._jobs[request_id] = future
        future.add_done_callback(self._finish_turn)
        return request_id

    def _finish_turn(self, future: Future[str]) -> None:
        with self._turn_lock:
            self._busy = False

    def job_snapshot(self, request_id: str) -> dict[str, Any] | None:
        with self._turn_lock:
            future = self._jobs.get(request_id)
        if future is None:
            return None
        if not future.done():
            return {"request_id": request_id, "status": "pending"}
        try:
            return {"request_id": request_id, "status": "complete", "reply": str(future.result())}
        except Exception as exc:
            return {"request_id": request_id, "status": "error", "error": str(exc)}

    def status_snapshot(self) -> dict[str, Any]:
        state = self.monitor.snapshot()
        with self._turn_lock:
            state["busy"] = self._busy
        state["controlled_actions"] = "Use the CLI to approve controlled actions."
        return state

    def server_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.monitor.stop()
        self.executor.shutdown(wait=True, cancel_futures=False)
        self.toolkit.permission_handler = self._previous_permission_handler
        super().server_close()


class JarvisRequestHandler(BaseHTTPRequestHandler):
    server: JarvisWebServer

    def log_message(self, format: str, *args: Any) -> None:
        # Keep prompts, tokens, workspace details and chat text out of access logs.
        pass

    def _check_local_request(self) -> bool:
        hosts = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        if len(hosts) != 1 or hosts[0] not in self.server.allowed_hosts:
            self._json(403, {"error": "Invalid local Host header."})
            return False
        if len(origins) > 1 or (origins and origins[0] not in self.server.allowed_origins):
            self._json(403, {"error": "Requests from this origin are not allowed."})
            return False
        return True

    def _check_token(self) -> bool:
        tokens = self.headers.get_all("X-Jarvis-Token", [])
        token = tokens[0] if len(tokens) == 1 else ""
        if not secrets.compare_digest(token.encode("utf-8"), self.server.csrf_token.encode("ascii")):
            self._json(403, {"error": "Missing or invalid local session token."})
            return False
        return True

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", (
            "default-src 'none'; "
            f"script-src 'nonce-{self.server.script_nonce}'; "
            "style-src 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        ))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: int, value: dict[str, Any]) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        if not self._check_local_request():
            return
        path = urlsplit(self.path).path
        if path in {"/", "/index.html"}:
            self._send(200, self.server.page, "text/html; charset=utf-8")
        elif path == "/api/status":
            if self._check_token():
                self._json(200, self.server.status_snapshot())
        elif path.startswith("/api/chat/"):
            if self._check_token():
                snapshot = self.server.job_snapshot(path.removeprefix("/api/chat/"))
                self._json(200 if snapshot is not None else 404, snapshot or {"error": "Turn not found."})
        else:
            self._json(404, {"error": "Not found."})

    def do_POST(self) -> None:
        if not self._check_local_request() or not self._check_token():
            return
        if urlsplit(self.path).path != "/api/chat":
            self._json(404, {"error": "Not found."})
            return
        if self.headers.get_content_type() != "application/json":
            self._json(415, {"error": "Send application/json."})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
            self._json(400, {"error": "A single Content-Length is required."})
            return
        try:
            length = int(lengths[0])
            if length < 0:
                raise ValueError
        except ValueError:
            self._json(400, {"error": "Invalid Content-Length."})
            return
        if length > MAX_BODY_BYTES:
            self._json(413, {"error": "Chat request is too large."})
            return
        try:
            self.connection.settimeout(5)
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("Incomplete request")
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("prompt"), str):
                raise ValueError("A prompt string is required")
            prompt = payload["prompt"].strip()
            if not prompt:
                raise ValueError("The prompt cannot be empty")
        except (ValueError, UnicodeDecodeError, OSError) as exc:
            self._json(400, {"error": str(exc) or "Invalid JSON request."})
            return
        if len(prompt) > MAX_PROMPT_CHARS:
            self._json(413, {"error": f"Prompt limit is {MAX_PROMPT_CHARS} characters."})
            return
        request_id = self.server.submit_prompt(prompt)
        if request_id is None:
            self._json(429, {"error": "JARVIS is finishing another request. Please wait."})
            return
        self._json(202, {"request_id": request_id, "status": "pending"})

    def do_OPTIONS(self) -> None:
        if self._check_local_request():
            self._json(405, {"error": "Cross-origin access is disabled."})


def create_server(
    assistant: Any,
    toolkit: Any,
    *,
    port: int = 8765,
    speak_answers: bool = False,
    voice: str = "Daniel",
) -> JarvisWebServer:
    return JarvisWebServer(assistant, toolkit, port=port, speak_answers=speak_answers, voice=voice)


def serve(
    assistant: Any,
    toolkit: Any,
    *,
    port: int = 8765,
    speak_answers: bool = False,
    voice: str = "Daniel",
) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        print("JARVIS: Web port must be an integer between 1 and 65535.")
        return 1
    try:
        server = create_server(assistant, toolkit, port=port, speak_answers=speak_answers, voice=voice)
    except OSError as exc:
        print(f"JARVIS: Could not start the local web interface: {exc}")
        return 1
    print(f"JARVIS orb: http://127.0.0.1:{port} (Ctrl-C to stop).")
    print("Controlled actions require approval in the CLI. Speech uses your browser's available voices.")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
