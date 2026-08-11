# alvaagent

On-device AI agent harness for **Termux (Android)** — runs an OpenAI-compatible
chat agent with real tool use (calculator, web_fetch, shell, file ops, skills,
memory, todos) entirely on your phone. **Zero pip installs** — Python stdlib only.

## What it is
A single-file Python TUI that talks to any OpenAI-compatible endpoint and can
actually *do* things on your device: run shell commands, read/write/edit files,
manage a to-do list, remember facts, and save reusable procedures as "skills".
Built for Termux, runs offline-friendly, and survives flaky mobile connections.

## Features
- **Python TUI** (`alvaagent_tui.py`) — stdlib only, no dependencies to install.
- **17 tools**: calculator, web_fetch, get_time, memory, todos, shell, file
  read/write/edit/list, skill save/read/list.
- **Provider profiles**: point at any OpenAI-compatible endpoint. Each
  `/provider` is a saved named profile, switchable anytime.
- **Sessions** persist to `store.json`; **context auto-compression** keeps long
  chats inside the model window.
- **Security-first**: command substitution, destructive flags, and out-of-project
  file access all require explicit approval. Store/config writes are atomic.

## Install on Termux
```bash
# 1. Base tools (Python + git)
pkg update && pkg install -y python git

# 2. Clone
git clone https://github.com/alvaschul/alvaagent.git
cd alvaagent

# 3. (optional) make `alvaagent` runnable from anywhere — like hermes
ln -sf "$PWD/alvaagent" /data/data/com.termux/files/usr/bin/alvaagent

# 4. Run
alvaagent            # if you did step 3
# or:
python3 alvaagent_tui.py
```

That's it — no `pip install`, no virtualenv. The script is self-contained.

## First-run setup
On first launch you'll be at the `⚡` prompt. Configure your endpoint:

```
/provider add        # create a new provider profile
```
You'll be asked for:
- **Name** — anything, e.g. `omniroute`
- **Base URL** — your OpenAI-compatible endpoint, e.g. `https://omniroute.alvaschul.cloud/v1`
- **API key** — paste it when prompted (it's masked and stored locally in
  `.alvaagent/config.json`, gitignored). Type `none` to clear it.
- **Model** — e.g. `auto/best-coding`
- **Temperature** — `0.7` is a sane default

Or set it non-interactively with env vars before launching:
```bash
export ALVA_BASE_URL="https://omniroute.alvaschul.cloud/v1"
export ALVA_API_KEY="your-key-here"
export ALVA_MODEL="auto/best-coding"
python3 alvaagent_tui.py
```

Switch profiles later with `/provider <name>` and list them with `/provider`.

## Basic usage
```
you> what time is it?
you> add "buy milk" to my todos
you> remember that my name is Alex
you> run: ls -la
you> read the file /sdcard/notes.txt
you> help          # full command list
you> /skills       # list saved procedures
you> /sessions     # list saved conversations
```

Slash commands: `/help /config /provider /models /test /tools /todos /todo
/memory /skin /sessions /session /new /clear /context /compress /multi /export
/stop /exit`. `Ctrl+C` cancels a running request; Tab completes slash commands.

## Tests
```bash
python3 test_tui.py      # headless validation against mock_llm_server.py
```
All checks should print `ALL TESTS PASSED ✓` (includes security regression
tests for the command classifier and atomic store writes).

## Security model
- Headless runs default to **deny** for risky/mutating commands; the TUI prompts
  `y/N` for anything dangerous.
- Blocked without approval: command substitution (`$()`, backticks), env-prefixed
  risky tokens (`env X=1 rm -rf /`), destructive `find` flags (`-delete`,
  `-exec`), and reading files outside the project folder.
- All persistence (store, config, skills, files) uses atomic temp-file + rename,
  so a kill mid-write can't corrupt your data.

## Files
- `alvaagent_tui.py` — the harness (Python stdlib only)
- `mock_llm_server.py` — offline OpenAI-compatible mock for the test suite
- `test_tui.py` — headless test suite (run it to verify your build)
- `start.sh` — `bash start.sh tui` launches the terminal client
- `.alvaagent/` — runtime config + store (gitignored, regenerated on first run)

## Updating
```bash
git pull        # pull the latest from this repo
alvaagent       # run — the symlink always points at the updated script
```
Your local `.alvaagent/` data is never touched by a pull. Because the launcher
is a symlink, you never re-run the `ln` command after a pull.
