# Capability update from Safari.pdf

Source: the user-supplied preview of *Build Your Own J.A.R.V.I.S.* by Sam Manina / ManinaLabs. The preview's "What You Are Actually Building" and architecture diagram list voice, persistent memory, tool calling, browser control, an orb interface, research, and modular services. The PDF does not include the later implementation chapters or a voice-training recipe.

The existing assistant runs on macOS with a local Ollama model and macOS speech. This update applies the preview's capability goals to that design; it does not require Windows, CUDA, or an Anthropic API key.

| Blueprint capability | Implementation | Limits / setup |
| --- | --- | --- |
| Voice conversations and activation | Optional local Whisper input; bounded wake-word gate; native macOS speech with British Daniel, preferring Enhanced/Premium | Install the voice extra and PortAudio for microphone input. Install the Mac voice in System Settings. Listening pauses while Jarvis speaks. This uses an installed voice rather than an actor clone. |
| Persistent memory | SQLite facts, conversation pairs, lexical relevance retrieval, explicit forget | Private local database; facts are saved explicitly. Retrieval uses keyword relevance rather than embeddings. |
| Tool calling | Existing Mac/Google/files/shell tools plus memory, reminders, arithmetic, web reading, browser, research | Small models choose JSON actions. Tools report actual outcomes; model calls have timeouts and bounded tool rounds. |
| Browser and desktop automation | Existing AppleScript desktop tools plus optional Playwright Chromium navigate/read/click/fill/screenshot | Fresh browser context; clicks/fills require CLI approval. Public HTTP(S) URLs only. This is URL validation, not a hardened browser network sandbox. |
| Orb interface | Local particle orb, chat thread, Mac-generated speech playback, standby/awake/workspace display modes, device and storage controls | Modes affect presentation. Animation reacts to speech state, not measured audio amplitude. Local Python backend required. |
| Telemetry and monitors | Required `psutil` CPU/per-core/RAM/swap/process memory/network readings; disk/load/uptime; background due-reminder delivery | macOS GPU data comes from IORegistry when exposed, with `system_profiler` metadata. Unavailable readings are identified; no sudo required. Reminders require Jarvis to be running. |
| Storage and RAM | `--full-access` or a local UI toggle expands file access; `--full-mem` uses an 8,192-token model context and longer replies/history | File access follows macOS account and Full Disk Access permissions. RAM means a larger model context, not access to other processes' private memory. Default file access remains within the workspace. |
| Camera, microphone, and screens | Off-by-default browser controls; bounded local Whisper recording; explicit camera/screen snapshots analyzed by a local vision model; individual screen and stop-all controls | Browser/OS permissions required. Install the voice extra and pull `moondream` for transcription and vision. Off stops tracks and suppresses pending capture results. No continuous automatic analysis. |
| Live and background research | Search + public HTML/text reading; queued source-linked excerpt reports | Four sources/job, one worker, bounded pages/timeouts. Reports collect excerpts rather than autonomous academic research or verified synthesis. |
| Modular architecture | Separate brain, memory, voice, speech, media, browser, research, monitoring, GPU, UI and dispatch modules | Core dependency is `psutil`; heavier integrations installed as extras. |
| Diagnostics and recovery | `--doctor`, Ollama JSON output, useful timeout/setup errors, bounded queues, cleanup | No model inference/hardware checks in automated tests. |

The preview mentions ~50 tools and a Flask/Socket.IO backend, Chroma vectors, a Markdown vault, Claude prompt caching, and XTTS cloning. It supplies no implementation detail for them. This update uses the smaller existing stack: SQLite relevance search and a loopback HTTP interface with polling. Camera and screen snapshots now use a separate local vision model. Voice cloning, XTTS training, CAD agents, meeting integration, multi-hour academic research, and cloud hosting of the assistant backend remain future work.

Cloudflare can publish the orb as a static interface preview using the repository's
Wrangler configuration. It shows a link to local Jarvis and keeps chat, telemetry,
storage, and hardware controls disabled without the local Python backend. See
[Cloudflare-deployment.md](Cloudflare-deployment.md) for the build settings.

## Local operation

- Run `python3 -m jarvis --doctor` before enabling optional integrations.
- Keep model caches outside OneDrive, Dropbox, and Google Drive sync folders.
- Choose a 3B-class model on a 16GB Mac. Background research never launches another language model.
- Conversations are retained locally, and saved facts and recent history are sent to the configured Ollama endpoint as context. Do not save credentials or access tokens.
- The web UI uses a loopback binding, session token, Host/Origin checks, and text-only rendering. It denies actions that would need interactive approval. The CLI preserves existing approval flows.
- Camera, microphone, and screen access start off and require explicit browser consent. Recording and snapshot analysis are initiated by the user; media is processed through local services rather than the hosted preview.
- Research honors configured HTTP proxies. Direct public-page connections use checked destination addresses; browser URL checks do not provide equivalent DNS pinning.
