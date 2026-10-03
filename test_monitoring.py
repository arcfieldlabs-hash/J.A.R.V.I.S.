from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.monitoring import SystemMonitor


class MonitoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_portable_snapshot_without_optional_packages(self) -> None:
        with patch("jarvis.monitoring.psutil", None):
            monitor = SystemMonitor(self.workspace)
        state = monitor.snapshot()
        self.assertIsNone(state["cpu_percent"])
        self.assertIsNone(state["memory"])
        self.assertIsNone(state["network"])
        self.assertIsNone(state["gpu"])
        self.assertGreater(state["disk"]["total_bytes"], 0)
        self.assertFalse(state["monitoring"])
        json.dumps(state, allow_nan=False)
        state["disk"]["free_bytes"] = -1
        self.assertGreaterEqual(monitor.snapshot()["disk"]["free_bytes"], 0)

    def test_actual_optional_metrics_and_network_rates(self) -> None:
        counters = [SimpleNamespace(bytes_sent=100, bytes_recv=200), SimpleNamespace(bytes_sent=160, bytes_recv=300)]
        optional = SimpleNamespace(
            cpu_percent=lambda interval: 24.5,
            virtual_memory=lambda: SimpleNamespace(total=1000, available=600, percent=40),
            net_io_counters=lambda: counters.pop(0),
        )
        with patch("jarvis.monitoring.psutil", optional), patch("jarvis.monitoring.time.monotonic", side_effect=[10,10,12]):
            monitor = SystemMonitor(self.workspace)
            self.assertIsNone(monitor.snapshot()["cpu_percent"])
            monitor._sample_metrics()
        state = monitor.snapshot()
        self.assertEqual(state["cpu_percent"], 24.5)
        self.assertEqual(state["memory"]["percent"], 40)
        self.assertEqual(state["network"]["sent_bytes_per_second"], 30)
        self.assertEqual(state["network"]["received_bytes_per_second"], 50)

    def test_lifecycle_and_deliver_due_reminder_once(self) -> None:
        notified = threading.Event()
        checked_again = threading.Event()
        notifications = []

        class Memory:
            checks = 0

            def due_reminders(self):
                self.checks += 1
                if self.checks == 1:
                    return [{"id":1,"text":"Take a break","due_at":"2026-10-03T00:00:00+00:00"}]
                checked_again.set()
                return []

        def on_reminder(reminder):
            notifications.append(reminder)
            notified.set()

        monitor = SystemMonitor(self.workspace, Memory(), on_reminder, interval=.01)
        try:
            monitor.start()
            worker = monitor._thread
            monitor.start()
            self.assertIs(monitor._thread, worker)
            self.assertTrue(notified.wait(1))
            self.assertTrue(checked_again.wait(1))
            self.assertTrue(monitor.snapshot()["monitoring"])
            self.assertEqual(len(notifications), 1)
            self.assertEqual(monitor.snapshot()["reminders"][0]["text"], "Take a break")
            self.assertIn("delivered_at", notifications[0])
        finally:
            monitor.stop()
        self.assertFalse(monitor.snapshot()["monitoring"])
        monitor.stop()

    def test_callback_failure_keeps_delivery_and_monitor_running(self) -> None:
        called = threading.Event()

        class Memory:
            reminders = [{"id":1,"text":"Reminder","due_at":"2026-10-03"}]

            def due_reminders(self):
                reminders, self.reminders = self.reminders, []
                return reminders

        def fail(reminder):
            called.set()
            raise RuntimeError("Notification failed")

        monitor = SystemMonitor(self.workspace, Memory(), fail, interval=10)
        try:
            monitor.start()
            self.assertTrue(called.wait(1))
        finally:
            monitor.stop()
        self.assertEqual(len(monitor.snapshot()["reminders"]), 1)
        self.assertIn("Reminder notification failed.", monitor.snapshot()["errors"])

    def test_unavailable_memory_store_does_not_stop_monitor(self) -> None:
        class Memory:
            def due_reminders(self):
                raise OSError("Unavailable")

        monitor = SystemMonitor(self.workspace, Memory())
        monitor._check_reminders()
        self.assertIn("Could not check due reminders.", monitor.snapshot()["errors"])

    def test_interval_validation(self) -> None:
        for value in [0,-1,float("inf"),float("nan")]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                SystemMonitor(self.workspace, interval=value)


if __name__ == "__main__":
    unittest.main()
