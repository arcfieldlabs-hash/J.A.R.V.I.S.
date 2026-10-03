"""Optional microphone input with local transcription and a wake word.

Importing this module requires only the standard library. Microphone and Whisper
dependencies are loaded when voice input is explicitly enabled.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from importlib import import_module


_INSTALL_HELP = (
    "Install voice support with pip install 'SpeechRecognition[audio,whisper-local]'. "
    "On macOS, install PortAudio first with brew install portaudio."
)


def _positive_seconds(value: float, name: str) -> float:
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number.")
    return value


class WakeWordGate:
    """Accept one command following a wake word, with a bounded follow-up window."""

    def __init__(
        self,
        wake_word: str = "jarvis",
        awake_seconds: float = 15.0,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        wake_word = wake_word.strip()
        if not wake_word or not re.search(r"\w", wake_word):
            raise ValueError("wake_word must contain at least one letter or number.")
        self.wake_word = wake_word
        self.awake_seconds = _positive_seconds(awake_seconds, "awake_seconds")
        words = [re.escape(word) for word in wake_word.split()]
        self._pattern = re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", re.I)
        self._clock = clock if clock is not None else time.monotonic
        self._awake_until = 0.0

    def process(self, transcript: str) -> str | None:
        """Return a command, or None when activation is absent or incomplete.

        A standalone wake word opens a 15-second window by default. The next
        nonempty utterance consumes that window; subsequent commands need the
        wake word again. A wake word and command in one utterance work directly.
        """
        transcript = transcript.strip()
        if not transcript:
            return None
        now = self._clock()
        match = self._pattern.search(transcript)
        if match is not None:
            command = transcript[match.end():].lstrip(" \t\r\n,:;.!?–—-")
            if command:
                self.reset()
                return command
            self._awake_until = now + self.awake_seconds
            return None
        if now < self._awake_until:
            self.reset()
            return transcript
        self.reset()
        return None

    def reset(self) -> None:
        self._awake_until = 0.0


class VoiceListener:
    """Listen for bounded utterances and transcribe them with local Whisper.

    Audio stays local. Whisper may download its model weights the first time it
    runs. Microphone streams are closed before transcription and between calls,
    so a caller can speak a reply synchronously without recording that reply.
    """

    def __init__(
        self,
        model: str = "base",
        wake_word: str = "jarvis",
        language: str = "en",
        listen_timeout: float = 3.0,
        phrase_time_limit: float = 10.0,
    ) -> None:
        if not model.strip():
            raise ValueError("model must not be empty.")
        if not language.strip():
            raise ValueError("language must not be empty.")
        self.model = model
        self.language = language
        self.listen_timeout = _positive_seconds(listen_timeout, "listen_timeout")
        self.phrase_time_limit = _positive_seconds(phrase_time_limit, "phrase_time_limit")
        self._gate = WakeWordGate(wake_word)

        try:
            self._sr = import_module("speech_recognition")
            import_module("whisper")
        except Exception as exc:
            raise RuntimeError(f"Local voice dependencies could not be loaded: {exc}. {_INSTALL_HELP}") from exc

        try:
            self._recognizer = self._sr.Recognizer()
            self._microphone = self._sr.Microphone()
            with self._microphone as source:
                self._recognizer.adjust_for_ambient_noise(source, duration=0.5)
        except Exception as exc:
            raise RuntimeError(
                f"Microphone setup failed: {exc}. Check the input device and microphone permission. "
                + _INSTALL_HELP
            ) from exc

    def listen(self) -> str | None:
        """Return an activated command, or None for silence or unrecognized audio."""
        try:
            with self._microphone as source:
                audio = self._recognizer.listen(
                    source,
                    timeout=self.listen_timeout,
                    phrase_time_limit=self.phrase_time_limit,
                )
        except self._sr.WaitTimeoutError:
            return None
        except Exception as exc:
            raise RuntimeError(f"Microphone listening failed: {exc}") from exc

        try:
            transcript = self._recognizer.recognize_whisper(
                audio, model=self.model, language=self.language
            )
        except self._sr.UnknownValueError:
            return None
        except Exception as exc:
            raise RuntimeError(f"Local voice transcription failed: {exc}") from exc
        if not isinstance(transcript, str):
            raise RuntimeError("Local voice transcription returned an invalid result.")
        return self._gate.process(transcript)

    def suspend_after_speech(self, cooldown: float = 0.5) -> None:
        """Clear activation and let speech echo fade with the microphone closed.

        Call after synchronous speech output and before the next listen(). A
        fresh microphone stream on the next call discards the previous stream's
        buffered audio. This listener must not be run concurrently with speech.
        """
        cooldown = float(cooldown)
        if not math.isfinite(cooldown) or not 0 <= cooldown <= 2:
            raise ValueError("cooldown must be between 0 and 2 seconds.")
        self._gate.reset()
        if cooldown:
            time.sleep(cooldown)
