# alvaagent

On-device AI agent harness for **Termux (Android)** — runs an OpenAI-compatible
chat agent with real tool use (calculator, web_fetch, shell, file ops, skills,
memory, todos) entirely on your phone. **Zero pip installs** — Python stdlib only.

## What it is
A stdlib-only Python **package** (`alvaagent/`) behind a one-file launcher shim
(`alvaagent_tui.py`) that talks to any OpenAI-compatible endpoint and can
actually *do* things on your device: run shell commands, read/write/edit files,
manage a to-do list, remember facts, and save reusable procedures as "skills".
Built for Termux, runs offline-friendly, and survives flaky mobile connections.

## Features
- **Python TUI** (`alvaagent_tui.py` shim → `alvaagent/` package) — stdlib only,
  no dependencies to install.
- **36 tools** (curated for the model): calculator, sandboxed run_python,
  web_fetch/web_head/web_json/web_markdown/web_search/web_download/web_post,
  get_time, memory save/recall/search/list, todos, shell, file
  read/write/edit/list/search, skill save/read/list/remove/install/sync,
  feedback/improvement/reflect, self_test.
- **Skills from anywhere**: `skill_install` pulls a single skill from a URL
  (any GitHub blob URL is auto-rewritten to `raw.githubusercontent.com`) or a
  local `.md`; `skill_sync_repo` clones a whole skills git repo (permission-
  gated) and imports every `.md` with folder names as categories. Try it in
  chat ("install this repo") or with `/skills install <url>` /
  `/skills sync <repo>`.
- **Tiered tool selection**: by default the model only sees a curated 19-tool
  CORE set (computation, web, memory, todos, shell, files) — not all 36 at once, so
  it mis-picks tools less and answers faster. Advanced meta-tools (skills,
  self-improvement, self_test, reflect) stay one keystroke away:
  `/tools full` advertises everything, `/tools core` reverts, and any advanced
  tool call auto-enables full mode so work never breaks. The choice persists in
  `.alvaagent/config.json` (`tool_mode`).
- **Resilient by default**: automatic retry with backoff on flaky mobile
  connections (transient HTTP 408/409/429/5xx, timeouts, dropped links), a
  stall watchdog for streams, a `/redo` command to replay the last turn, a
  15-minute per-turn budget, and a circuit breaker that stops the agent after
  repeated tool failures instead of burning through the whole step budget.
- **Observable**: every agent turn and tool call (name, status, latency) is
  logged to `.alvaagent/trace.log` (JSON-lines, auto-capped) — view it with
  `/trace [n]` or `cat .alvaagent/trace.log`. Failed tools also return a
  recovery hint to the agent so it can switch strategy.
- **Provider profiles**: point at any OpenAI-compatible endpoint. Each
  `/provider` is a saved named profile, switchable anytime.
- **Sessions** persist to `store.json` (auto-pruned to the newest 30);
  **context auto-compression** keeps long chats inside the model window.
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
# or:
python3 -m alvaagent
```

That's it — no `pip install`, no virtualenv. The package is self-contained.

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

Slash commands: `/help /config /provider /models /test /tools /trace /todos
/todo /memory /skin /sessions /session /new /clear /context /compress /multi
/export /stop /exit`. `Ctrl+C` cancels a running request; Tab completes slash
commands. `/tools` lists what the model can currently see (and `/tools full` /
`/tools core` switch the advertised set); `/trace [n]` shows the last n agent
turns.
**Command history persists across restarts** (up-arrow recalls previous inputs) —
stored in `.alvaagent/cmd_history.txt`.

## Tests
```bash
python3 test_tui.py      # headless validation against mock_llm_server.py
```
All checks should print `ALL TESTS PASSED ✓` (includes security regression
tests for the command classifier and atomic store writes).

## Security model
- Headless runs default to **deny** for risky/mutating commands; the TUI prompts
  `allow? [y/N/a]` for anything dangerous.
- **Anti-nagging**: an approved action is remembered for the rest of the
  session (exact-match, in-memory only — `y` or `a`) so the agent can repeat it
  without re-asking. Nothing is ever permanently approved; a restart resets the
  cache.
- The read-only allowlist covers everyday inspection (`ls/cat/ps/sort/diff/...`),
  the dev loop (`python3 -m pyflakes`, `python3 test_tui.py`,
  `python3 -m py_compile`), git read commands, and archive *listing*
  (`unzip -l`, `tar -tf`) — while execution (`python3 -c`, `pip`, `sh -c`,
  extraction, chaining, substitution) always stays gated.
- Blocked without approval: command substitution (`$()`, backticks), env-prefixed
  risky tokens (`env X=1 rm -rf /`), destructive `find` flags (`-delete`,
  `-exec`), and reading files outside the project folder.
- All persistence (store, config, skills, files) uses atomic temp-file + rename,
  so a kill mid-write can't corrupt your data.

## Files
- `alvaagent/` — the app (Python stdlib only), split into focused modules:
  - `context.py` — the `Runtime` context object (data dir, config, store,
    approvals, cancel flag, UI hooks) + `build_runtime()`
  - `config.py` / `store.py` / `permissions.py` / `sessions.py` / `trace.py`
    — persistence, provider profiles, and the security classifier
  - `client.py` / `agent.py` — OpenAI-compatible client + agent loop
  - `skills.py` / `tools.py` — skill management and the 30 tool implementations
  - `tui.py` / `commands.py` / `repl.py` — rendering, slash commands, REPL
  - `util.py` — shared helpers
- `alvaagent_tui.py` — one-line launcher shim (historical entry point)
- `mock_llm_server.py` — offline OpenAI-compatible mock for the test suite
- `test_tui.py` — headless test suite (run it to verify your build)
- `start.sh` — `bash start.sh tui` launches the terminal client
- `.alvaagent/` — runtime config + store (gitignored, regenerated on first run)
- `.alvaagent/trace.log` — agent trace (JSON-lines, auto-capped, gitignored via `*.log`)

## Updating
```bash
git pull        # pull the latest from this repo
alvaagent       # run — the symlink always points at the updated code
```
Your local `.alvaagent/` data is never touched by a pull. Because the launcher
is a symlink, you never re-run the `ln` command after a pull.
