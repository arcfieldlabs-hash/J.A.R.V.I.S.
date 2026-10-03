import json
import math
import threading
import time
import unittest
from unittest.mock import patch

from jarvis.approvals import (
    ApprovalBroker, MAX_DETAILS_BYTES, MAX_PENDING_APPROVALS,
)


class ApprovalBrokerTests(unittest.TestCase):
    def setUp(self):
        self.broker = ApprovalBroker(timeout=2)
        self.threads = []

    def tearDown(self):
        self.broker.close()
        for thread in self.threads:
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive(), "Approval worker did not stop.")

    def start_request(self, summary="Apply proposed change", details=None, broker=None):
        broker = broker or self.broker
        result = {}

        def request():
            result["approved"] = broker.request(summary, details or {"diff": "+ new tool"})

        thread = threading.Thread(target=request)
        self.threads.append(thread)
        thread.start()
        return thread, result

    def wait_pending(self, count=1, broker=None):
        broker = broker or self.broker
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            pending = broker.pending()
            if len(pending) == count:
                return pending
            time.sleep(0.001)
        self.fail(f"Expected {count} pending approvals; found {len(broker.pending())}.")

    def test_waits_for_explicit_approval_and_uses_it_once(self):
        thread, result = self.start_request()
        record, = self.wait_pending()
        self.assertTrue(thread.is_alive())
        self.assertEqual(result, {})
        self.assertEqual(set(record), {"id", "summary", "details", "created_at", "expires_at"})
        self.assertEqual(record["expires_at"] - record["created_at"], 2)
        self.assertGreaterEqual(len(record["id"]), 24)
        self.assertTrue(self.broker.decide(record["id"], True))
        self.assertFalse(self.broker.decide(record["id"], True))
        thread.join(timeout=1)
        self.assertEqual(result, {"approved": True})
        self.assertEqual(self.broker.pending(), [])

    def test_rejection_is_accepted_but_returns_no_permission(self):
        thread, result = self.start_request()
        record, = self.wait_pending()
        self.assertTrue(self.broker.decide(record["id"], False))
        thread.join(timeout=1)
        self.assertEqual(result, {"approved": False})
        self.assertFalse(self.broker.decide(record["id"], True))

    def test_timeout_denies_and_withdraws_request(self):
        broker = ApprovalBroker(timeout=0.05)
        self.addCleanup(broker.close)
        thread, result = self.start_request(broker=broker)
        record, = self.wait_pending(broker=broker)
        thread.join(timeout=1)
        self.assertEqual(result, {"approved": False})
        self.assertEqual(broker.pending(), [])
        self.assertFalse(broker.decide(record["id"], True))

    def test_zero_timeout_never_waits_for_user(self):
        broker = ApprovalBroker(timeout=0)
        self.assertFalse(broker.request("Apply", {}))
        self.assertEqual(broker.pending(), [])

    def test_pending_expires_requests_and_unblocks_worker(self):
        thread, result = self.start_request()
        record, = self.wait_pending()
        with patch("jarvis.approvals.time.monotonic", return_value=time.monotonic() + 3):
            self.assertEqual(self.broker.pending(), [])
            self.assertFalse(self.broker.decide(record["id"], True))
        thread.join(timeout=1)
        self.assertEqual(result, {"approved": False})

    def test_decide_alone_rejects_expired_id(self):
        thread, result = self.start_request()
        record, = self.wait_pending()
        with patch("jarvis.approvals.time.monotonic", return_value=time.monotonic() + 3):
            self.assertFalse(self.broker.decide(record["id"], True))
        thread.join(timeout=1)
        self.assertEqual(result, {"approved": False})

    def test_strict_boolean_and_unknown_ids(self):
        thread, result = self.start_request()
        record, = self.wait_pending()
        for decision in (1, 0, "true", "false", None, [], {}):
            with self.subTest(decision=decision), self.assertRaises(ValueError):
                self.broker.decide(record["id"], decision)
        self.assertTrue(thread.is_alive())
        for unknown in ("unknown", "", None, [], 123):
            with self.subTest(unknown=unknown):
                self.assertFalse(self.broker.decide(unknown, True))
        self.assertTrue(self.broker.decide(record["id"], False))
        thread.join(timeout=1)
        self.assertEqual(result, {"approved": False})

    def test_details_cannot_be_changed_through_caller_or_snapshot(self):
        details = {"code": "def tool():\n    return 42\n", "files": [{"path": "tools.py"}]}
        thread, result = self.start_request(details=details)
        record, = self.wait_pending()
        details["code"] = "changed after request"
        details["files"][0]["path"] = "caller mutation"
        record["details"]["code"] = "snapshot mutation"
        record["details"]["files"][0]["path"] = "nested mutation"
        current, = self.broker.pending()
        self.assertEqual(current["details"], {
            "code": "def tool():\n    return 42\n", "files": [{"path": "tools.py"}],
        })
        json.dumps(current, allow_nan=False)
        self.assertTrue(self.broker.decide(current["id"], True))
        thread.join(timeout=1)
        self.assertEqual(result, {"approved": True})

    def test_complete_proposal_at_size_limit_is_preserved(self):
        overhead = len(json.dumps({"code": ""}, separators=(",", ":")).encode("utf-8"))
        source = "x" * (MAX_DETAILS_BYTES - overhead)
        thread, result = self.start_request(details={"code": source})
        record, = self.wait_pending()
        self.assertEqual(record["details"]["code"], source)
        self.assertTrue(self.broker.decide(record["id"], True))
        thread.join(timeout=1)
        self.assertEqual(result, {"approved": True})
        with self.assertRaisesRegex(ValueError, "512 KiB"):
            self.broker.request("Apply", {"code": source + "x"})
        self.assertEqual(self.broker.pending(), [])

    def test_utf8_bytes_determine_size_limit(self):
        with self.assertRaisesRegex(ValueError, "512 KiB"):
            self.broker.request("Apply", {"code": "é" * (MAX_DETAILS_BYTES // 2)})
        self.assertEqual(self.broker.pending(), [])

    def test_invalid_details_and_summaries_do_not_create_pending_approval(self):
        for details in (None, [], "code", {"set": {1}}, {"bad": math.nan},
                        {"bad": math.inf}, {"bad": object()}, {"bad": "\ud800"}):
            with self.subTest(details=repr(details)), self.assertRaises(ValueError):
                self.broker.request("Apply", details)
        circular = {}
        circular["self"] = circular
        with self.assertRaises(ValueError):
            self.broker.request("Apply", circular)
        for summary in (None, [], 1, "", "  "):
            with self.subTest(summary=summary), self.assertRaises(ValueError):
                self.broker.request(summary, {})
        self.assertEqual(self.broker.pending(), [])

    def test_cancel_all_denies_and_old_id_cannot_approve_new_request(self):
        first_thread, first_result = self.start_request()
        old_record, = self.wait_pending()
        self.broker.cancel_all()
        first_thread.join(timeout=1)
        self.assertEqual(first_result, {"approved": False})
        self.assertEqual(self.broker.pending(), [])
        second_thread, second_result = self.start_request()
        new_record, = self.wait_pending()
        self.assertNotEqual(new_record["id"], old_record["id"])
        self.assertFalse(self.broker.decide(old_record["id"], True))
        self.assertEqual(second_result, {})
        self.assertTrue(second_thread.is_alive())
        self.assertTrue(self.broker.decide(new_record["id"], True))
        second_thread.join(timeout=1)
        self.assertEqual(second_result, {"approved": True})

    def test_parallel_requests_are_distinct_and_decisions_do_not_mix(self):
        workers = [self.start_request(summary=f"Change {index}")
                   for index in range(MAX_PENDING_APPROVALS)]
        records = self.wait_pending(MAX_PENDING_APPROVALS)
        self.assertEqual(len({record["id"] for record in records}), MAX_PENDING_APPROVALS)
        self.assertFalse(self.broker.request("Excess request", {}))
        self.assertEqual(len(self.broker.pending()), MAX_PENDING_APPROVALS)
        for record in reversed(records):
            index = int(record["summary"].split()[-1])
            self.assertTrue(self.broker.decide(record["id"], index % 2 == 0))
        for index, (thread, result) in enumerate(workers):
            thread.join(timeout=1)
            self.assertEqual(result, {"approved": index % 2 == 0})
        self.assertEqual(self.broker.pending(), [])

    def test_close_denies_all_pending_and_all_future_requests(self):
        workers = [self.start_request(summary=f"Change {index}") for index in range(4)]
        records = self.wait_pending(4)
        self.broker.close()
        self.broker.close()
        for thread, result in workers:
            thread.join(timeout=1)
            self.assertEqual(result, {"approved": False})
        for record in records:
            self.assertFalse(self.broker.decide(record["id"], True))
        self.broker.cancel_all()
        self.assertFalse(self.broker.request("After close", {}))
        self.assertEqual(self.broker.pending(), [])

    def test_invalid_constructor_timeout_is_rejected(self):
        for timeout in (-1, math.inf, math.nan, True, "180", None):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                ApprovalBroker(timeout=timeout)


if __name__ == "__main__":
    unittest.main()
