# J.A.R.V.I.S. — Local Movie-Style Assistant for macOS

**Just A Rather Very Intelligent System**  
A private, local AI assistant inspired by the JARVIS of the Iron Man films.  
Powered by Ollama on your Mac. Witty, formal, British, and useful.

## What it can do

- Chat with dry British wit and address you as "Sir"
- Speak answers using a British macOS voice (`Daniel` by default)
- Open Mac apps
- Run shell commands (after your explicit approval)
- Search the web
- Read / write / list files inside a safe workspace
- Report system status (battery, memory, load, disk)
- Show macOS notifications
- Set system volume
- Open URLs in the browser

## 1. Install Ollama

Install from [ollama.com](https://ollama.com), then make sure it is running:

```bash
ollama serve
```

Pull a good starter model:

```bash
ollama pull llama3.2:3b
```

You can change the model with `JARVIS_MODEL` or `--model`.

## 2. Run Jarvis

No third-party Python packages required.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m jarvis --speak
```

Or double-click `run_jarvis.command` in Finder.

### Useful flags

| Flag | Purpose |
|------|---------|
| `--speak` | Speak replies with macOS `say` |
| `--voice Daniel` | Choose voice (default is Daniel) |
| `--model llama3.2:3b` | Select Ollama model |
| `--workspace ~/Documents` | Safe file workspace |
| `--once "status"` | One-shot question |
| `--no-tools` | Disable all tools |

Environment variables: `JARVIS_MODEL`, `JARVIS_OLLAMA_URL`, `JARVIS_WORKSPACE`, `JARVIS_VOICE`.

## Movie-style examples

```text
Good evening, JARVIS.
What's the system status?
Set the volume to 35.
Notify me that the build finished.
Open Visual Studio Code.
Search the web for local-first AI assistants.
Open https://github.com
List the files in my workspace.
Write a file called notes/plan.txt with a short launch plan.
```

## Siri / "Hey Jarvis"

Create a Shortcut named **Ask Jarvis**:

1. Shortcuts app → New Shortcut
2. **Ask for Input** → prompt: `What should I ask Jarvis?`
3. **Run Shell Script** → Pass Input: `to stdin`
4. Script (adjust the path to your clone):

```bash
cd "/path/to/your/J.A.R.V.I.S."
./jarvis_once.sh
```

5. **Speak Text** using the script result.

Then say: **Hey Siri, Ask Jarvis**

For a true wake phrase on Apple silicon:

- System Settings → Accessibility → Speech → Vocal Shortcuts
- Add action → Siri Request → "Ask Jarvis"
- Phrase: `Hey Jarvis`

## Safety defaults

- Shell commands always require your confirmation
- Destructive patterns are blocked
- File access is sandboxed to the configured workspace
- Model runs fully locally via Ollama
- Web access only when you explicitly ask for a search

## Next upgrades (ideas)

- Local speech-to-text (Whisper / MLX Whisper / Vosk)
- True continuous wake-word listening
- Menu-bar companion app
- Calendar, Reminders, Mail, HomeKit tools
- Per-tool trusted permissions
- Persistent memory / user preferences

## Troubleshooting

**Could not reach Ollama**
```bash
ollama serve
```

**Model missing**
```bash
ollama pull llama3.2:3b
```

**Voice not found**  
List available voices: `say -v '?'`  
Then run with `--voice "Alex"` or set `JARVIS_VOICE`.

---

*"Sometimes you gotta run before you can walk."* — Tony Stark  
Now go build something brilliant, Sir.
