# J.A.R.V.I.S. — Local Movie-Style Assistant for macOS

**Just A Rather Very Intelligent System**  
Private local AI assistant inspired by Iron Man’s JARVIS.  
Optimized by default for **Apple Silicon 16GB** (M1/M2/M3 Pro class).

## Quick start (MacBook 16GB)

```bash
# Recommended small model for 16GB
ollama pull llama3.2:3b

python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -e .
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
- Speak with the installed British Daniel voice, preferring Enhanced or Premium quality
- Desktop: open/activate apps, windows, screenshots, volume, notifications
- Advanced desktop (with confirmation): type, key, click, window bounds
- Files, shell (approved), web search
- Google via `gog` CLI or optional native OAuth
- Optional menu bar: `python3 -m jarvis --menubar` (needs `rumps`)
- OpenClaw skill in `openclaw/`
- Persistent local facts and conversation history, recalled across restarts
- Reminders checked by a background monitor while Jarvis runs
- Safe arithmetic, live web-page reading, and background research with source links
- Optional Chromium browser control with approved clicks and form input
- Optional local Whisper microphone input with a configurable wake word
- Local orb interface with chat, native Mac speech, display modes, and system telemetry
- CPU, RAM, disk, and network metrics with `psutil` included in the normal install
- GPU readings when the operating system exposes them
- Optional access to files across your Mac, plus a larger model context
- Camera, microphone, and individually selected screens with immediate off controls

These additions follow the capability outline in the supplied **Safari.pdf** preview of *Build Your Own J.A.R.V.I.S.*. They retain this project's macOS/Ollama design. See [CAPABILITIES.md](CAPABILITIES.md) for the mapping and practical limits.

## New modes

Run these commands from the cloned repository after installing the package:

```bash
# Orb, chat, telemetry, and Mac speech at http://127.0.0.1:8765
python3 -m jarvis --web --speak

# Optional microphone + local Whisper (downloads model weights on first use)
brew install portaudio
python3 -m pip install -e '.[voice]'
python3 -m jarvis --listen --wake-word jarvis --stt-model base

# Optional browser automation in a fresh Chromium session
python3 -m pip install -e '.[browser]'
python3 -m playwright install chromium

