# Runtime Context Object — Design (Phase A + B)

Date: 2026-08-14. Replaces/refines the Task 14-15 sections of
`2026-08-14-package-rearchitecture-design.md` and its plan. This is the
authoritative design for retiring the package's process-global mutable state.

Status: brainstormed with user; approved section-by-section.

## 1. Problem

Tasks 1-13 split `alvaagent_tui.py` into a `alvaagent/` package behind a
`_Facade` proxy that forwards reads to the `alvaagent_tui` shim and forwards
writes to every loaded `alvaagent.*` submodule exposing a name. The remaining
mutable process globals are the state the app actually runs on:

| Global | Home | Becomes |
|---|---|---|
| `_store` (loaded at import) | store.py | `rt.store` |
| `_APPROVED_SET` | permissions.py | `rt.approved` |
| `ON_PERMISSION` | permissions.py | `rt.on_permission` |
| `_TOOLS_MODE` | tools.py | `rt.tool_mode` |
| `ON_TOOL` | agent.py | `rt.on_tool` |
| `_cancel_flag` (a `[False]` list) | util.py | `rt.cancel` (`threading.Event`) |
| `_UI["spinner"]` | tui.py | `rt.spinner` |
| `CUR_SKIN` / `COLOR` | tui.py | `rt.skin` |
| `_last_turn` | repl.py | `rt.last_turn` |
| `active_cfg` | config.py | `rt.active_cfg` (property) |
| `TRACE_PATH` (from data_dir) | config.py | derived from `rt.data_dir` inside trace |
| `SKILLS_DIR` (from data_dir) | skills.py | `rt.skills_dir` (property) |

These globals are what make the package untestable in isolation and what the
`_Facade` write-through proxy exists to keep patching. Retiring them is the
point of Phase B of the whole refactor.

## 2. Runtime model (`alvaagent/context.py`)

```python
"""Runtime context object — replaces the single-file module globals."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import threading
from typing import Callable, Optional

@dataclass
class Runtime:
    data_dir: str
    cfg: dict = field(default_factory=dict)
    store: dict = field(default_factory=dict)
    tool_mode: str = "core"
    approved: set = field(default_factory=set)
    cancel: threading.Event = field(default_factory=threading.Event)
    on_permission: Optional[Callable] = None
    on_tool: Optional[Callable] = None
    spinner: object = None
    skin: str = "midnight"
    session: str = "default"
    history: list = field(default_factory=list)
    last_turn: dict = field(default_factory=dict)

    @property
    def active_cfg(self) -> dict:
        return self.cfg["profiles"][self.cfg["active"]]

    @property
    def skills_dir(self) -> str:
        return os.path.join(self.data_dir, "skills")
```

`build_runtime(data_dir=None)` (in context.py):

```python
def build_runtime(data_dir=None):
    import alvaagent.config as config
    import alvaagent.store as store
    if data_dir is None:
        data_dir = config.data_dir()
    rt = Runtime(data_dir=data_dir)
    rt.cfg = config.load_state()
    rt.tool_mode = rt.cfg.get("tool_mode", "core")
    rt.skin = rt.cfg.get("skin", "midnight")
    store.load(rt)
    return rt
```

### Threading rule

The app creates ONE runtime in `repl.main()` and threads it explicitly:

```python
def main():
    rt = build_runtime()
    rt.on_permission = ask_permission
    rt.on_tool = on_tool
    set_active_skin(rt)
    # ... existing signal + alternate-screen handling verbatim ...
    banner(rt)          # banner reads rt.skin
    repl(rt)
```

`repl(rt)` → `send_message(rt, text)` → `run_agent(rt, messages)` /
`cmd_*(rt, ...)` → client / sessions / tools / tui / trace / permissions /
store / config. The app path never touches the facade or any process global.

### What stays module-level

