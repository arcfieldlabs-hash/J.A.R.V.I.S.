from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from .assistant import JarvisAssistant
from .ollama_client import OllamaClient
from .tools import ToolKit


# Defaults tuned for Apple Silicon 16GB (M1/M2/M3 Pro class).
DEFAULT_MODEL = os.getenv("JARVIS_MODEL", "llama3.2:3b")
DEFAULT_OLLAMA_URL = os.getenv("JARVIS_OLLAMA_URL", "http://localhost:11434")
DEFAULT_VOICE = os.getenv("JARVIS_VOICE", "Daniel")

# Low-memory profile (good default on 16GB unified memory).
LOWMEM_NUM_CTX = 2048
LOWMEM_NUM_PREDICT = 256
LOWMEM_HISTORY = 10
LOWMEM_TOOL_ROUNDS = 3
LOWMEM_KEEP_ALIVE = "5m"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local JARVIS assistant (optimized for Apple Silicon 16GB)."
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    parser.add_argument("--workspace", default=os.getenv("JARVIS_WORKSPACE", os.getcwd()))
    parser.add_argument("--speak", action="store_true", help="Speak answers with macOS say.")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help="say voice (default: Daniel)")
    parser.add_argument("--no-tools", action="store_true", help="Disable tools.")
    parser.add_argument("--once", help="One-shot question then exit.")
    parser.add_argument("--menubar", action="store_true", help="Optional menu-bar app (needs rumps).")
    parser.add_argument(
        "--low-mem",
        action="store_true",
        default=True,
        help="Low-memory profile for 16GB Macs (default: on).",
    )
    parser.add_argument(
        "--full-mem",
        action="store_true",
        help="Disable low-memory limits (more context, longer replies).",
    )
    parser.add_argument(
        "--num-ctx",
        type=int,
        default=None,
        help="Override Ollama context size (e.g. 2048 or 4096).",
    )
    return parser


def speak(text: str, voice: str = DEFAULT_VOICE) -> None:
    try:
        cmd = ["say", "-v", voice, text[:2000]]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            subprocess.run(["say", text[:2000]], check=False)
    except FileNotFoundError:
        pass


def print_help() -> None:
    print(
        """
Commands:
  :help          Show this help
  :speak on|off  Toggle spoken answers
  :model         Show active model
  :workspace     Show workspace
  :status        Quick system status
  :quit          Exit

Tips for 16GB MacBook:
  Default is --low-mem (small context, shorter history).
  Use a 3B–8B model:  ollama pull llama3.2:3b
  Optional stronger small model:  ollama pull phi3:mini  or  gemma2:2b
  Quit other heavy apps while chatting for best latency.
""".strip()
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.menubar:
        from . import menubar as mb

        return mb.main()

    low_mem = args.low_mem and not args.full_mem

    num_ctx = args.num_ctx
    if num_ctx is None and low_mem:
        num_ctx = LOWMEM_NUM_CTX

    client = OllamaClient(
        base_url=args.ollama_url,
        model=args.model,
        temperature=0.25 if low_mem else 0.3,
        num_ctx=num_ctx,
        num_predict=LOWMEM_NUM_PREDICT if low_mem else None,
        keep_alive=LOWMEM_KEEP_ALIVE if low_mem else "10m",
    )

    workspace = Path(args.workspace).expanduser().resolve()
    toolkit = ToolKit(workspace=workspace)
    assistant = JarvisAssistant(
        client,
        toolkit,
        tools_enabled=not args.no_tools,
        max_tool_rounds=LOWMEM_TOOL_ROUNDS if low_mem else 4,
        history_limit=LOWMEM_HISTORY if low_mem else 20,
    )

    speak_answers = args.speak
    voice = args.voice

    if args.once:
        return ask_once(assistant, args.once, speak_answers, voice)

    mode = "low-mem" if low_mem else "full"
    print("JARVIS online.")
    print(f"Model: {args.model}  |  Mode: {mode}  |  Workspace: {workspace}")
    if low_mem:
        print("Low-memory profile active (16GB-friendly). Use --full-mem to raise limits.")
    print("Type :help for commands or :quit to exit.")

    greeting = "Good evening, Sir. Systems nominal. How may I assist?"
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
            print(f"JARVIS: {args.model} ({mode})")
            continue
        if user_text == ":workspace":
            print(f"JARVIS: {workspace}")
            continue
        if user_text == ":status":
            user_text = "Give a brief system status."
        if user_text.startswith(":speak"):
            speak_answers = user_text.lower().endswith(" on")
            print(f"JARVIS: Spoken answers are {'on' if speak_answers else 'off'}, Sir.")
            continue

        ask_once(assistant, user_text, speak_answers, voice)


def ask_once(
    assistant: JarvisAssistant,
    prompt: str,
    speak_answers: bool,
    voice: str = DEFAULT_VOICE,
) -> int:
    try:
        answer = assistant.ask(prompt)
    except RuntimeError as exc:
        print(f"JARVIS: {exc}", file=sys.stderr)
        return 1

    print(f"\nJARVIS: {answer}")
    if speak_answers:
        speak(answer, voice)
    return 0
