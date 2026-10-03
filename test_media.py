import base64
import io
import struct
import threading
import types
import unittest
import wave
from unittest.mock import MagicMock, patch

from jarvis.media import (
    DevicePermissions, MAX_MEDIA_BYTES, MediaError, transcribe_audio,
    validate_audio, validate_image,
)


# Actual 2x2 JPEG fixtures; validating them needs no imaging dependency.
BASELINE_JPEG = (
    '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwg'
    'JC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIy'
    'MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAACAAIDASIAAhEBAxEB/8QA'
    'HwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIh'
    'MUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVW'
    'V1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG'
    'x8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQF'
    'BgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAV'
    'YnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOE'
    'hYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq'
    '8vP09fb3+Pn6/9oADAMBAAIRAxEAPwCxRRRX0B+cH//Z'
)
PROGRESSIVE_JPEG = (
    '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwg'
    'JC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIy'
    'MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wgARCAACAAIDASIAAhEBAxEB/8QA'
    'FQABAQAAAAAAAAAAAAAAAAAAAAP/xAAVAQEBAAAAAAAAAAAAAAAAAAAFBv/aAAwDAQACEAMQAAABoEJv'
    '/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgB'
    'AwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPwF//8QAFBABAAAAAAAAAAAAAAAAAAAA'
    'AP/aAAgBAQAGPwJ//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPyF//9oADAMBAAIAAwAAABAH'
    '/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPxB//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgB'
    'AgEBPxB//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxB//9k='
)


def encoded(raw):
    return base64.b64encode(raw).decode("ascii")


def wav_recording(*, rate=16000, channels=1, width=2, frames=None, seconds=0.01):
    if frames is None:
        frames = b"\x00" * (round(rate * seconds) * channels * width)
    output = io.BytesIO()
    with wave.open(output, "wb") as recording:
        recording.setnchannels(channels)
        recording.setsampwidth(width)
        recording.setframerate(rate)
        recording.writeframes(frames)
    return output.getvalue()


class DevicePermissionTests(unittest.TestCase):
    def setUp(self):
        self.permissions = DevicePermissions()

    def test_every_source_is_off_initially(self):
        for source, state in self.permissions.snapshot().items():
            self.assertEqual(state, {"enabled": False, "generation": 0})
            with self.assertRaisesRegex(MediaError, "access is off"):
                self.permissions.ticket(source)

    def test_off_and_on_revokes_old_generation(self):
        self.permissions.set_enabled("camera", True)
        ticket = self.permissions.ticket("camera")
        self.assertTrue(self.permissions.is_current("camera", ticket))
        self.permissions.set_enabled("camera", False)
        self.assertFalse(self.permissions.is_current("camera", ticket))
        self.permissions.set_enabled("camera", True)
        self.assertFalse(self.permissions.is_current("camera", ticket))
        self.assertGreater(self.permissions.ticket("camera"), ticket)

    def test_repeated_enable_also_revokes_previous_ticket(self):
        self.permissions.set_enabled("screen", True)
        ticket = self.permissions.ticket("screen")
        self.permissions.set_enabled("screen", True)
        self.assertFalse(self.permissions.is_current("screen", ticket))

    def test_unrelated_source_does_not_revoke_a_ticket(self):
        self.permissions.set_enabled("camera", True)
        ticket = self.permissions.ticket("camera")
        self.permissions.set_enabled("microphone", True)
        self.permissions.set_enabled("microphone", False)
        self.assertTrue(self.permissions.is_current("camera", ticket))

    def test_snapshot_cannot_mutate_internal_state(self):
        snapshot = self.permissions.set_enabled("screen", True)
        snapshot["screen"]["enabled"] = False
        self.assertTrue(self.permissions.snapshot()["screen"]["enabled"])

    def test_bad_sources_and_non_boolean_permission_rejected(self):
        for source in ("all", "", None, []):
            with self.subTest(source=source), self.assertRaises(MediaError):
                self.permissions.set_enabled(source, True)
        for value in (1, 0, "false", None):
            with self.subTest(value=value), self.assertRaises(MediaError):
                self.permissions.set_enabled("camera", value)

    def test_concurrent_changes_preserve_all_generations(self):
        def toggle():
            for _ in range(100):
                self.permissions.set_enabled("camera", True)
                self.permissions.set_enabled("camera", False)
        threads = [threading.Thread(target=toggle) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.permissions.snapshot()["camera"],
                         {"enabled": False, "generation": 800})