- Immutable constants: `_TURN_TIMEOUT`, `_MAX_CONSEC_TOOL_FAILURES`,
  `_PY_RUN_TIMEOUT`, `_PY_MAX_BYTES`, `_PY_MAX_CHARS`, `_MAX_RETRIES`,
  `_RETRY_BACKOFF`, `_STREAM_IDLE_LIMIT`, `_STREAM_POLL`, `SKINS`, `TOOLS`,
  `C`, `_CALC_ALLOWED`, `_READONLY_PREFIXES`, `_RISKY_TOKENS`,
  `_RISKY_OPERATORS`, `_TOOL_ERROR_HINTS`, `_MD_STYLE`, `_ANSI_RE`,
  `HISTORY_PATH`, `MAX_SESSIONS`, `ACTIVE_SESSION_KEY`, `DEFAULT_CONTEXT_WINDOW`,
  etc.
- Patchable DI seams (tests inject by qualified name in Phase B, or via the
  facade write-through in Phase A): `util._raw_fetch`, `client._sleep_retry`.

### Module API changes (rt-first)

- store: `load(rt)`, `save(rt)`, `get(rt, key, default=None)`,
  `set(rt, key, value)`. Drop import-time `_load_store()` and the `_store`
  global.
- config: `load_state()` unchanged (pure), `save_state(rt)`,
  `active_cfg(rt)`.
- permissions: `request_permission(rt, action, ok=True, hint="")` (was
  `_permission(action, ok, hint)`), `classify_file_action(rt, path, mode)`.
  Reads `rt.approved` / `rt.on_permission`.
- skills: `skill_list(rt)`, `skill_read(rt, name)`, `skill_save(rt, name,
  content, category=None)`, `skill_remove(rt, name)`, `skill_install(rt,
  source, category=None)`, `skill_sync_repo(rt, repo, subdir=None)`. Paths
  derive from `rt.skills_dir`.
- tools: `class Tools: def __init__(self, rt): self.rt = rt` with one method
  per tool (named exactly the TOOLS schema name, `args` = full params dict);
  `dispatch_tool(rt, name, args)`, `visible(rt)`, `set_mode(rt, mode)`,
  `maybe_enable_full(rt, name)`, `self_test(rt)`. Delete `TOOL_IMPL` dict and
  `_TOOLS_MODE`. The existing `tool_*` logic becomes private functions
  `(rt, ...)` called by the `Tools` methods.
- client: `chat_completion(rt, messages, **kw)`,
  `chat_completion_stream(rt, messages, **kw)`, `fetch_models(rt, ...)`,
  `cancel_agent(rt)` → `rt.cancel.set()`; streams check `rt.cancel.is_set()`.
- agent: `run_agent(rt, messages)`, `run_agent_stream(rt, messages)`; agent
  calls `rt.on_tool` if set.
- sessions: `sessions_map(rt)`, `load_session(rt)`, `save_session(rt)`,
  `delete_session(rt, name)`, `rename_session(rt, old, new)` (was
  `_rename_session_in_store`), `find_session(rt, name)`,
  `unique_session_name(rt, name)`, `context_usage(rt, history)`,
  `compress_now(rt, threshold=None)`, `trim_history(rt, history)`,
  `auto_title(rt, text, history)`, `new_session_name(rt)`, `summarize(rt, ...)`.
- trace: `trace(rt, **event)`, `read_trace(rt, n)`, `trace_count(rt)`.
- tui: `set_active_skin(rt)`, `run_agent_tui(rt)`, `render_status_bar(rt,
  session, elapsed, tools, history)`, `print_user_turn(rt, ...)`; tool
  open/close operate on `rt.spinner`; color/skin helpers read `rt.skin`.
  `on_tool` hook lives on rt (agent calls it); tui's spinner render stays.
- commands: `cmd_*(rt, ...)` — the old `state`/`history`/`session` params
  come from rt.
- repl: `send_message(rt, text) -> str` (returns session name), `repl(rt)`,
  `main()`. `/redo` reads `rt.last_turn`.

## 3. Phase A — the bridge (behavior-preserving threading)

Goal: all threading + global retirement lands while the ORIGINAL 249-check
suite (`test_tui.py`) still passes essentially unchanged. The suite is the
machine proof that nothing changed.

