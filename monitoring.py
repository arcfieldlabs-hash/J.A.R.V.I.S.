"""Portable background telemetry and delivery of local, persisted reminders."""
from __future__ import annotations

import copy
import math
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .gpu import GPUReader

try:
    import psutil
except ImportError:  # Graceful upgrade path for older installs missing psutil.
    psutil = None


class SystemMonitor:
    """Sample actual system metrics and claim due reminders from a memory store.

    ``memory.due_reminders()`` must atomically mark the returned reminders as
    delivered. The snapshot retains the last 50 deliveries for the web interface;
    notification callbacks are best effort and do not re-claim a reminder.
    """

    def __init__(
        self,
        workspace: Path,
        memory: Any = None,
        on_reminder: Callable[[dict[str, Any]], None] | None = None,
        interval: float = 5,
    ) -> None:
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("Monitor interval must be a positive number.")
        self.workspace = Path(workspace).expanduser().resolve()
        self.memory = memory
        self.on_reminder = on_reminder
        self.interval = interval
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._started_at = time.monotonic()
        self._network_sample: tuple[float, int, int] | None = None
        self._cpu_thread_id: int | None = None
        self._cpu_core_thread_id: int | None = None
        self._gpu_reader = GPUReader()
        self._deliveries: list[dict[str, Any]] = []
        self._state: dict[str, Any] = {}
        self._sample_metrics()

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="jarvis-monitor", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop.set()
            thread = self._thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = copy.deepcopy(self._state)
            state["reminders"] = copy.deepcopy(self._deliveries)
        state["monitoring"] = bool(self._thread and self._thread.is_alive())
        return state

    def _run(self) -> None:
        while not self._stop.is_set():
            self._sample_metrics()
            self._check_reminders()
            self._stop.wait(self.interval)

    def _sample_metrics(self) -> None:
        now = time.monotonic()
        state: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "workspace": str(self.workspace),
            "uptime_seconds": round(now - self._started_at, 1),
            "cpu_percent": None,
            "cpu": {"core_percent": None, "logical_cores": None, "physical_cores": None},
            "load_average": None,
            "memory": None,
            "disk": None,
            "network": None,
            "gpu": None,
            "gpu_reason": None,
            "metrics_source": "stdlib" if psutil is None else "psutil",
            "errors": [],
        }
        try:
            state["load_average"] = list(os.getloadavg())
        except (AttributeError, OSError):
            pass
        try:
            disk = shutil.disk_usage(self.workspace)
            state["disk"] = {
                "total_bytes": disk.total,
                "used_bytes": disk.used,
                "free_bytes": disk.free,
                "percent": round(100 * disk.used / disk.total, 1) if disk.total else 0,
            }
        except OSError:
            state["errors"].append("Workspace disk metrics unavailable.")
        if psutil is None:
            state["errors"].append("CPU, RAM and network metrics need psutil; run python -m pip install -e .")
        else:
            thread_id = threading.get_ident()
            try:
                cpu = psutil.cpu_percent(interval=None)
                # psutil maintains its CPU baseline separately for each thread.
                state["cpu_percent"] = cpu if self._cpu_thread_id == thread_id else None
                self._cpu_thread_id = thread_id
            except Exception:
                state["errors"].append("CPU usage unavailable.")
            try:
                cores = psutil.cpu_percent(interval=None, percpu=True)
                state["cpu"]["core_percent"] = cores if self._cpu_core_thread_id == thread_id else None
                self._cpu_core_thread_id = thread_id
            except Exception:
                state["errors"].append("Per-core CPU usage unavailable.")
            for key, logical in (("logical_cores", True), ("physical_cores", False)):
                try:
                    state["cpu"][key] = psutil.cpu_count(logical=logical)
                except Exception:
                    state["errors"].append(f"{key.replace('_', ' ').capitalize()} unavailable.")
            try:
                memory = psutil.virtual_memory()
                state["memory"] = {
                    "total_bytes": memory.total,
                    "available_bytes": memory.available,
                    "used_bytes": getattr(memory, "used", None),
                    "percent": memory.percent,
                    "swap": None,
                    "process_rss_bytes": None,
                }
            except Exception:
                state["errors"].append("RAM metrics unavailable.")
            if state["memory"] is not None:
                try:
                    swap = psutil.swap_memory()
                    state["memory"]["swap"] = {
                        "total_bytes": swap.total,
                        "used_bytes": swap.used,
                        "free_bytes": swap.free,
                        "percent": swap.percent,
                    }
                except Exception:
                    state["errors"].append("Swap metrics unavailable.")
                try:
                    state["memory"]["process_rss_bytes"] = psutil.Process(os.getpid()).memory_info().rss
                except Exception:
                    state["errors"].append("Jarvis process RAM metrics unavailable.")
            try:
                network = psutil.net_io_counters()
                if network is not None:
                    previous = self._network_sample
                    elapsed = now - previous[0] if previous else 0
                    state["network"] = {
                        "sent_bytes": network.bytes_sent,
                        "received_bytes": network.bytes_recv,
                        "sent_bytes_per_second": (
                            max(0, network.bytes_sent - previous[1]) / elapsed
                            if previous and elapsed > 0 else None
                        ),
                        "received_bytes_per_second": (
                            max(0, network.bytes_recv - previous[2]) / elapsed
                            if previous and elapsed > 0 else None
                        ),
                    }
                    self._network_sample = (now, network.bytes_sent, network.bytes_recv)
            except Exception:
                state["errors"].append("Network metrics unavailable.")
        try:
            state["gpu"], state["gpu_reason"] = self._gpu_reader.snapshot()
        except Exception:
            state["gpu_reason"] = "GPU metrics could not be read from the operating system."
        with self._lock:
            self._state = state

    def _check_reminders(self) -> None:
        if self.memory is None:
            return
        try:
            reminders = self.memory.due_reminders()
        except Exception:
            with self._lock:
                self._state["errors"].append("Could not check due reminders.")
            return
        for reminder in reminders:
            delivery = dict(reminder)
            delivery["delivered_at"] = datetime.now(timezone.utc).isoformat()
            with self._lock:
                self._deliveries.append(delivery)
                self._deliveries = self._deliveries[-50:]
            if self.on_reminder is not None:
                try:
                    self.on_reminder(copy.deepcopy(delivery))
                except Exception:
                    with self._lock:
                        self._state["errors"].append("Reminder notification failed.")
