# Capability update from Safari.pdf

Source: the user-supplied preview of *Build Your Own J.A.R.V.I.S.* by Sam Manina / ManinaLabs. The preview's "What You Are Actually Building" and architecture diagram list voice, persistent memory, tool calling, browser control, an orb interface, research, and modular services. The PDF does not include the later implementation chapters or a voice-training recipe.

The existing assistant runs on macOS with a local Ollama model and macOS speech. This update applies the preview's capability goals to that design; it does not require Windows, CUDA, or an Anthropic API key.

| Blueprint capability | Implementation | Limits / setup |
| --- | --- | --- |
| Voice conversations and activation | Optional local Whisper input; bounded wake-word gate; existing macOS `say` output | Install the voice extra and PortAudio; microphone permissions and first-run model download required. Listening pauses while Jarvis speaks. |
| Persistent memory | SQLite facts, conversation pairs, lexical relevance retrieval, explicit forget | Private local database; facts are saved explicitly. Retrieval uses keyword relevance rather than embeddings. |
| Tool calling | Existing Mac/Google/files/shell tools plus memory, reminders, arithmetic, web reading, browser, research | Small models choose JSON actions. Tools report actual outcomes; model calls have timeouts and bounded tool rounds. |
| Browser and desktop automation | Existing AppleScript desktop tools plus optional Playwright Chromium navigate/read/click/fill/screenshot | Fresh browser context; clicks/fills require CLI approval. Public HTTP(S) URLs only. This is URL validation, not a hardened browser network sandbox. |
| Orb interface | Local particle orb, chat thread, browser speech, standby/awake/workspace display modes | Text input; modes affect presentation. Animation reacts to speech state, not measured audio amplitude. No cloned voice supplied. |
| Telemetry and monitors | Disk/load/uptime, optional psutil CPU/memory/network, background due-reminder delivery | GPU shown as unavailable. Monitoring is operational telemetry, not intrusion detection. Reminders require Jarvis to be running. |
| Live and background research | Search + public HTML/text reading; queued source-linked excerpt reports | Four sources/job, one worker, bounded pages/timeouts. Reports collect excerpts rather than autonomous academic research or verified synthesis. |
| Modular architecture | Separate brain, memory, voice, browser, research, monitoring, UI and dispatch modules | Standard-library core; heavier integrations installed as extras. |
| Diagnostics and recovery | `--doctor`, Ollama JSON output, useful timeout/setup errors, bounded queues, cleanup | No model inference/hardware checks in automated tests. |

The preview mentions ~50 tools and a Flask/Socket.IO backend, Chroma vectors, a Markdown vault, Claude prompt caching, and XTTS cloning. It supplies no implementation detail for them. This update uses the smaller existing stack: SQLite relevance search and a loopback HTTP interface with polling. Voice cloning, XTTS training, camera vision, CAD agents, meeting integration, multi-hour academic research, and production cloud hosting remain future work.

## Local operation

- Run `python3 -m jarvis --doctor` before enabling optional integrations.
- Keep model caches outside OneDrive, Dropbox, and Google Drive sync folders.
- Choose a 3B-class model on a 16GB Mac. Background research never launches another language model.
- Conversations are retained locally, and saved facts and recent history are sent to the configured Ollama endpoint as context. Do not save credentials or access tokens.
- The web UI uses a loopback binding, session token, Host/Origin checks, and text-only rendering. It denies actions that would need interactive approval. The CLI preserves existing approval flows.
- Research honors configured HTTP proxies. Direct public-page connections use checked destination addresses; browser URL checks do not provide equivalent DNS pinning.