### 3.1 Default runtime behind the facade

`alvaagent/__init__.py` gains a lazily-initialized module-level default
runtime:

```python
_rt = None
def _get_rt():
    global _rt
    if _rt is None:
        _rt = build_runtime()   # honors ALVA_DATA_DIR via config.data_dir()
    return _rt
```

This reproduces today's import-time process globals (store loaded from the env
data dir) inside one object. Used only by the compat layer; the app path does
not use it.

### 3.2 Retired-state accessors on `_Facade`

The `_Facade` proxy class and its write-through stay intact in Phase A (they
still deliver patched *module functions*: `pa.save_session = lambda …` →
`sessions.save_session`, `pa._sleep_retry` → client, `pa._raw_fetch` → util,
`pa._TURN_TIMEOUT` → agent, etc.). For the handful of names whose backing
global is gone, add explicit accessors over `_get_rt()`:

- `pa._store` → property → `_get_rt().store` (item mutation goes to the dict)
- `pa._store_get(k)` / `pa._save_store()` → `store.get(_get_rt(), k)` /
  `store.save(_get_rt())`
- `pa._APPROVED_SET` → property → `_get_rt().approved` (`.clear()` works)
- `pa._permission(desc)` → `request_permission(_get_rt(), desc)`
- `pa.active_cfg` → getter/setter → `_get_rt().cfg` active profile (test
  monkeypatches it at test_tui.py:700-707)

Read-forward order means the proxy tries the shim first, then falls back to
these explicit facade members — no interference.

### 3.3 Old-signature flat adapters

The suite calls flat entry points with the OLD signatures. Add thin adapters
that build the rt from the args and call the rt-based function:

- `tool_*` (all of them) → build rt, call the rt-based tool
- `send_message(text, history, state, session)` → set
  `rt.history/rt.cfg/rt.session` from the args, call `repl.send_message(rt, text)`
- `run_agent(json_hist, json_cfg)` / `run_agent_stream(messages, cfg_s)` →
  parse, set rt, call rt-based
- `chat_completion(messages, **kw)` / `chat_completion_stream(...)`
- `fetch_models(base, key)`
- `self_test()`
- `cmd_provider(state, args)` (test uses the old state-dict signature)
- `cmd_trace(n)`

### 3.4 Surgical test edits (Phase A only)

- The tiered-tool block (test_tui.py ~1133-1160) reads/writes
  `_tools._TOOLS_MODE` directly; `_TOOLS_MODE` no longer exists. Rewrite the
  block against rt: build a rt, `rt.tool_mode = "full"`, drive via
  `pa.dispatch_tool(rt, ...)`, assert `rt.tool_mode`, restore.
- Before committing, `grep` the suite for any other direct leaf-module-global
  reads that vanish (e.g. `_tools._TOOLS_MODE`) and fix them the same way.
- No other test edits are allowed in Phase A.

### 3.5 Phase A verification

- Original suite green ×2 (`ALL TESTS PASSED ✓`)
- Smoke probes: both import orders, `pa.main` entry via shim, write-through
  still reaches rt-backed functions
- `python3 alvaagent_tui.py` and `python3 -m alvaagent` start cleanly
- pyflakes: no new findings (existing sanctioned F401 re-export noise only)
- `grep -rn '^_store\b\|^_TOOLS_MODE\|^_APPROVED_SET\|^_cancel_flag\|^ON_PERMISSION\|^ON_TOOL\|^_last_turn' alvaagent/*.py`
  → no hits in leaf modules (facade adapters excluded from the check)

## 4. Phase B — suite migration and retirement

### 4.1 Suite becomes pytest-style functions

- Convert `test_tui.py` to `test_*` functions with per-test isolation:
  - `mkrt(data_dir=None)` builder (wrapper over `pa.build_runtime`)
  - each test gets a fresh rt and, where isolation matters, a fresh temp DATA
    dir (no shared `_store`/`approved`/`tool_mode` between tests)
  - mock server (port 8210) as a session-scoped fixture: start once, stop once
  - bundled zero-dependency runner: `if __name__ == "__main__":` discovers
    `test_*` functions, runs each with fresh state, prints `ok/fail` lines and
    a pass/fail summary — `python3 test_tui.py` keeps working exactly as the
    verification loop expects
  - pytest-collection-compatible: functions named `test_*`, no global side
    effects at import time
