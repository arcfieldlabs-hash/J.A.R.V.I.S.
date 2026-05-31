#!/bin/zsh
set -e

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

if ! command -v ollama >/dev/null 2>&1; then
  echo "Ollama is not installed or is not on your PATH."
  echo "Install it from https://ollama.com, then run this launcher again."
  read "?Press return to close this window."
  exit 1
fi

if ! curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then
  echo "Starting Ollama in the background..."
  nohup ollama serve > "$PROJECT_DIR/.ollama.log" 2>&1 &
  sleep 3
fi

if [ ! -d "$PROJECT_DIR/.venv" ]; then
  echo "Creating Python virtual environment..."
  python3 -m venv "$PROJECT_DIR/.venv"
fi

source "$PROJECT_DIR/.venv/bin/activate"
python3 -m jarvis --speak

read "?Press return to close this window."
