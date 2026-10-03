"""Local macOS speech with an explicitly selected British voice.

Daniel is a built-in British male voice. Enhanced and Premium variants are
preferred when installed; this is an approximation of the film's delivery, not
a recording or a clone of its actor. No speech text or audio leaves the Mac.
"""

from __future__ import annotations

import io
import platform
import re
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path


MAX_TEXT_CHARACTERS = 1600
MAX_AUDIO_BYTES = 8 * 1024 * 1024
_VOICE_CACHE_SECONDS = 60.0
_VOICE_PATTERN = re.compile(r"^(.+?)\s+([a-z]{2,3}_[A-Z]{2})\s+#")
_inventory_lock = threading.Lock()
_inventory_cache: tuple[float, list[dict[str, str]]] | None = None
_INSTALL_HELP = (
    "Install Daniel (Enhanced) or Daniel (Premium) in System Settings > "
    "Accessibility > Read & Speak (called Spoken Content on older macOS) > "
    "System Voice > Manage Voices > English (United Kingdom). "
    "After installing, allow up to one minute for the voice list to refresh."
)


class SpeechError(RuntimeError):
    """The local speech service or requested voice is unavailable."""


def _require_macos() -> None:
    if platform.system() != "Darwin":
        raise SpeechError("Native British speech is available when Jarvis runs on your Mac.")