class ImageValidationTests(unittest.TestCase):
    def test_actual_baseline_and_progressive_snapshots_are_accepted(self):
        for snapshot in (BASELINE_JPEG, PROGRESSIVE_JPEG):
            with self.subTest(progressive=snapshot == PROGRESSIVE_JPEG):
                self.assertEqual(validate_image(snapshot), snapshot)

    def test_bad_base64_and_other_formats_are_rejected(self):
        for image in (None, "", "!!!!", "é", "data:image/jpeg;base64," + BASELINE_JPEG,
                      encoded(b"\x89PNG\r\n\x1a\n")):
            with self.subTest(image=str(image)[:20]), self.assertRaises(MediaError):
                validate_image(image)

    def test_oversized_data_is_rejected_before_parsing(self):
        with self.assertRaisesRegex(MediaError, "2 MiB"):
            validate_image(encoded(b"x" * (MAX_MEDIA_BYTES + 1)))

    def test_truncated_markers_segments_and_entropy_are_rejected(self):
        raw = base64.b64decode(BASELINE_JPEG)
        bad_length = bytearray(raw)
        dqt = raw.index(b"\xff\xdb")
        bad_length[dqt + 2:dqt + 4] = b"\xff\xff"
        for data in (raw[:-1], raw[:-2], raw[:5], b"\xff\xd8\xff", bytes(bad_length)):
            with self.subTest(size=len(data)), self.assertRaises(MediaError):
                validate_image(encoded(data))

    def test_missing_frame_and_scan_headers_are_rejected(self):
        raw = base64.b64decode(BASELINE_JPEG)
        start = raw.index(b"\xff\xc0")
        end = start + 2 + int.from_bytes(raw[start + 2:start + 4], "big")
        with self.assertRaisesRegex(MediaError, "frame header"):
            validate_image(encoded(raw[:start] + raw[end:]))
        scan = raw.index(b"\xff\xda")
        with self.assertRaises(MediaError):
            validate_image(encoded(raw[:scan] + b"\xff\xd9"))
        scan_end = scan + 2 + int.from_bytes(raw[scan + 2:scan + 4], "big")
        with self.assertRaises(MediaError):
            validate_image(encoded(raw[:scan_end] + b"\xff\xd9"))

    def test_dimensions_and_pixel_budget_are_enforced(self):
        raw = base64.b64decode(BASELINE_JPEG)
        start = raw.index(b"\xff\xc0")
        for height, width in ((0, 2), (2, 0), (2049, 1), (1, 2049), (2048, 2048)):
            data = bytearray(raw)
            data[start + 5:start + 9] = struct.pack(">HH", height, width)
            with self.subTest(height=height, width=width), self.assertRaisesRegex(MediaError, "dimensions"):
                validate_image(encoded(data))

    def test_trailing_data_and_invalid_markers_are_rejected(self):
        raw = base64.b64decode(BASELINE_JPEG)
        for data in (raw + b"junk", raw[:2] + b"bad" + raw[2:], b"\xff\xd8\xff\x00\xff\xd9"):
            with self.subTest(size=len(data)), self.assertRaises(MediaError):
                validate_image(encoded(data))


