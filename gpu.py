"""Read GPU statistics exposed by the operating system, without elevated access.

Apple GPUs use shared system memory. IORegistry may expose allocated bytes and
utilization, but availability varies by Mac model and macOS release. Missing
metrics stay ``None`` rather than being inferred from CPU or memory usage.
"""
from __future__ import annotations

import csv
import io
import json
import math
import platform
import plistlib
import shutil
import subprocess
from typing import Any


def _number(value: Any, *, maximum: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result) or result < 0 or (maximum is not None and result > maximum):
        return None
    return result


def _stat(statistics: dict[str, Any], names: tuple[str, ...], *, maximum: float | None = None) -> float | None:
    for name in names:
        result = _number(statistics.get(name), maximum=maximum)
        if result is not None:
            return result
    return None


def _label(value: Any) -> str | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        result = value.rstrip("\x00").strip()
        return result[:200] or None
    return None


def parse_ioreg(data: bytes) -> list[dict[str, Any]]:
    """Extract only known, directly reported IOAccelerator statistics."""
    registry = plistlib.loads(data)
    readings: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for child in value:
                visit(child)
            return
        if not isinstance(value, dict):
            return
        statistics = value.get("PerformanceStatistics")
        if isinstance(statistics, dict):
            utilization = _stat(
                statistics, ("Device Utilization %", "GPU Utilization %"), maximum=100
            )
            allocation = _stat(
                statistics, ("Allocated system memory", "In use system memory")
            )
            frequency = _stat(statistics, ("Core Clock(MHz)", "GPU Core Clock(MHz)"))
            if any(metric is not None for metric in (utilization, allocation, frequency)):
                readings.append({
                    "name": _label(value.get("model")),
                    "utilization_percent": utilization,
                    "allocated_bytes": int(allocation) if allocation is not None else None,
                    "frequency_mhz": frequency,
                    "source": "ioreg",
                })
        visit(value.get("IORegistryEntryChildren"))

    visit(registry)
    return readings


class GPUReader:
    """Bounded, unprivileged GPU sampling with cached Mac model information."""

    def __init__(self) -> None:
        self._platform = platform.system()
        self._models: list[str] = []
        self._models_loaded = False

    @staticmethod
    def _command(arguments: list[str]) -> bytes:
        return subprocess.run(
            arguments, capture_output=True, check=True, timeout=5,
        ).stdout

    def _mac_models(self) -> list[str]:
        if self._models_loaded:
            return self._models
        self._models_loaded = True
        try:
            report = json.loads(self._command([
                "/usr/sbin/system_profiler", "SPDisplaysDataType", "-json",
            ]))
            displays = report.get("SPDisplaysDataType", []) if isinstance(report, dict) else []
            if isinstance(displays, list):
                for display in displays:
                    if isinstance(display, dict):
                        model = _label(display.get("sppci_model")) or _label(display.get("_name"))
                        if model and model not in self._models:
                            self._models.append(model)
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            pass
        return self._models

    def snapshot(self) -> tuple[dict[str, Any] | None, str | None]:
        if self._platform == "Darwin":
            return self._mac_snapshot()
        if self._platform in ("Linux", "Windows"):
            return self._nvidia_snapshot()
        return None, "GPU metrics are not available on this operating system."

    def _mac_snapshot(self) -> tuple[dict[str, Any] | None, str | None]:
        models = self._mac_models()
        readings: list[dict[str, Any]] = []
        for driver in ("IOAccelerator", "AGXAccelerator"):
            try:
                readings = parse_ioreg(self._command([
                    "/usr/sbin/ioreg", "-r", "-c", driver, "-a",
                ]))
            except (OSError, subprocess.SubprocessError, ValueError, TypeError, plistlib.InvalidFileException):
                continue
            if readings:
                break
        if readings:
            for reading in readings:
                reading["name"] = reading["name"] or (models[0] if len(models) == 1 else "GPU")
            result = dict(readings[0])
            if len(readings) > 1:
                result["devices"] = readings
            reason = None if result["utilization_percent"] is not None else (
                "macOS did not expose GPU utilization through IORegistry."
            )
            return result, reason
        reason = "macOS did not expose GPU statistics through IORegistry."
        if models:
            return {
                "name": ", ".join(models),
                "utilization_percent": None,
                "allocated_bytes": None,
                "frequency_mhz": None,
                "source": "system_profiler",
            }, reason
        return None, reason

    def _nvidia_snapshot(self) -> tuple[dict[str, Any] | None, str | None]:
        executable = shutil.which("nvidia-smi")
        if executable is None:
            return None, "GPU metrics require a supported driver; nvidia-smi is unavailable."
        try:
            output = self._command([
                executable,
                "--query-gpu=name,utilization.gpu,memory.used,clocks.current.graphics",
                "--format=csv,noheader,nounits",
            ]).decode("utf-8", errors="replace")
            readings = []
            for row in csv.reader(io.StringIO(output)):
                if len(row) != 4 or not _label(row[0]):
                    continue
                allocation = _number(row[2])
                readings.append({
                    "name": _label(row[0]),
                    "utilization_percent": _number(row[1], maximum=100),
                    "allocated_bytes": int(allocation * 1024 * 1024) if allocation is not None else None,
                    "frequency_mhz": _number(row[3]),
                    "source": "nvidia-smi",
                })
            if readings:
                result = dict(readings[0])
                if len(readings) > 1:
                    result["devices"] = readings
                reason = None if result["utilization_percent"] is not None else (
                    "The GPU driver did not report utilization."
                )
                return result, reason
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            pass
        return None, "The GPU driver did not return usable metrics."
