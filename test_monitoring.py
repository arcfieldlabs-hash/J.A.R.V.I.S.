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
        gpu_patch = patch("jarvis.monitoring.GPUReader")
        gpu_reader = gpu_patch.start()
        gpu_reader.return_value.snapshot.return_value = (None, "GPU unavailable in test.")
        self.addCleanup(gpu_patch.stop)

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

    def test_actual_metrics_and_network_rates(self) -> None:
        counters = [SimpleNamespace(bytes_sent=100, bytes_recv=200), SimpleNamespace(bytes_sent=160, bytes_recv=300)]
        optional = SimpleNamespace(
            cpu_percent=lambda interval, percpu=False: [20,29] if percpu else 24.5,
            cpu_count=lambda logical: 2 if logical else 1,
            virtual_memory=lambda: SimpleNamespace(total=1000, available=600, used=400, percent=40),
            swap_memory=lambda: SimpleNamespace(total=500, used=100, free=400, percent=20),
            Process=lambda pid: SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=150)),
            net_io_counters=lambda: counters.pop(0),
        )
        with patch("jarvis.monitoring.psutil", optional), patch("jarvis.monitoring.time.monotonic", side_effect=[10,10,12]):
            monitor = SystemMonitor(self.workspace)
            self.assertIsNone(monitor.snapshot()["cpu_percent"])
            monitor._sample_metrics()
        state = monitor.snapshot()
        self.assertEqual(state["cpu_percent"], 24.5)
        self.assertEqual(state["cpu"], {"core_percent":[20,29],"logical_cores":2,"physical_cores":1})
        self.assertEqual(state["memory"]["percent"], 40)
        self.assertEqual(state["memory"]["used_bytes"], 400)
        self.assertEqual(state["memory"]["swap"]["used_bytes"], 100)
        self.assertEqual(state["memory"]["process_rss_bytes"], 150)
        self.assertEqual(state["network"]["sent_bytes_per_second"], 30)
        self.assertEqual(state["network"]["received_bytes_per_second"], 50)

    def test_cpu_initializes_independently_on_sampling_thread(self) -> None:
        optional = SimpleNamespace(
            cpu_percent=lambda interval, percpu=False: [10,30] if percpu else 20,
            cpu_count=lambda logical: 2,
            virtual_memory=lambda: SimpleNamespace(total=1000, available=600, used=400, percent=40),
            net_io_counters=lambda: None,
        )
        with patch("jarvis.monitoring.psutil", optional), patch("jarvis.monitoring.threading.get_ident", side_effect=[101,101,202,202]):
            monitor = SystemMonitor(self.workspace)
            self.assertIsNone(monitor.snapshot()["cpu_percent"])
            self.assertIsNone(monitor.snapshot()["cpu"]["core_percent"])
            monitor._sample_metrics()
            self.assertEqual(monitor.snapshot()["cpu_percent"], 20)
            monitor._sample_metrics()
            self.assertIsNone(monitor.snapshot()["cpu_percent"])
            self.assertIsNone(monitor.snapshot()["cpu"]["core_percent"])
            monitor._sample_metrics()
            self.assertEqual(monitor.snapshot()["cpu"]["core_percent"], [10,30])

    def test_collector_failure_preserves_unrelated_metrics(self) -> None:
        def unavailable(*args, **kwargs):
            raise PermissionError("Unavailable")

        optional = SimpleNamespace(
            cpu_percent=unavailable, cpu_count=unavailable,
            virtual_memory=lambda: SimpleNamespace(total=1000, available=600, used=400, percent=40),
            swap_memory=unavailable, Process=unavailable,
            net_io_counters=lambda: SimpleNamespace(bytes_sent=100, bytes_recv=200),
        )
        with patch("jarvis.monitoring.psutil", optional):
            monitor = SystemMonitor(self.workspace)
        state = monitor.snapshot()
        self.assertIsNone(state["cpu_percent"])
        self.assertEqual(state["memory"]["used_bytes"], 400)
        self.assertEqual(state["network"]["received_bytes"], 200)
        self.assertIn("CPU usage unavailable.", state["errors"])
        self.assertIn("Swap metrics unavailable.", state["errors"])

    def test_ram_and_network_failures_do_not_lose_cpu(self) -> None:
        def unavailable():
            raise OSError("Unavailable")

        optional = SimpleNamespace(
            cpu_percent=lambda interval, percpu=False: [10] if percpu else 10,
            cpu_count=lambda logical: 1,
            virtual_memory=unavailable, net_io_counters=unavailable,
        )
        with patch("jarvis.monitoring.psutil", optional):
            monitor = SystemMonitor(self.workspace)
            monitor._sample_metrics()
        state = monitor.snapshot()
        self.assertEqual(state["cpu_percent"], 10)
        self.assertEqual(state["cpu"]["core_percent"], [10])
        self.assertIsNone(state["memory"])
        self.assertIsNone(state["network"])

    def test_network_counter_reset_and_zero_elapsed(self) -> None:
        counters = [SimpleNamespace(bytes_sent=100, bytes_recv=200), SimpleNamespace(bytes_sent=50, bytes_recv=80), SimpleNamespace(bytes_sent=70, bytes_recv=100)]
        optional = SimpleNamespace(net_io_counters=lambda: counters.pop(0))
        with patch("jarvis.monitoring.psutil", optional), patch("jarvis.monitoring.time.monotonic", side_effect=[10,10,12,12]):
            monitor = SystemMonitor(self.workspace)
            monitor._sample_metrics()
            self.assertEqual(monitor.snapshot()["network"]["sent_bytes_per_second"], 0)
            self.assertEqual(monitor.snapshot()["network"]["received_bytes_per_second"], 0)
            monitor._sample_metrics()
        self.assertIsNone(monitor.snapshot()["network"]["sent_bytes_per_second"])

    def test_gpu_failure_does_not_drop_core_metrics(self) -> None:
        optional = SimpleNamespace(
            cpu_percent=lambda interval, percpu=False: [10] if percpu else 10,
            cpu_count=lambda logical: 1,
            virtual_memory=lambda: SimpleNamespace(total=1000, available=600, used=400, percent=40),
            net_io_counters=lambda: SimpleNamespace(bytes_sent=100, bytes_recv=200),
        )
        with patch("jarvis.monitoring.GPUReader") as reader, patch("jarvis.monitoring.psutil", optional):
            reader.return_value.snapshot.side_effect = RuntimeError("GPU unavailable")
            monitor = SystemMonitor(self.workspace)
        self.assertEqual(monitor.snapshot()["memory"]["used_bytes"], 400)
        self.assertEqual(monitor.snapshot()["network"]["received_bytes"], 200)
        self.assertIn("could not be read", monitor.snapshot()["gpu_reason"])

    def test_native_gpu_metrics_do_not_depend_on_psutil(self) -> None:
        gpu = {"name":"Apple M3","utilization_percent":15,"allocated_bytes":None,"frequency_mhz":None,"source":"ioreg"}
        with patch("jarvis.monitoring.GPUReader") as reader, patch("jarvis.monitoring.psutil", None):
            reader.return_value.snapshot.return_value = (gpu, None)
            monitor = SystemMonitor(self.workspace)
        self.assertEqual(monitor.snapshot()["gpu"], gpu)
        self.assertIsNone(monitor.snapshot()["memory"])
        self.assertIn("need psutil", monitor.snapshot()["errors"][0])

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
