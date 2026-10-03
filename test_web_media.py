"""Local HTTP boundaries for revocable devices, native speech, and settings."""
from __future__ import annotations

import base64
import http.client
import json
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.media import MAX_MEDIA_BYTES, MediaError
from jarvis.tools import ToolKit
from jarvis.web import MAX_MEDIA_BODY_BYTES, create_server
from jarvis.test_media import BASELINE_JPEG, wav_recording


class VisionClient:
    def __init__(self) -> None:
        self.num_ctx = None
        self.num_predict = None
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def describe_image(self, prompt, image, *, model):
        self.calls.append((prompt, image, model))
        self.started.set()
        if not self.release.wait(3):
            raise RuntimeError("Test vision worker did not release")
        return "There is a local preview, Sir."


class Assistant:
    def __init__(self) -> None:
        self.client = VisionClient()
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def ask(self, prompt):
        self.started.set()
        if not self.release.wait(3):
            raise RuntimeError("Test chat worker did not release")
        return "Answer: " + prompt


class WebMediaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.toolkit = ToolKit(workspace=self.root / "workspace")
        self.assistant = Assistant()
        self.vision_client = self.assistant.client
        self.blocked_operations: list[threading.Event] = []
        monitor_patch = patch("jarvis.web.SystemMonitor")
        monitor = monitor_patch.start()
        monitor.return_value.snapshot.return_value = {"memory": None, "gpu": None}
        self.addCleanup(monitor_patch.stop)
        self.server = create_server(self.assistant, self.toolkit, port=0)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True,
        )
        self.thread.start()

    def tearDown(self) -> None:
        self.assistant.release.set()
        self.vision_client.release.set()
        for release in self.blocked_operations:
            release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        self.toolkit.close()
        self.directory.cleanup()

    def request(self, method, path, body=None, *, token=True, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        request_headers = {"X-Jarvis-Token": self.server.csrf_token} if token else {}
        if isinstance(body, dict) or isinstance(body, list):
            body = json.dumps(body)
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        try:
            connection.request(method, path, body, request_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def enable(self, source, enabled=True):
        status, _, body = self.request("POST", "/api/devices", {"source": source, "enabled": enabled})
        self.assertEqual(status, 200)
        return json.loads(body)

    def submit(self, path, body):
        status, _, response = self.request("POST", path, body)
        self.assertEqual(status, 202, response.decode())
        return json.loads(response)["request_id"]

    def wait_for_result(self, request_id, *, speech=False, chat=False):
        resource = "chat" if chat else "speech" if speech else "media"
        for _ in range(100):
            status, _, body = self.request("GET", f"/api/{resource}/{request_id}")
            self.assertEqual(status, 200)
            result = json.loads(body)
            if result["status"] != "pending":
                return result
            threading.Event().wait(.005)
        self.fail(f"{resource} request did not complete")

    def test_new_endpoints_require_token_and_local_origin(self) -> None:
        posts = {
            "/api/devices": {"source": "camera", "enabled": True},
            "/api/perception": {"source": "camera", "image": BASELINE_JPEG},
            "/api/transcribe": {"audio": "recording"},
            "/api/speech": {"text": "Good evening, Sir."},
            "/api/settings": {"full_access": True},
        }
        gets = ["/api/voices", "/api/media/not-found", "/api/speech/not-found", "/api/speech/not-found/audio"]
        for path, payload in posts.items():
            with self.subTest(path=path):
                self.assertEqual(self.request("POST", path, payload, token=False)[0], 403)
                self.assertEqual(self.request("POST", path, payload, headers={"Origin": "https://attacker.example"})[0], 403)
        for path in gets:
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path, token=False)[0], 403)
                self.assertEqual(self.request("GET", path, headers={"Origin": "https://attacker.example"})[0], 403)
        self.assertFalse(self.toolkit.full_disk_access)
        self.assertEqual(self.assistant.client.calls, [])
        self.assertTrue(all(not state["enabled"] for state in self.server.devices.snapshot().values()))

    def test_devices_start_off_and_reject_recordings_before_processing(self) -> None:
        status, _, body = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(all(not state["enabled"] for state in json.loads(body)["devices"].values()))
        with patch("jarvis.web.transcribe_audio") as transcribe:
            status, _, body = self.request("POST", "/api/transcribe", {"audio": "recording"})
            self.assertEqual(status, 400)
            self.assertIn("off", json.loads(body)["error"])
            transcribe.assert_not_called()
        for source in ("camera", "screen"):
            status, _, body = self.request("POST", "/api/perception", {"source": source, "image": BASELINE_JPEG})
            self.assertEqual(status, 400)
            self.assertIn("off", json.loads(body)["error"])
        self.assertEqual(self.assistant.client.calls, [])

    def test_device_permissions_require_actual_booleans_and_known_sources(self) -> None:
        for enabled in (1, 0, "false", None, []):
            with self.subTest(enabled=enabled):
                self.assertEqual(self.request("POST", "/api/devices", {"source": "camera", "enabled": enabled})[0], 400)
        for source in (None, "all", [], "camera; open /tmp"):
            with self.subTest(source=source):
                self.assertEqual(self.request("POST", "/api/devices", {"source": source, "enabled": True})[0], 400)
        self.assertFalse(self.server.devices.snapshot()["camera"]["enabled"])

    def test_off_revokes_pending_result_immediately_and_reenable_does_not_restore_it(self) -> None:
        self.enable("camera")
        self.assistant.client.release.clear()
        request_id = self.submit("/api/perception", {"source": "camera", "image": BASELINE_JPEG})
        self.assertTrue(self.assistant.client.started.wait(1))
        self.enable("camera", False)
        result = self.wait_for_result(request_id)
        self.assertEqual(result["status"], "error")
        self.assertIn("discarded", result["error"])
        self.assertNotIn("reply", result)
        self.enable("camera")
        self.assertEqual(self.wait_for_result(request_id)["status"], "error")
        self.assistant.client.release.set()
        future = self.server._media_jobs[request_id][0]
        with self.assertRaisesRegex(MediaError, "discarded"):
            future.result(timeout=1)
        self.assertNotIn("reply", self.wait_for_result(request_id))

    def test_microphone_off_discards_transcript_even_after_reenable(self) -> None:
        self.enable("microphone")
        started, release = threading.Event(), threading.Event()
        self.blocked_operations.append(release)

        def transcribe(audio, *, model):
            started.set()
            if not release.wait(3):
                raise RuntimeError("Test transcription worker did not release")
            return "Private words from a revoked recording."

        with patch("jarvis.web.transcribe_audio", side_effect=transcribe):
            request_id = self.submit("/api/transcribe", {"audio": "recording"})
            self.assertTrue(started.wait(1))
            self.enable("microphone", False)
            self.assertNotIn("text", self.wait_for_result(request_id))
            self.enable("microphone")
            release.set()
            with self.assertRaises(MediaError):
                self.server._media_jobs[request_id][0].result(timeout=1)
        result = self.wait_for_result(request_id)
        self.assertEqual(result["status"], "error")
        self.assertNotIn("text", result)

    def test_camera_jpeg_validation_and_byte_pixel_budgets(self) -> None:
        self.enable("camera")
        raw = base64.b64decode(BASELINE_JPEG)
        oversized_dimensions = bytearray(raw)
        frame = raw.index(b"\xff\xc0")
        oversized_dimensions[frame + 5:frame + 9] = struct.pack(">HH", 1, 2049)
        malformed = (
            "!!!!", base64.b64encode(b"\x89PNG\r\n\x1a\n").decode(),
            base64.b64encode(raw[:-1]).decode(),
            base64.b64encode(oversized_dimensions).decode(),
            base64.b64encode(b"x" * (MAX_MEDIA_BYTES + 1)).decode(),
        )
        for image in malformed:
            with self.subTest(encoded_length=len(image)):
                status, _, _ = self.request("POST", "/api/perception", {"source": "camera", "image": image})
                self.assertEqual(status, 400)
        self.assertEqual(self.assistant.client.calls, [])
        status, _, _ = self.request(
            "POST", "/api/perception", "{}",
            headers={"Content-Type": "application/json", "Content-Length": str(MAX_MEDIA_BODY_BYTES + 1)},
        )
        self.assertEqual(status, 413)
        request_id = self.submit("/api/perception", {"source": "camera", "image": BASELINE_JPEG, "prompt": "What is visible?"})
        self.assertEqual(self.wait_for_result(request_id)["reply"], "There is a local preview, Sir.")
        self.assertEqual(self.assistant.client.calls, [("What is visible?", BASELINE_JPEG, "moondream")])

    def test_one_media_job_accepts_no_queue_and_recovers_after_completion(self) -> None:
        self.enable("camera")
        self.enable("microphone")
        self.assistant.client.release.clear()
        first = self.submit("/api/perception", {"source": "camera", "image": BASELINE_JPEG})
        self.assertTrue(self.assistant.client.started.wait(1))
        with patch("jarvis.web.transcribe_audio", return_value="Open the calendar.") as transcribe:
            status, _, _ = self.request("POST", "/api/transcribe", {"audio": "recording"})
            self.assertEqual(status, 429)
            transcribe.assert_not_called()
            self.assertEqual(len(self.server._media_jobs), 1)
            self.assistant.client.release.set()
            self.assertEqual(self.wait_for_result(first)["status"], "complete")
            second = self.submit("/api/transcribe", {"audio": "recording"})
            self.assertEqual(self.wait_for_result(second)["text"], "Open the calendar.")
            transcribe.assert_called_once_with("recording", model="base")

    def test_native_wav_results_require_token_and_return_no_filesystem_path(self) -> None:
        audio = wav_recording(rate=22050)
        with patch("jarvis.web.synthesize", return_value=audio) as synthesize:
            request_id = self.submit("/api/speech", {"text": "Good evening, Sir.", "voice": "Daniel"})
            result = self.wait_for_result(request_id, speech=True)
        self.assertEqual(set(result), {"request_id", "status"})
        self.assertEqual(result["status"], "complete")
        synthesize.assert_called_once_with("Good evening, Sir.", voice="Daniel")
        path = f"/api/speech/{request_id}/audio"
        self.assertEqual(self.request("GET", path, token=False)[0], 403)
        self.assertEqual(self.request("GET", path, headers={"Origin": "https://attacker.example"})[0], 403)
        status, headers, body = self.request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "audio/wav")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(body, audio)
        self.assertEqual(self.request("GET", "/api/speech/../../etc/passwd/audio")[0], 404)
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_speech_has_one_worker_and_exposes_local_voice_inventory(self) -> None:
        started, release = threading.Event(), threading.Event()
        self.blocked_operations.append(release)

        def synthesize(text, *, voice):
            started.set()
            if not release.wait(3):
                raise RuntimeError("Test speech worker did not release")
            return wav_recording(rate=22050)

        with patch("jarvis.web.synthesize", side_effect=synthesize):
            request_id = self.submit("/api/speech", {"text": "Good evening."})
            self.assertTrue(started.wait(1))
            self.assertEqual(self.request("POST", "/api/speech", {"text": "A second reply."})[0], 429)
            release.set()
            self.assertEqual(self.wait_for_result(request_id, speech=True)["status"], "complete")
        voices = [{"name": "Daniel (Enhanced)", "language": "en_GB"}]
        with patch("jarvis.web.voice_inventory", return_value=voices):
            status, _, body = self.request("GET", "/api/voices")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"voices": voices, "preferred": "Daniel"})

    def test_settings_grant_and_revoke_storage_and_set_model_context(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("An explicitly granted local file.")
        self.assertFalse(self.toolkit.read_file({"path": str(outside)}).ok)
        status, _, body = self.request("POST", "/api/settings", {"full_access": True, "full_memory": True})
        self.assertEqual(status, 200)
        settings = json.loads(body)
        self.assertTrue(settings["full_access"])
        self.assertTrue(settings["full_memory"])
        self.assertEqual(settings["context_tokens"], 8192)
        self.assertEqual(self.assistant.client.num_predict, 1024)
        self.assertTrue(self.toolkit.read_file({"path": str(outside)}).ok)
        self.assertFalse(self.toolkit.permission_handler("shell command", "reason"))
        status, _, body = self.request("POST", "/api/settings", {"full_access": False, "full_memory": False})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["full_access"])
        self.assertFalse(self.toolkit.read_file({"path": str(outside)}).ok)
        self.assertEqual(self.assistant.client.num_ctx, 2048)
        self.assertEqual(self.assistant.client.num_predict, 256)

    def test_settings_reject_invalid_payloads_and_busy_memory_changes(self) -> None:
        for payload in ({}, {"full_access": 1}, {"full_memory": "false"}, {"unknown": True}):
            with self.subTest(payload=payload):
                self.assertEqual(self.request("POST", "/api/settings", payload)[0], 400)
        self.assertFalse(self.toolkit.full_disk_access)
        self.assistant.release.clear()
        request_id = self.submit("/api/chat", {"prompt": "Think for a moment."})
        self.assertTrue(self.assistant.started.wait(1))
        status, _, _ = self.request("POST", "/api/settings", {"full_access": True, "full_memory": True})
        self.assertEqual(status, 409)
        self.assertFalse(self.toolkit.full_disk_access)
        self.assertIsNone(self.assistant.client.num_ctx)
        self.assistant.release.set()
        self.assertEqual(self.wait_for_result(request_id, chat=True)["status"], "complete")

    def test_failed_memory_setting_does_not_partially_grant_storage(self) -> None:
        self.assistant.client = None
        status, _, _ = self.request("POST", "/api/settings", {"full_access": True, "full_memory": True})
        self.assertEqual(status, 400)
        self.assertFalse(self.toolkit.full_disk_access)


if __name__ == "__main__":
    unittest.main()
