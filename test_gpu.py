from __future__ import annotations

import json
import plistlib
import subprocess
import unittest
from unittest.mock import patch

from jarvis.gpu import GPUReader, parse_ioreg


def registry(*statistics: dict, model: bytes | str | None = None) -> bytes:
    entries = []
    for index, values in enumerate(statistics):
        entry = {"IORegistryEntryName": f"AGXAccelerator{index}", "PerformanceStatistics": values}
        if model is not None:
            entry["model"] = model
        entries.append(entry)
    return plistlib.dumps([{"IORegistryEntryChildren": entries}])


class GPUParsingTests(unittest.TestCase):
    def test_apple_gpu_direct_statistics_nested_and_zero_usage(self) -> None:
        result = parse_ioreg(registry({
            "Device Utilization %": 0,
            "Allocated system memory": 4294967296,
            "Core Clock(MHz)": 1398,
        }, model=b"Apple M3\x00"))
        self.assertEqual(result, [{
            "name": "Apple M3", "utilization_percent": 0.0,
            "allocated_bytes": 4294967296, "frequency_mhz": 1398.0, "source": "ioreg",
        }])

    def test_missing_and_invalid_metrics_are_not_inferred(self) -> None:
        self.assertEqual(parse_ioreg(plistlib.dumps([{"model": "Apple M1"}])), [])
        self.assertEqual(parse_ioreg(registry({
            "Device Utilization %": 101, "Allocated system memory": -1,
            "Core Clock(MHz)": "nan", "Renderer Utilization %": 25,
        })), [])
        result = parse_ioreg(registry({"Device Utilization %": True, "In use system memory": 0}))
        self.assertIsNone(result[0]["utilization_percent"])
        self.assertEqual(result[0]["allocated_bytes"], 0)
        json.dumps(result, allow_nan=False)

    def test_invalid_plist_raises_for_reader_to_handle(self) -> None:
        with self.assertRaises(plistlib.InvalidFileException):
            parse_ioreg(b"not a plist")


class GPUReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        platform_patch = patch("jarvis.gpu.platform.system", return_value="Darwin")
        platform_patch.start()
        self.addCleanup(platform_patch.stop)
        self.reader = GPUReader()

    def test_mac_model_cached_and_metrics_sampled_each_time(self) -> None:
        model = json.dumps({"SPDisplaysDataType": [{"sppci_model": "Apple M2"}]}).encode()
        samples = [model, registry({"Device Utilization %": 12}), registry({"Device Utilization %": 25})]
        with patch.object(self.reader, "_command", side_effect=samples) as run:
            first, reason = self.reader.snapshot()
            second, _ = self.reader.snapshot()
        self.assertEqual(first["name"], "Apple M2")
        self.assertEqual(first["utilization_percent"], 12)
        self.assertEqual(second["utilization_percent"], 25)
        self.assertIsNone(reason)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args_list[0].args[0], ["/usr/sbin/system_profiler", "SPDisplaysDataType", "-json"])

    def test_agx_fallback_and_multiple_gpu_readings(self) -> None:
        samples = [b"{}", plistlib.dumps([]), registry(
            {"Device Utilization %": 20}, {"Device Utilization %": 60}, model="Apple GPU",
        )]
        with patch.object(self.reader, "_command", side_effect=samples) as run:
            result, reason = self.reader.snapshot()
        self.assertEqual(result["utilization_percent"], 20)
        self.assertEqual([device["utilization_percent"] for device in result["devices"]], [20, 60])
        self.assertIsNone(reason)
        self.assertEqual(run.call_args_list[-1].args[0], ["/usr/sbin/ioreg", "-r", "-c", "AGXAccelerator", "-a"])

    def test_unsupported_mac_reports_model_with_unavailable_metrics(self) -> None:
        samples = [b'{"SPDisplaysDataType":[{"_name":"Apple M4"}]}', b"broken", plistlib.dumps([])]
        with patch.object(self.reader, "_command", side_effect=samples):
            result, reason = self.reader.snapshot()
        self.assertEqual(result["name"], "Apple M4")
        self.assertEqual(result["source"], "system_profiler")
        self.assertIsNone(result["utilization_percent"])
        self.assertIsNone(result["allocated_bytes"])
        self.assertIn("did not expose", reason)

    def test_permission_or_timeout_failure_needs_no_privilege(self) -> None:
        for error in (PermissionError("Denied"), subprocess.TimeoutExpired("ioreg", 5), subprocess.CalledProcessError(1, "ioreg")):
            with self.subTest(error=type(error).__name__):
                reader = GPUReader()
                with patch.object(reader, "_command", side_effect=error) as run:
                    result, reason = reader.snapshot()
                self.assertIsNone(result)
                self.assertIn("did not expose", reason)
                self.assertTrue(all("sudo" not in call.args[0] for call in run.call_args_list))

    def test_subprocess_is_bounded_and_has_no_shell(self) -> None:
        with patch("jarvis.gpu.subprocess.run") as run:
            run.return_value.stdout = b"output"
            self.assertEqual(GPUReader._command(["/usr/sbin/ioreg", "-a"]), b"output")
        run.assert_called_once_with(["/usr/sbin/ioreg", "-a"], capture_output=True, check=True, timeout=5)

    def test_nvidia_memory_units_and_absent_values(self) -> None:
        self.reader._platform = "Linux"
        with patch("jarvis.gpu.shutil.which", return_value="/usr/bin/nvidia-smi"), patch.object(
            self.reader, "_command", return_value=b"NVIDIA RTX 4090, 35, 1024, 2520\nNVIDIA RTX 3090, N/A, N/A, N/A\n"
        ):
            result, reason = self.reader.snapshot()
        self.assertEqual(result["allocated_bytes"], 1024 * 1024 * 1024)
        self.assertEqual(result["frequency_mhz"], 2520)
        self.assertIsNone(result["devices"][1]["utilization_percent"])
        self.assertIsNone(result["devices"][1]["allocated_bytes"])
        self.assertIsNone(reason)

    def test_nvidia_missing_driver_or_invalid_output(self) -> None:
        self.reader._platform = "Linux"
        with patch("jarvis.gpu.shutil.which", return_value=None):
            result, reason = self.reader.snapshot()
        self.assertIsNone(result)
        self.assertIn("unavailable", reason)
        with patch("jarvis.gpu.shutil.which", return_value="/usr/bin/nvidia-smi"), patch.object(
            self.reader, "_command", return_value=b"invalid output"
        ):
            result, reason = self.reader.snapshot()
        self.assertIsNone(result)
        self.assertIn("usable metrics", reason)


if __name__ == "__main__":
    unittest.main()
