"""Optional macOS menu-bar companion for JARVIS.

Requires: pip install rumps

Usage:
  python3 -m jarvis.menubar
  # or from the package root after installing optional deps
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    try:
        import rumps
    except ImportError:
        print(
            "Menu-bar requires rumps.\n"
            "  pip install rumps\n"
            "Then run again: python3 -m jarvis.menubar",
            file=sys.stderr,
        )
        return 1

    # Resolve package root so we can call the CLI
    root = Path(__file__).resolve().parent

    class JarvisBar(rumps.App):
        def __init__(self) -> None:
            super().__init__("J.A.R.V.I.S.", quit_button=None)
            self.menu = [
                rumps.MenuItem("Ask JARVIS…", callback=self.ask),
                rumps.MenuItem("System status", callback=self.status),
                rumps.MenuItem("Unread mail", callback=self.mail),
                None,
                rumps.MenuItem("Open workspace", callback=self.open_workspace),
                rumps.MenuItem("Quit", callback=self.quit_app),
            ]

        def _run_once(self, prompt: str) -> str:
            try:
                completed = subprocess.run(
                    [sys.executable, "-m", "jarvis", "--once", prompt],
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                out = (completed.stdout or "").strip()
                err = (completed.stderr or "").strip()
                return out or err or "(no output)"
            except Exception as exc:
                return f"Error: {exc}"

        def ask(self, _):
            # Simple prompt via rumps
            response = rumps.Window(
                title="JARVIS",
                message="What shall I do, Sir?",
                default_text="",
                ok="Ask",
                cancel="Cancel",
                dimensions=(320, 24),
            ).run()
            if not response.clicked:
                return
            text = (response.text or "").strip()
            if not text:
                return
            answer = self._run_once(text)
            rumps.alert(title="JARVIS", message=answer[:1500] or "(empty)")

        def status(self, _):
            answer = self._run_once("Give me a quick system status report.")
            rumps.alert(title="System Status", message=answer[:1500])

        def mail(self, _):
            answer = self._run_once("Show my unread emails.")
            rumps.alert(title="Unread Mail", message=answer[:1500])

        def open_workspace(self, _):
            subprocess.run(["open", str(root)], check=False)

        def quit_app(self, _):
            rumps.quit_application()

    JarvisBar().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
