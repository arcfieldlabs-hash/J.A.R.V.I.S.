# J.A.R.V.I.S. — Local Movie-Style Assistant for macOS

**Just A Rather Very Intelligent System**  
A private, local AI assistant inspired by the JARVIS of the Iron Man films.  
Powered by Ollama on your Mac. Witty, formal, British, and useful.

Works standalone **and** as an OpenClaw skill.

## Capabilities

| Area | What you get |
|------|----------------|
| **Personality** | Dry British wit, addresses you as "Sir", anticipates needs |
| **Voice** | macOS `say` with Daniel (British) by default |
| **Desktop** | Open / activate apps, frontmost app, list windows, screenshots, volume, notifications |
| **Files** | Safe read / write / list inside a workspace |
| **Shell** | Commands with explicit approval + destructive-pattern blocking |
| **Web** | DuckDuckGo search |
| **System** | Battery, load, disk status |
| **Google** | Gmail, Calendar, Drive, Docs, Sheets, Contacts via `gog` CLI |
| **OpenClaw** | Drop-in skill so the lobster can speak and act as JARVIS |

## Quick start

```bash
# 1. Ollama
ollama serve
ollama pull llama3.2:3b

# 2. Run
python3 -m venv .venv && source .venv/bin/activate
python3 -m jarvis --speak
```

Or double-click `run_jarvis.command`.

### Flags

| Flag | Purpose |
|------|---------|
| `--speak` | Speak replies |
| `--voice Daniel` | Choose voice |
| `--model ...` | Ollama model |
| `--workspace ~/Documents` | Safe file root |
| `--once "question"` | One-shot |
| `--no-tools` | Chat only |

Env vars: `JARVIS_MODEL`, `JARVIS_OLLAMA_URL`, `JARVIS_WORKSPACE`, `JARVIS_VOICE`.

## Desktop assistance

Examples:

```text
Open Visual Studio Code.
Activate Safari.
What app is frontmost?
List open windows.
Take a screenshot and save it as desk.png.
Set the volume to 35.
Notify me that the build finished.
```

**Accessibility**: `list_windows` needs Terminal (or your Python) granted in  
System Settings → Privacy & Security → Accessibility.

## Google accounts (Gmail, Calendar, Drive, …)

JARVIS uses the excellent **`gog`** CLI (same ecosystem OpenClaw uses).

```bash
# Install (example)
brew install teru-0529/tap/gog   # or download from the gog releases

# Connect your Google account(s)
gog auth login
```

Then in JARVIS:

```text
Show my unread emails.
What's on my calendar today?
Search Drive for the quarterly report.
Find contact Alice.
```

Under the hood this calls the `google` tool with a `gog ...` command.  
Mutating actions (send / create / delete / upload) still require your confirmation.

## OpenClaw integration

### Option A — Use the included skill

Copy the skill into your OpenClaw workspace:

```bash
cp -R openclaw ~/.openclaw/skills/jarvis
# or wherever your OpenClaw skills directory lives
```

Then tell OpenClaw something like:

> Use the jarvis skill. Good evening.

OpenClaw will adopt the JARVIS personality and use the desktop + Google patterns described in `openclaw/SKILL.md`.

### Option B — Run local JARVIS from OpenClaw

OpenClaw can shell out to the pure-Python package:

```bash
python3 -m jarvis --once "System status"
```

or keep the full interactive CLI in a terminal while you message the OpenClaw gateway from WhatsApp / Telegram / Discord / etc.

### Option C — Google inside OpenClaw directly

Install the community Google skill as well:

```bash
clawhub install gog          # or the equivalent ClawHub / BetterClaw command
```

Then OpenClaw has first-class Gmail / Calendar / Drive tools; JARVIS personality remains available via the skill above.

## Safety defaults

- Shell and mutating Google actions require confirmation
- Destructive shell patterns are blocked
- File access sandboxed to the workspace
- Model runs fully locally via Ollama
- Web access only when you ask for a search

## Example prompts

```text
Good evening, JARVIS.
What's the system status?
Open VS Code and activate it.
Take a screenshot.
Show unread mail.
What's on my calendar today?
Search Drive for budget spreadsheet.
Set volume to 40 and notify me when done.
```

## Next ideas

- Continuous wake-word listening
- Menu-bar companion
- Full Accessibility-driven mouse/keyboard automation (with tight allow-lists)
- Native Google OAuth without the gog CLI
- Persistent memory of preferences

## Troubleshooting

**Ollama unreachable** → `ollama serve`  
**Model missing** → `ollama pull llama3.2:3b`  
**Voice missing** → `say -v '?'` then `--voice Alex`  
**gog not found** → install + `gog auth login`  
**Windows list fails** → grant Accessibility permission

---

*"Sometimes you gotta run before you can walk."* — Tony Stark  
Now go build something brilliant, Sir.