class AudioValidationTests(unittest.TestCase):
    def test_mono_pcm_remains_unchanged(self):
        frames = struct.pack("<hhhh", 100, -100, 32767, -32768)
        self.assertEqual(validate_audio(encoded(wav_recording(frames=frames))), (frames, 16000))

    def test_stereo_is_averaged_to_mono_without_audioop(self):
        stereo = struct.pack("<hhhhhh", 1000, -1000, 32767, 32767, -32768, -32768)
        mono = struct.pack("<hhh", 0, 32767, -32768)
        self.assertEqual(validate_audio(encoded(wav_recording(channels=2, frames=stereo))), (mono, 16000))

    def test_bad_encoding_empty_and_other_formats_are_rejected(self):
        for data in (None, "", "!!!", encoded(b"not a wav"), encoded(wav_recording(seconds=0))):
            with self.subTest(data=str(data)[:20]), self.assertRaises(MediaError):
                validate_audio(data)

    def test_unsupported_formats_and_sample_rates_are_rejected(self):
        cases = ({"rate": 7999}, {"rate": 48001}, {"width": 1}, {"width": 3}, {"channels": 3})
        for options in cases:
            with self.subTest(options=options), self.assertRaisesRegex(MediaError, "PCM16"):
                validate_audio(encoded(wav_recording(**options)))
        float_wav = bytearray(wav_recording())
        float_wav[20:22] = struct.pack("<H", 3)
        with self.assertRaisesRegex(MediaError, "PCM16"):
            validate_audio(encoded(float_wav))

    def test_duration_is_bounded_and_exact_limit_accepted(self):
        self.assertEqual(len(validate_audio(encoded(wav_recording(rate=8000, seconds=20)))[0]), 320000)
        with self.assertRaisesRegex(MediaError, "20 seconds"):
            validate_audio(encoded(wav_recording(rate=8000, seconds=20.001)))

    def test_size_cap_applies_to_wav_too(self):
        with self.assertRaisesRegex(MediaError, "2 MiB"):
            validate_audio(encoded(wav_recording(rate=48000, channels=2, seconds=20)))

    def test_truncated_recordings_and_incomplete_frames_are_rejected(self):
        raw = wav_recording()
        incomplete = bytearray(wav_recording(frames=b"\x01\x02\x03"))
        # The data chunk has three bytes, so include a valid RIFF padding byte.
        incomplete += b"\x00"
        incomplete[4:8] = struct.pack("<I", len(incomplete) - 8)
        for data in (raw[:-1], raw[:15], bytes(incomplete)):
            with self.subTest(size=len(data)), self.assertRaises(MediaError):
                validate_audio(encoded(data))

    def test_invalid_lengths_byte_rates_and_duplicate_chunks_are_rejected(self):
        raw = wav_recording()
        wrong_byte_rate = bytearray(raw)
        wrong_byte_rate[28:32] = struct.pack("<I", 1)
        duplicate = bytearray(raw + raw[12:36])
        duplicate[4:8] = struct.pack("<I", len(duplicate) - 8)
        missing_data = bytearray(raw[:36])
        missing_data[4:8] = struct.pack("<I", len(missing_data) - 8)
        for data in (raw + b"junk", bytes(wrong_byte_rate), bytes(duplicate), bytes(missing_data)):
            with self.subTest(size=len(data)), self.assertRaises(MediaError):
                validate_audio(encoded(data))


class TranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.sr = types.SimpleNamespace(AudioData=MagicMock(), Recognizer=MagicMock())
        self.recognizer = self.sr.Recognizer.return_value
        self.recognizer.recognize_whisper.return_value = "  Open my calendar.  "
        self.imports = patch("jarvis.media.import_module",
                             side_effect=lambda name: self.sr if name == "speech_recognition" else object())
        self.imports.start()
        self.addCleanup(self.imports.stop)

    def test_validated_pcm_and_preferred_model_reach_local_whisper(self):
        frames = struct.pack("<hh", 100, -100)
        self.assertEqual(transcribe_audio(encoded(wav_recording(frames=frames)), model="tiny"),
                         "Open my calendar.")
        self.sr.AudioData.assert_called_once_with(frames, 16000, 2)
        self.recognizer.recognize_whisper.assert_called_once_with(
            self.sr.AudioData.return_value, model="tiny", language="en")

    def test_invalid_audio_never_imports_heavy_dependencies(self):
        with patch("jarvis.media.import_module") as loader:
            with self.assertRaises(MediaError):
                transcribe_audio(encoded(b"invalid"))
            loader.assert_not_called()

    def test_missing_voice_dependencies_explain_installation(self):
        with patch("jarvis.media.import_module", side_effect=ImportError("whisper")):
            with self.assertRaisesRegex(MediaError, r"pip install -e '\.\[voice\]'"):
                transcribe_audio(encoded(wav_recording()))

    def test_transcription_failures_are_clear(self):
        self.recognizer.recognize_whisper.side_effect = RuntimeError("model unavailable")
        with self.assertRaisesRegex(MediaError, "Local voice transcription failed: model unavailable"):
            transcribe_audio(encoded(wav_recording()))

    def test_invalid_or_empty_results_and_long_transcripts_are_rejected(self):
        for result in (None, "  ", "x" * 8001):
            self.recognizer.recognize_whisper.return_value = result
            with self.subTest(result=str(result)[:10]), self.assertRaises(MediaError):
                transcribe_audio(encoded(wav_recording()))

    def test_invalid_model_is_rejected(self):
        for model in (None, "", " " * 3, "x" * 121):
            with self.subTest(model=model), self.assertRaises(MediaError):
                transcribe_audio(encoded(wav_recording()), model=model)


if __name__ == "__main__":
    unittest.main()
