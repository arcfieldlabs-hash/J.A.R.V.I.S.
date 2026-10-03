"""Read-only setup checks; no optional models are loaded by diagnostics."""

from __future__ import annotations

from importlib.util import find_spec
import json
from pathlib import Path
import platform
import shutil
from urllib import request


def diagnose(*, ollama_url: str, model: str, workspace: Path, data_dir: Path) -> dict:
    report = {
        "platform": platform.system(),
        "workspace": str(workspace),
        "data_dir": str(data_dir),
        "model": model,
        "ollama": {"ready": False},
        "optional": {
            "voice_input": all(find_spec(name) is not None for name in ("speech_recognition", "whisper", "pyaudio")),
            "browser": find_spec("playwright") is not None,
            "detailed_telemetry": find_spec("psutil") is not None,
            "macos_speech": shutil.which("say") is not None,
            "google_cli": shutil.which("gog") is not None,
        },
    }
    try:
        with request.urlopen(ollama_url.rstrip("/") + "/api/tags", timeout=5) as response:
            payload = json.loads(response.read(1_000_000))
        models = [item.get("name", "") for item in payload.get("models", [])]
        report["ollama"] = {"ready": True, "models": models, "model_installed": model in models or model + ":latest" in models}
    except Exception as exc:
        report["ollama"]["error"] = f"{type(exc).__name__}: {exc}"
    return report
