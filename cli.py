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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a local Jarvis assistant powered by Ollama.")
    parser.add_argument("--model", default=os.getenv("JARVIS_MODEL", DEFAULT_MODEL))
    parser.add_argument("--ollama-url", default=os.getenv("JARVIS_OLLAMA_URL", DEFAULT_OLLAMA_URL))
    parser.add_argument("--workspace", default=os.getenv("JARVIS_WORKSPACE", os.getcwd()))
    parser.add_argument("--speak", action="store_true", help="Speak answers using macOS say.")
    parser.add_argument("--no-tools", action="store_true", help="Disable app, shell, web, and file tools.")
    parser.add_argument("--once", help="Ask one question and exit.")
    return parser


def speak(text: str) -> None:
    try:
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
  :quit             Exit

Try:
  Open Visual Studio Code.
  List the files in my workspace.
  Write a file called notes/plan.txt with a 3-step launch plan.
  Search the web for local-first personal assistant projects.
  Run pwd.
""".strip()
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = Path(args.workspace).expanduser().resolve()
    client = OllamaClient(base_url=args.ollama_url, model=args.model)
    toolkit = ToolKit(workspace=workspace)
    assistant = JarvisAssistant(client, toolkit, tools_enabled=not args.no_tools)
    speak_answers = args.speak

    if args.once:
        return ask_once(assistant, args.once, speak_answers)

    print(f"JARVIS online. Model: {args.model}")
    print(f"Workspace: {workspace}")
    print("Type :help for commands or :quit to exit.")

    while True:
        try:
            user_text = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nJARVIS: Shutting down.")
            return 0

        if not user_text:
            continue

        if user_text in {":quit", ":exit", "quit", "exit"}:
            print("JARVIS: Goodbye.")
            return 0
        if user_text == ":help":
            print_help()
            continue
        if user_text == ":model":
            print(f"JARVIS: {args.model}")
            continue
        if user_text == ":workspace":
            print(f"JARVIS: {workspace}")
            continue
        if user_text.startswith(":speak"):
            speak_answers = user_text.lower().endswith(" on")
            state = "on" if speak_answers else "off"
            print(f"JARVIS: Spoken answers are {state}.")
            continue

        ask_once(assistant, user_text, speak_answers)


def ask_once(assistant: JarvisAssistant, prompt: str, speak_answers: bool) -> int:
    try:
        answer = assistant.ask(prompt)
    except RuntimeError as exc:
        print(f"JARVIS: {exc}", file=sys.stderr)
        return 1

    print(f"\nJARVIS: {answer}")
    if speak_answers:
        speak(answer)
    return 0
