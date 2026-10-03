from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .assistant import JarvisAssistant
from .ollama_client import OllamaClient
from .speech import MAX_TEXT_CHARACTERS, SpeechError, speak_text
from .tools import ToolKit


# Defaults tuned for Apple Silicon 16GB (M1/M2/M3 Pro class).
DEFAULT_MODEL = os.getenv("JARVIS_MODEL", "llama3.2:3b")
DEFAULT_OLLAMA_URL = os.getenv("JARVIS_OLLAMA_URL", "http://localhost:11434")
DEFAULT_VOICE = os.getenv("JARVIS_VOICE", "Daniel")
DEFAULT_VISION_MODEL = os.getenv("JARVIS_VISION_MODEL", "moondream")

# Low-memory profile (good default on 16GB unified memory).
LOWMEM_NUM_CTX = 2048
LOWMEM_NUM_PREDICT = 256
LOWMEM_HISTORY = 10
LOWMEM_TOOL_ROUNDS = 3
LOWMEM_KEEP_ALIVE = "5m"
FULLMEM_NUM_CTX = 8192
FULLMEM_NUM_PREDICT = 1024


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local JARVIS assistant (optimized for Apple Silicon 16GB)."
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    parser.add_argument("--workspace", default=os.getenv("JARVIS_WORKSPACE", os.getcwd()))
    parser.add_argument(
        "--full-access", action="store_true",
        help="Allow file tools outside the workspace, subject to macOS permissions; switch off in the web interface.",
    )
    parser.add_argument(
        "--self-develop", action="store_true",
        help="Enable proposing and reviewing Jarvis improvements; testing, applying, and running extensions still require approval.",
    )
    parser.add_argument("--speak", action="store_true", help="Speak answers with macOS say.")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help="Installed macOS voice (default: best installed Daniel variant).")
    parser.add_argument("--no-tools", action="store_true", help="Disable tools.")
    parser.add_argument("--once", help="One-shot question then exit.")
    parser.add_argument("--menubar", action="store_true", help="Optional menu-bar app (needs rumps).")
    parser.add_argument("--data-dir", default=os.getenv("JARVIS_DATA_DIR", "~/.jarvis"), help="Private memory and reminder storage.")
    parser.add_argument("--listen", action="store_true", help="Local Whisper microphone input; say the wake word.")
    parser.add_argument("--wake-word", default="jarvis")
    parser.add_argument("--stt-model", default="base", help="Local Whisper model (e.g. tiny, base).")
    parser.add_argument(
        "--vision-model", default=DEFAULT_VISION_MODEL,
        help="Local Ollama image model for enabled camera/screens (default: moondream).",
    )
    parser.add_argument("--web", action="store_true", help="Local orb, chat, and telemetry interface.")
    parser.add_argument("--port", type=int, default=8765, help="Loopback web interface port.")
    parser.add_argument("--doctor", action="store_true", help="Report model and optional dependency readiness.")
    parser.add_argument("--timeout", type=int, default=120, help="Ollama request timeout in seconds.")
    parser.add_argument(
        "--low-mem",
        action="store_true",
        default=True,
        help="Low-memory profile for 16GB Macs (default: on).",
    )
    parser.add_argument(
        "--full-mem",
        action="store_true",
        help="Use 8192-token context and up to 1024 output tokens; Ollama manages available RAM.",
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
        speak_text(text[:MAX_TEXT_CHARACTERS], voice=voice)
    except (SpeechError, ValueError) as exc:
        print(f"JARVIS speech: {exc}", file=sys.stderr)


def print_help() -> None:
    print(
        """
Commands:
  :help          Show this help
  :speak on|off  Toggle spoken answers
  :model         Show active model
  :workspace     Show workspace
  :access on|off  Allow/restrict file access outside the workspace
  :develop on|off Enable/disable code development and extension execution
  :permissions   Show storage and code development permissions
  :status        Quick system status
  :memory [text] List/search saved facts
  :remember ...  Save a fact explicitly
  :forget ID     Delete a saved fact
  :reminders     List pending reminders
  :jobs          List background research jobs
  :quit          Exit

Tips for 16GB MacBook:
  Default is --low-mem (small context, shorter history).
  Use a 3B–8B model:  ollama pull llama3.2:3b
  Optional stronger small model:  ollama pull phi3:mini  or  gemma2:2b
  Quit other heavy apps while chatting for best latency.
  --full-mem gives Ollama more context; macOS still manages RAM.
  --full-access allows file tools across storage you can access.
""".strip()
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if sum(bool(value) for value in (args.web, args.listen, args.once, args.menubar)) > 1:
        print("Choose one of --web, --listen, --once, or --menubar.", file=sys.stderr)
        return 2
    if args.timeout <= 0 or (args.num_ctx is not None and args.num_ctx < 512):
        print("Use a positive --timeout and --num-ctx of at least 512.", file=sys.stderr)
        return 2
    if not 1 <= args.port <= 65535:
        print("--port must be between 1 and 65535.", file=sys.stderr)
        return 2

    workspace = Path(args.workspace).expanduser().resolve()
    data_dir = Path(args.data_dir).expanduser().resolve()
    if args.doctor:
        from .diagnostics import diagnose
        report = diagnose(ollama_url=args.ollama_url, model=args.model, workspace=workspace, data_dir=data_dir)
        print(json.dumps(report, indent=2))
        return 0 if report["ollama"].get("ready") and report["ollama"].get("model_installed") else 1

    if args.menubar:
        from . import menubar as mb

        return mb.main()

    low_mem = args.low_mem and not args.full_mem

    num_ctx = args.num_ctx
    if num_ctx is None:
        num_ctx = LOWMEM_NUM_CTX if low_mem else FULLMEM_NUM_CTX

    client = OllamaClient(
        base_url=args.ollama_url,
        model=args.model,
        temperature=0.25 if low_mem else 0.3,
        num_ctx=num_ctx,
        num_predict=LOWMEM_NUM_PREDICT if low_mem else FULLMEM_NUM_PREDICT,
        keep_alive=LOWMEM_KEEP_ALIVE if low_mem else "10m",
        timeout=args.timeout,
    )

    try:
        toolkit = ToolKit(workspace=workspace, data_dir=data_dir, full_disk_access=args.full_access)
        if args.self_develop:
            toolkit.development.set_enabled(True)
    except (OSError, ValueError) as exc:
        print(f"Could not open Jarvis storage: {exc}", file=sys.stderr)
        return 1
    assistant = JarvisAssistant(
        client,
        toolkit,
        tools_enabled=not args.no_tools,
        max_tool_rounds=LOWMEM_TOOL_ROUNDS if low_mem else 4,
        history_limit=LOWMEM_HISTORY if low_mem else 20,
        memory=toolkit.memory,
    )

    speak_answers = args.speak
    voice = args.voice

    from .monitoring import SystemMonitor

    def show_reminder(reminder):
        message = str(reminder["text"])
        print(f"\nJARVIS reminder: {message}", flush=True)
        toolkit.execute("notify", {"title": "JARVIS reminder", "message": message})

    monitor = SystemMonitor(workspace, memory=toolkit.memory, on_reminder=show_reminder)
    toolkit.monitor = monitor
    try:
        if args.web:
            from .web import serve
            return serve(
                assistant, toolkit, port=args.port, speak_answers=speak_answers,
                voice=voice, vision_model=args.vision_model, stt_model=args.stt_model,
            )
        monitor.start()
        if args.once:
            return ask_once(assistant, args.once, speak_answers, voice)
        if args.listen:
            return listen_loop(assistant, wake_word=args.wake_word, model=args.stt_model, voice=voice)
        return interactive_loop(assistant, args, low_mem, speak_answers, voice)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"JARVIS: {exc}", file=sys.stderr)
        return 1
    finally:
        monitor.stop()
        toolkit.close()


def interactive_loop(assistant, args, low_mem, speak_answers, voice) -> int:
    workspace = assistant.toolkit.workspace

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
        if user_text.startswith(":access"):
            if user_text not in {":access on", ":access off"}:
                print("JARVIS: Use :access on or :access off, Sir.")
                continue
            enabled = user_text == ":access on"
            assistant.toolkit.set_full_disk_access(enabled)
            print(f"JARVIS: Storage access is {'all permitted paths' if enabled else 'workspace only'}, Sir.")
            continue
        if user_text == ":permissions":
            toolkit = assistant.toolkit
            print(f"JARVIS: Storage: {'all permitted paths' if toolkit.full_disk_access else 'workspace only'}; "
                  f"self-development: {'on' if toolkit.development.enabled else 'off'}. "
                  "Code testing, applying, and extension execution each require approval, Sir.")
            continue
        if user_text.startswith(":develop"):
            if user_text not in {":develop on", ":develop off"}:
                print("JARVIS: Use :develop on or :develop off, Sir.")
                continue
            enabled = user_text == ":develop on"
            assistant.toolkit.development.set_enabled(enabled)
            print(f"JARVIS: Self-development is {'on' if enabled else 'off'}. Code execution still requires approval, Sir.")
            continue
        if user_text == ":status":
            user_text = "Give a brief system status."
        if user_text.startswith(":speak"):
            speak_answers = user_text.lower().endswith(" on")
            print(f"JARVIS: Spoken answers are {'on' if speak_answers else 'off'}, Sir.")
            continue
        direct_tool = None
        if user_text == ":memory" or user_text.startswith(":memory "):
            direct_tool = ("memory", {"action": "recall", "query": user_text[7:].strip()})
        elif user_text.startswith(":remember "):
            direct_tool = ("memory", {"action": "remember", "text": user_text[10:]})
        elif user_text.startswith(":forget "):
            try:
                memory_id = int(user_text[8:])
            except ValueError:
                print("JARVIS: Use :forget followed by a numeric memory ID, Sir.")
                continue
            direct_tool = ("memory", {"action": "forget", "id": memory_id})
        elif user_text == ":reminders":
            direct_tool = ("reminder", {"action": "list"})
        elif user_text == ":jobs":
            direct_tool = ("research", {"action": "list"})
        if direct_tool:
            result = assistant.toolkit.execute(*direct_tool)
            print(f"JARVIS: {result.content}")
            continue

        ask_once(assistant, user_text, speak_answers, voice)


def listen_loop(assistant, *, wake_word: str, model: str, voice: str) -> int:
    from .voice import VoiceListener
    listener = VoiceListener(model=model, wake_word=wake_word)
    print(f'JARVIS listening. Say "{wake_word}" followed by your request. Ctrl-C to stop.')
    try:
        while True:
            prompt = listener.listen()
            if prompt is None:
                continue
            print(f"You: {prompt}")
            ask_once(assistant, prompt, True, voice)
            listener.suspend_after_speech()
    except KeyboardInterrupt:
        print("\nJARVIS: Voice input stopped, Sir.")
        return 0


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
