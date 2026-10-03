"""Bounded, local media input and revocable device permissions.

Browser capture happens only after the user enables a device. This module never
opens a camera, microphone, or display, and never saves recordings to disk.
"""

from __future__ import annotations

import array
import base64
import binascii
import struct
import sys
import threading
from importlib import import_module


MAX_MEDIA_BYTES = 2 * 1024 * 1024
MAX_AUDIO_SECONDS = 20
MAX_IMAGE_DIMENSION = 2048
MAX_IMAGE_PIXELS = 4_000_000
_DEVICE_SOURCES = ("camera", "microphone", "screen")
_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


class MediaError(ValueError):
    """A disabled device, invalid recording, or unavailable local transcriber."""


class DevicePermissions:
    """Permission generations let an off switch revoke already queued results."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states = {source: {"enabled": False, "generation": 0}
                        for source in _DEVICE_SOURCES}

    @staticmethod
    def _check_source(source: str) -> None:
        if not isinstance(source, str) or source not in _DEVICE_SOURCES:
            raise MediaError("Device source must be camera, microphone, or screen.")

    def _snapshot_locked(self) -> dict:
        return {source: dict(state) for source, state in self._states.items()}

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot_locked()

    def set_enabled(self, source: str, enabled: bool) -> dict:
        self._check_source(source)
        if type(enabled) is not bool:
            raise MediaError("Device enabled must be true or false.")
        with self._lock:
            state = self._states[source]
            state["generation"] += 1
            state["enabled"] = enabled
            return self._snapshot_locked()

    def ticket(self, source: str) -> int:
        self._check_source(source)
        with self._lock:
            state = self._states[source]
            if not state["enabled"]:
                raise MediaError(f"{source.capitalize()} access is off. Enable it first.")
            return state["generation"]

    def is_current(self, source: str, generation: int) -> bool:
        self._check_source(source)
        with self._lock:
            state = self._states[source]
            return state["enabled"] and state["generation"] == generation


def _decode_media(encoded: str) -> bytes:
    if not isinstance(encoded, str) or not encoded:
        raise MediaError("Media must contain a base64 recording.")
    if len(encoded) > ((MAX_MEDIA_BYTES + 2) // 3) * 4:
        raise MediaError("Media must be no larger than 2 MiB.")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MediaError("Media contains invalid base64.") from exc
    if not raw:
        raise MediaError("Media recording is empty.")
    if len(raw) > MAX_MEDIA_BYTES:
        raise MediaError("Media must be no larger than 2 MiB.")
    return raw


def validate_image(encoded: str) -> str:
    """Validate JPEG structure and bounds, returning canonical raw base64.

    Header and marker checks deliberately use the standard library; the local
    vision model remains responsible for decoding the compressed image itself.
    """
    raw = _decode_media(encoded)
    if not raw.startswith(b"\xff\xd8"):
        raise MediaError("Image must be a JPEG snapshot.")
    offset = 2
    frame_seen = False
    scan_seen = False
    scan_data_seen = False
    in_scan = False
    components = 0
    while offset < len(raw):
        if in_scan:
            marker_start = raw.find(b"\xff", offset)
            if marker_start < 0:
                raise MediaError("JPEG image is truncated.")
            if marker_start > offset:
                scan_data_seen = True
            offset = marker_start
        if raw[offset] != 0xFF:
            raise MediaError("JPEG image contains an invalid marker.")
        while offset < len(raw) and raw[offset] == 0xFF:
            offset += 1
        if offset >= len(raw):
            raise MediaError("JPEG image is truncated.")
        marker = raw[offset]
        offset += 1
        if marker == 0x00:
            if not in_scan:
                raise MediaError("JPEG image contains an invalid marker.")
            scan_data_seen = True
            continue
        if 0xD0 <= marker <= 0xD7:
            if not in_scan:
                raise MediaError("JPEG image contains an invalid restart marker.")
            continue
        if marker == 0xD9:
            if not frame_seen or not scan_seen or not scan_data_seen or offset != len(raw):
                raise MediaError("JPEG image is incomplete or has trailing data.")
            return base64.b64encode(raw).decode("ascii")
        if marker in (0xD8, 0x01):
            raise MediaError("JPEG image contains an unsupported marker.")
        in_scan = False
        if offset + 2 > len(raw):
            raise MediaError("JPEG image is truncated.")
        length = int.from_bytes(raw[offset:offset + 2], "big")
        if length < 2 or offset + length > len(raw):
            raise MediaError("JPEG image contains a truncated segment.")
        payload = raw[offset + 2:offset + length]
        offset += length
        if marker in _SOF_MARKERS:
            if frame_seen or len(payload) < 6:
                raise MediaError("JPEG image contains an invalid frame header.")
            precision, height, width, components = struct.unpack(">BHHB", payload[:6])
            if precision != 8 or components not in (1, 3, 4) or len(payload) != 6 + 3 * components:
                raise MediaError("JPEG image must use an 8-bit frame.")
            if (not width or not height or width > MAX_IMAGE_DIMENSION
                    or height > MAX_IMAGE_DIMENSION or width * height > MAX_IMAGE_PIXELS):
                raise MediaError("JPEG dimensions must be at most 2048 by 2048 and 4 million pixels.")
            frame_seen = True
        elif marker == 0xDA:
            if not frame_seen or not payload:
                raise MediaError("JPEG image is missing its frame header.")
            scan_components = payload[0]
            if (not 1 <= scan_components <= components
                    or len(payload) != 4 + 2 * scan_components):
                raise MediaError("JPEG image contains an invalid scan header.")
            scan_seen = True
            in_scan = True
    raise MediaError("JPEG image is truncated.")


def validate_audio(encoded: str) -> tuple[bytes, int]:
    """Read uncompressed little-endian WAV and return mono signed PCM16."""
    raw = _decode_media(encoded)
    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise MediaError("Audio must be an uncompressed PCM WAV recording.")
    if int.from_bytes(raw[4:8], "little") != len(raw) - 8:
        raise MediaError("WAV recording is truncated or has an invalid length.")
    offset = 12
    format_data = None
    frames = None
    while offset < len(raw):
        if offset + 8 > len(raw):
            raise MediaError("WAV recording contains a truncated chunk.")
        chunk_name = raw[offset:offset + 4]
        length = int.from_bytes(raw[offset + 4:offset + 8], "little")
        offset += 8
        if offset + length + (length % 2) > len(raw):
            raise MediaError("WAV recording contains truncated audio data.")
        data = raw[offset:offset + length]
        offset += length + (length % 2)
        if chunk_name == b"fmt ":
            if format_data is not None or length < 16:
                raise MediaError("WAV recording contains an invalid format chunk.")
            format_data = struct.unpack("<HHIIHH", data[:16])
        elif chunk_name == b"data":
            if frames is not None:
                raise MediaError("WAV recording must contain a single audio data chunk.")
            frames = data
    if format_data is None or frames is None:
        raise MediaError("WAV recording is missing its format or audio data.")
    codec, channels, rate, byte_rate, block_align, bits = format_data
    if (codec != 1 or channels not in (1, 2) or bits != 16
            or not 8000 <= rate <= 48000
            or block_align != channels * 2 or byte_rate != rate * block_align):
        raise MediaError("Use a mono or stereo PCM16 WAV at 8000 to 48000 Hz.")
    if not frames or len(frames) % block_align:
        raise MediaError("WAV recording contains empty or incomplete audio frames.")
    if len(frames) // block_align > rate * MAX_AUDIO_SECONDS:
        raise MediaError("Audio recording must be at most 20 seconds.")
    if channels == 2:
        mono = array.array("h", ((left + right) // 2
                                 for left, right in struct.iter_unpack("<hh", frames)))
        if sys.byteorder != "little":
            mono.byteswap()
        frames = mono.tobytes()
    return frames, rate


def transcribe_audio(encoded: str, *, model: str = "base") -> str:
    """Transcribe one explicit recording with local Whisper, without cloud APIs."""
    frames, rate = validate_audio(encoded)
    if not isinstance(model, str) or not model.strip() or len(model) > 120:
        raise MediaError("Local transcription model must be a nonempty model name.")
    try:
        sr = import_module("speech_recognition")
        import_module("whisper")
    except Exception as exc:
        raise MediaError(
            "Local transcription needs voice support: pip install -e '.[voice]'. "
            "On macOS, install PortAudio first with brew install portaudio."
        ) from exc
    try:
        audio = sr.AudioData(frames, rate, 2)
        transcript = sr.Recognizer().recognize_whisper(audio, model=model, language="en")
    except Exception as exc:
        raise MediaError(f"Local voice transcription failed: {exc}") from exc
    if not isinstance(transcript, str):
        raise MediaError("Local voice transcription returned an invalid result.")
    transcript = transcript.strip()
    if not transcript:
        raise MediaError("No speech was recognized. Try a clearer recording.")
    if len(transcript) > 8000:
        raise MediaError("Transcript is too long; record a shorter command.")
    return transcript
