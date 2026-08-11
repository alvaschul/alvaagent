# alvaagent

On-device AI agent harness for **Termux (Android)** — runs an OpenAI-compatible
chat agent with real tool use (calculator, web_fetch, shell, file ops, skills,
memory, todos) entirely on your phone. No pip installs required.

## Features
- **Two frontends**: `alvaagent_tui.py` (plain-Python TUI, stdlib only) and
  `index.html` (Pyodide browser build — no server needed).
- **17 tools**: calculator, web_fetch, get_time, memory, todos, shell, file
  read/write/edit/list, skill save/read/list.
- **Provider profiles**: point at any OpenAI-compatible endpoint
  (default: omniroute). Each `/provider` is a saved named profile.
- **Sessions** persist to `store.json`; **context auto-compression** keeps long
  chats inside the model window.

## Quick start (Termux)
```bash
pkg install python
git clone https://github.com/alvaschul/alvaagent.git
cd alvaagent
python3 alvaagent_tui.py
```
On first run, set your endpoint with `/provider <name>` (e.g. omniroute) and
paste the API key when prompted (or set `ALVA_API_KEY`).

## Tests
```bash
python3 test_tui.py      # headless validation against mock_llm_server.py
```

## Security model
Headless runs default to **deny** for risky/mutating commands; the TUI wires
`ON_PERMISSION` to an interactive y/N prompt. Command substitution (`$()`,
backticks), destructive `find` flags, and out-of-project file reads all require
explicit approval.

## Files
- `alvaagent_tui.py` — the harness (Python stdlib only)
- `index.html` — Pyodide browser build
- `mock_llm_server.py` — offline OpenAI-compatible mock for tests
- `test_tui.py` — headless test suite
- `start.sh` — launcher (`bash start.sh` = web, `bash start.sh tui` = terminal)
- `.alvaagent/config.json` — provider profiles (regenerate on first run)
# Last updated: Wed Aug 12 12:06:37 AM WIB 2026
