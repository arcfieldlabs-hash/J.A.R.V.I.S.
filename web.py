"""An optional, loopback-only orb interface, using Python's standard library."""
from __future__ import annotations

import json
import secrets
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .monitoring import SystemMonitor
from .media import DevicePermissions, MediaError, transcribe_audio, validate_image
from .speech import MAX_TEXT_CHARACTERS, choose_voice, synthesize, voice_inventory
from .approvals import ApprovalBroker


MAX_BODY_BYTES = 16_384
MAX_PROMPT_CHARS = 8_000
MAX_MEDIA_BODY_BYTES = 3_000_000


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
        vision_model: str = "moondream",
        stt_model: str = "base",
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
        self._previous_screen_capture = getattr(toolkit, "screen_capture_allowed", True)
        toolkit.screen_capture_allowed = False
        self.approvals = ApprovalBroker()
        self._previous_development_permission = getattr(toolkit, "development_permission_handler", None)
        toolkit.development_permission_handler = self.approvals.request
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-turn")
        self._turn_lock = threading.Lock()
        self._busy = False
        self._jobs: dict[str, Future[str]] = {}
        self._closed = False
        self.devices = DevicePermissions()
        self.vision_model = vision_model
        self.stt_model = stt_model
        self.voice = voice
        self.media_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-media")
        self.speech_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-speech")
        self._resource_lock = threading.Lock()
        self._media_jobs: dict[str, tuple[Future, str, int, str]] = {}
        self._speech_jobs: dict[str, Future] = {}
        self.development_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-development")
        self._development_lock = threading.Lock()
        self._development_jobs: dict[str, Future] = {}
        self.monitor = SystemMonitor(toolkit.workspace, memory=getattr(toolkit, "memory", None))
        self._previous_monitor = getattr(toolkit, "monitor", None)
        toolkit.monitor = self.monitor
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
        state["devices"] = self.devices.snapshot()
        state["settings"] = self.settings_snapshot()
        state["development"] = self.development_snapshot()
        state["approvals"] = self.approvals.pending()
        return state

    def development_snapshot(self) -> dict[str, Any]:
        manager = getattr(self.toolkit, "development", None)
        if manager is None:
            return {"enabled": False, "proposals": [], "extensions": []}
        return {"enabled": manager.enabled, "proposals": manager.list(), "extensions": self.toolkit.extension_catalog()}

    def settings_snapshot(self) -> dict[str, Any]:
        client = getattr(self.assistant, "client", None)
        return {
            "full_access": bool(getattr(self.toolkit, "full_disk_access", False)),
            "full_memory": (getattr(client, "num_ctx", None) or 2048) >= 8192,
            "context_tokens": getattr(client, "num_ctx", None) or 2048,
            "vision_model": self.vision_model,
            "self_development": bool(getattr(getattr(self.toolkit, "development", None), "enabled", False)),
        }

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not payload or set(payload) - {"full_access", "full_memory", "self_development"}:
            raise ValueError("Use full_access, full_memory, or self_development settings.")
        if any(type(value) is not bool for value in payload.values()):
            raise ValueError("Settings must be true or false.")
        with self._turn_lock:
            client = getattr(self.assistant, "client", None)
            if "full_memory" in payload and client is None:
                raise ValueError("Model memory settings are unavailable.")
            if "full_memory" in payload and self._busy:
                raise RuntimeError("Wait for the current answer before changing model memory.")
            manager = getattr(self.toolkit, "development", None) if "self_development" in payload else None
            if "self_development" in payload and manager is None:
                raise ValueError("Code development is unavailable in this session.")
            if manager is not None:
                if not payload["self_development"]:
                    self.approvals.cancel_all()
                manager.set_enabled(payload["self_development"])
                if not payload["self_development"]:
                    self.approvals.cancel_all()
            if "full_access" in payload:
                self.toolkit.set_full_disk_access(payload["full_access"])
            if "full_memory" in payload:
                client.num_ctx = 8192 if payload["full_memory"] else 2048
                client.num_predict = 1024 if payload["full_memory"] else 256
        return self.settings_snapshot()

    def submit_development_action(self, payload: dict[str, Any]) -> str | None:
        manager = getattr(self.toolkit, "development", None)
        if manager is None:
            raise ValueError("Code development is unavailable in this session.")
        if payload.get("action") not in ("test", "apply", "rollback"):
            raise ValueError("Choose test, apply, or rollback for an existing proposal.")
        if not isinstance(payload.get("id"), str) or len(payload["id"]) > 100:
            raise ValueError("A proposal ID is required.")
        with self._development_lock:
            if self._closed or any(not job.done() for job in self._development_jobs.values()):
                return None
            while len(self._development_jobs) >= 4:
                self._development_jobs.pop(next(iter(self._development_jobs)))
            request_id = secrets.token_urlsafe(18)
            self._development_jobs[request_id] = self.development_executor.submit(manager.handle, payload)
        return request_id

    def development_job_snapshot(self, request_id: str) -> dict[str, Any] | None:
        with self._development_lock:
            future = self._development_jobs.get(request_id)
        if future is None:
            return None
        if not future.done():
            return {"request_id": request_id, "status": "pending"}
        try:
            result = future.result()
            if isinstance(result, dict) and result.get("ok") is False:
                return {"request_id": request_id, "status": "error", "error": result.get("error") or result.get("content") or json.dumps(result), "result": result}
            return {"request_id": request_id, "status": "complete", "result": result}
        except Exception as exc:
            return {"request_id": request_id, "status": "error", "error": str(exc)}

    def submit_media(self, source: str, operation: Any, result_key: str) -> str | None:
        generation = self.devices.ticket(source)
        def guarded_operation():
            if not self.devices.is_current(source, generation):
                raise MediaError("Device access has been turned off.")
            result = operation()
            if not self.devices.is_current(source, generation):
                raise MediaError("Device access has been turned off; this result was discarded.")
            return str(result)[:12000]
        with self._resource_lock:
            if self._closed or any(not job[0].done() for job in self._media_jobs.values()):
                return None
            while len(self._media_jobs) >= 4:
                self._media_jobs.pop(next(iter(self._media_jobs)))
            request_id = secrets.token_urlsafe(18)
            future = self.media_executor.submit(guarded_operation)
            self._media_jobs[request_id] = (future, source, generation, result_key)
        return request_id

    def submit_speech(self, text: str, voice: str) -> str | None:
        with self._resource_lock:
            if self._closed or any(not job.done() for job in self._speech_jobs.values()):
                return None
            while len(self._speech_jobs) >= 2:
                self._speech_jobs.pop(next(iter(self._speech_jobs)))
            request_id = secrets.token_urlsafe(18)
            self._speech_jobs[request_id] = self.speech_executor.submit(synthesize, text, voice=voice)
        return request_id

    def resource_snapshot(self, request_id: str, *, speech: bool = False) -> dict[str, Any] | None:
        with self._resource_lock:
            job = (self._speech_jobs if speech else self._media_jobs).get(request_id)
        if job is None:
            return None
        if speech:
            future, result_key = job, None
        else:
            future, source, generation, result_key = job
            if not self.devices.is_current(source, generation):
                return {"status": "error", "error": "Device access is off; this result was discarded."}
        if not future.done():
            return {"request_id": request_id, "status": "pending"}
        try:
            result = future.result()
            if not speech and not self.devices.is_current(source, generation):
                raise MediaError("Device access is off; this result was discarded.")
            return {"request_id": request_id, "status": "complete", **({result_key: result} if result_key else {})}
        except Exception as exc:
            return {"request_id": request_id, "status": "error", "error": str(exc)}

    def server_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.approvals.close()
        for source in ("camera", "microphone", "screen"):
            self.devices.set_enabled(source, False)
        self.monitor.stop()
        self.executor.shutdown(wait=True, cancel_futures=False)
        self.media_executor.shutdown(wait=True, cancel_futures=True)
        self.speech_executor.shutdown(wait=True, cancel_futures=True)
        self.development_executor.shutdown(wait=True, cancel_futures=True)
        self.toolkit.permission_handler = self._previous_permission_handler
        self.toolkit.screen_capture_allowed = self._previous_screen_capture
        self.toolkit.monitor = self._previous_monitor
        self.toolkit.development_permission_handler = self._previous_development_permission
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
            "img-src 'self' data:; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
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
        elif path in {"/devices.js", "/development.js"}:
            self._send(200, (Path(__file__).parent / "web" / path[1:]).read_bytes(), "text/javascript; charset=utf-8")
        elif path == "/api/status":
            if self._check_token():
                self._json(200, self.server.status_snapshot())
        elif path.startswith("/api/chat/"):
            if self._check_token():
                snapshot = self.server.job_snapshot(path.removeprefix("/api/chat/"))
                self._json(200 if snapshot is not None else 404, snapshot or {"error": "Turn not found."})
        elif path == "/api/approvals":
            if self._check_token():
                self._json(200, {"approvals": self.server.approvals.pending()})
        elif path.startswith("/api/development/"):
            if self._check_token():
                try:
                    manager = getattr(self.server.toolkit, "development", None)
                    if manager is None:
                        raise ValueError("Code development is unavailable in this session.")
                    if path == "/api/development/source":
                        query = parse_qs(urlsplit(self.path).query)
                        result = manager.inspect(query.get("path", ["."])[0], int(query.get("line", ["1"])[0]))
                        self._json(200, result)
                    elif path.startswith("/api/development/proposals/"):
                        self._json(200, manager.review(path.removeprefix("/api/development/proposals/")))
                    elif path.startswith("/api/development/jobs/"):
                        snapshot = self.server.development_job_snapshot(path.removeprefix("/api/development/jobs/"))
                        self._json(200 if snapshot is not None else 404, snapshot or {"error": "Development job not found."})
                    else:
                        self._json(404, {"error": "Not found."})
                except (ValueError, OSError) as exc:
                    self._json(400, {"error": str(exc)})
        elif path == "/api/voices":
            if self._check_token():
                try:
                    voices = voice_inventory()
                    try:
                        preferred = choose_voice(self.server.voice)
                    except (RuntimeError, ValueError):
                        preferred = self.server.voice
                    self._json(200, {"voices": voices, "preferred": preferred})
                except Exception as exc:
                    self._json(503, {"error": str(exc)})
        elif path.startswith("/api/media/") or path.startswith("/api/speech/"):
            if self._check_token():
                speech = path.startswith("/api/speech/")
                parts = path.split("/")
                if len(parts) not in (4, 5) or (len(parts) == 5 and (not speech or parts[4] != "audio")):
                    self._json(404, {"error": "Not found."})
                    return
                request_id = parts[3]
                snapshot = self.server.resource_snapshot(request_id, speech=speech)
                if speech and len(parts) == 5 and snapshot and snapshot["status"] == "complete":
                    with self.server._resource_lock:
                        future = self.server._speech_jobs.get(request_id)
                    if future is not None:
                        self._send(200, future.result(), "audio/wav")
                        return
                self._json(200 if snapshot is not None else 404, snapshot or {"error": "Request not found."})
        else:
            self._json(404, {"error": "Not found."})

    def do_POST(self) -> None:
        if not self._check_local_request() or not self._check_token():
            return
        path = urlsplit(self.path).path
        if path not in {"/api/chat", "/api/devices", "/api/perception", "/api/transcribe", "/api/speech", "/api/settings", "/api/development/action"} and not path.startswith("/api/approvals/"):
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
        limit = MAX_MEDIA_BODY_BYTES if path in {"/api/perception", "/api/transcribe"} else MAX_BODY_BYTES
        if length > limit:
            self._json(413, {"error": "Request is too large."})
            return
        try:
            self.connection.settimeout(5)
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("Incomplete request")
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("A JSON object is required")
            if path != "/api/chat":
                self._handle_resource_post(path, payload)
                return
            if not isinstance(payload.get("prompt"), str):
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

    def _handle_resource_post(self, path: str, payload: dict[str, Any]) -> None:
        try:
            if path == "/api/devices":
                state = self.server.devices.set_enabled(payload.get("source"), payload.get("enabled"))
                self._json(200, state)
                return
            if path == "/api/settings":
                self._json(200, self.server.update_settings(payload))
                return
            if path.startswith("/api/approvals/"):
                if set(payload) != {"approved"}:
                    raise ValueError("Send only an approved boolean decision.")
                approval_id = path.removeprefix("/api/approvals/")
                accepted = self.server.approvals.decide(approval_id, payload["approved"])
                self._json(200 if accepted else 404, {"accepted": accepted, **({} if accepted else {"error": "Approval expired or was already resolved."})})
                return
            if path == "/api/development/action":
                request_id = self.server.submit_development_action(payload)
                if request_id is None:
                    self._json(429, {"error": "A development action is already running. Please wait."})
                else:
                    self._json(202, {"request_id": request_id, "status": "pending"})
                return
            if path == "/api/transcribe":
                audio = payload.get("audio")
                if not isinstance(audio, str) or not audio:
                    raise ValueError("A base64 WAV recording is required.")
                request_id = self.server.submit_media("microphone", lambda: transcribe_audio(audio, model=self.server.stt_model), "text")
            elif path == "/api/perception":
                source = payload.get("source")
                if source not in ("camera", "screen"):
                    raise ValueError("Choose a camera or screen snapshot.")
                self.server.devices.ticket(source)
                image = validate_image(payload.get("image"))
                prompt = payload.get("prompt", "Describe what you see, Sir.")
                if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 2000:
                    raise ValueError("Use a snapshot question between 1 and 2000 characters.")
                client = getattr(self.server.assistant, "client", None)
                if client is None:
                    raise ValueError("The local vision client is unavailable.")
                request_id = self.server.submit_media(source, lambda: client.describe_image(prompt.strip(), image, model=self.server.vision_model), "reply")
            else:
                text = payload.get("text")
                voice = payload.get("voice", self.server.voice)
                if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARACTERS:
                    raise ValueError(f"Use speech text between 1 and {MAX_TEXT_CHARACTERS} characters.")
                if not isinstance(voice, str) or not voice.strip() or len(voice) > 120:
                    raise ValueError("Choose an installed Mac voice.")
                request_id = self.server.submit_speech(text.strip(), voice)
            if request_id is None:
                self._json(429, {"error": "JARVIS is finishing another media request. Please wait."})
                return
            self._json(202, {"request_id": request_id, "status": "pending"})
        except (ValueError, MediaError) as exc:
            self._json(400, {"error": str(exc)})
        except RuntimeError as exc:
            self._json(409, {"error": str(exc)})

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
    vision_model: str = "moondream",
    stt_model: str = "base",
) -> JarvisWebServer:
    return JarvisWebServer(assistant, toolkit, port=port, speak_answers=speak_answers, voice=voice, vision_model=vision_model, stt_model=stt_model)


def serve(
    assistant: Any,
    toolkit: Any,
    *,
    port: int = 8765,
    speak_answers: bool = False,
    voice: str = "Daniel",
    vision_model: str = "moondream",
    stt_model: str = "base",
) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        print("JARVIS: Web port must be an integer between 1 and 65535.")
        return 1
    try:
        server = create_server(assistant, toolkit, port=port, speak_answers=speak_answers, voice=voice, vision_model=vision_model, stt_model=stt_model)
    except OSError as exc:
        print(f"JARVIS: Could not start the local web interface: {exc}")
        return 1
    print(f"JARVIS orb: http://127.0.0.1:{port} (Ctrl-C to stop).")
    print("British speech uses your Mac's installed voice. Camera, microphone and screens start off.")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
