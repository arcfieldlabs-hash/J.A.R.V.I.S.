# J.A.R.V.I.S. — Local Movie-Style Assistant for macOS

**Just A Rather Very Intelligent System**  
Private local AI assistant inspired by Iron Man’s JARVIS.  
Optimized by default for **Apple Silicon 16GB** (M1/M2/M3 Pro class).

## Quick start (MacBook 16GB)

```bash
# Recommended small model for 16GB
ollama pull llama3.2:3b

python3 -m venv .venv && source .venv/bin/activate
python3 -m jarvis --speak
```

Low-memory mode is **on by default**:
- Context ~2048 tokens
- Shorter history
- Capped reply length
- Model unloaded after a few idle minutes

```bash
# Raise limits if you have headroom
python3 -m jarvis --full-mem --speak

# Explicit model
JARVIS_MODEL=phi3:mini python3 -m jarvis --speak
```

### Good models on 16GB unified memory

| Model | Command | Feel |
|-------|---------|------|
| **llama3.2:3b** (default) | `ollama pull llama3.2:3b` | Fast, solid personality |
| phi3:mini | `ollama pull phi3:mini` | Strong reasoning for size |
| gemma2:2b | `ollama pull gemma2:2b` | Very light |
| qwen2.5:3b | `ollama pull qwen2.5:3b` | Good tool use |

Avoid 13B+ on 16GB while doing normal work — swapping will make everything slow.

## What it can do

- British dry-wit personality (“Sir”)
- Speak with macOS `say` (Daniel)
- Desktop: open/activate apps, windows, screenshots, volume, notifications
- Advanced desktop (with confirmation): type, key, click, window bounds
- Files, shell (approved), web search
- Google via `gog` CLI or optional native OAuth
- Optional menu bar: `python3 -m jarvis --menubar` (needs `rumps`)
- OpenClaw skill in `openclaw/`

## Flags

| Flag | Purpose |
|------|---------|
| `--speak` | Spoken replies |
| `--low-mem` | 16GB profile (default on) |
| `--full-mem` | Larger context / history |
| `--model ...` | Ollama model |
| `--num-ctx 2048` | Override context size |
| `--workspace ...` | Safe file root |
| `--once "..."` | One-shot |
| `--menubar` | Menu-bar companion |
| `--no-tools` | Chat only |

Env: `JARVIS_MODEL`, `JARVIS_WORKSPACE`, `JARVIS_VOICE`, `JARVIS_OLLAMA_URL`.

## Tips for smooth 16GB use

1. Prefer 3B-class models.
2. Quit heavy browsers/IDEs while running long tool chains if latency spikes.
3. Keep low-mem mode on unless you need long conversations.
4. `ollama stop` when finished to free memory quickly.
5. For always-on / larger models, a secondary mini PC is still the better long-term host; this MacBook path is tuned for interactive daily use.

## Optional extras

```bash
pip install -r requirements-optional.txt   # Google native + menu bar
```

Google native: put OAuth desktop credentials at `~/.jarvis/google/credentials.json`.

## OpenClaw

```bash
cp -R openclaw ~/.openclaw/skills/jarvis
```

---

*Optimized for your M2 Pro 16GB. Stay efficient, Sir.*
