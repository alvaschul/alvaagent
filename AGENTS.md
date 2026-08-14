# AGENTS.md

Python package (`alvaagent/`) for Termux (Android), with a one-file shim
(`alvaagent_tui.py`) keeping the historical entry points working. The whole
app is Python stdlib only (`rich` is optional and gracefully skipped). All
state lives on a `Runtime` context object (`alvaagent/context.py`); module
globals are banned except `tui.py`'s current-skin (`CUR_SKIN`).

## Commands
- **Run all tests**: `python3 test_tui.py`. Not pytest — it's a flat script
  that runs every check sequentially, spawns `mock_llm_server.py` (port 8210)
  itself, and uses per-test temp data dirs. Success = `ALL TESTS PASSED ✓`.
- **In-app checks**: `/self-test` (offline, no network) and `/test` (hits the
  active provider's `/models`).
- **Launch**: `python3 alvaagent_tui.py`, `python3 -m alvaagent`,
  `bash start.sh tui`, or the `alvaagent` bash launcher.

## Python quirk (this box)
- `python3` = `/usr/bin/python3` (3.12, **no pip, no rich**) — this is what runs
  the tests.
- `/data/data/com.termux/files/usr/bin/python3` is the Termux 3.14 build with
  pip. Lints only work there:
  `/data/data/com.termux/files/usr/bin/python3 -m pyflakes alvaagent test_tui.py`
  (the `alvaagent/__init__.py` `imported but unused` re-export lines are
  intentional and sanctioned).

## Architecture
- **Module map** (all in `alvaagent/`, downward dependencies only):
  `context.py` (Runtime + `build_runtime`) ← config, store, permissions,
  sessions, trace ← client, skills, tools ← agent ← tui, commands ← repl ←
  `__init__.py` (plain re-export facade). `util.py` shared helpers.
- **Runtime context object**: every leaf function takes `rt` first —
  `run_agent(rt, history_json)`, `dispatch_tool(rt, name, args)`,
  `send_message(rt, text)`, `trace(rt, entry)`, `store_get(rt, key)`,
  `skill_list(rt)`, `request_permission(rt, desc)`, `visible(rt)`,
  `set_active_skin(rt)`, `cmd_* (rt, ...)`. The `Runtime` holds the data dir,
  config, store, tool mode, approved set, cancel flag, and UI hooks.
- **New tool** = a `Tools` method in `tools.py` + a schema entry in `TOOLS`
  (`dispatch_tool(rt, name, args)` dispatches via `getattr(Tools(rt), name)`).
  Keep everything stdlib-only.
- **Streaming** (`client.py` `chat_completion_stream`, ~:181): gateways may
  ignore `stream:true` and return plain JSON — the fallback must parse the raw
  bytes body (minified or pretty). The incremental UTF-8 decoder is
  **bytes-based**: never flush it with a `str` and never feed it `str`. Test
  fakes (`_FakeResp`) return `bytes` — keep it that way.
- **Session model** (`repl.py` `send_message`): appends the user message once;
  failed or empty turns MUST remove it again (and any trailing empty assistant
  message). Persisting an unanswered user message caused a real ghost-duplicate
  bug.
- **Turn safety** (`agent.py`): `run_agent` / `run_agent_stream` must keep the
  two runaway guards — the wall-clock `_TURN_TIMEOUT` check and the
  `_MAX_CONSEC_TOOL_FAILURES` circuit breaker (stop early with a clear message,
  never keep calling the API). `dispatch_tool` MUST attach a `hint` to failed
  tool results (`_TOOL_ERROR_HINTS` in `tools.py`). Trace every turn/tool event
  via `trace(rt, {...})` (JSON-lines to trace.log in the runtime data dir) —
  keep it best-effort (never raises).

## Persistence (`.alvaagent/`)
- `store.json` (sessions/memory/todos; keys namespaced `alvaagent.*`),
  `config.json` (provider profiles incl. API keys), `cmd_history.txt` (readline
  history), `trace.log` (JSON-lines agent trace, capped). Override the folder
  with `ALVA_DATA_DIR` (or `POCKET_DATA_DIR`).
- Managed by `alvaagent/store.py`, `alvaagent/config.py` (both write
  atomically — temp file + rename). The file paths derive from the runtime's
  `data_dir`; per-test runtimes use temp dirs.
- Those files are gitignored, but **`.alvaagent/skills/` IS tracked** —
  committed skills are the repo's default skills. Legacy store keys (plain
  `sessions`, `active_session`) are ignored by the namespaced reader; an old
  store can look "lost".

## Skills convention
- Each skill is a `.md` with YAML frontmatter that MUST include a `name:`.
  Categorized skills live in `category/SKILL.md`; `_resolve_skill_path`
  (`alvaagent/skills.py`) maps a frontmatter `name` or `category/name` to a
  path and `_inside_skills` blocks traversal outside the skills dir.

## Self-modification hygiene (leave nothing behind)
When you improve / modify / test your own system (`alvaagent/`, `test_tui.py`,
`mock_llm_server.py`, README, `start.sh`, skills):

1. **Edit in place, never copy-then-patch** — no `.orig`, `.bak`, `_backup`, or
   duplicate source files. If a backup is unavoidable, put it in `/tmp`.
2. **Tests clean up after themselves** — every skill, todo, memory fact, and
   scratch file a test creates must be removed (use `try/finally`). Never leave
   `proj-demo.txt`-style artifacts or test skills behind.
3. **Scratch work goes to `/tmp`** (or `tempfile.mkdtemp`), never into the repo
   or `.alvaagent/`. Downloaded/cloned skill material is wiped after importing.
4. **Finish with a clean tree** — `git status` must show ONLY the intended
   changes; delete stray untracked files (draft skills, exploratory scripts,
   dumps). Commit specific files with `git add <file>`, never `git add -A`.
5. **Don't commit session/runtime junk** — `.opencode/`, `opencode.json`,
   `.alvaagent/config.json`, `store.json`, `cmd_history.txt`, and `*.log` are
   gitignored; keep them out. API keys in config.json must never be committed.
6. **New reusable skills/scripts are optional, not automatic** — ask the user
   before committing anything you created only to explore or test.

## Git
- Remote `https://github.com/alvaschul/alvaagent.git`, branch `main`. Auth via the
  gh CLI credential helper (`gh auth login` + `gh auth setup-git`) — no tokens
  in URLs. Plain `git push origin main` just works.