# Read-only setup check; reports a missing Ollama/model with exit code 1
python3 -m jarvis --doctor
```

Voice mode records bounded utterances, accepts a command after "Jarvis", closes the microphone during the reply, and waits briefly before listening again. Say the name alone to open a 15-second window for your next utterance. Use Ctrl-C to stop. `--listen` speaks replies automatically. The local web interface generates speech on your Mac and plays it in the browser, using the same installed voice.

The web server binds only to `127.0.0.1`. CLI authorization prompts remain available for shell commands, desktop input, Google changes, and browser clicks/fills. These actions are denied in web mode; use the CLI when they require approval. `--once`, `--listen`, `--web`, and `--menubar` are separate modes.

## Update an existing installation

Stop Jarvis with Ctrl-C, then run:

```bash
cd ~/jarvis
git pull
source .venv/bin/activate
brew install portaudio
python3 -m pip install -e '.[voice]'
ollama pull moondream
python3 -m jarvis --web --speak --full-access --full-mem
```

The voice extra supplies local Whisper transcription; PortAudio supports terminal microphone listening. `moondream` is the separate local vision model used for camera and screen snapshots. Text chat still uses `llama3.2:3b`. If you only want chat, speech, and metrics, `python3 -m pip install -e .` is sufficient.

For a British male voice, install **Daniel (Enhanced)** or **Daniel (Premium)** in **System Settings → Accessibility → Read & Speak → System Voice → Manage Voices → English (United Kingdom)**. Older macOS versions call this panel **Spoken Content**. Jarvis prefers the best installed Daniel variant. This is a movie-inspired voice using Apple's speech engine; it does not clone the actor's voice. Select another installed voice with `--voice "Voice name"`.

## Storage, RAM, and live metrics

File tools start inside the selected `--workspace`. `--full-access` permits reading and writing paths anywhere your Mac account can access, including external volumes. Relative paths still refer to the workspace. The local orb's storage switch can enable or revoke broader file access while Jarvis runs. macOS permissions continue to apply: grant the app launching Python, usually **Terminal**, access in **System Settings → Privacy & Security → Full Disk Access** if you need protected folders. Restart that app after changing the permission. Shell commands and desktop controls retain their existing approval requirements.

`--full-mem` raises the model context to 8,192 tokens, reply limit to 1,024 tokens, and history limits. It lets Ollama use more available RAM for your conversation. It does not allocate all physical RAM or grant access to other processes' private memory. Watch RAM and swap readings on a 16GB Mac; use the smaller default profile when you need headroom. `--num-ctx` overrides the context size.

`psutil` is the library that reads system statistics. It installs with Jarvis and supplies CPU load, individual core load and core counts, RAM used and available, swap, Jarvis process memory, disk usage, and network upload/download rates. Network rates measure traffic across the monitored machine, rather than only Jarvis traffic. On macOS, GPU activity and allocation come from IORegistry when the driver exposes them; GPU names come from `system_profiler`. Unsupported readings appear as unavailable. No administrator command is needed to collect these metrics. The older `.[monitor]` install option remains compatible.

## Camera, microphone, and screens

Open the local orb at **http://127.0.0.1:8765**. All device controls start **off**. Enable a device to request browser permission; choose each screen or window through the browser's sharing dialog. Add another screen individually if you use several displays. Turning a device off stops its browser tracks immediately; individual screen stops and **Stop all** are also available. Nothing is captured automatically after a restart.

Camera and screen analysis uses an explicit snapshot and question sent to your local Ollama vision model. Enabling a preview alone does not continuously analyze it. Microphone input uses an explicit bounded recording, transcribed locally by Whisper; it pauses while Jarvis speaks. Turning a source off suppresses results from its pending captures.

Grant camera and microphone access when your browser asks. For screen sharing, allow the browser in **System Settings → Privacy & Security → Screen & System Audio Recording** (called **Screen Recording** on older macOS), then restart the browser if prompted. These controls require the local Python application. The Cloudflare preview keeps hardware controls disabled.

## Memory, reminders, and research

Facts, the latest 500 completed conversation pairs, and reminders live in `~/.jarvis/memory.sqlite3` with private file permissions. Override the directory with `--data-dir` or `JARVIS_DATA_DIR`. Only explicit requests or `:remember` add facts; conversation history is stored separately. The assistant restores a few recent conversation pairs and recalls relevant saved facts within its context budget. Do not put secrets in conversations you intend to save.

Try these requests:

```text
Remember that my current project is the Arc Field Labs website.
What do you remember about my project?
Remind me to review the prototype tomorrow at 9 AM.
Calculate (42 * 17) / 3.
Search for today's weather in London and cite the source.
Start background research on small local language models for a 16GB Mac.
List my research jobs, then show the results of the latest one.
Navigate the browser to https://example.com and read the page.
```

The terminal also supports `:memory [query]`, `:remember a fact`, `:forget ID`, `:reminders`, and `:jobs` without a model call. Reminders require an unambiguous date/time; Jarvis's tool uses timezone-aware ISO8601 timestamps. Reminders are delivered once while the process runs, including overdue reminders after a restart. Research runs in one background worker, collects up to four web-page excerpts, and saves source-linked Markdown under `<workspace>/research/`. Jobs are tracked during the current session; saved reports remain after exit. Pending jobs are cancelled on shutdown, and an active fetch may finish before Python exits. Keep an interactive or web session running for background work.

Tool calls are bounded per request. Repeated identical actions are stopped, and the final model round accepts an answer only. If the model cannot finish its explanation, Jarvis reports the confirmed tool results, including any completed writes or other actions. Tool exchanges stay within the current request; later conversation turns receive completed user/reply pairs. Asking to assign research without a topic should prompt for the topic. Reading Jarvis's source does not automatically add new tools; extensions require code changes, approval where applicable, and a restart.

## Install from a fresh clone

```bash
git clone https://github.com/arcfieldlabs-hash/J.A.R.V.I.S. jarvis
cd jarvis
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
ollama pull llama3.2:3b
python3 -m jarvis --speak
```

Jarvis supports Python 3.10+ and includes `psutil` as its core runtime dependency. A virtual environment is recommended for the optional voice and browser packages. Model caches should stay on a local SSD outside cloud-synced folders.

## Flags

| Flag | Purpose |
|------|---------|
| `--speak` | Spoken replies |
| `--voice ...` | Installed Mac voice (default: Daniel, best available variant) |
| `--low-mem` | 16GB profile (default on) |
| `--full-mem` | 8,192-token context, longer replies and history |
| `--model ...` | Ollama model |
| `--num-ctx 2048` | Override context size |
| `--workspace ...` | Safe file root |
| `--full-access` | Files across storage within your Mac account's permissions |
| `--once "..."` | One-shot |
| `--menubar` | Menu-bar companion |
| `--no-tools` | Chat only |
| `--listen` | Local microphone input and spoken replies |
| `--wake-word ...` | Voice activation phrase (default: jarvis) |
| `--stt-model ...` | Whisper model (default: base) |
| `--vision-model ...` | Camera/screen snapshot model (default: moondream) |
| `--web` | Local orb, chat, and telemetry |
| `--port 8765` | Web interface port |
| `--data-dir ...` | Private memory/reminder directory |
| `--doctor` | Ollama/model and optional dependency checks |
| `--timeout 120` | Model request timeout in seconds |

Env: `JARVIS_MODEL`, `JARVIS_VISION_MODEL`, `JARVIS_WORKSPACE`, `JARVIS_DATA_DIR`, `JARVIS_VOICE`, `JARVIS_OLLAMA_URL`.

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

## Cloudflare deployment

Cloudflare can host the static orb interface as a preview. The repository's
`wrangler.jsonc` points to `./web`; run `npm ci` and `npm run check` to validate it
locally. Follow [Cloudflare-deployment.md](Cloudflare-deployment.md) for the exact
build settings and branch to deploy. Chat, Ollama, memory, telemetry, desktop
tools, and hardware controls run in the local Python application on your Mac.

## Validation

```bash
python3 -m pip install -e .
python3 -m unittest discover -s .
```

Tests use fake model responses and mocked microphone, camera, screen, GPU, and network services. Real macOS permissions, GPU driver readings, speech hardware, downloaded voice models, Google OAuth, and Ollama inference must be verified on your Mac. `--doctor` checks installed Python dependencies and the selected Ollama model; it does not load Whisper or verify Chromium binaries or microphone access.

## OpenClaw

```bash
cp -R openclaw ~/.openclaw/skills/jarvis
```

---

*Optimized for your M2 Pro 16GB. Stay efficient, Sir.*
