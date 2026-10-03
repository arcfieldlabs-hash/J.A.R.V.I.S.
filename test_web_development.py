"""Real HTTP and extension lifecycle checks for user-controlled development."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from urllib.parse import urlencode

from jarvis.selfdev import SelfDevelopment
from jarvis.tools import ToolKit
from jarvis.web import create_server


EXTENSION_CODE = "def run(args, context):\n    return {'answer': args['number'] * 2}\n"
EXTENSION_PARAMETERS = {
    "type": "object", "properties": {"number": {"type": "integer"}},
    "required": ["number"], "additionalProperties": False,
}


class DevelopmentAssistant:
    """Exercise real tool dispatch without making a model or network request."""

    def __init__(self, toolkit):
        self.toolkit = toolkit
        self.client = SimpleNamespace(num_ctx=2048, num_predict=256)
        self.action = None

    def ask(self, prompt):
        if self.action is None:
            return "Ready, Sir."
        name, args = self.action
        return self.toolkit.execute(name, args).to_json()


class WebDevelopmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "__init__.py").write_text("", encoding="utf-8")
        (self.source / "pyproject.toml").write_text("[project]\nname = 'test-jarvis'\n", encoding="utf-8")
        (self.source / "helper.py").write_text("def example():\n    return 42\n", encoding="utf-8")
        self.toolkit = ToolKit(workspace=self.root / "workspace", data_dir=self.root / "private")
        self.manager = SelfDevelopment(
            source_root=self.source, data_dir=self.root / "private",
            workspace=self.toolkit.workspace, registry=self.toolkit.extensions,
            permission_handler=self.toolkit._development_permission,
        )
        self.toolkit._development = self.manager
        self.assistant = DevelopmentAssistant(self.toolkit)
        self.server = create_server(self.assistant, self.toolkit, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.approvals.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        self.toolkit.close()
        self.temporary.cleanup()

    def request(self, method, path, payload=None, *, token=True, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        request_headers = {"X-Jarvis-Token": self.server.csrf_token} if token else {}
        body = None
        if payload is not None:
            body = json.dumps(payload)
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def enable(self, enabled=True):
        status, result = self.request("POST", "/api/settings", {"self_development": enabled})
        self.assertEqual(status, 200, result)
        self.assertIs(result["self_development"], enabled)
        return result

    def propose(self):
        proposal = self.manager.propose({
            "kind": "extension", "name": "ext_double", "title": "Double a number",
            "description": "Return twice the requested integer.",
            "parameters": EXTENSION_PARAMETERS, "code": EXTENSION_CODE,
            "sample_args": {"number": 7},
        })
        self.assertEqual(proposal["status"], "staged")
        return proposal

    def action(self, action, proposal_id):
        status, result = self.request("POST", "/api/development/action",
                                      {"action": action, "id": proposal_id})
        self.assertEqual(status, 202, result)
        return result["request_id"]

    def wait_approval(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status, result = self.request("GET", "/api/approvals")
            self.assertEqual(status, 200, result)
            if result["approvals"]:
                self.assertEqual(len(result["approvals"]), 1)
                return result["approvals"][0]
            time.sleep(0.005)
        self.fail("No approval request reached the HTTP interface.")

    def decide(self, approval, approved):
        status, result = self.request("POST", "/api/approvals/" + approval["id"],
                                      {"approved": approved})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["accepted"])

    def wait_job(self, request_id, *, chat=False):
        path = ("/api/chat/" if chat else "/api/development/jobs/") + request_id
        deadline = time.monotonic() + 5
        last = None
        while time.monotonic() < deadline:
            status, last = self.request("GET", path)
            self.assertEqual(status, 200, last)
            if last["status"] != "pending":
                return last
            time.sleep(0.005)
        self.fail(f"Worker did not finish: {last}")

    def install_fixture(self):
        self.enable()
        proposal = self.propose()
        for action in ("test", "apply"):
            request_id = self.action(action, proposal["id"])
            self.decide(self.wait_approval(), True)
            completed = self.wait_job(request_id)
            self.assertEqual(completed["status"], "complete", completed)
            self.assertTrue(completed["result"]["ok"], completed)
        return proposal

    def submit_chat_tool(self, name, args):
        self.assistant.action = (name, args)
        status, result = self.request("POST", "/api/chat", {"prompt": "Run the requested fixture tool."})
        self.assertEqual(status, 202, result)
        return result["request_id"]

    def test_new_routes_require_local_origin_host_and_session_token(self):
        routes = [
            ("GET", "/api/approvals", None),
            ("GET", "/api/development/source?path=helper.py", None),
            ("GET", "/api/development/proposals/unknown", None),
            ("GET", "/api/development/jobs/unknown", None),
            ("POST", "/api/settings", {"self_development": True}),
            ("POST", "/api/development/action", {"action": "test", "id": "unknown"}),
            ("POST", "/api/approvals/unknown", {"approved": True}),
        ]
        for method, path, payload in routes:
            with self.subTest(method=method, path=path, protection="missing token"):
                self.assertEqual(self.request(method, path, payload, token=False)[0], 403)
            for headers in ({"X-Jarvis-Token": "wrong"}, {"Origin": "https://attacker.example"},
                            {"Origin": "null"}, {"Host": "attacker.example"}):
                with self.subTest(method=method, path=path, headers=headers):
                    self.assertEqual(self.request(method, path, payload, headers=headers)[0], 403)
        self.assertFalse(self.manager.enabled)
        self.assertEqual(self.manager.list(), [])
        self.assertEqual(self.server.approvals.pending(), [])

    def test_permission_is_off_initially_and_settings_are_independent(self):
        status, state = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertFalse(state["settings"]["self_development"])
        self.assertFalse(state["development"]["enabled"])
        self.assertFalse(state["settings"]["full_access"])
        self.assertFalse(state["settings"]["full_memory"])
        with self.assertRaisesRegex(ValueError, "Self-development is off"):
            self.propose()
        enabled = self.enable()
        self.assertFalse(enabled["full_access"])
        self.assertFalse(enabled["full_memory"])
        self.assertTrue(self.manager.enabled)
        self.enable(False)
        self.assertFalse(self.manager.enabled)

    def test_source_inspection_cannot_escape_checkout_or_read_private_data(self):
        status, result = self.request("GET", "/api/development/source?path=helper.py")
        self.assertEqual(status, 200, result)
        self.assertIn("return 42", result["content"])
        (self.root / "secret.py").write_text("secret outside checkout", encoding="utf-8")
        (self.source / "escape.py").symlink_to(self.root / "secret.py")
        (self.source / ".env").write_text("SECRET=private", encoding="utf-8")
        for path in ("../secret.py", str(self.root / "secret.py"), "escape.py", ".env", "../private/memory.sqlite3"):
            with self.subTest(path=path):
                status, result = self.request("GET", "/api/development/source?" + urlencode({"path": path}))
                self.assertEqual(status, 400, result)
                self.assertNotIn("secret outside checkout", json.dumps(result))
                self.assertNotIn("SECRET=private", json.dumps(result))
        self.assertEqual(self.request("GET", "/api/development/source?line=invalid")[0], 400)

    def test_review_test_apply_and_dynamic_invocation_require_separate_approvals(self):
        self.enable()
        proposal = self.propose()
        status, review = self.request("GET", "/api/development/proposals/" + proposal["id"])
        self.assertEqual(status, 200, review)
        self.assertEqual(review["code"], EXTENSION_CODE)
        self.assertIn("+    return {'answer': args['number'] * 2}", review["diff"])
        self.assertEqual(self.toolkit.extension_catalog(), [])

        rejected_job = self.action("test", proposal["id"])
        approval = self.wait_approval()
        self.assertEqual(approval["details"]["code"], EXTENSION_CODE)
        self.assertEqual(approval["details"]["sample_args"], {"number": 7})
        status, pending = self.request("GET", "/api/development/jobs/" + rejected_job)
        self.assertEqual(status, 200)
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(self.request("POST", "/api/development/action",
                                      {"action": "test", "id": proposal["id"]})[0], 429)
        self.decide(approval, False)
        rejected = self.wait_job(rejected_job)
        self.assertEqual(rejected["status"], "error", rejected)
        self.assertIn("approval", rejected["error"])
        self.assertEqual(self.manager.review(proposal["id"])["status"], "staged")
        self.assertEqual(self.toolkit.extension_catalog(), [])

        tested_job = self.action("test", proposal["id"])
        test_approval = self.wait_approval()
        self.assertNotEqual(test_approval["id"], approval["id"])
        self.decide(test_approval, True)
        tested = self.wait_job(tested_job)
        self.assertEqual(tested["status"], "complete", tested)
        self.assertTrue(tested["result"]["test_result"]["passed"])
        self.assertEqual(json.loads(tested["result"]["test_result"]["output"]), {"answer": 14})
        self.assertEqual(self.toolkit.extension_catalog(), [])

        applied_job = self.action("apply", proposal["id"])
        apply_approval = self.wait_approval()
        self.assertNotEqual(apply_approval["id"], test_approval["id"])
        self.assertTrue(apply_approval["details"]["test_result"]["passed"])
        self.assertEqual(self.toolkit.extension_catalog(), [])
        self.decide(apply_approval, True)
        applied = self.wait_job(applied_job)
        self.assertEqual(applied["status"], "complete", applied)
        self.assertEqual(applied["result"]["status"], "applied")
        self.assertIn("ext_double", self.toolkit.available_tools)

        chat_job = self.submit_chat_tool("ext_double", {"number": 9})
        invocation = self.wait_approval()
        self.assertEqual(invocation["details"]["args"], {"number": 9})
        self.assertEqual(invocation["details"]["code"], EXTENSION_CODE)
        status, state = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(state["busy"])
        self.assertEqual(state["approvals"][0]["id"], invocation["id"])
        self.assertEqual(self.request("POST", "/api/chat", {"prompt": "another turn"})[0], 429)
        self.decide(invocation, True)
        reply = self.wait_job(chat_job, chat=True)
        self.assertEqual(reply["status"], "complete", reply)
        tool_result = json.loads(reply["reply"])
        self.assertTrue(tool_result["ok"])
        self.assertEqual(json.loads(tool_result["content"]), {"answer": 18})

        denied_job = self.submit_chat_tool("ext_double", {"number": 10})
        denied_approval = self.wait_approval()
        self.decide(denied_approval, False)
        denied = json.loads(self.wait_job(denied_job, chat=True)["reply"])
        self.assertFalse(denied["ok"])
        self.assertIn("not approved", denied["content"])

    def test_permission_off_cancels_pending_test_and_reenabling_rejects_old_id(self):
        self.enable()
        proposal = self.propose()
        request_id = self.action("test", proposal["id"])
        approval = self.wait_approval()
        self.enable(False)
        result = self.wait_job(request_id)
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(self.server.approvals.pending(), [])
        self.assertEqual(self.manager.review(proposal["id"])["status"], "staged")
        self.assertEqual(self.toolkit.extension_catalog(), [])
        self.enable()
        status, decision = self.request("POST", "/api/approvals/" + approval["id"], {"approved": True})
        self.assertEqual(status, 404, decision)
        retry = self.action("test", proposal["id"])
        fresh = self.wait_approval()
        self.assertNotEqual(fresh["id"], approval["id"])
        self.decide(fresh, False)
        self.assertEqual(self.wait_job(retry)["status"], "error")

    def test_permission_off_revokes_pending_extension_execution(self):
        self.install_fixture()
        request_id = self.submit_chat_tool("ext_double", {"number": 4})
        approval = self.wait_approval()
        self.enable(False)
        result = json.loads(self.wait_job(request_id, chat=True)["reply"])
        self.assertFalse(result["ok"])
        self.assertIn("not approved", result["content"])
        self.assertFalse(self.manager.enabled)
        self.enable()
        self.assertEqual(self.request("POST", "/api/approvals/" + approval["id"], {"approved": True})[0], 404)

    def test_decisions_and_settings_require_booleans_and_unknown_ids_are_404(self):
        for value in (1, 0, "true", "false", None, [], {}):
            with self.subTest(value=value):
                self.assertEqual(self.request("POST", "/api/settings", {"self_development": value})[0], 400)
                self.assertEqual(self.request("POST", "/api/approvals/unknown", {"approved": value})[0], 400)
        for payload in ({}, {"approved": True, "extra": "field"}):
            self.assertEqual(self.request("POST", "/api/approvals/unknown", payload)[0], 400)
        self.assertEqual(self.request("POST", "/api/approvals/unknown", {"approved": True})[0], 404)
        self.assertEqual(self.request("GET", "/api/development/jobs/unknown")[0], 404)
        self.assertFalse(self.manager.enabled)

    def test_server_close_unblocks_pending_worker_and_restores_permission_handler(self):
        self.enable()
        proposal = self.propose()
        request_id = self.action("test", proposal["id"])
        approval = self.wait_approval()
        future = self.server._development_jobs[request_id]
        self.server.shutdown()
        finished = threading.Event()

        def close():
            self.server.server_close()
            finished.set()

        closing = threading.Thread(target=close, daemon=True)
        closing.start()
        self.assertTrue(finished.wait(2), "Server shutdown left a worker awaiting approval.")
        closing.join(timeout=1)
        self.assertTrue(future.done())
        with self.assertRaisesRegex(ValueError, "approval"):
            future.result()
        self.assertEqual(self.toolkit.extension_catalog(), [])
        self.assertIsNone(self.toolkit.development_permission_handler)
        self.assertFalse(self.server.approvals.decide(approval["id"], True))


if __name__ == "__main__":
    unittest.main()
