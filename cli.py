from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from .assistant import JarvisAssistant
from .ollama_client import OllamaClient
from .tools import ToolKit


DEFAULT_MODEL = "llama3.2:3b"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
# Prefer the British male voice "Daniel" when available; fall back gracefully.
DEFAULT_VOICE = os.getenv("JARVIS_VOICE", "Daniel")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a local Jarvis assistant powered by Ollama.")
    parser.add_argument("--model", default=os.getenv("JARVIS_MODEL", DEFAULT_MODEL))
    parser.add_argument("--ollama-url", default=os.getenv("JARVIS_OLLAMA_URL", DEFAULT_OLLAMA_URL))
    parser.add_argument("--workspace", default=os.getenv("JARVIS_WORKSPACE", os.getcwd()))
    parser.add_argument("--speak", action="store_true", help="Speak answers using macOS say.")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help="macOS say voice (default: Daniel)")
    parser.add_argument("--no-tools", action="store_true", help="Disable app, shell, web, and file tools.")
    parser.add_argument("--once", help="Ask one question and exit.")
    return parser


def speak(text: str, voice: str = DEFAULT_VOICE) -> None:
    try:
        # Try preferred voice first, fall back to system default if missing.
        cmd = ["say", "-v", voice, text[:4000]]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            subprocess.run(["say", text[:4000]], check=False)
    except FileNotFoundError:
        pass


def print_help() -> None:
    print(
        """
Commands:
  :help             Show this help
  :speak on|off     Toggle spoken answers
  :model            Show the active Ollama model
  :workspace        Show the safe file workspace
  :status           Quick system status (via tools)
  :quit             Exit

Try saying:
  "Good evening, JARVIS."
  "What's the system status?"
  "Open Visual Studio Code."
  "Set the volume to 40."
  "Notify me that the build is finished."
  "Search the web for local-first personal assistants."
  "List the files in my workspace."
  "Write a file called notes/plan.txt with a 3-step launch plan."
""".strip()
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = Path(args.workspace).expanduser().resolve()
    client = OllamaClient(base_url=args.ollama_url, model=args.model)
    toolkit = ToolKit(workspace=workspace)
    assistant = JarvisAssistant(client, toolkit, tools_enabled=not args.no_tools)
    speak_answers = args.speak
    voice = args.voice

    if args.once:
        return ask_once(assistant, args.once, speak_answers, voice)

    # Movie-style startup
    print("JARVIS online.")
    print(f"Model: {args.model}  |  Workspace: {workspace}")
    print("Type :help for commands or :quit to exit.")
    greeting = (
        "Good evening, Sir. All systems are nominal. "
        "How may I assist you?"
    )
    print(f"\nJARVIS: {greeting}")
    if speak_answers:
        speak(greeting, voice)

    while True:
        try:
            user_text = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nJARVIS: Shutting down. Good night, Sir.")
            return 0

        if not user_text:
            continue

        if user_text in {":quit", ":exit", "quit", "exit"}:
            print("JARVIS: Goodbye, Sir. Always a pleasure.")
            if speak_answers:
                speak("Goodbye, Sir. Always a pleasure.", voice)
            return 0
        if user_text == ":help":
            print_help()
            continue
        if user_text == ":model":
            print(f"JARVIS: Currently running {args.model}, Sir.")
            continue
        if user_text == ":workspace":
            print(f"JARVIS: Workspace is {workspace}, Sir.")
            continue
        if user_text == ":status":
            # Trigger a natural status request
            user_text = "Give me a quick system status report."
        if user_text.startswith(":speak"):
            speak_answers = user_text.lower().endswith(" on")
            state = "on" if speak_answers else "off"
            print(f"JARVIS: Spoken answers are now {state}, Sir.")
            continue

        ask_once(assistant, user_text, speak_answers, voice)


def ask_once(assistant: JarvisAssistant, prompt: str, speak_answers: bool, voice: str = DEFAULT_VOICE) -> int:
    try:
        answer = assistant.ask(prompt)
    except RuntimeError as exc:
        print(f"JARVIS: {exc}", file=sys.stderr)
        return 1

    print(f"\nJARVIS: {answer}")
    if speak_answers:
        speak(answer, voice)
    return 0
