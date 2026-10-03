import io
import subprocess
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from jarvis import speech


VOICES = (
    "Samantha             en_US    # Hello, my name is Samantha.\n"
    "Daniel               en_GB    # Hello, my name is Daniel.\n"
    "Daniel (Enhanced)    en_GB    # Hello, my name is Daniel.\n"
    "Reed (English (UK))  en_GB    # Hello, my name is Reed.\n"
)


def wav_bytes():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(22050)
        audio.writeframes(b"\x00\x00" * 50)
    return stream.getvalue()


class SpeechTests(unittest.TestCase):
    def setUp(self):
        self.mac = patch("jarvis.speech.platform.system", return_value="Darwin")
        self.mac.start()
        self.addCleanup(self.mac.stop)
        self.cache = patch("jarvis.speech._inventory_cache", None)
        self.cache.start()
        self.addCleanup(self.cache.stop)
        self.run = patch("jarvis.speech.subprocess.run")
        self.mock_run = self.run.start()
        self.addCleanup(self.run.stop)
        self.mock_run.return_value = subprocess.CompletedProcess([], 0, VOICES, "")

    def test_voice_inventory_parses_names_with_spaces_and_returns_copies(self):
        voices = speech.voice_inventory()
        self.assertEqual(voices[2], {"name": "Daniel (Enhanced)", "language": "en_GB"})
        self.assertEqual(voices[3]["name"], "Reed (English (UK))")
        voices[0]["name"] = "Changed"
        self.assertEqual(speech.voice_inventory()[0]["name"], "Samantha")
        self.mock_run.assert_called_once_with(
            ["/usr/bin/say", "-v", "?"], input=None, text=True,
            capture_output=True, check=True, timeout=5,
        )

    def test_voice_inventory_cache_expires_after_one_minute(self):
        with patch("jarvis.speech.time.monotonic", return_value=10):
            speech.voice_inventory()
        with patch("jarvis.speech.time.monotonic", return_value=69.9):
            speech.voice_inventory()
        with patch("jarvis.speech.time.monotonic", return_value=70):
            speech.voice_inventory()
        self.assertEqual(self.mock_run.call_count, 2)

    def test_prefers_enhanced_daniel(self):
        self.assertEqual(speech.choose_voice(), "Daniel (Enhanced)")

    def test_prefers_premium_daniel_over_enhanced(self):
        self.mock_run.return_value.stdout += "Daniel (Premium) en_GB # Hello\n"
        self.assertEqual(speech.choose_voice(), "Daniel (Premium)")

    def test_plain_daniel_is_used_if_it_is_the_installed_variant(self):
        self.mock_run.return_value.stdout = "Daniel en_GB # Hello\n"
        self.assertEqual(speech.choose_voice(), "Daniel")

    def test_daniel_must_be_british(self):
        self.mock_run.return_value.stdout = "Daniel en_US # Hello\n"
        with self.assertRaisesRegex(speech.SpeechError, "English \\(United Kingdom\\)"):
            speech.choose_voice()

    def test_explicit_installed_voice_is_respected(self):
        self.assertEqual(speech.choose_voice("Reed (English (UK))"), "Reed (English (UK))")
        self.assertEqual(speech.choose_voice("Daniel (Enhanced)"), "Daniel (Enhanced)")

    def test_missing_voice_never_uses_default(self):
        with self.assertRaisesRegex(speech.SpeechError, "requested voice.*not installed"):
            speech.choose_voice("Not a voice")
        self.assertEqual(self.mock_run.call_count, 1)

    def test_missing_daniel_explains_installation(self):
        self.mock_run.return_value.stdout = "Samantha en_US # Hello\n"
        with self.assertRaisesRegex(speech.SpeechError, "British Daniel.*System Settings"):
            speech.choose_voice()

    def test_non_mac_error_is_explicit_even_with_cached_inventory(self):
        speech.voice_inventory()
        with patch("jarvis.speech.platform.system", return_value="Linux"):
            with self.assertRaisesRegex(speech.SpeechError, "runs on your Mac"):
                speech.voice_inventory()

    def test_missing_say_is_actionable(self):
        self.mock_run.side_effect = FileNotFoundError("say missing")
        with self.assertRaisesRegex(speech.SpeechError, "say is unavailable"):
            speech.voice_inventory()

    def test_inventory_timeout_has_context(self):
        self.mock_run.side_effect = subprocess.TimeoutExpired("say", 5)
        with self.assertRaisesRegex(speech.SpeechError, "say timed out"):
            speech.voice_inventory()

    def test_speech_text_uses_stdin_and_no_shell(self):
        text = "-v Samantha; $(touch nope) `echo no`"
        speech.speak_text(text)
        self.mock_run.assert_called_with(
            ["/usr/bin/say", "-v", "Daniel (Enhanced)", "-r", "165"],
            input=text, text=True, capture_output=True, check=True, timeout=60,
        )

    def test_invalid_parameters_fail_before_native_commands(self):
        for text in ("", " ", None, "a" * 1601, "a\x00b"):
            with self.subTest(text=str(text)[:20]), self.assertRaises(ValueError):
                speech.speak_text(text)
        for rate in (99, 301, True, 165.5, "165"):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                speech.speak_text("hello", rate=rate)
        for voice in ("", " ", None, "v" * 101):
            with self.subTest(voice=voice), self.assertRaises(ValueError):
                speech.speak_text("hello", voice=voice)
        self.mock_run.assert_not_called()

    def test_synthesis_converts_to_pcm_wav_and_erases_temporary_files(self):
        paths = []
        expected = wav_bytes()

        def native(command, **kwargs):
            if command == ["/usr/bin/say", "-v", "?"]:
                return subprocess.CompletedProcess(command, 0, VOICES, "")
            if command[0] == "/usr/bin/say":
                output = Path(command[-1])
                paths.append(output)
                output.write_bytes(b"AIFF stub")
                self.assertEqual(kwargs["input"], "Hello, sir.")
                self.assertEqual(kwargs["timeout"], 60)
            else:
                self.assertEqual(command[:5], ["/usr/bin/afconvert", "-f", "WAVE", "-d", "LEI16@22050"])
                output = Path(command[-1])
                paths.append(output)
                output.write_bytes(expected)
                self.assertEqual(kwargs["timeout"], 10)
            return subprocess.CompletedProcess(command, 0, "", "")

        self.mock_run.side_effect = native
        self.assertEqual(speech.synthesize("Hello, sir."), expected)
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(not path.exists() and not path.parent.exists() for path in paths))

    def test_synthesis_failure_still_erases_intermediate_audio(self):
        paths = []

        def native(command, **kwargs):
            if command == ["/usr/bin/say", "-v", "?"]:
                return subprocess.CompletedProcess(command, 0, VOICES, "")
            if command[0] == "/usr/bin/say":
                output = Path(command[-1])
                paths.append(output)
                output.write_bytes(b"AIFF stub")
                return subprocess.CompletedProcess(command, 0, "", "")
            raise subprocess.TimeoutExpired(command, 10)

        self.mock_run.side_effect = native
        with self.assertRaisesRegex(speech.SpeechError, "afconvert timed out"):
            speech.synthesize("hello")
        self.assertTrue(all(not path.parent.exists() for path in paths))

    def test_invalid_or_incomplete_wav_is_rejected(self):
        expected = wav_bytes()
        for output_bytes in (b"not a WAV", expected[:-4]):
            with self.subTest(output=output_bytes[:10]):
                def native(command, **kwargs):
                    if command == ["/usr/bin/say", "-v", "?"]:
                        return subprocess.CompletedProcess(command, 0, VOICES, "")
                    Path(command[-1]).write_bytes(b"AIFF stub" if command[0].endswith("say") else output_bytes)
                    return subprocess.CompletedProcess(command, 0, "", "")

                self.mock_run.side_effect = native
                with self.assertRaisesRegex(speech.SpeechError, "invalid WAV|incomplete audio"):
                    speech.synthesize("hello")

    def test_oversized_intermediate_audio_is_rejected_before_conversion(self):
        def native(command, **kwargs):
            if command == ["/usr/bin/say", "-v", "?"]:
                return subprocess.CompletedProcess(command, 0, VOICES, "")
            with Path(command[-1]).open("wb") as output:
                output.truncate(speech.MAX_AUDIO_BYTES + 1)
            return subprocess.CompletedProcess(command, 0, "", "")

        self.mock_run.side_effect = native
        with self.assertRaisesRegex(speech.SpeechError, "oversized audio"):
            speech.synthesize("hello")
        self.assertEqual(self.mock_run.call_count, 2)

    def test_native_failure_has_command_and_bounded_detail(self):
        speech.voice_inventory()
        self.mock_run.side_effect = subprocess.CalledProcessError(
            1, "say", stderr="Requested voice failed " + "x" * 1000,
        )
        with self.assertRaisesRegex(speech.SpeechError, "say failed: Requested voice failed") as error:
            speech.speak_text("hello")
        self.assertLess(len(str(error.exception)), 400)


if __name__ == "__main__":
    unittest.main()
