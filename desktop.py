"""Extra desktop automation helpers for JARVIS (macOS).

All actions go through AppleScript or system utilities.
Mouse/keyboard simulation is deliberately limited and requires confirmation.
"""

from __future__ import annotations

import subprocess
from typing import Any


def run_applescript(script: str, timeout: int = 15) -> tuple[bool, str]:
    completed = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    out = (completed.stdout or completed.stderr or "").strip()
    return completed.returncode == 0, out


def set_window_bounds(app_name: str, x: int, y: int, w: int, h: int) -> tuple[bool, str]:
    script = f'''
tell application "{app_name}"
    if (count of windows) > 0 then
        set bounds of front window to {{{x}, {y}, {x + w}, {y + h}}}
        return "OK"
    else
        return "No windows"
    end if
end tell
'''
    return run_applescript(script)


def keystroke(text: str, modifiers: list[str] | None = None) -> tuple[bool, str]:
    """Type text into the frontmost app. modifiers: command, option, control, shift."""
    mods = modifiers or []
    mod_clause = ""
    if mods:
        using = ", ".join(f"{m} down" for m in mods)
        mod_clause = f" using {{{using}}}"
    # Escape for AppleScript string
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
tell application "System Events"
    keystroke "{escaped}"{mod_clause}
end tell
'''
    return run_applescript(script)


def key_code(code: int, modifiers: list[str] | None = None) -> tuple[bool, str]:
    mods = modifiers or []
    mod_clause = ""
    if mods:
        using = ", ".join(f"{m} down" for m in mods)
        mod_clause = f" using {{{using}}}"
    script = f'''
tell application "System Events"
    key code {code}{mod_clause}
end tell
'''
    return run_applescript(script)


def click_at(x: int, y: int) -> tuple[bool, str]:
    """Click at screen coordinates. Prefer cliclick if present; else pure AppleScript is limited."""
    # Try cliclick first (brew install cliclick)
    try:
        completed = subprocess.run(
            ["cliclick", f"c:{x},{y}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if completed.returncode == 0:
            return True, f"Clicked at ({x},{y}) via cliclick"
    except FileNotFoundError:
        pass

    # Fallback: very limited – move + click via System Events is unreliable without accessibility
    script = f'''
tell application "System Events"
    click at {{{x}, {y}}}
end tell
'''
    ok, out = run_applescript(script)
    if ok:
        return True, f"Clicked at ({x},{y})"
    return False, out or "Click failed. Install cliclick (`brew install cliclick`) and grant Accessibility."


def dispatch(action: str, args: dict[str, Any]) -> tuple[bool, str]:
    action = action.strip().lower()
    if action == "set_window":
        app = str(args.get("app", "")).strip()
        try:
            x, y, w, h = int(args["x"]), int(args["y"]), int(args["w"]), int(args["h"])
        except (KeyError, TypeError, ValueError):
            return False, "set_window requires app, x, y, w, h"
        return set_window_bounds(app, x, y, w, h)
    if action == "type":
        text = str(args.get("text", ""))
        if not text:
            return False, "Missing text"
        mods = args.get("modifiers") or []
        if isinstance(mods, str):
            mods = [mods]
        return keystroke(text, list(mods))
    if action == "key":
        try:
            code = int(args.get("code", 0))
        except (TypeError, ValueError):
            return False, "Missing or invalid key code"
        mods = args.get("modifiers") or []
        if isinstance(mods, str):
            mods = [mods]
        return key_code(code, list(mods))
    if action == "click":
        try:
            x, y = int(args["x"]), int(args["y"])
        except (KeyError, TypeError, ValueError):
            return False, "click requires x, y"
        return click_at(x, y)
    return False, f"Unknown desktop action: {action}"