def _run(command: list[str], *, timeout: float, text: str | None = None) -> subprocess.CompletedProcess:
    """Invoke only fixed macOS executables, with speech supplied on stdin."""
    try:
        return subprocess.run(
            command, input=text, text=True, capture_output=True,
            check=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise SpeechError(f"macOS speech command {Path(command[0]).name} is unavailable.") from exc
    except subprocess.TimeoutExpired as exc:
        raise SpeechError(
            f"macOS speech command {Path(command[0]).name} timed out. Try a shorter reply."
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()[:300]
        if not detail:
            detail = f"exit status {exc.returncode}"
        raise SpeechError(f"macOS speech command {Path(command[0]).name} failed: {detail}") from exc
    except OSError as exc:
        raise SpeechError(f"macOS speech command {Path(command[0]).name} failed: {exc}") from exc


def voice_inventory() -> list[dict[str, str]]:
    """Return installed voice names and languages, cached for one minute.

    Returned dictionaries are copies so callers cannot alter the shared cache.
    Downloading a voice is always performed by the user in System Settings.
    """
    global _inventory_cache
    _require_macos()
    with _inventory_lock:
        now = time.monotonic()
        if _inventory_cache is not None and now - _inventory_cache[0] < _VOICE_CACHE_SECONDS:
            return [dict(voice) for voice in _inventory_cache[1]]
        result = _run(["/usr/bin/say", "-v", "?"], timeout=5)
        voices = []
        seen = set()
        for line in result.stdout.splitlines():
            match = _VOICE_PATTERN.match(line)
            if match is None:
                continue
            name, language = match.groups()
            name = name.strip()
            if name not in seen:
                voices.append({"name": name, "language": language})
                seen.add(name)
        _inventory_cache = (time.monotonic(), voices)
        return [dict(voice) for voice in voices]


def choose_voice(preferred: str = "Daniel") -> str:
    """Resolve an installed voice without silently using the system default.

    The default Daniel request selects its highest-quality installed British
    variant. A different requested voice must match an installed name exactly.
    """
    if not isinstance(preferred, str) or not preferred.strip() or len(preferred) > 100:
        raise ValueError("voice must be a nonempty installed voice name of at most 100 characters.")
    preferred = preferred.strip()
    voices = voice_inventory()
    if preferred.casefold() == "daniel":
        daniels = [
            voice for voice in voices
            if voice["language"] == "en_GB"
            and re.fullmatch(r"Daniel(?:\s+\((?:Enhanced|Premium)\))?", voice["name"], re.I)
        ]
        if daniels:
            def quality(voice: dict[str, str]) -> int:
                name = voice["name"].casefold()
                return 2 if "premium" in name else 1 if "enhanced" in name else 0
            return max(daniels, key=quality)["name"]
        raise SpeechError("The British Daniel voice is not installed. " + _INSTALL_HELP)
    for voice in voices:
        if voice["name"] == preferred:
            return preferred
    raise SpeechError(
        f"The requested voice '{preferred}' is not installed. "
        "Choose a name from the installed voice list. " + _INSTALL_HELP
    )


def _speech_parameters(text: str, voice: str, rate: int) -> tuple[str, str, int]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Speech text must not be empty.")
    if len(text) > MAX_TEXT_CHARACTERS:
        raise ValueError(f"Speech text must be at most {MAX_TEXT_CHARACTERS} characters.")
    if "\x00" in text:
        raise ValueError("Speech text must not contain a null character.")
    if isinstance(rate, bool) or not isinstance(rate, int) or not 100 <= rate <= 300:
        raise ValueError("Speech rate must be an integer between 100 and 300 words per minute.")
    return text.strip(), choose_voice(voice), rate


def _bounded_file(path: Path) -> bytes:
    try:
        size = path.stat().st_size
        if not 0 < size <= MAX_AUDIO_BYTES:
            raise SpeechError("macOS speech produced empty or oversized audio.")
        data = path.read_bytes()
    except OSError as exc:
        raise SpeechError(f"Could not read generated speech audio: {exc}") from exc
    if not 0 < len(data) <= MAX_AUDIO_BYTES:
        raise SpeechError("macOS speech produced empty or oversized audio.")
    return data


def synthesize(text: str, voice: str = "Daniel", rate: int = 165) -> bytes:
    """Generate PCM WAV bytes for local browser playback, then erase temp files.

    Generation does not play audio on the Mac itself, so the browser can pause
    or stop playback immediately. Both native commands have bounded runtimes.
    """
    text, selected, rate = _speech_parameters(text, voice, rate)
    try:
        with tempfile.TemporaryDirectory(prefix="jarvis-speech-") as directory:
            aiff_path = Path(directory) / "speech.aiff"
            wav_path = Path(directory) / "speech.wav"
            _run(
                ["/usr/bin/say", "-v", selected, "-r", str(rate), "-o", str(aiff_path)],
                timeout=60, text=text,
            )
            # Check the intermediate output before launching the converter.
            if not 0 < aiff_path.stat().st_size <= MAX_AUDIO_BYTES:
                raise SpeechError("macOS speech produced empty or oversized audio.")
            _run(
                ["/usr/bin/afconvert", "-f", "WAVE", "-d", "LEI16@22050", str(aiff_path), str(wav_path)],
                timeout=10,
            )
            data = _bounded_file(wav_path)
            try:
                with wave.open(io.BytesIO(data), "rb") as audio:
                    if (
                        audio.getcomptype() != "NONE" or audio.getsampwidth() != 2
                        or audio.getframerate() != 22050 or audio.getnchannels() not in (1, 2)
                        or audio.getnframes() <= 0
                    ):
                        raise SpeechError("macOS speech produced an unsupported audio format.")
                    expected = audio.getnframes() * audio.getnchannels() * audio.getsampwidth()
                    if len(audio.readframes(audio.getnframes())) != expected:
                        raise SpeechError("macOS speech produced incomplete audio.")
            except (wave.Error, EOFError) as exc:
                raise SpeechError("macOS speech produced an invalid WAV file.") from exc
            return data
    except OSError as exc:
        raise SpeechError(f"Could not create temporary speech audio: {exc}") from exc


def speak_text(text: str, voice: str = "Daniel", rate: int = 165) -> None:
    """Speak synchronously on the Mac with an explicitly installed voice."""
    text, selected, rate = _speech_parameters(text, voice, rate)
    _run(["/usr/bin/say", "-v", selected, "-r", str(rate)], timeout=60, text=text)