- Rewrite every flat call site to the rt API, preserving each test's intent
  and assertions 1:1:
  - `pa.Tools(rt).calculator({"expression": x})` and so on for each tool
  - `send_message(rt, text)`, `run_agent(rt, messages)`,
    `chat_completion(rt, messages, **kw)`, `cmd_*(rt, ...)`,
    `fetch_models(rt, ...)`, `request_permission(rt, ...)`
  - state assertions → `rt.store`, `rt.approved`, `rt.tool_mode`, `rt.cancel`
  - DI seams patched by qualified name (`client._sleep_retry = …`,
    `util._raw_fetch = …`)

### 4.2 Delete the bridge

- `_get_rt()` default runtime, all retired-state accessors, all
  old-signature adapters, the `_Facade` proxy class + generic write-through,
  and the `_Facade._tui` read-forward all go away.
- `alvaagent_tui.py` remains a shim re-exporting only the repl surface for
  `python3 alvaagent_tui.py`.
- The facade becomes a plain module re-exporting `Runtime`, `build_runtime`,
  `Tools`, `dispatch_tool`, and the rt-based module functions.

### 4.3 Architecture tests (added in Phase B)

- No-import-cycles: import every `alvaagent.*` module in sequence; must not
  raise.
- Facade surface: `Runtime`, `build_runtime`, `Tools`, `dispatch_tool`,
  `TOOLS`, `SKINS`, `AgentWriter`, `Spinner`, `chat_completion`,
  `chat_completion_stream`, `run_agent`, `run_agent_stream`,
  `classify_command`, `classify_python`, `load_session`, `main` all exist.
- No retired globals remain: assert none of `_store`, `_TOOLS_MODE`,
  `_APPROVED_SET`, `_cancel_flag`, `ON_PERMISSION`, `ON_TOOL`, `_last_turn`,
  `_UI` exist as leaf-module module-globals.

### 4.4 Phase B verification

- `python3 test_tui.py` (pytest-structured) green
- smoke probes; both import orders; app entry works
- pyflakes clean; `git grep` for retired names → only docs/comments
- reviewer ACCEPT gate as usual

## 5. Sequencing and commits

**Phase A (Task 14), committed in green-checkpoint chunks** — the original
suite must be green after every commit:

1. `context.py` + thread `store` / `config` / `permissions`
2. thread `skills` / `tools` (Tools class, dispatch_tool, visible/set_mode/
   maybe_enable_full/self_test)
3. thread `client` / `agent` / `sessions` / `trace`
4. thread `tui` / `commands` / `repl`; `main()` = build_runtime + hooks +
   `repl(rt)`
5. facade bridge (default rt + accessors + adapters) + surgical test edits

Each checkpoint: suite green, app entry starts, pyflakes. After Phase A:
reviewer ACCEPT gate, ledger update, STOP for user go-ahead.

**Phase B (Task 15, after explicit go-ahead):**

1. suite conversion to pytest-style functions + rt API migration
2. delete bridge/proxy; facade becomes plain module
3. architecture tests

Same rhythm. Task 16 (docs/README/launcher cleanup) unchanged.

## 6. Constraints

- Stdlib only at runtime; no new dependencies. pytest is NOT required to run
  the suite (bundled runner). If pytest is installed, the suite is
  collection-compatible.
- Mechanical moves: no body rewrites beyond threading `rt` and removing the
  retired globals; no behavior changes.
- The original suite's 249 checks are the regression contract through Phase A;
  their assertions (not their call syntax) are the contract through Phase B.
- Leaf modules import only stdlib + other `alvaagent.*` leaves; never the
  facade or `alvaagent_tui`.
- Commits are granular and each leaves the tree runnable.
