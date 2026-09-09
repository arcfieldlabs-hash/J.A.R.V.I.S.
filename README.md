# J.A.R.V.I.S. — Local Movie-Style Assistant for macOS

**Just A Rather Very Intelligent System**  
Private, local AI assistant inspired by the Iron Man films.  
Powered by Ollama. Witty, formal, British — and now with deeper desktop control, native Google support, a menu-bar companion, and OpenClaw compatibility.

## Capabilities at a glance

| Area | Features |
|------|----------|
| **Personality** | Dry British wit, addresses you as "Sir" |
| **Voice** | macOS `say` (Daniel by default) |
| **Desktop** | Open/activate apps, windows, screenshots, volume, notifications, window bounds, typed input & clicks (with confirmation) |
| **Files & shell** | Sandboxed workspace + approved commands |
| **Web** | DuckDuckGo search |
| **System** | Battery, load, disk |
| **Google** | `gog` CLI *or* native OAuth (Gmail / Calendar / Drive) |
| **Menu bar** | Optional always-on companion (`--menubar`) |
| **OpenClaw** | Skill included under `openclaw/` |

## Quick start (core — zero extra packages)

```bash
ollama serve
ollama pull llama3.2:3b

python3 -m venv .venv && source .venv/bin/activate
python3 -m jarvis --speak
```

## Optional extras

```bash
pip install -r requirements-optional.txt
```

This adds:
- Native Google API client libraries
- `rumps` for the menu-bar app

### Menu-bar companion

```bash
python3 -m jarvis --menubar
```

Gives you a menu-bar item with “Ask JARVIS…”, system status, unread mail, and quit.

### Native Google OAuth (no gog required)

1. Create a Google Cloud project and enable Gmail, Calendar, Drive APIs.
2. Create OAuth client ID → **Desktop app** → download JSON.
3. Save it as:

```text
~/.jarvis/google/credentials.json
```

4. First use of `google_native` opens a browser for consent; token is stored locally.

Then ask:

```text
Show my unread emails.
What's on my calendar today?
Search Drive for the budget spreadsheet.
```

(You can still use the `gog` CLI if you prefer — JARVIS will use whichever is available.)

### Deeper desktop control

- Window positioning: *“Resize Safari to 1000×700 at 50,50”*
- Typing / hotkeys / clicks (always confirmed):
  - Requires Accessibility permission for Terminal/Python
  - For reliable clicks: `brew install cliclick`

## OpenClaw

```bash
cp -R openclaw ~/.openclaw/skills/jarvis
```

Then tell OpenClaw to use the jarvis skill. It will adopt the personality and desktop/Google patterns.

## Safety

- Shell and mutating actions require confirmation
- Desktop type/key/click require confirmation
- Destructive shell patterns blocked
- File access limited to the workspace
- Model runs fully locally

## Flags & env

| Flag / Env | Purpose |
|------------|---------|
| `--speak` | Spoken replies |
| `--voice Daniel` | Voice name |
| `--model ...` | Ollama model |
| `--workspace ...` | Safe file root |
| `--once "..."` | One-shot |
| `--menubar` | Launch menu-bar app |
| `--no-tools` | Chat only |
| `JARVIS_MODEL` / `JARVIS_WORKSPACE` / `JARVIS_VOICE` | Same as flags |

## Example prompts

```text
Good evening, JARVIS.
System status.
Activate Safari and take a screenshot.
Resize the front window of Visual Studio Code to 1200 by 800.
Show unread mail.
What's on my calendar today?
Search Drive for quarterly report.
```

---

*"Sometimes you gotta run before you can walk."* — Tony Stark
