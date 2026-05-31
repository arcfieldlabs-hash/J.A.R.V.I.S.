# Local Jarvis for macOS

This is a starter Jarvis you can run locally on your MacBook Pro from VS Code.
The AI brain talks to Ollama on your machine, and the assistant can:

- Chat and answer questions
- Speak answers with macOS `say`
- Open Mac apps
- Run shell commands after your approval
- Search the web when you ask it to
- Read and write files inside a safe workspace

## 1. Install Ollama

Install Ollama from [ollama.com](https://ollama.com), then make sure it is running.
You can start it from the app, or in Terminal:

```bash
ollama serve
```

Pull a laptop-friendly starter model:

```bash
ollama pull llama3.2:3b
```

You can use a different local Ollama chat model by setting `JARVIS_MODEL` or passing `--model`.

## 2. Open This Folder in VS Code

```bash
code /Users/hassanshahid/Documents/Codex/2026-05-25/i-want-to-make-my-very
```

## 3. Run Jarvis

No third-party Python packages are needed for this first version.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m jarvis --speak
```

Or run the included VS Code launch config named **Jarvis CLI**.

## Run Without VS Code

Double-click this file in Finder:

```text
run_jarvis.command
```

It starts Ollama if needed, creates the Python virtual environment if needed, then launches Jarvis in Terminal.

You can also run it from Terminal:

```bash
./run_jarvis.command
```

## Run From Siri With Shortcuts

Create a macOS Shortcut named **Ask Jarvis**:

1. Open the Shortcuts app.
2. Click **+** to create a new shortcut.
3. Name it **Ask Jarvis**.
4. Add **Ask for Input**.
5. Set the prompt to `What should I ask Jarvis?`.
6. Add **Run Shell Script**.
7. Set **Pass Input** to `to stdin`.
8. Paste this command:

```bash
cd "/Users/hassanshahid/Documents/Codex/2026-05-25/i-want-to-make-my-very"
./scripts/jarvis_once.sh
```

9. Add **Speak Text** and use the shell script result as the text.

Then say:

```text
Hey Siri, Ask Jarvis
```

Siri launches the shortcut, asks what you want, passes that to your local Jarvis, then reads the answer.

## Say "Hey Jarvis"

On Apple silicon Macs, you can use **Vocal Shortcuts** to trigger Jarvis with your own phrase.
This does not rename Siri; it teaches macOS to listen for a custom phrase and run an action.

First create the **Ask Jarvis** shortcut from the section above.
Then:

1. Open **System Settings**.
2. Go to **Accessibility**.
3. Go to **Speech**.
4. Open **Vocal Shortcuts**.
5. Click **Set Up** or **Add Action**.
6. Choose **Siri Request**.
7. Enter `Ask Jarvis`.
8. Set the phrase to:

```text
Hey Jarvis
```

9. Repeat the phrase when macOS asks you to train it.
10. Turn **Vocal Shortcuts** on.

Now you should be able to say:

```text
Hey Jarvis
```

macOS will run the Ask Jarvis shortcut, ask what you want, send that to your local Jarvis, and speak the answer.

If Vocal Shortcuts is not available, use **Voice Control** instead:

1. Go to **System Settings** > **Accessibility** > **Voice Control**.
2. Turn **Voice Control** on.
3. Open **Commands**.
4. Add a custom command named `Hey Jarvis`.
5. Set the action to **Run Shortcut**.
6. Choose **Ask Jarvis**.

## Test It

```bash
python3 -m unittest discover -s tests
```

## Example Prompts

```text
Open Visual Studio Code.
List the files in my workspace.
Write a file called notes/plan.txt with a 3-step launch plan.
Search the web for local-first personal assistant projects.
Run pwd.
```

## Safety Defaults

- Shell commands require your approval before they run.
- Obvious destructive commands are blocked.
- File access is limited to the configured workspace.
- The model runs locally through Ollama at `http://localhost:11434`.
- Web search only uses the internet when you explicitly ask for a search.

To change the safe file workspace:

```bash
JARVIS_WORKSPACE="$HOME/Documents" python3 -m jarvis --speak
```

To change the model:

```bash
JARVIS_MODEL="llama3.2:3b" python3 -m jarvis --speak
```

## Next Upgrades

Good next additions:

- Local speech-to-text with Whisper.cpp, MLX Whisper, or Vosk
- A wake word such as "Jarvis"
- A small menu bar app
- Calendar, reminders, email, and HomeKit tools
- Per-tool permissions so trusted actions can run faster

## Troubleshooting

If you see `Could not reach Ollama`, start Ollama and try again:

```bash
ollama serve
```

If Ollama says the model is missing:

```bash
ollama pull llama3.2:3b
```

If the `ollama` command crashes with a Metal or MLX error, try it from a normal Terminal window first.
If it still crashes there, update or reinstall Ollama before running Jarvis.
