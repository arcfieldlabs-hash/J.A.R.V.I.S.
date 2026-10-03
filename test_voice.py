import types
import unittest
from unittest.mock import MagicMock, patch

from jarvis.voice import VoiceListener, WakeWordGate


class WakeWordGateTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.gate = WakeWordGate(clock=lambda: self.now)

    def test_wake_word_and_command_in_one_utterance(self):
        self.assertEqual(self.gate.process("Hey, JARVIS, open my calendar."), "open my calendar.")

    def test_unactivated_speech_is_ignored(self):
        self.assertIsNone(self.gate.process("open my calendar"))

    def test_wake_word_must_be_a_whole_word(self):
        for transcript in ("jarvises open calendar", "myjarvis open calendar", "jarvis_2 open calendar"):
            with self.subTest(transcript=transcript):
                self.assertIsNone(self.gate.process(transcript))

    def test_standalone_wake_word_accepts_one_follow_up(self):
        self.assertIsNone(self.gate.process("Hey Jarvis!"))
        self.now += 14.9
        self.assertEqual(self.gate.process("what time is it?"), "what time is it?")
        self.assertIsNone(self.gate.process("and tomorrow?"))

    def test_awake_window_expires(self):
        self.gate.process("Jarvis")
        self.now += 15
        self.assertIsNone(self.gate.process("open calendar"))

    def test_empty_transcription_keeps_pending_activation(self):
        self.gate.process("Jarvis")
        self.assertIsNone(self.gate.process("   "))
        self.assertEqual(self.gate.process("open calendar"), "open calendar")

    def test_command_in_same_utterance_consumes_pending_activation(self):
        self.gate.process("Jarvis")
        self.assertEqual(self.gate.process("Jarvis open calendar"), "open calendar")
        self.assertIsNone(self.gate.process("open mail"))

    def test_custom_wake_word(self):
        gate = WakeWordGate("hey computer", clock=lambda: self.now)
        self.assertEqual(gate.process("Hey   Computer: tell me a joke"), "tell me a joke")

    def test_reset_clears_pending_activation(self):
        self.gate.process("Jarvis")
        self.gate.reset()
        self.assertIsNone(self.gate.process("open calendar"))

    def test_invalid_gate_settings(self):
        for wake_word in ("", "   ", "!!!"):
            with self.subTest(wake_word=wake_word), self.assertRaises(ValueError):
                WakeWordGate(wake_word)
        for seconds in (0, -1, float("inf"), float("nan")):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                WakeWordGate(awake_seconds=seconds)


class VoiceListenerTests(unittest.TestCase):
    def setUp(self):
        self.sr = types.SimpleNamespace(
            Recognizer=MagicMock(),
            Microphone=MagicMock(),
            WaitTimeoutError=type("WaitTimeoutError", (Exception,), {}),
            UnknownValueError=type("UnknownValueError", (Exception,), {}),
        )
        self.recognizer = self.sr.Recognizer.return_value
        self.microphone = self.sr.Microphone.return_value
        self.source = self.microphone.__enter__.return_value
        self.audio = object()
        self.recognizer.listen.return_value = self.audio
        self.recognizer.recognize_whisper.return_value = "Jarvis open calendar"
        self.imports = patch(
            "jarvis.voice.import_module",
            side_effect=lambda name: self.sr if name == "speech_recognition" else object(),
        )
        self.imports.start()
        self.addCleanup(self.imports.stop)

    def test_parameters_reach_local_recognition_and_bounded_microphone(self):
        listener = VoiceListener(
            model="tiny", wake_word="computer", language="fr",
            listen_timeout=2, phrase_time_limit=6,
        )
        self.recognizer.recognize_whisper.return_value = "Computer ouvre le calendrier"
        self.assertEqual(listener.listen(), "ouvre le calendrier")
        self.recognizer.listen.assert_called_once_with(self.source, timeout=2.0, phrase_time_limit=6.0)
        self.recognizer.recognize_whisper.assert_called_once_with(self.audio, model="tiny", language="fr")
        self.recognizer.adjust_for_ambient_noise.assert_called_once_with(self.source, duration=0.5)

    def test_microphone_closes_before_transcription(self):
        listener = VoiceListener()
        self.microphone.reset_mock()

        def transcribe(*args, **kwargs):
            self.microphone.__exit__.assert_called_once()
            return "Jarvis hello"

        self.recognizer.recognize_whisper.side_effect = transcribe
        self.assertEqual(listener.listen(), "hello")

    def test_silence_returns_none_without_transcription(self):
        listener = VoiceListener()
        self.recognizer.listen.side_effect = self.sr.WaitTimeoutError()
        self.assertIsNone(listener.listen())
        self.recognizer.recognize_whisper.assert_not_called()

    def test_unrecognizable_audio_returns_none(self):
        listener = VoiceListener()
        self.recognizer.recognize_whisper.side_effect = self.sr.UnknownValueError()
        self.assertIsNone(listener.listen())

    def test_no_wake_word_returns_none(self):
        listener = VoiceListener()
        self.recognizer.recognize_whisper.return_value = "background conversation"
        self.assertIsNone(listener.listen())

    def test_missing_dependencies_explain_installation(self):
        with patch("jarvis.voice.import_module", side_effect=ImportError("missing whisper")):
            with self.assertRaisesRegex(RuntimeError, r"SpeechRecognition\[audio,whisper-local\]"):
                VoiceListener()

    def test_unavailable_microphone_has_actionable_error(self):
        self.sr.Microphone.side_effect = OSError("No input device")
        with self.assertRaisesRegex(RuntimeError, "Microphone setup failed.*No input device"):
            VoiceListener()

    def test_microphone_error_has_context(self):
        listener = VoiceListener()
        self.recognizer.listen.side_effect = OSError("device disconnected")
        with self.assertRaisesRegex(RuntimeError, "Microphone listening failed.*device disconnected"):
            listener.listen()

    def test_transcription_error_has_context(self):
        listener = VoiceListener()
        self.recognizer.recognize_whisper.side_effect = RuntimeError("model not available")
        with self.assertRaisesRegex(RuntimeError, "Local voice transcription failed.*model not available"):
            listener.listen()

    def test_invalid_transcription_result_has_context(self):
        listener = VoiceListener()
        self.recognizer.recognize_whisper.return_value = None
        with self.assertRaisesRegex(RuntimeError, "invalid result"):
            listener.listen()

    def test_after_speech_cooldown_clears_wake_window_with_microphone_closed(self):
        listener = VoiceListener()
        listener._gate.process("Jarvis")
        self.microphone.reset_mock()
        with patch("jarvis.voice.time.sleep") as sleep:
            listener.suspend_after_speech()
        sleep.assert_called_once_with(0.5)
        self.microphone.__enter__.assert_not_called()
        self.assertIsNone(listener._gate.process("open calendar"))

    def test_cooldown_is_bounded(self):
        listener = VoiceListener()
        for seconds in (-1, 2.1, float("inf"), float("nan")):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                listener.suspend_after_speech(seconds)

    def test_invalid_listener_settings_fail_before_loading_dependencies(self):
        for params in (
            {"model": ""}, {"language": ""}, {"listen_timeout": 0},
            {"phrase_time_limit": -1}, {"listen_timeout": float("inf")},
        ):
            with self.subTest(params=params), patch("jarvis.voice.import_module") as imports:
                with self.assertRaises(ValueError):
                    VoiceListener(**params)
                imports.assert_not_called()


if __name__ == "__main__":
    unittest.main()
