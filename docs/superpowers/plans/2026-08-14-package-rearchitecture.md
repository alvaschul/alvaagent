# alvaagent Package Re-architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the 5,212-line single-file `alvaagent_tui.py` into a Python package (`alvaagent/`) with clean module boundaries and an explicit `Runtime` context object replacing every module-level mutable global — behavior-identical, all existing tests green.

**Architecture:** Bottom-up mechanical extraction into modules (leaf modules first, so there are never import cycles), each ending in a green test run, followed by a single Runtime-threading pass that retires the globals (`_store`, `_TOOLS_MODE`, `_APPROVED_SET`, `_cancel_flag`, `ON_PERMISSION`, `ON_TOOL`, `_UI`, `CUR_SKIN`, `COLOR`, `state`/`session`/`history` params). A facade `alvaagent/__init__.py` keeps `import alvaagent as pa` working; `alvaagent_tui.py` becomes a 5-line shim so launchers keep working.

**Tech Stack:** Python 3 stdlib only (no pip, no rich required — `rich` is optional as today). Test runner is the flat script `python3 test_tui.py` (`ALL TESTS PASSED ✓` = success; no pytest).

**Spec:** `docs/superpowers/specs/2026-08-14-package-rearchitecture-design.md` — read it first; this plan argues from it.

## Global Constraints

- **Behavior-identical.** All existing `test_tui.py` checks must pass at every task checkpoint. No feature changes, no new dependencies.
- **Test command:** `python3 test_tui.py` (must end `ALL TESTS PASSED ✓`). Sanity: `/data/data/com.termux/files/usr/bin/python3 -m pyflakes <file>` for lint (the `python3` on this box is 3.12 with no pyflakes; Termux's 3.14 has pip).
- **Stdlib-only.** The `rich` import stays optional with the existing ANSI fallback; `yaml` stays optional with the existing mini-parser fallback.
- **Data layout unchanged.** `.alvaagent/` stays at the repo root (the package's parent dir), not inside the package. Config/store/skill JSON shapes unchanged.
- **Commit hygiene (AGENTS.md):** `git add <file>` per file, never `git add -A`; clean tree at end of each task; `__pycache__/`, `.alvaagent/*`, `.opencode/` are already gitignored. Commit style: `refactor: ...`.
- **Dependency rule:** modules import only from leaf-side modules, never upward, never the facade. Bidirectional needs become fields/hooks on `Runtime` or leaf helpers in `util.py`.
- **Facade rule:** modules never `import alvaagent`; only `alvaagent/__init__.py` re-exports.

## File Structure Map

```
alvaagent/
  alvaagent/
    __init__.py     facade: re-exports old flat API (tests do `import alvaagent as pa`)
    __main__.py     `python3 -m alvaagent` -> repl.main()
    context.py      Runtime dataclass + build_runtime()
    trace.py        leaf: _trace, _read_trace, _trace_count, _TRACE_MAX_LINES,
                    _TRACE_MAX_BYTES (JSON-lines agent trace, capped)
    util.py         leaf helpers: _env, now_iso, _fmt_k, _atomic_write, _raw_fetch,
                    _looks_like_html, mask_key, mini-yaml (frontmatter), yaml-optional
    config.py       leaf: data_dir, path consts, PROVIDERS, DEFAULT_CFG, FIRST_RUN_CFG,
                    SKIN_NAMES, ALVA_VERSION, MODEL_CONTEXT, TOOL_MODES, load_state,
                    save_state, active_cfg
    store.py        leaf: store.json load/save, namespaced get/set, key consts
                    (TODO_KEY, MEM_PREFIX, ..., ACTIVE_SESSION_KEY, MAX_SESSIONS)
    permissions.py  classify_command, classify_python, classify_file_action,
                    request_permission
    skills.py       frontmatter skill machinery + skill_save/read/remove/install/sync
    tools.py        TOOLS schemas, Tools class (tool methods), dispatch_tool,
                    self_test, tool-mode selection
    client.py       chat_completion, chat_completion_stream, fetch_models, retries,
                    stall watchdog, cancel
    agent.py        run_agent, run_agent_stream, _repair_tool_pairs, ON_TOOL hook
    sessions.py     sessions_map, load/save/delete/rename, context_usage,
                    estimate_tokens, auto_title, compress/compress_now, trim_history
    tui.py          SKINS, C, colors, print_user_turn, render_agent_panel,
                    render_status_bar, AgentWriter, Spinner, banner, markdown renderer
    commands.py     all cmd_* slash handlers + ask/ask_key/pick_model/ask_permission
    repl.py         setup_completion, save_completion_history, send_message, repl,
                    main, screen (alternate buffer) + signal handling
  alvaagent_tui.py  shim (after Task 13): `from alvaagent.repl import main`
  test_tui.py       modified: `import alvaagent as pa`; direct stdlib imports
  mock_llm_server.py, start.sh, alvaagent, .gitignore  (unchanged)
  docs/superpowers/specs/2026-08-14-package-rearchitecture-design.md
```

Dependency direction: `util` → `config` → `trace` → `store` → `permissions`/`skills` → `tools` → `client`/`agent`/`sessions` → `tui` → `commands` → `repl`.

---

## Phase A — Mechanical extraction (behavior-identical)

### Task 1: Package scaffold + facade + test import swap

**Files:**
- Create: `alvaagent/__init__.py`
- Create: `alvaagent/__main__.py`
- Create: `alvaagent/context.py` (placeholder — real Runtime lands in Task 14)
- Modify: `test_tui.py` (import line only)

**Interfaces:**
- Consumes: the still-full `alvaagent_tui.py` (nothing extracted yet).
- Produces: `alvaagent` importable as `pa` with the full old symbol surface, so the test suite passes with a one-line import change.

- [ ] **Step 1: Create the facade**

`alvaagent/__init__.py` — re-export everything from the single file, including the underscore names the tests touch:

```python
"""alvaagent — on-device AI agent harness for Termux (stdlib-only).

Package layout replaces the original single-file alvaagent_tui.py. This
facade re-exports the old flat API so `import alvaagent as pa` behaves like
the original module.
"""
from alvaagent_tui import *  # noqa: F401,F403
from alvaagent_tui import (  # noqa: F401
    _store, _TOOLS_MODE, _APPROVED_SET, _cancel_flag,
    ON_PERMISSION, ON_TOOL, _UI, CUR_SKIN, COLOR,
    _atomic_write, _find_session, _fmt_k, _looks_like_html, _md_line,
    _normalize_state, _parse_xml_tool_calls, _permission, _raw_fetch,
    _read_trace, _save_store, _sleep_retry, _store_get, _strip_xml,
    _trace_count, _unique_session_name, signal, subprocess, urllib, time, yaml,
)

# The single file's functions read module globals (ON_PERMISSION, _TOOLS_MODE,
# _raw_fetch, ...). The test suite monkeypatches them through `pa.<name> = ...`.
# As the mechanical split moves readers into alvaagent.* submodules, a write to
# the facade must land in every loaded module that exposes the name (the
# def-owner plus any module that imported it by name). Reads forward to
# alvaagent_tui, which re-imports the full surface until Task 13.
import sys as _sys, types as _types


class _Facade(_types.ModuleType):
    _tui = None

    def __getattribute__(self, name):
        if name.startswith("__") and name.endswith("__"):
            return super().__getattribute__(name)
        return getattr(_Facade._tui, name)

    def __setattr__(self, name, value):
        if name.startswith("__") and name.endswith("__"):
            super().__setattr__(name, value)
            return
        super().__setattr__(name, value)
        setattr(_Facade._tui, name, value)
        for _mname, _mod in _sys.modules.items():
            if _mname.startswith("alvaagent.") and hasattr(_mod, name):
                setattr(_mod, name, value)


_Facade._tui = _sys.modules["alvaagent_tui"]
_sys.modules[__name__].__class__ = _Facade
```

**Why the proxy (Ruling 3):** the plan originally specified a plain re-export, but `test_tui.py` monkeypatches module globals via `pa.<name> = ...` (~35 sites) and the single file's functions read those globals from `alvaagent_tui`'s own namespace — a plain re-export breaks every patch. The proxy forwards reads to `alvaagent_tui` (which re-imports the whole surface after every extraction task) and forwards writes to `alvaagent_tui` **plus every loaded `alvaagent.*` submodule that currently exposes the name**. This keeps all existing patch sites working unchanged as readers move: e.g. `pa.ON_PERMISSION = ...` lands on `permissions.ON_PERMISSION` from Task 5 on, `pa._TOOLS_MODE = ...` lands on `tools._TOOLS_MODE` from Task 7 on, `pa.dispatch_tool`/`pa.chat_completion`/`pa._TURN_TIMEOUT` land on `agent`'s imported bindings from Task 10 on, and the Task-13 `repl` imports are patched too. `pa.urllib.request.urlopen = ...` patches the shared stdlib module in place and needs no help.

**Known residual gap (handled in Task 7):** reads of names that code mutates *internally* (not via a facade write) are stale — `tools._set_tool_mode`/`_maybe_enable_full` mutate `tools._TOOLS_MODE` directly, so `pa._TOOLS_MODE` (forwarded to `alvaagent_tui`) won't see it. Task 7 redirects the tiered-tool-selection test block to `_tools._TOOLS_MODE`.

**The proxy is temporary:** delete it when Task 13 rewrites the facade to stop importing `alvaagent_tui` (Task 13 Step 4 does this explicitly).

- [ ] **Step 2: Create `__main__.py`**

```python
from alvaagent_tui import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Create the placeholder `context.py`**

```python
"""Runtime context object — placeholder during the mechanical split.

The real Runtime lands in the Runtime-threading phase; until then the
modules keep using the single file's module globals.
"""

class Runtime:  # noqa: D101 - replaced in Task 14
    def __init__(self, data_dir=None):
        self.data_dir = data_dir
```

- [ ] **Step 4: Swap the test import**

In `test_tui.py`, change line ~26 from:

```python
import alvaagent_tui as pa  # noqa: E402
```

to:

```python
import alvaagent as pa  # noqa: E402
```

Keep the `# noqa: E402` comment.

- [ ] **Step 5: Run the tests**

Run: `python3 test_tui.py`
Expected: `ALL TESTS PASSED ✓` (all existing checks, unchanged).

- [ ] **Step 6: Commit**

```bash
git add alvaagent/__init__.py alvaagent/__main__.py alvaagent/context.py test_tui.py
git commit -m "refactor: scaffold alvaagent package with facade re-exporting the single file"
```

---

### Task 2: Extract `util.py` (leaf helpers)

**Files:**
- Create: `alvaagent/util.py`
- Modify: `alvaagent_tui.py` (delete helpers, add import)
- Modify: `alvaagent/__init__.py` (re-export from util)

**Interfaces:**
- Consumes: nothing but stdlib + optional `yaml`.
- Produces (used by every later module):
  - `_env(*names) -> str|None`
  - `now_iso() -> str`
  - `_fmt_k(n) -> str`
  - `_atomic_write(path, text, mode="w")` (raises on failure)
  - `_raw_fetch(url) -> str|None` (None on network failure / HTML-looking body)
  - `_looks_like_html(text) -> bool`
  - `mask_key(key) -> str`
  - `_parse_frontmatter(text) -> (dict, str)` / `_frontmatter_load(raw) -> dict` / `_frontmatter_dump(fm) -> str` / `_mini_yaml(text)` / `_mini_scalar(v)` / `_finish_block(kind, lines, chomp)`
  - module-level `yaml` (optional import, falls back to `None`)

- [ ] **Step 1: Create `alvaagent/util.py`**

Copy these functions verbatim from `alvaagent_tui.py` (locate by name — line numbers shift as the file shrinks):

- `_env` (lines ~118-123)
- `_fmt_k` (~2896)
- `now_iso` (~2902)
- `_atomic_write` (~1016)
- `_looks_like_html` (~1083)
- `_raw_fetch` (~1089)
- `mask_key` (~3796)
- `_mini_scalar` (~671), `_finish_block` (~691), `_mini_yaml` (~705), `_frontmatter_load` (~756), `_frontmatter_dump` (~772), `_parse_frontmatter` (~798)

Add these imports to `util.py` (the ones each helper needs — copy from the single file's top):

```python
import html
import json
import os
import re
import urllib.error
import urllib.request

try:
    import yaml
except Exception:
    yaml = None
```

Keep every function body byte-identical. Keep their docstrings.

- [ ] **Step 2: Delete the helpers from `alvaagent_tui.py` and import them**

Delete each moved function from `alvaagent_tui.py`. At the top of the file (right after the `try: from rich...` block, line ~111) add:

```python
from alvaagent.util import (  # noqa: E402,F401
    _env, now_iso, _fmt_k, _atomic_write, _looks_like_html, _raw_fetch,
    mask_key, _parse_frontmatter, _frontmatter_load, _frontmatter_dump,
    _mini_yaml, _mini_scalar, _finish_block,
)
```

Remove now-unused imports from `alvaagent_tui.py`'s top only if the rest of the file no longer uses them (e.g. `urllib.error`/`urllib.request`/`html` — verify with a grep first; the file is still large so most stay).

- [ ] **Step 3: Re-export from the facade**

Add to `alvaagent/__init__.py` (after the existing `from alvaagent_tui import ...`):

```python
from alvaagent.util import (  # noqa: F401
    _env, now_iso, _fmt_k, _atomic_write, _looks_like_html, _raw_fetch,
    mask_key, _parse_frontmatter, _frontmatter_load, _frontmatter_dump,
    _mini_yaml, _mini_scalar, _finish_block,
)
```

The `alvaagent_tui` import of these names is still in the facade, but keep the explicit re-export so the facade stops depending on them once `alvaagent_tui` thins out.

- [ ] **Step 4: Run the tests**

Run: `python3 test_tui.py` — Expected: `ALL TESTS PASSED ✓`

- [ ] **Step 5: Commit**

```bash
git add alvaagent/util.py alvaagent/__init__.py alvaagent_tui.py
git commit -m "refactor: extract util.py (env, atomic write, fetch, mini-yaml, fmt helpers)"
```

---

### Task 3: Extract `config.py` (paths, profiles, state)

**Files:**
- Create: `alvaagent/config.py`
- Modify: `alvaagent_tui.py` (delete config section, add import + tool-mode sync)
- Modify: `alvaagent/__init__.py`

**Interfaces:**
- Consumes: `util._env`.
- Produces:
  - `data_dir() -> str` (env override or `<repo root>/.alvaagent`)
  - path constants: `CONFIG_PATH`, `STORE_PATH`, `HISTORY_PATH`, `TRACE_PATH` (module-level, derived from `data_dir()`)
  - `PROVIDERS`, `DEFAULT_CFG`, `FIRST_RUN_CFG`, `DEFAULT_SKIN`, `SKIN_NAMES`, `ALVA_VERSION`, `DEFAULT_CONTEXT_WINDOW`, `MODEL_CONTEXT`
  - `TOOL_MODES = ("core", "full")` — single source of truth from now on
  - `_tool_mode_of(raw) -> str` (validates against `TOOL_MODES`)
  - `_skin_of(raw) -> str`
  - `_normalize_state(raw) -> dict` (now validates tool_mode inline via `TOOL_MODES` — **no longer calls the tools-section `_tool_mode_of`**)
  - `load_state() -> dict` (env overrides; **does not touch the `_TOOLS_MODE` global anymore**)
  - `save_state(state)` (atomic, unchanged body)
  - `active_cfg(state) -> dict`

- [ ] **Step 1: Create `alvaagent/config.py`**

Move from `alvaagent_tui.py` (locate by name):

- `DATA_DIR` computation → a `data_dir()` function. **Important change:** the original is `os.path.join(os.path.dirname(os.path.abspath(__file__)), ".alvaagent")`. The package lives in `alvaagent/`, so the repo root is one level up:

```python
def data_dir():
    return (_env("ALVA_DATA_DIR", "POCKET_DATA_DIR")
            or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".alvaagent"))
```

- `_LEGACY_DIRS` (lines ~130-133), `CONFIG_PATH`, `STORE_PATH`, `HISTORY_PATH`, `TRACE_PATH` (derive from `data_dir()` at module import — same as today).
- **`DATA_DIR` module constant** `DATA_DIR = data_dir()` — remaining `alvaagent_tui.py` code references `DATA_DIR` at 10+ sites (SKILLS_DIR ~365, `_inside_skills` boundary ~476, self-test ~2535, export ~4426, status bar ~4628) and must import it back. `_LEGACY_DIRS` needs the **two-level-up `__file__` correction** (original `__file__` was the repo root; in config.py it is the package dir).
- `PROVIDERS`, `DEFAULT_CFG`, `FIRST_RUN_CFG`, `DEFAULT_SKIN`, `SKIN_NAMES`, `ALVA_VERSION`, `DEFAULT_CONTEXT_WINDOW`, `MODEL_CONTEXT` (lines ~139-181, verbatim).
- `TOOL_MODES = ("core", "full")` (move the tuple; **delete it from the tools section of `alvaagent_tui.py` now** — it is the private `_TOOL_MODES` in tui, renamed `TOOL_MODES` here; the two remaining tui references in `_set_tool_mode` (~1643) and the `/tools` command (~4803) must be updated to `TOOL_MODES`).
- `_tool_mode_of` (verbatim, but validate against `TOOL_MODES`).
- `_skin_of` (verbatim).
- `_normalize_state` (verbatim EXCEPT: replace both `_tool_mode_of(raw)` calls with the config-local `_tool_mode_of(raw)` — it's in the same module now).
- `load_state` (verbatim EXCEPT: **remove the `global _TOOLS_MODE` / `_TOOLS_MODE = state.get(...)` lines** — the mode is synced by the REPL instead, see Step 3).
- `save_state`, `active_cfg` (verbatim).

Header imports:

```python
import json
import os

from alvaagent.util import _env
```

- [ ] **Step 2: Delete the section from `alvaagent_tui.py` and import**

Delete the whole paths/config block (functions + constants above) from `alvaagent_tui.py`. Where the section was, keep the marker comment and add:

```python
# paths / config moved to alvaagent/config.py (Task 3)
from alvaagent.config import (  # noqa: E402,F401
    data_dir, DATA_DIR, _LEGACY_DIRS, CONFIG_PATH, STORE_PATH, HISTORY_PATH,
    TRACE_PATH, PROVIDERS, DEFAULT_CFG, FIRST_RUN_CFG, DEFAULT_SKIN,
    SKIN_NAMES, ALVA_VERSION, DEFAULT_CONTEXT_WINDOW, MODEL_CONTEXT,
    TOOL_MODES, _tool_mode_of, _skin_of, _normalize_state, load_state,
    save_state, active_cfg,
)
```

- [ ] **Step 3: Sync tool mode at the two REPL call sites**

`load_state()` no longer sets the `_TOOLS_MODE` global. In `alvaagent_tui.py`, in the tools section, add a sync helper right after the `_TOOLS_MODE = "core"` line:

```python
def _sync_tool_mode(state):
    """Restore the persisted tool mode after config load (no cycle: config is a leaf)."""
    global _TOOLS_MODE
    _TOOLS_MODE = state.get("tool_mode", "core")
```

Then update BOTH call sites in `alvaagent_tui.py` — `repl()` (`state = load_state()` ~line 4990) and `main()` (~5161):

```python
    state = load_state()
    _sync_tool_mode(state)
```

(These two sites are rewritten in Task 13 and the Runtime phase; this keeps behavior identical in between.)

- [ ] **Step 4: Create `alvaagent/trace.py` (Ruling 1)**

`_trace` is **called from the tools section** (`_maybe_enable_full`, `_set_tool_mode`) but **defined in the agent section** — if `tools.py` imports `_trace` from `agent.py` we get a `tools ↔ agent` cycle. Fix by extracting the trace helpers to a leaf module now (they only need `json`/`os`/`TRACE_PATH`).

Move from `alvaagent_tui.py` lines ~2351-2393, verbatim, into `alvaagent/trace.py`:

```python
"""JSON-lines agent trace (trace.log) — leaf module (imports config, util only)."""
import json
import os

from alvaagent.config import TRACE_PATH

_TRACE_MAX_LINES = 2000      # cap for trace.log
_TRACE_MAX_BYTES = 1_000_000  # cap before trace.log is trimmed back


def _trace(entry):
    ...   # verbatim lines 2355-2374 (the `import datetime as _dt` stays inside)


def _read_trace(limit=15):
    ...   # verbatim lines 2377-2384


def _trace_count():
    ...   # verbatim lines 2387-2393
```

Delete those definitions from `alvaagent_tui.py` (keep the agent-loop body that *calls* them). Then import in `alvaagent_tui.py`:

```python
# trace moved to alvaagent/trace.py (Task 3)
from alvaagent.trace import (  # noqa: E402,F401
    _trace, _read_trace, _trace_count, _TRACE_MAX_LINES, _TRACE_MAX_BYTES,
)
```

- [ ] **Step 5: Re-export from the facade**

Add to `alvaagent/__init__.py`:

```python
from alvaagent.config import (  # noqa: F401
    data_dir, DATA_DIR, _LEGACY_DIRS, CONFIG_PATH, STORE_PATH, HISTORY_PATH,
    TRACE_PATH, PROVIDERS, DEFAULT_CFG, FIRST_RUN_CFG, DEFAULT_SKIN,
    SKIN_NAMES, ALVA_VERSION, DEFAULT_CONTEXT_WINDOW, MODEL_CONTEXT,
    TOOL_MODES, _tool_mode_of, _skin_of, _normalize_state, load_state,
    save_state, active_cfg,
)
from alvaagent.trace import (  # noqa: F401
    _trace, _read_trace, _trace_count, _TRACE_MAX_LINES, _TRACE_MAX_BYTES,
)
```

- [ ] **Step 6: Run the tests**

Run: `python3 test_tui.py` — Expected: `ALL TESTS PASSED ✓`

- [ ] **Step 7: Commit**

```bash
git add alvaagent/config.py alvaagent/trace.py alvaagent/__init__.py alvaagent_tui.py
git commit -m "refactor: extract config.py (paths, profiles, provider defaults, tool_mode validation)"
```

---

### Task 4: Extract `store.py` (persistence)

**Files:**
- Create: `alvaagent/store.py`
- Modify: `alvaagent_tui.py`, `alvaagent/__init__.py`

**Interfaces:**
- Consumes: `config.STORE_PATH`, `config.DATA_DIR` via `config.data_dir()`, `util._env`, `util._atomic_write`.
- Produces:
  - `_store` (module dict — still module-global until the Runtime phase)
  - `_migrate_legacy_dir()`, `_load_store()`, `_save_store()`
  - `_store_get(key, default=None)`, `_store_set(key, value)`
  - key constants: `TODO_KEY`, `MEM_PREFIX`, `FEEDBACK_KEY`, `IMPROVEMENT_KEY`, `HISTORY_KEY`, `SESSION_KEY`, `ACTIVE_SESSION_KEY`, `MAX_SESSIONS`
  - keeps the import-time `_load_store()` call (moved to Runtime in the Runtime phase)

- [ ] **Step 1: Create `alvaagent/store.py`**

Move the persistence section from `alvaagent_tui.py` (the block between the `# ---------------- persistence ...` marker and the `# ---------------- autonomy: permissions ...` marker — roughly lines 277-359): `_store = {}`, `_migrate_legacy_dir`, `_load_store`, `_save_store`, `_store_get`, `_store_set`, the trailing `_load_store()` call, and all the key constants (line ~357-358).

Header imports:

```python
import json
import os

from alvaagent.config import STORE_PATH
from alvaagent.util import _env
```

`_migrate_legacy_dir` uses `_env` and `DATA_DIR` — it references `DATA_DIR`; replace with a call to `config.data_dir()` if needed (check the original body: it only acts when `_env("ALVA_DATA_DIR", "POCKET_DATA_DIR")` is set, and reads from `_LEGACY_DIRS`; keep it identical and import `_LEGACY_DIRS` from config if referenced). Bodies stay verbatim.

- [ ] **Step 2: Delete the section from `alvaagent_tui.py` and import**

```python
# store moved to alvaagent/store.py (Task 4)
from alvaagent.store import (  # noqa: E402,F401
    _store, _migrate_legacy_dir, _load_store, _save_store,
    _store_get, _store_set, TODO_KEY, MEM_PREFIX, FEEDBACK_KEY,
    IMPROVEMENT_KEY, HISTORY_KEY, SESSION_KEY, ACTIVE_SESSION_KEY, MAX_SESSIONS,
)
```

- [ ] **Step 3: Re-export from the facade**

```python
from alvaagent.store import (  # noqa: F401
    _store, _migrate_legacy_dir, _load_store, _save_store,
    _store_get, _store_set, TODO_KEY, MEM_PREFIX, FEEDBACK_KEY,
    IMPROVEMENT_KEY, HISTORY_KEY, SESSION_KEY, ACTIVE_SESSION_KEY, MAX_SESSIONS,
)
```

- [ ] **Step 4: Run the tests**

Run: `python3 test_tui.py` — Expected: `ALL TESTS PASSED ✓` (the atomic-write and store regression checks must still pass).

- [ ] **Step 5: Commit**

```bash
git add alvaagent/store.py alvaagent/__init__.py alvaagent_tui.py
git commit -m "refactor: extract store.py (atomic store.json persistence + namespaced keys)"
```

---

### Task 5: Extract `permissions.py` (security classifiers)

**Files:**
- Create: `alvaagent/permissions.py`
- Modify: `alvaagent_tui.py`, `alvaagent/__init__.py`

**Interfaces:**
- Consumes: `util` (nothing else).
- Produces:
  - `_READONLY_PREFIXES`, `_RISKY_TOKENS`, `_RISKY_OPERATORS` (module consts)
  - `_APPROVED_SET` (module set — Runtime phase moves it to `rt.approved`)
  - `_tokenize_shell(cmd) -> list`, `classify_command(cmd) -> "allow"|"ask"|"deny"` (empty command → "deny"; code comment says block, body returns deny)
  - `_in_project(path) -> bool`, `classify_file_action(path, mode) -> "allow"|"ask"` — keep the existing signature verbatim (gains `rt` only in Task 14)
  - `_permission(action, ok=True, hint="")` — keep the exact current callable signature; it reads the module-level `_APPROVED_SET` and `ON_PERMISSION` defined in this module.

- [ ] **Step 1: Create `alvaagent/permissions.py`**

Move verbatim from `alvaagent_tui.py`: the block between the `# ---------------- autonomy: permissions ...` marker and the `# ---------------- autonomy: shell + files + skills ...` marker (roughly lines 360-509). That includes `_READONLY_PREFIXES`, `_RISKY_TOKENS`, `_RISKY_OPERATORS`, `_tokenize_shell`, `classify_command`, `_in_project`, `classify_file_action`, `_APPROVED_SET`, `_permission`.

Header imports: none beyond stdlib (uses `shlex`, `re`, `os` if present in the moved code — add whatever the moved bodies import).

**The `ON_PERMISSION` hook** is set by `main()` (`ON_PERMISSION = ask_permission`). In `permissions.py` keep a module-level hook exactly as today:

```python
ON_PERMISSION = None  # set by the REPL to an interactive prompt; headless = deny
```

`_permission` keeps reading the module-level `ON_PERMISSION` and `_APPROVED_SET`. `alvaagent_tui.py` must forward its `ON_PERMISSION = ask_permission` assignment onto the permissions module (Step 2). Behavior is identical; the Runtime phase moves the hook onto `rt.on_permission`.

`classify_file_action`/`_in_project` use `DATA_DIR`/project dir — check the bodies: `_in_project` compares against the project folder. In `permissions.py` compute the project root from the package path the same way `config.data_dir()` does (repo root = package parent) and **export it as `PROJECT_DIR`** (tui still references it at ~379/3600/3613, so the Step 2/3 import lists include `PROJECT_DIR`). `SKILLS_DIR` stays defined in `alvaagent_tui.py` (it belongs to the skills subsystem, Task 6). Keep the allow/ask semantics byte-identical.

- [ ] **Step 2: Patch `alvaagent_tui.py`**

Delete the moved block and add the import:

```python
# permissions moved to alvaagent/permissions.py (Task 5)
from alvaagent.permissions import (  # noqa: E402,F401
    _READONLY_PREFIXES, _RISKY_TOKENS, _RISKY_OPERATORS, _tokenize_shell,
    classify_command, PROJECT_DIR, _in_project, classify_file_action,
    _APPROVED_SET, _permission, ON_PERMISSION,
)
```

In `main()` (where `ON_PERMISSION = ask_permission` is assigned), change to forward to the permissions module:

```python
    from alvaagent.permissions import ON_PERMISSION as _perm_hook
    # keep the assignment style minimal:
    permissions_module.ON_PERMISSION = ask_permission
```

Concretely, replace:

```python
    ON_TOOL = on_tool        # live tool-progress blocks
    ON_PERMISSION = ask_permission  # interactive y/N for risky actions
```

with:

```python
    ON_TOOL = on_tool        # live tool-progress blocks
    import alvaagent.permissions as _perms
    _perms.ON_PERMISSION = ask_permission  # interactive y/N for risky actions
```

(The `ON_TOOL` global is still in `alvaagent_tui.py` until Task 11.) Also update the `global ON_TOOL, ON_PERMISSION` line in `main()` to just `global ON_TOOL` (ON_PERMISSION is no longer assigned there).

- [ ] **Step 3: Re-export from the facade**

```python
from alvaagent.permissions import (  # noqa: F401
    _READONLY_PREFIXES, _RISKY_TOKENS, _RISKY_OPERATORS, _tokenize_shell,
    classify_command, PROJECT_DIR, _in_project, classify_file_action,
    _APPROVED_SET, _permission, ON_PERMISSION,
)
```

- [ ] **Step 4: Run the tests**

Run: `python3 test_tui.py` — Expected: `ALL TESTS PASSED ✓` (security classifier + `_permission` checks must pass).

- [ ] **Step 5: Commit**

```bash
git add alvaagent/permissions.py alvaagent/__init__.py alvaagent_tui.py
git commit -m "refactor: extract permissions.py (command/python/file classifiers + approval hook)"
```

---

### Task 6: Extract `skills.py` (skill machinery)

**Files:**
- Create: `alvaagent/skills.py`
- Modify: `alvaagent_tui.py`, `alvaagent/__init__.py`

**Interfaces:**
- Consumes: `util` (`_parse_frontmatter`, `_frontmatter_dump`, `_raw_fetch`, `_looks_like_html`, `_atomic_write`), `store` (`_store_get`, `_store_set`), `permissions` (`classify_file_action`, `_permission`, `ON_PERMISSION`), `config` (project root).
- Produces: `_skill_body_for_tool`, `_detect_category`, `_skill_filepath`, `_inside_skills`, `_resolve_skill_path`, `_skill_read`, `_scan_skill_files`, `_skill_list_all`, `tool_skill_list`, `tool_skill_read`, `tool_skill_remove`, `tool_skill_save`, `tool_skill_install`, `tool_skill_sync_repo`.

- [ ] **Step 1: Create `alvaagent/skills.py`**

Move verbatim from `alvaagent_tui.py`: the block between `# ---------------- skills: Hermes-style frontmatter ...` and `# ---------------- tools ...` (roughly lines 635-1215), EXCEPT the mini-yaml helpers already moved to `util.py` in Task 2 (`_mini_scalar`, `_finish_block`, `_mini_yaml`, `_frontmatter_load`, `_frontmatter_dump`, `_parse_frontmatter` — do not copy them again; import them instead). Also exclude `_atomic_write` (Task 2), `_looks_like_html`, `_raw_fetch` (Task 2), and the constants `_SKILL_FM_RE`, `_SKILL_FM_DEFAULT`, `_VALID_FM_KEYS`, `_SKILL_RAW_MAX` — these now live in `util.py` (Task 2 deviation, reviewed and accepted) and must be IMPORTED from `alvaagent.util`, not re-copied from git history (a re-copy would shadow util's copies with divergent objects that `_parse_frontmatter`/`_raw_fetch` still read).

Header imports:

```python
import os
import re
import shutil

from alvaagent import config
from alvaagent.permissions import classify_file_action, _permission
from alvaagent.store import _store_get, _store_set
from alvaagent.util import (
    _atomic_write, _looks_like_html, _raw_fetch,
    _SKILL_FM_RE, _SKILL_FM_DEFAULT, _VALID_FM_KEYS, _SKILL_RAW_MAX,
    _parse_frontmatter, _frontmatter_load, _frontmatter_dump,
)
```

Any references the moved bodies make to `DATA_DIR` / project dir become `config.data_dir()` (check each; the skill path logic and `_inside_skills` boundary use the project folder).

- [ ] **Step 2: Patch `alvaagent_tui.py`**

Delete the moved block and add:

```python
# skills moved to alvaagent/skills.py (Task 6)
from alvaagent.skills import (  # noqa: E402,F401
    _SKILL_FM_RE, _SKILL_FM_DEFAULT, _VALID_FM_KEYS, _SKILL_RAW_MAX,
    _skill_body_for_tool, _detect_category, _skill_filepath, _inside_skills,
    _resolve_skill_path, _skill_read, _scan_skill_files, _skill_list_all,
    tool_skill_list, tool_skill_read, tool_skill_remove, tool_skill_save,
    tool_skill_install, tool_skill_sync_repo,
)
```

- [ ] **Step 3: Re-export from the facade**

Add the same names from `alvaagent.skills` to `alvaagent/__init__.py`.

- [ ] **Step 4: Run the tests**

Run: `python3 test_tui.py` — Expected: `ALL TESTS PASSED ✓` (skill install/sync/list/read checks must pass — the mock server serves skills).

- [ ] **Step 5: Commit**

```bash
git add alvaagent/skills.py alvaagent/__init__.py alvaagent_tui.py
git commit -m "refactor: extract skills.py (frontmatter skills, skill install/sync from URL and git repo)"
```

---

### Task 7: Extract `tools.py` (tools, registry, dispatch, self-test)

**Files:**
- Create: `alvaagent/tools.py`
- Modify: `alvaagent_tui.py`, `alvaagent/__init__.py`

**Interfaces:**
- Consumes: `config` (`TOOL_MODES`, `data_dir`), `store` (`_store_get`, `_store_set`), `permissions` (`classify_command`, `classify_python` is defined here, `_permission`, `classify_file_action`), `skills` (all `skill_*`), `util` (`_raw_fetch`, `_atomic_write`, `now_iso`).
- Produces (Phase A, flat functions still):
  - `_PY_RUN_TIMEOUT`, `_PY_MAX_BYTES`, `_PY_MAX_CHARS`, `_CALC_ALLOWED`, `_SKILL_RAW_MAX` usage, `_TOOLS_MODE` (module global), `_CORE_TOOL_NAMES`, `_ADVANCED_TOOL_NAMES`, `_TOOL_MODES` → import from config now
  - `_tool_mode_of` — **delete** (config owns it now)
  - `active_tools()`, `_maybe_enable_full(name)`, `_set_tool_mode(state, mode)`, `_sync_tool_mode(state)`
  - all `tool_*` functions: `tool_run_command`, `tool_file_read/write/edit/list/search`, `tool_todo_*`, `tool_memory_*`, `tool_get_time`, `tool_feedback`, `tool_improvement_*`, `tool_reflect`, `tool_web_fetch`, `_safe_factorial`, `_calc_eval`, `_fmt_num`, `tool_calculator`, `classify_python`, `tool_run_python`, `tool_count`
  - `TOOLS` schema list, `TOOL_IMPL`, `_TOOL_ERROR_HINTS`, `dispatch_tool(name, args)`
  - `self_test()`, `tool_self_test()`

- [ ] **Step 1: Create `alvaagent/tools.py`**

Move verbatim from `alvaagent_tui.py`: everything between the `# ---------------- tools ...` marker and the `# ---------------- LLM client ...` marker (roughly lines 1216-1925), PLUS `classify_python` and `tool_run_python` (lines ~1529-1823, they are inside that range already), PLUS the self-test block (`# ---------------- harness self-test ...`, lines ~2683-2850: `tool_count`, `self_test`, `tool_self_test`).

Changes while moving:
- `_TOOL_MODES = ("core", "full")` tuple was moved to `config.py` in Task 3 — **delete it here** and import it.
- `_tool_mode_of` was moved to `config.py` in Task 3 — **delete it here**.
- `_sync_tool_mode(state)` currently lives in `alvaagent_tui.py` (added in Task 3) — move it into this module.
- Keep `_TOOLS_MODE = "core"` as a module global in `tools.py` (still global until the Runtime phase).
- Add the sync call sites: `alvaagent_tui.py` `repl()`/`main()` currently call `_sync_tool_mode(state)` — they must call `tools._sync_tool_mode(state)` instead (Step 2).

Header imports (add exactly what the moved bodies use; the big cross-cutting ones):

```python
import os
import re
import subprocess
import sys

from alvaagent.config import TOOL_MODES, data_dir
from alvaagent.permissions import classify_command, classify_file_action, _permission
from alvaagent.skills import (
    tool_skill_list, tool_skill_read, tool_skill_remove, tool_skill_save,
    tool_skill_install, tool_skill_sync_repo,
)
from alvaagent.store import _store_get, _store_set
from alvaagent.trace import _trace            # Ruling 1: tools' _set_tool_mode/_maybe_enable_full trace
from alvaagent.util import _raw_fetch, _atomic_write, now_iso
```

`tool_web_fetch` uses `_raw_fetch` (util). `tool_run_python` uses `classify_python`, `_permission`, `_PY_*` consts, `subprocess`, `select`, `sys`. `self_test()` uses `_TOOLS_MODE`, `_CORE_TOOL_NAMES`, `classify_python`, `classify_command`, the `tool_*` functions, `os.path.join(DATA_DIR, ...)` → use `data_dir()`, and `__file__` (its own module file — still fine). `tool_self_test()` runs `test_tui.py` via `subprocess` using `os.path.dirname(os.path.abspath(__file__))` → the package dir is one level down from the repo root, so:

```python
    my_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
```

Adjust the moved `tool_self_test` body accordingly (its `tpath = os.path.join(my_dir, "test_tui.py")` must resolve to the repo root). Verify with the test suite.

- [ ] **Step 2: Patch `alvaagent_tui.py`**

Delete the moved blocks and add:

```python
# tools moved to alvaagent/tools.py (Task 7)
from alvaagent.tools import (  # noqa: E402,F401
    _PY_RUN_TIMEOUT, _PY_MAX_BYTES, _PY_MAX_CHARS, _CALC_ALLOWED,
    _TOOLS_MODE, _CORE_TOOL_NAMES, _ADVANCED_TOOL_NAMES,
    active_tools, _maybe_enable_full, _set_tool_mode, _sync_tool_mode,
    tool_run_command, tool_file_read, tool_file_write, tool_file_edit,
    tool_file_list, tool_file_search, tool_todo_list, tool_todo_add,
    tool_todo_toggle, tool_todo_remove, tool_memory_save, tool_memory_recall,
    tool_memory_list, tool_memory_search, tool_get_time, tool_feedback,
    tool_improvement_set, tool_improvement_list, tool_improvement_done,
    tool_reflect, tool_web_fetch, _safe_factorial, _calc_eval, _fmt_num,
    tool_calculator, classify_python, tool_run_python, tool_count,
    TOOLS, TOOL_IMPL, _TOOL_ERROR_HINTS, dispatch_tool, self_test, tool_self_test,
)
```

The REPL's two `_sync_tool_mode(state)` calls must now go through the tools module — the import above brings `_sync_tool_mode` into `alvaagent_tui`'s namespace, so the existing calls keep working unchanged. `TOOL_IMPL` calls the `tool_*` functions by bare name inside `tools.py` — that still works (same module).

- [ ] **Step 3: Redirect the tiered-mode test block (Ruling 3)**

`_TOOLS_MODE` is now owned by `tools.py`, and `_maybe_enable_full`/`_set_tool_mode` mutate `tools._TOOLS_MODE` directly. The facade write-through keeps `pa._TOOLS_MODE = ...` assignments working, but the test's READ of the internally-mutated value (`assert pa._TOOLS_MODE == "full"` after `dispatch_tool("self_test", {})`) would see the stale `alvaagent_tui` copy. In `test_tui.py`:

1. Add at the top (with the other module-level imports): `import alvaagent.tools as _tools`.
2. Replace every `pa._TOOLS_MODE` in the tiered tool-selection block (lines ~1133-1160 — the save, both assignments, the auto-enable assert, and the finally-restores) with `_tools._TOOLS_MODE`.

This is the only test-patch redirection the whole phase-A needs; every other `pa.<name> = ...` site keeps working via the facade write-through.

- [ ] **Step 4: Re-export from the facade**

Add the same names from `alvaagent.tools` to `alvaagent/__init__.py`.

- [ ] **Step 5: Run the tests**

Run: `python3 test_tui.py` — Expected: `ALL TESTS PASSED ✓` (calculator, sandbox, classifiers, tool tiering, run_python, skill tools, self-test path checks).

- [ ] **Step 6: Commit**

```bash
git add alvaagent/tools.py alvaagent/__init__.py alvaagent_tui.py test_tui.py
git commit -m "refactor: extract tools.py (tool impls, registry, dispatch, tiered selection, self-test)"
```

---

### Task 8: Extract `client.py` (LLM client)

**Files:**
- Create: `alvaagent/client.py`
- Modify: `alvaagent_tui.py`, `alvaagent/__init__.py`

**Interfaces:**
- Consumes: `config` (`active_cfg`, `data_dir`), `util` (`_env`? check bodies), `store` (trace not here).
- Produces: `_MAX_RETRIES`, `_RETRY_BACKOFF`, `_STREAM_IDLE_LIMIT`, `_STREAM_POLL`, `_cancel_flag`, `_readable_error`, `_retryable_status`, `_sleep_retry`, `class _Cancelled`, `chat_completion(state, messages, ...)` (verbatim signature), `chat_completion_stream(...)` (verbatim), `fetch_models(state)`, `cancel_agent()`.

- [ ] **Step 1: Create `alvaagent/client.py`**

Move verbatim from `alvaagent_tui.py`: the block between `# ---------------- LLM client ...` and `# ---------------- agent loop ...` (roughly lines 1926-2291), including the `_cancel_flag` global.

Header imports: the moved bodies use `json`, `os`, `select`, `signal`(?), `socket`, `sys`, `threading`, `time`, `urllib.error`, `urllib.request`, `config.active_cfg`/`active_cfg` (check which name the bodies call — they call `active_cfg(state)`), `_readable_error` local. Add:

```python
import json
import os
import select
import sys
import threading
import time
import urllib.error
import urllib.request

from alvaagent.config import active_cfg
```

`_sleep_retry` and `cancel_agent` reference `_cancel_flag` — keep it as a module global here (Runtime phase → `rt.cancel`). `cancel_agent` is called from `alvaagent_tui.py` REPL.

- [ ] **Step 2: Patch `alvaagent_tui.py`**

Delete the block and add:

```python
# LLM client moved to alvaagent/client.py (Task 8)
from alvaagent.client import (  # noqa: E402,F401
    _MAX_RETRIES, _RETRY_BACKOFF, _STREAM_IDLE_LIMIT, _STREAM_POLL,
    _cancel_flag, _readable_error, _retryable_status, _sleep_retry,
    _Cancelled, chat_completion, chat_completion_stream, fetch_models,
    cancel_agent,
)
```

Note: if `alvaagent_tui.py`'s remaining code calls `active_cfg` (bare), it already imports it from config (Task 3). If it referenced `cancel_agent`, the import above covers it.

- [ ] **Step 3: Re-export from the facade**

Add the same names from `alvaagent.client` to `alvaagent/__init__.py`.

- [ ] **Step 4: Run the tests**

Run: `python3 test_tui.py` — Expected: `ALL TESTS PASSED ✓` (streaming + plain-JSON fallback checks hit the mock server through this module).

- [ ] **Step 5: Commit**

```bash
git add alvaagent/client.py alvaagent/__init__.py alvaagent_tui.py
git commit -m "refactor: extract client.py (chat completion, streaming, retries, models, cancel)"
```

---

### Task 9: Extract `sessions.py` (sessions, context, compression)

**Files:**
- Create: `alvaagent/sessions.py`
- Modify: `alvaagent_tui.py`, `alvaagent/__init__.py`

**Interfaces:**
- Consumes: `config` (`MODEL_CONTEXT`, `DEFAULT_CONTEXT_WINDOW`), `store` (`_store_get`, `_store_set`, `ACTIVE_SESSION_KEY`, `MAX_SESSIONS`, `SESSION_KEY`), `client` (`chat_completion`), `util` (`now_iso`).
- Produces: `context_window_for(model)`, `estimate_tokens(text)`, `estimate_message_tokens(m)`, `context_usage(history, cfg)`, `sessions_map()`, `load_session(name)`, `save_session(name, history)`, `delete_session(name)`, `_find_session(name)`, `_rename_session_in_store(old, new)`, `auto_title(text)`, `_unique_session_name(base)`, `summarize_with_llm(history, cfg)`, `_fallback_summary(history)`, `compress_history(history, cfg)`, `trim_history(history)`, `new_session_name()`.

> **Ruling 9 (amended):** `compress_now` does NOT move at Task 9 — it is the only body in the sessions region that prints via the UI helpers `p_info`/`p_ok` (which move with `tui.py` at Task 11), so it stays in `alvaagent_tui.py` (it consumes `context_usage`/`compress_history`/`active_cfg`, imported back into tui). Revisit its final home at Task 14 (likely a print hook in the Runtime phase). Task 9's move range ends at `compress_history` (tui 708); the `Terminal UI` banner, `class C`, and `COLOR` (tui ~748-765) belong to the UI subsystem and do not move.

- [ ] **Step 1: Create `alvaagent/sessions.py`**

Move verbatim from `alvaagent_tui.py`:
- the context/sessions/auto-compression block from `# ---------------- context tracking & sessions ...` through the end of `compress_history` (current tui lines 517-708; `compress_now` at 709-745 stays behind per Ruling 9),
- `trim_history` (current line 1433-1447, in the display section),
- `new_session_name` (current line 2343-2345, in the REPL section).

Header imports:

```python
import re
import secrets

from alvaagent.client import chat_completion
from alvaagent.config import DEFAULT_CONTEXT_WINDOW, MODEL_CONTEXT
from alvaagent.store import (
    ACTIVE_SESSION_KEY, MAX_SESSIONS, SESSION_KEY,
    _store_get, _store_set,
)
from alvaagent.util import now_iso
```

Note: `summarize_with_llm` calls `chat_completion(msgs, cfg)` — pass the `cfg` through verbatim.

- [ ] **Step 2: Patch `alvaagent_tui.py`**

Delete the moved blocks and add:

```python
# sessions/context/compression moved to alvaagent/sessions.py (Task 9)
from alvaagent.sessions import (  # noqa: E402,F401
    context_window_for, estimate_tokens, estimate_message_tokens, context_usage,
    sessions_map, load_session, save_session, delete_session, _find_session,
    _rename_session_in_store, auto_title, _unique_session_name,
    summarize_with_llm, _fallback_summary, compress_history,
    trim_history, new_session_name,
)
```

- [ ] **Step 3: Re-export from the facade**

Add the same names from `alvaagent.sessions` to `alvaagent/__init__.py`.

- [ ] **Step 4: Run the tests**

Run: `python3 test_tui.py` — Expected: `ALL TESTS PASSED ✓` (session save/load/rename, compression, context-usage checks).

- [ ] **Step 5: Commit**

```bash
git add alvaagent/sessions.py alvaagent/__init__.py alvaagent_tui.py
git commit -m "refactor: extract sessions.py (session store, context tracking, auto-compression)"
```

---

### Task 10: Extract `agent.py` (agent loop, XML parsing, tool-report hook)

**Files:**
- Create: `alvaagent/agent.py`
- Modify: `alvaagent_tui.py`, `alvaagent/__init__.py`

**Interfaces:**
- Consumes: `client` (`chat_completion_stream`, `_Cancelled`), `tools` (`dispatch_tool`, `active_tools`), `trace` (`_trace`, `_read_trace`, `_trace_count`), `util` (`_cancel_flag`).
- Produces: `_TURN_TIMEOUT`, `_MAX_CONSEC_TOOL_FAILURES`, **`ON_TOOL`** (module hook — stays in this module, see Ruling 2), XML regex consts (`_XML_*`), `_repair_tool_pairs`, `_report_tool(...)`, `run_agent(messages, cfg, ...)` (verbatim), `_clean_segment`, `_strip_xml_blocks`, `_parse_xml_tool_calls`, `_strip_xml`, `run_agent_stream(messages, cfg, ...)` (verbatim). (`MAX_STEPS` stays local to agent.py — no tui/test use.)
- Note: the trace helpers (`_trace`/`_read_trace`/`_trace_count`/`_TRACE_MAX_LINES`/`_TRACE_MAX_BYTES`) already moved to `trace.py` in Task 3 — **do not move them again**; import them.

> **Ruling 10 (amended):** The move range is the agent loop section *only*: current tui **176-521** (`# ---------------- agent loop` marker through `run_agent_stream`'s final `yield`). The old end marker `# ---------------- harness self-test` no longer exists (moved to tools.py in Task 7). `compress_now` (524-560), the `Terminal UI` banner, `class C`, and `COLOR` (through the `skins` marker at 583) stay behind per Rulings 9 + the UI boundary. Verified header is smaller than originally planned: `config`, `_sleep_retry`, `TOOLS`/`TOOL_IMPL`/`_maybe_enable_full`/`_TOOL_ERROR_HINTS` have zero uses in the section; `_cancel_flag` is imported from `alvaagent.util` (Ruling 8 canonical owner, not client). The trace import block that sits mid-section (old tui ~229) moves along and is trimmed to `_trace, _read_trace, _trace_count`; tui gets a fresh `from alvaagent.trace import _read_trace` because `cmd_trace` (~1773) still needs it.

- [ ] **Step 1: Create `alvaagent/agent.py`**

Move verbatim from `alvaagent_tui.py`: the block between `# ---------------- agent loop ...` and the end of `run_agent_stream` (current lines 176-521), **minus** the trace helpers already extracted in Task 3. Include `ON_TOOL = None` (line ~180).

Header imports:

```python
import json
import re
import time

from alvaagent.client import chat_completion_stream, _Cancelled
from alvaagent.tools import dispatch_tool, active_tools
from alvaagent.trace import _trace, _read_trace, _trace_count
from alvaagent.util import _cancel_flag
```

`run_agent_stream` references `_cancel_flag`, `_TURN_TIMEOUT`, `_MAX_CONSEC_TOOL_FAILURES`, `dispatch_tool`, `_trace`, `_repair_tool_pairs` — all local to this module (or imported above) after the move. `_sleep_retry` is not used.

- [ ] **Step 2: Patch `alvaagent_tui.py`**

Delete the block and add:

```python
# agent loop moved to alvaagent/agent.py (Task 10)
from alvaagent.agent import (  # noqa: E402,F401
    _TURN_TIMEOUT, _MAX_CONSEC_TOOL_FAILURES, ON_TOOL,
    _repair_tool_pairs, _report_tool,
    run_agent, _clean_segment, _strip_xml_blocks, _parse_xml_tool_calls,
    _strip_xml, run_agent_stream,
)
```

**Ruling 2 (forward the ON_TOOL hook):** `main()` currently does `ON_TOOL = on_tool` (~line 5159). That now rebinds the *local imported name*, not the `agent` module's global, so the agent loop would never see it. Change it to:

```python
    import alvaagent.agent as _agent
    _agent.ON_TOOL = on_tool
```

(`on_tool` still lives in `alvaagent_tui.py` until Task 11; the same forwarding is rewritten in `repl.main()` in Task 13.)

- [ ] **Step 3: Re-export from the facade**

Add the same names from `alvaagent.agent` to `alvaagent/__init__.py`.

- [ ] **Step 4: Run the tests**

Run: `python3 test_tui.py` — Expected: `ALL TESTS PASSED ✓` (agent-loop + streaming tests, trace checks, ghost-duplicate regression).

- [ ] **Step 5: Commit**

```bash
git add alvaagent/agent.py alvaagent/__init__.py alvaagent_tui.py
git commit -m "refactor: extract agent.py (turn loop, runaway guards, XML tool-call parsing)"
```

---

### Task 11: Extract `tui.py` (skins + rendering)

**Files:**
- Create: `alvaagent/tui.py`
- Modify: `alvaagent_tui.py`, `alvaagent/__init__.py`

**Interfaces:**
- Consumes: `config` (`ALVA_VERSION`, `DATA_DIR`, `DEFAULT_SKIN`, `active_cfg`), `agent` (`run_agent_stream`, `_strip_xml_blocks`), `sessions` (`context_usage`, `context_window_for`), `tools` (`TOOLS`, `TOOLSETS`, `active_tools`, `_tools._TOOLS_MODE`), `util` (`_fmt_k`).
- Produces: `SKINS`, `C`, `CUR_SKIN`, `set_active_skin(state)`, `col`, `p_info`, `p_err`, `p_ok`, `p_warn`, `_term_width`, `_hrgb`, `_fgh`, `_rsth`, `_tool_line`, `print_user_turn`, `render_agent_panel`, `_md_attr_sgr`, `_has_ansi`, `_md_line`, `_md_prefix`, `style_inline`, `AgentWriter`, `fmt_args`, `tool_summary`, `Spinner`, `tool_open`, `tool_close`, `on_tool`, `run_agent_tui(history, cfg)`, `_ANSI_RE`, `_MD_STYLE`, `_UI`, `_CON`/`Panel`/`Console`/`HORIZONTALS` + `_Shim*` (optional-Rich block), `COLOR`/`CUR_SKIN` globals, `ALVA_WORDMARK`, `_markup_safe`, `_banner_tools_lines`, `_banner_skills_lines`, `banner(state)`, `render_status_bar(...)`.

> **Ruling 11 (amended):** The verified move ranges are four regions: optional-Rich block (old tui 71-102), `# Terminal UI` comment + `class C` + `COLOR` (228-244), the `# ---------------- skins` marker through the end of `run_agent_tui` (247-909), and `ALVA_WORDMARK` through the end of `render_status_bar` (1847-2015). `compress_now` (188-227) STAYS in the main file (Ruling 9); `ask`/`parse_key`/`ask_key`/`ask_permission` (916-968) STAY for Task 12; `send_message`/`repl`/`setup_completion` STAY. Because `set_active_skin` REBINDS `CUR_SKIN` inside tui.py, the main file's two `CUR_SKIN` reads (`ask_permission`, repl prompt) must become `_tui.CUR_SKIN` (`import alvaagent.tui as _tui`) — the same qualified-read idiom as Ruling 7's `_tools._TOOLS_MODE`. Verified header is smaller than originally planned: no `SKIN_NAMES`/`_skin_of`/`data_dir`; adds `datetime`, `shutil`, `TOOLSETS`, `import alvaagent.tools as _tools`.

- [ ] **Step 1: Create `alvaagent/tui.py`**

Move verbatim from `alvaagent_tui.py`: the block between `# ---------------- skins ...` and the end of `run_agent_tui` (current lines 247-909), PLUS `ALVA_WORDMARK` → `render_status_bar` (lines 1847-2015, currently in the REPL section), PLUS `class C`/`COLOR` (lines 228-244), PLUS the optional-Rich fallback block (lines 71-102).

Header imports:

```python
import datetime
import json
import os
import re
import shutil
import sys
import threading
import time

from alvaagent.config import ALVA_VERSION, DATA_DIR, DEFAULT_SKIN, active_cfg
from alvaagent.agent import _strip_xml_blocks, run_agent_stream
from alvaagent.sessions import context_usage, context_window_for
from alvaagent.tools import TOOLS, TOOLSETS, active_tools
from alvaagent.util import _fmt_k
import alvaagent.tools as _tools
```

Carry over the optional rich import exactly as it exists today (the `try: from rich... except:` fallback with `_ShimConsole`/`_ShimPanel`/`_ShimBox`). The `CUR_SKIN` / `COLOR` globals and `_UI = {"spinner": None}` stay module-level here (Runtime phase → `rt.skin` / `rt.spinner`). `set_active_skin(state)` stays as-is (reads `state["skin"]`, rebinds the module global — hence Ruling 11's qualified reads). `AgentWriter` needs `_strip_xml_blocks` from agent; `banner` needs `_tools._TOOLS_MODE` and the lazy `from rich.table import Table` it already wraps in try/except.

- [ ] **Step 2: Patch `alvaagent_tui.py`**

Delete the moved blocks and add:

```python
# TUI rendering moved to alvaagent/tui.py (Task 11)
from alvaagent.tui import (  # noqa: E402,F401
    SKINS, C, set_active_skin, col, p_info, p_err, p_ok, p_warn, _term_width,
    _hrgb, _fgh, _rsth, _tool_line, print_user_turn, render_agent_panel,
    _md_attr_sgr, _has_ansi, _md_line, _md_prefix, style_inline, AgentWriter,
    fmt_args, tool_summary, Spinner, tool_open, tool_close, on_tool,
    run_agent_tui, _ANSI_RE, _MD_STYLE, _UI, COLOR, CUR_SKIN, _CON,
    Console, Panel, HORIZONTALS, banner, render_status_bar,
)
import alvaagent.tui as _tui  # noqa: E402  (Ruling 11: qualified CUR_SKIN reads)
```

**Ruling 11 (hook staleness):** `set_active_skin` rebinds `CUR_SKIN` inside tui.py, so the main file's two `CUR_SKIN` reads (`ask_permission` and the repl prompt) become `_tui.CUR_SKIN`. `main()`'s hook assignment (`_agent.ON_TOOL = on_tool`) is unchanged — `on_tool` now resolves via the import above.

- [ ] **Step 3: Re-export from the facade**

Add the same names from `alvaagent.tui` to `alvaagent/__init__.py`.

- [ ] **Step 4: Run the tests**

Run: `python3 test_tui.py` — Expected: `ALL TESTS PASSED ✓` (render/panel/writer/status-bar checks).

- [ ] **Step 5: Commit**

```bash
git add alvaagent/tui.py alvaagent/__init__.py alvaagent_tui.py
git commit -m "refactor: extract tui.py (skins, rich/ANSI panels, agent writer, spinner, banner)"
```

---

> **Ruling 12 (amended):** `compress_now` must move to `alvaagent/tui.py` NOW, not at Task 14 as Ruling 9 deferred. `cmd_compress` (which moves with commands.py) calls `compress_now`, and commands.py is a leaf — it cannot import from the main file. `compress_now`'s dependencies are `context_usage`/`compress_history` (sessions), `p_info`/`p_ok`/`Spinner`/`_UI` (tui.py locals), `_fmt_k` (util) — all already available in tui.py except `compress_history`, so tui.py's sessions import gains `compress_history`. sessions.py cannot host it (needs tui → import cycle). `send_message` (main, until Task 13) also calls `compress_now`, so the main file's tui import block adds `compress_now`, and commands.py imports `compress_now` from tui. Facade write-through keeps `pa.compress_now` monkeypatching (test_tui.py:693/702) working: main binds it (updated), tui.py owns it, commands.py exposes it — all three get written.

> **Ruling 12 (amended) — verified header for commands.py** (the plan's Step 1 header was aspirational): stdlib `datetime`, `json`, `os`, `urllib.error`, `urllib.request` (cmd_test does raw urllib calls); config `PROVIDERS, DEFAULT_CFG, FIRST_RUN_CFG, DEFAULT_SKIN, DATA_DIR, active_cfg, save_state`; store `_store, _store_get, _store_set, TODO_KEY, MEM_PREFIX, FEEDBACK_KEY, IMPROVEMENT_KEY, HISTORY_KEY, ACTIVE_SESSION_KEY`; permissions `classify_command, PROJECT_DIR` (NO `ask_permission` — it is a local def in the moved region; NO `_permission`/`ON_PERMISSION` — zero callers in the region); skills `tool_skill_list, tool_skill_read, tool_skill_install, tool_skill_sync_repo`; tools `_TOOLS_MODE, _ADVANCED_TOOL_NAMES, active_tools, TOOLS, tool_file_read, tool_file_write, tool_file_edit, tool_todo_list, tool_todo_add, tool_todo_toggle, tool_todo_remove, tool_memory_save, tool_memory_recall, tool_feedback, tool_improvement_set, tool_improvement_done, tool_reflect, tool_calculator`; client `SYSTEM_PROMPT, _readable_error, fetch_models`; sessions `estimate_tokens, context_usage, sessions_map, save_session`; trace `_read_trace` (only); tui `SKINS, C, col, p_info, p_err, p_ok, p_warn, set_active_skin, _UI` + `compress_now` + `import alvaagent.tui as _tui` (ask_permission reads `_tui.CUR_SKIN` — Ruling 11 qualified-read idiom); util `mask_key, _fmt_k`. Not used in the region (drop from plan header): `SKIN_NAMES`, `ALVA_VERSION`, `DEFAULT_CONTEXT_WINDOW`, `MODEL_CONTEXT`, `load_state`, `data_dir`, `tool_skill_remove/save`, `_skill_list_all`, `_set_tool_mode`, `_TOOL_MODES`, `tool_self_test`, `dispatch_tool`, `load_session`, `delete_session`, `_find_session`, `_rename_session_in_store`, `_unique_session_name`, `auto_title`, `new_session_name`, `_trace`, `_trace_count`, `CUR_SKIN`, `COLOR`, `banner`, `print_user_turn`, `render_agent_panel`, `render_status_bar`, `_md_line`, `AgentWriter`, `Spinner`, `now_iso`, `_raw_fetch`.

### Task 12: Extract `commands.py` (slash commands + prompts)

**Files:**
- Create: `alvaagent/commands.py`
- Modify: `alvaagent_tui.py`, `alvaagent/__init__.py`, `alvaagent/tui.py` (add `compress_now` + `compress_history` import)

**Interfaces:**
- Consumes: everything above — `config`, `store`, `permissions`, `skills`, `tools`, `client`, `sessions`, `agent`, `tui`.
- Produces: `ask`, `parse_key`, `ask_key`, `ask_permission`, `pick_model`, `_SLASH_COMMANDS`, and every `cmd_*`: `cmd_models`, `cmd_skin`, `cmd_sessions`, `cmd_context`, `cmd_compress`, `cmd_self_test`, `cmd_help`, `cmd_config`, `cmd_provider`, `cmd_test`, `cmd_tools`, `cmd_trace`, `cmd_todos`, `cmd_todo`, `cmd_memory`, `cmd_feedback`, `cmd_skills`, `cmd_skill_category`, `cmd_reflect`, `cmd_improve`, `cmd_install_skill`, `cmd_clear`, `cmd_export`, `cmd_multi` — signatures unchanged (they already take `state`/`history`/`rest` params as today). Local helpers `_check`, `_raises`, `_todo_check`, `_mem_check`, `_skill_check`, `_file_write_check`, `_file_edit_check`, `_feedback_check`, `_list_providers` stay inside commands.py.

- [ ] **Step 1: Create `alvaagent/commands.py`**

Move verbatim from `alvaagent_tui.py`: lines **203-1084** (from `def ask(` through the end of `cmd_multi`, just before the `# ---------------- REPL ----------------` marker at 1085). This includes `ask`, `parse_key`, `ask_key`, `ask_permission`, the `# ---------------- slash commands ----------------` marker, `pick_model`, `_SLASH_COMMANDS`, and all `cmd_*`. (`mask_key` is already in util — import it.)

Header imports (VERIFIED — the plan's Step 1 header was aspirational; use these, then let pyflakes F401 refine):

```python
import datetime
import json
import os
import urllib.error
import urllib.request

from alvaagent.config import (
    PROVIDERS, DEFAULT_CFG, FIRST_RUN_CFG, DEFAULT_SKIN, DATA_DIR,
    active_cfg, save_state,
)
from alvaagent.store import (
    _store, _store_get, _store_set, TODO_KEY, MEM_PREFIX, FEEDBACK_KEY,
    IMPROVEMENT_KEY, HISTORY_KEY, ACTIVE_SESSION_KEY,
)
from alvaagent.permissions import classify_command, PROJECT_DIR
from alvaagent.skills import (
    tool_skill_list, tool_skill_read, tool_skill_install, tool_skill_sync_repo,
)
from alvaagent.tools import (
    _TOOLS_MODE, _ADVANCED_TOOL_NAMES, active_tools, TOOLS,
    tool_file_read, tool_file_write, tool_file_edit, tool_todo_list,
    tool_todo_add, tool_todo_toggle, tool_todo_remove, tool_memory_save,
    tool_memory_recall, tool_feedback, tool_improvement_set,
    tool_improvement_done, tool_reflect, tool_calculator,
)
from alvaagent.client import SYSTEM_PROMPT, _readable_error, fetch_models
from alvaagent.sessions import (
    estimate_tokens, context_usage, sessions_map, save_session,
)
from alvaagent.trace import _read_trace
from alvaagent.tui import (
    compress_now, SKINS, C, col, p_info, p_err, p_ok, p_warn,
    set_active_skin, _UI,
)
from alvaagent.util import mask_key, _fmt_k
import alvaagent.tui as _tui
```

`ask_permission`/`ask_key`/`pick_model`/`parse_key`/`ask` are DEFINED in this module; they are NOT in `permissions`. The permissions hook (`_perms.ON_PERMISSION = ask_permission`) is set in `main()` — it must now reference the commands-module `ask_permission`, which the main file imports by name. `ask_permission` reads `_UI` and `col` (imported) and `_tui.CUR_SKIN` (kept verbatim from Ruling 11's qualified-read form) — the `import alvaagent.tui as _tui` provides it. No `cmd_*` calls `_permission`/`ON_PERMISSION` (verified: the only `ON_PERMISSION` hit in the region is ask_permission's docstring).

- [ ] **Step 2: Patch `alvaagent_tui.py`**

Delete lines 203-1084 and add:

```python
# slash commands moved to alvaagent/commands.py (Task 12)
from alvaagent.commands import (  # noqa: E402,F401
    _SLASH_COMMANDS, ask_permission,
    cmd_models, cmd_skin, cmd_sessions, cmd_context, cmd_compress,
    cmd_self_test, cmd_help, cmd_config, cmd_provider, cmd_test, cmd_tools,
    cmd_trace, cmd_memory, cmd_export, cmd_multi,
    cmd_install_skill, cmd_improve, cmd_skills, cmd_skill_category, cmd_clear,
)
```

This is exactly the 22 names the remaining REPL section (1085+) references (verified by grep of the dispatch at ~1239-1349, `_slash_complete` at 1122, and `main`'s hook at ~1382). Also add `compress_now` to the main file's `from alvaagent.tui import (...)` block (Ruling 12). The facade's eager list is unaffected (it pulls no cmd_* / compress_now).

- [ ] **Step 3: Re-export from the facade**

In `alvaagent/__init__.py`, after the `alvaagent.tui` re-export block, add a `from alvaagent.commands import (...)` block re-exporting the full surface: `ask, parse_key, ask_key, ask_permission, pick_model, _SLASH_COMMANDS, cmd_models, cmd_skin, cmd_sessions, cmd_context, cmd_compress, cmd_self_test, cmd_help, cmd_config, cmd_provider, cmd_test, cmd_tools, cmd_trace, cmd_todos, cmd_todo, cmd_memory, cmd_feedback, cmd_skills, cmd_skill_category, cmd_reflect, cmd_improve, cmd_install_skill, cmd_clear, cmd_export, cmd_multi` (mirror the generous established pattern; tests use `pa.cmd_provider`, `pa.cmd_trace`, `pa.ask_permission`, `pa.parse_key`, `pa.compress_now`).

- [ ] **Step 4: Run the tests**

Run: `python3 test_tui.py` — Expected: `ALL TESTS PASSED ✓` (`cmd_provider`/`cmd_trace`/`ask_permission`/`parse_key`/`compress_now`-monkeypatch checks plus all others).

- [ ] **Step 2: Patch `alvaagent_tui.py`**

Delete the block and add:

```python
# slash commands moved to alvaagent/commands.py (Task 12)
from alvaagent.commands import (  # noqa: E402,F401
    ask, parse_key, ask_key, ask_permission, pick_model,
    cmd_models, cmd_skin, cmd_sessions, cmd_context, cmd_compress,
    cmd_self_test, cmd_help, cmd_config, cmd_provider, cmd_test, cmd_tools,
    cmd_trace, cmd_todos, cmd_todo, cmd_memory, cmd_feedback, cmd_skills,
    cmd_skill_category, cmd_reflect, cmd_improve, cmd_install_skill, cmd_clear,
    cmd_export, cmd_multi,
)
```

Any helper the REPL uses that moved here (`_check`, `_raises`, etc.) must also be imported if referenced by `alvaagent_tui.py`'s remaining REPL code — after this task the only things left in `alvaagent_tui.py` are the REPL block.

- [ ] **Step 3: Re-export from the facade**

Add the same names from `alvaagent.commands` to `alvaagent/__init__.py`.

- [ ] **Step 4: Run the tests**

Run: `python3 test_tui.py` — Expected: `ALL TESTS PASSED ✓` (`cmd_provider`/`cmd_trace` checks plus all others).

- [ ] **Step 5: Commit**

```bash
git add alvaagent/commands.py alvaagent/__init__.py alvaagent_tui.py
git commit -m "refactor: extract commands.py (slash commands, provider prompts, self-test checks)"
```

---

> **Ruling 13 (amended) — keep the `_Facade` proxy; do NOT delete it at Task 13.** The plan's original Step 4 retired the proxy, but the unmodified test suite depends on its write-through: `test_tui.py:700-707` monkeypatches `pa.active_cfg`/`pa.compress_now`/`pa.run_agent_tui`/`pa.render_agent_panel`/`pa.render_status_bar`/`pa.print_user_turn`/`pa.context_usage`/`pa.save_session` and those MUST land on the bare-name module globals that `repl.send_message` reads (repl.py imports them by name, so write-through reaches it); likewise `pa._sleep_retry` (test 749), `pa._TURN_TIMEOUT` (agent.py), `pa.COLOR` (tui.py), `pa.yaml` (util.py:7). With the proxy gone, `pa.active_cfg = ...` would only set `alvaagent.active_cfg` and `send_message` would run against the real config — the dead-turn tests fail. Proxy retirement is deferred to Task 15 (after Task 14 reworks the tests). Also: do NOT add `from alvaagent.repl import main` directly to the facade — in import order 2 (`import alvaagent_tui` first) the facade runs while `repl` is mid-import (shim → repl → config → alvaagent), so a facade-level `from alvaagent.repl import main` raises `ImportError: cannot import name 'main' from partially initialized module`. Instead the SHIM re-exports the repl surface and `pa.<repl-name>` reads resolve through the `_Facade._tui` read-forward (established Task 1 pattern). The facade's eager block (lines 24-33) no-ops after shimification (`_store` absent) — remove it and add `import urllib.request, urllib.error` + guarded `import yaml` (the only eager names the tests still read at runtime; `pa.signal` appears only inside a string literal at test 567, `pa.time` is never read — neither needed).

> **Ruling 13 (amended) — verified repl.py header** (plan's Step 1 was aspirational; scan of the moved 171-518 shows): stdlib `os, readline, signal, sys, threading` (NOT time/urllib); config `HISTORY_PATH, TOOL_MODES, active_cfg, load_state` (NOT save_state/data_dir); store `_load_store, _store_get, ACTIVE_SESSION_KEY` (NOT _store_set); client `cancel_agent`; sessions `load_session, save_session, delete_session, _find_session, _rename_session_in_store, auto_title, _unique_session_name, trim_history, new_session_name, context_usage` (NOT estimate_tokens/sessions_map/summarize_with_llm/compress_history); tools `_set_tool_mode, _sync_tool_mode, active_tools, tool_skill_remove` + `import alvaagent.tools as _tools` (repl's `/tools` branch reads `_tools._TOOLS_MODE` — Ruling 7/11 qualified idiom); tui `C, COLOR, banner, col, compress_now, on_tool, p_err, p_info, p_ok, p_warn, print_user_turn, render_agent_panel, render_status_bar, run_agent_tui, set_active_skin` + `import alvaagent.tui as _tui` (repl prompt reads `_tui.CUR_SKIN` — Ruling 11); commands `_SLASH_COMMANDS, ask_permission` + the 20 dispatched `cmd_*` (cmd_clear, cmd_compress, cmd_config, cmd_context, cmd_export, cmd_help, cmd_improve, cmd_install_skill, cmd_memory, cmd_models, cmd_multi, cmd_provider, cmd_self_test, cmd_sessions, cmd_skill_category, cmd_skills, cmd_skin, cmd_test, cmd_tools, cmd_trace — cmd_todos/todo/feedback/reflect NOT used by the REPL); util `_fmt_k`. `_agent`/`_perms` stay as the existing local imports inside `main()` (Ruling 2 form already present — move verbatim, do NOT rework).

### Task 13: Extract `repl.py` + make `alvaagent_tui.py` a shim

**Files:**
- Create: `alvaagent/repl.py`
- Modify: `alvaagent_tui.py` (becomes the shim), `alvaagent/__init__.py` (drop the eager block, keep the proxy), `alvaagent/__main__.py`

**Interfaces:**
- Consumes: everything — `config`, `store`, `permissions`, `skills`, `tools`, `client`, `sessions`, `agent`, `tui`, `commands`.
- Produces: `setup_completion()`, `save_completion_history()`, `send_message(text, history, state, session)`, `repl()`, `main()`, `_slash_complete`.

- [ ] **Step 1: Create `alvaagent/repl.py`**

Move verbatim from `alvaagent_tui.py`: lines **171-517** — the `# ---------------- REPL ----------------` marker through the end of `main()` (`        _cleanup()` at 517), trimming trailing blanks. Contains `setup_completion`, `save_completion_history`, `_slash_complete`, `send_message`, `repl`, `main`. The `if __name__ == "__main__": main()` block (old 520-521) does NOT move — it belongs to the shim.

Header imports (VERIFIED — use these, then let pyflakes F401 refine):

```python
import os
import readline
import signal
import sys
import threading

from alvaagent.config import HISTORY_PATH, TOOL_MODES, active_cfg, load_state
from alvaagent.store import _load_store, _store_get, ACTIVE_SESSION_KEY
from alvaagent.client import cancel_agent
from alvaagent.sessions import (
    auto_title, context_usage, delete_session, _find_session, load_session,
    new_session_name, _rename_session_in_store, save_session, trim_history,
    _unique_session_name,
)
from alvaagent.tools import (
    _set_tool_mode, _sync_tool_mode, active_tools, tool_skill_remove,
)
from alvaagent.tui import (
    C, COLOR, banner, col, compress_now, on_tool, p_err, p_info, p_ok, p_warn,
    print_user_turn, render_agent_panel, render_status_bar, run_agent_tui,
    set_active_skin,
)
from alvaagent.commands import (
    _SLASH_COMMANDS, ask_permission,
    cmd_clear, cmd_compress, cmd_config, cmd_context, cmd_export, cmd_help,
    cmd_improve, cmd_install_skill, cmd_memory, cmd_models, cmd_multi,
    cmd_provider, cmd_self_test, cmd_sessions, cmd_skill_category, cmd_skills,
    cmd_skin, cmd_test, cmd_tools, cmd_trace,
)
from alvaagent.util import _fmt_k
import alvaagent.tools as _tools
import alvaagent.tui as _tui
```

`main()` keeps its existing body verbatim: `import alvaagent.agent as _agent`, `import alvaagent.permissions as _perms`, `_agent.ON_TOOL = on_tool`, `_perms.ON_PERMISSION = ask_permission`, plus the signal/alt-screen `_cleanup`/`_restored`/`banner(state)`/`repl()` handling. `ask_permission` comes from commands, `on_tool` from tui, `_perms`/`_agent` are local module aliases. The REPL marker comment travels into repl.py as its section header.

- [ ] **Step 2: Turn `alvaagent_tui.py` into the shim**

Replace the entire contents of `alvaagent_tui.py` with:

```python
#!/usr/bin/env python3
"""Compatibility shim — the real app lives in the alvaagent package.

Keeps the historical entry points working unchanged:
    python3 alvaagent_tui.py   (start.sh and the `alvaagent` launcher)
    import alvaagent_tui       (old docs / external scripts)
"""
from alvaagent.repl import (  # noqa: E402,F401
    _slash_complete, main, repl, save_completion_history, send_message,
    setup_completion,
)

if __name__ == "__main__":
    main()
```

The shim re-exports the full repl surface (not just `main`) because the facade's `_Facade` proxy reads `pa.<name>` by forwarding to this module first (Ruling 13) — `pa.send_message`, `pa.setup_completion`, `pa.save_completion_history`, `pa.main` all resolve here. `start.sh`, `alvaagent.sh`, `alva_fix.sh`, and the launcher keep working (they exec `python3 alvaagent_tui.py`).

- [ ] **Step 3: Update `__main__.py`**

```python
from alvaagent.repl import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Patch the facade (KEEP the proxy — Ruling 13)**

In `alvaagent/__init__.py`:
1. Keep lines 20-23 (`_tui = _sys.modules.get("alvaagent_tui")` / `import alvaagent_tui` fallback) — `_Facade._tui` must point at the shim.
2. Remove the eager re-export block (lines 24-33: the `if "_store" in _tui.__dict__:` star-import + explicit `from alvaagent_tui import (...)`).
3. Add near `import subprocess` (line 7):
   ```python
   import urllib.error  # noqa: F401
   import urllib.request  # noqa: F401
   try:
       import yaml  # noqa: F401
   except ImportError:
       yaml = None
   ```
   (the only eager-list names the test suite still reads at runtime; `pa.signal` appears only inside a string literal at test 567, `pa.time` is never read.)
4. **KEEP the `_Facade` class + write-through + `_Facade._tui = _tui` + `__class__ = _Facade` unchanged.** Update the stale comments (the "until Task 13" note at ~122-123 and the import-order comment at 10-19) to describe the new arrangement: reads forward to the shim (repl surface) with fallback to the facade's own re-exported namespace; writes land on the facade + shim + every loaded `alvaagent.*` submodule exposing the name.
5. Do NOT add `from alvaagent.repl import ...` to the facade (import-order-2 circularity — Ruling 13).

- [ ] **Step 5: Verify `import alvaagent_tui` still works**

Run:

```bash
python3 -c "import alvaagent_tui; print('shim ok', alvaagent_tui.main)"
python3 -c "import alvaagent as pa; print('facade ok', pa.main)"
```

Both must print without error.

- [ ] **Step 6: Run the tests**

Run: `python3 test_tui.py` — Expected: `ALL TESTS PASSED ✓`

- [ ] **Step 7: Commit**

```bash
git add alvaagent/repl.py alvaagent_tui.py alvaagent/__init__.py alvaagent/__main__.py
git commit -m "refactor: extract repl.py (REPL, send_message, main, signal/screen handling); alvaagent_tui.py becomes a shim"
```

---

## Phase B — Runtime context object (thread + bridge, behavior-preserving)

> **Design authority:** `docs/superpowers/specs/2026-08-14-runtime-context-design.md`
> (spec, committed `b0b4e11`). This plan implements that spec; the spec wins on
> any conflict with the older Phase B section it replaces.
>
> **Ruling 14 (bridge sequencing):** the spec's §5 listed the facade bridge as a
> final commit, but the original suite must be green after EVERY commit. The
> facade bridge pieces therefore land in the SAME commit as the module threading
> they serve: commit 1 introduces `_get_rt()`; each later commit adds that
> module's accessors/adapters. The `_Facade` proxy, generic write-through, and
> shim stay intact throughout Phase A and are retired only in Task 15.

### Task 14: Thread `Runtime` through every module; bridge keeps original suite green

**Goal:** retire the mutable process globals by threading a `Runtime` context
object; the ORIGINAL `test_tui.py` (249 checks) keeps passing essentially
unchanged as the machine proof that behavior is preserved.

**Files:**
- Create: `alvaagent/context.py`
- Modify: `alvaagent/{store,config,permissions,skills,tools,client,agent,sessions,trace,tui,commands,repl}.py`
- Modify: `alvaagent/__init__.py` (facade bridge, per-commit)
- Modify: `test_tui.py` (ONLY the `_tools._TOOLS_MODE` block in Step 2 — no other test edits in Phase A)
- Test: `python3 test_tui.py` — must end `ALL TESTS PASSED ✓` after every commit

**Interfaces (spec §2):**

- `alvaagent/context.py`:

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

- Module signatures (all rt-first; details per step below):
  - store: `load(rt)`, `save(rt)`, `get(rt, key, default=None)`, `set(rt, key, value)`
  - config: `load_state()` (pure), `save_state(rt)`, `active_cfg(rt)`
  - permissions: `request_permission(rt, desc)` (matches current `_permission(desc)`; the spec's vestigial `ok=True, hint=""` params do NOT exist in current code — YAGNI), `classify_file_action(rt, path, mode)`
  - skills: `skill_list(rt)`, `skill_read(rt, name)`, `skill_save(rt, name, content, category=None)`, `skill_remove(rt, name)`, `skill_install(rt, source, category=None)`, `skill_sync_repo(rt, repo, subdir=None)`
  - tools: `Tools(rt)` class (one method per TOOLS schema name, takes full `args` dict), `dispatch_tool(rt, name, args)`, `visible(rt)`, `set_mode(rt, mode)`, `maybe_enable_full(rt, name)`, `self_test(rt)`; delete `TOOL_IMPL` dict and `_TOOLS_MODE`
  - client: `chat_completion(rt, messages, **kw)`, `chat_completion_stream(rt, messages, **kw)`, `fetch_models(rt, base_url, api_key)`, `cancel_agent(rt)` → `rt.cancel.set()`; streams check `rt.cancel.is_set()`
  - agent: `run_agent(rt, messages)`, `run_agent_stream(rt, messages)`; calls `rt.on_tool` if set; delete `ON_TOOL` global
  - sessions: `sessions_map(rt)`, `load_session(rt)`, `save_session(rt)`, `delete_session(rt, name)`, `rename_session(rt, old, new)`, `find_session(rt, name)`, `unique_session_name(rt, name)`, `context_usage(rt, history)`, `compress_now(rt, threshold=None)`, `trim_history(rt, history)`, `auto_title(rt, text, history)`, `new_session_name(rt)`, `summarize(rt, ...)`
  - trace: `trace(rt, **event)`, `read_trace(rt, n)`, `trace_count(rt)` — paths derived from `rt.data_dir`
  - tui: `set_active_skin(rt)`, `run_agent_tui(rt)`, `render_status_bar(rt, session, elapsed, tools, history)`, `print_user_turn(rt, ...)`; tool open/close operate on `rt.spinner`; delete `_UI` global, `CUR_SKIN`/`COLOR` derive from `rt.skin`
  - commands: `cmd_*(rt, ...)`; `ask_permission(rt, ...)`
  - repl: `send_message(rt, text) -> str`, `repl(rt)`, `main()` = build_runtime + hooks + `set_active_skin(rt)` + `banner(rt)` + `repl(rt)`

---

- [ ] **Step 1: context.py + thread `store`/`config`/`permissions` + their facade accessors**

1. Create `alvaagent/context.py` with the exact code above.
2. `store.py`: delete the `_store` module global and the import-time
   `_load_store()` call. Add `load(rt)` (was `_load_store`), `save(rt)` (was
   `_save_store`), `get(rt, key, default=None)` (was `_store_get`),
   `set(rt, key, value)` (was `_store_set`). All internal reads/writes go
   through `rt.store`. `_migrate_legacy_dir(data_dir)` is unchanged.
3. `config.py`: `save_state(state)` → `save_state(rt)` (persists `rt.cfg`);
   `active_cfg(state)` → `active_cfg(rt)`. `load_state()` stays pure. Delete
   the module-level `active_cfg` global if one exists (it does not — verify
   with grep). `TRACE_PATH` stays a config constant but `trace.py` derives the
   actual path from `rt.data_dir` (Step 3).
4. `permissions.py`: rename `_permission(desc)` → `request_permission(rt, desc)`
   (same body, reading `rt.approved`/`rt.on_permission` instead of the
   globals); `classify_file_action(path, mode)` → `classify_file_action(rt,
   path, mode)` (uses `rt.data_dir` + `PROJECT_DIR`). Delete `_APPROVED_SET`
   and `ON_PERMISSION` globals. `classify_command` and `_in_project` unchanged
   (pure; `PROJECT_DIR`/`DATA_DIR` module constants stay).
5. `alvaagent/__init__.py` — bridge for the retired names (do NOT touch the
   `_Facade` class or write-through yet):

```python
_rt = None
def _get_rt():
    global _rt
    if _rt is None:
        _rt = build_runtime()
    return _rt
```

   Add to the `_Facade` class (properties on the facade module class so both
   read (`__getattribute__`) and write (`__setattr__`) resolve through them —
   `super().__getattribute__`/`super().__setattr__` honor data descriptors):

```python
    @property
    def _store(self):
        return _get_rt().store

    @property
    def _APPROVED_SET(self):
        return _get_rt().approved

    @property
    def ON_PERMISSION(self):
        return _get_rt().on_permission

    @ON_PERMISSION.setter
    def ON_PERMISSION(self, value):
        _get_rt().on_permission = value

    @property
    def active_cfg(self):
        return _get_rt().active_cfg

    @active_cfg.setter
    def active_cfg(self, value):
        rt = _get_rt()
        rt.cfg["profiles"][rt.cfg["active"]] = value
```

   And facade adapter functions (module-level, plain defs):

```python
def _permission(desc):
    return request_permission(_get_rt(), desc)

def _store_get(key, default=None):
    return store_get(_get_rt(), key, default)

def _save_store():
    store_save(_get_rt())
```

   (Import `store.get`/`store.set`/`store.load`/`store.save` as
   `store_get`/`store_set`/`store_load`/`store_save` if their names collide
   with facade re-exports, or import the store module as `_store_mod`.)
6. Verify (must ALL pass):
   - `python3 test_tui.py` → `ALL TESTS PASSED ✓`
   - `python3 -c "import alvaagent as pa; pa._APPROVED_SET.clear(); assert isinstance(pa._store, dict); print('bridge ok')"`
7. Commit:

```bash
git add alvaagent/context.py alvaagent/store.py alvaagent/config.py alvaagent/permissions.py alvaagent/__init__.py
git commit -m "refactor: introduce Runtime context; thread store/config/permissions; facade bridge accessors"
```

---

- [ ] **Step 2: Thread `skills`/`tools` + tool adapters + the surgical test edit**

1. `skills.py`: `tool_skill_list/read/save/remove/install/sync_repo` →
   `skill_list(rt)`, `skill_read(rt, name)`, `skill_save(rt, name, content,
   category=None)`, `skill_remove(rt, name)`, `skill_install(rt, source,
   category=None)`, `skill_sync_repo(rt, repo, subdir=None)`. Paths derive
   from `rt.skills_dir` (drop the `SKILLS_DIR` module global; keep `_inside_skills`
   etc. taking a dir). Facade: re-export the new names as `tool_skill_list(rt)`
   etc. AND keep flat adapters `tool_skill_list()`, `tool_skill_read(name)`,
   `tool_skill_save(name, content, category=None)`, `tool_skill_remove(name)`,
   `tool_skill_install(source, category=None)`, `tool_skill_sync_repo(repo,
   subdir=None)` that call `_get_rt()` + the new functions.
2. `tools.py`: build the `Tools` class — one method per name in `TOOLS` (from
   the `TOOL_IMPL` dict keys), each method takes the full `args` dict and
   calls the existing private logic now taking `(rt, ...)`. E.g.:

```python
class Tools:
    def __init__(self, rt):
        self.rt = rt

    def calculator(self, args):
        return tool_calculator(self.rt, args.get("expression"))

    def run_command(self, args):
        return tool_run_command(self.rt, args.get("command"))
```

   Replace `TOOL_IMPL`-driven dispatch with:

```python
def dispatch_tool(rt, name, args):
    tool = getattr(Tools(rt), name, None)
    if tool is None:
        return {"error": "unknown tool: %s" % name}
    switched = maybe_enable_full(rt, name)
    try:
        result = tool(args)
        if isinstance(result, dict) and not result.get("ok", True) and "hint" not in result:
            result["hint"] = _TOOL_ERROR_HINTS.get(name, "")
        if switched and isinstance(result, dict):
            result.setdefault("hint",
                "Advanced tool set enabled: all %d tools are now advertised to the model." % len(TOOLS))
        return result
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e),
                "hint": _TOOL_ERROR_HINTS.get(name, "check the tool arguments and try again")}
```

   Rename `_TOOLS_MODE` reads to `rt.tool_mode`; `_set_tool_mode(state, mode)` →
   `set_mode(rt, mode)`; `_sync_tool_mode(cfg)` → `sync_tool_mode(rt)`;
   `active_tools()` → `visible(rt)`; `_maybe_enable_full(name)` →
   `maybe_enable_full(rt, name)` (reads/writes `rt.tool_mode`); `self_test()` →
   `self_test(rt)`. Delete the `_TOOLS_MODE` global and the `TOOL_IMPL` dict.
   Keep constants (`_PY_RUN_TIMEOUT`, `_PY_MAX_BYTES`, `_PY_MAX_CHARS`,
   `_CALC_ALLOWED`, `_TOOL_ERROR_HINTS`, `TOOLS`, `_CORE_TOOL_NAMES`,
   `_ADVANCED_TOOL_NAMES`). Memory/todo/feedback/reflect tool logic takes
   `(rt, ...)` and reads/writes `rt.store` via `store.get(rt, ...)`/`store.set(rt, ...)`.
3. `alvaagent/__init__.py` — tool flat adapters (plain defs) for every name the
   suite calls: `tool_calculator(expr)`, `tool_todo_add(text)`,
   `tool_todo_list()`, `tool_todo_toggle(i)`, `tool_todo_remove(i)`,
   `tool_memory_save(k, v)`, `tool_memory_recall(k)`, `tool_memory_list()`,
   `tool_memory_search(q)`, `tool_get_time()`, `tool_run_command(cmd)`,
   `tool_file_read(p)`, `tool_file_write(p, c)`, `tool_file_edit(p, o, n)`,
   `tool_file_list(p)`, `tool_file_search(pat, path=None, max_depth=None)`,
   `tool_web_fetch(url)`, `tool_skill_*` (Step 2.1), `self_test()` →
   `tools_self_test(_get_rt())`, `dispatch_tool(rt, name, args)` re-exported.
   Each adapter: `return <rt-based fn>(_get_rt(), <old args mapped>)`.
   `tool_web_fetch(url)` keeps using `util._raw_fetch` (still module-level).
4. `test_tui.py` — the ONLY Phase A edit: the tiered-tool block
   (the lines using `_tools._TOOLS_MODE`, ~1133-1160). Rewrite it to drive rt:

```python
    _rt14 = pa.build_runtime()
    _tools_tool_mode = _rt14.tool_mode
    _rt14.tool_mode = "full"
    assert_ok(pa.visible(_rt14) == pa.TOOLS, "full mode advertises all tools")
    _res14 = pa.dispatch_tool(_rt14, "self_test", {})
    assert_ok(_rt14.tool_mode == "full" and "hint" in _res14,
              "dispatch_tool auto-enables full mode for advanced tools")
    _rt14.tool_mode = "core"
    assert_ok(pa.visible(_rt14) != pa.TOOLS, "core mode hides advanced tools")
    _rt14.tool_mode = _tools_tool_mode
```

   Preserve the block's original assertions/restore intent exactly.
5. Verify:
   - `python3 test_tui.py` → `ALL TESTS PASSED ✓`
   - `/data/data/com.termux/files/usr/bin/python3 -m pyflakes alvaagent/tools.py alvaagent/skills.py` → 0 findings
   - `python3 -c "import alvaagent as pa; print(pa.tool_calculator('6*7'))"`
6. Commit:

```bash
git add alvaagent/skills.py alvaagent/tools.py alvaagent/__init__.py test_tui.py
git commit -m "refactor: thread Runtime through skills/tools (Tools class, dispatch_tool, set_mode, self_test); flat tool adapters"
```

---

- [ ] **Step 3: Thread `client`/`agent`/`sessions`/`trace` + their facade adapters**

1. `client.py`: `chat_completion(rt, messages, **kw)`,
   `chat_completion_stream(rt, messages, **kw)`, `fetch_models(rt, base_url,
   api_key)`; replace `_cancel_flag` (imported from util as a `[False]` list)
   with `rt.cancel` (`rt.cancel.is_set()` checks, `cancel_agent(rt)` →
   `rt.cancel.set()`); remove `_cancel_flag` from `util.py` if unused elsewhere
   (grep; `agent.py` also uses it — handled this step). Keep `_sleep_retry`,
   `_MAX_RETRIES`, `_RETRY_BACKOFF`, `_STREAM_*`, `_readable_error`,
   `_retryable_status`, `SYSTEM_PROMPT`.
2. `agent.py`: `run_agent(rt, messages)`, `run_agent_stream(rt, messages)`;
   delete `ON_TOOL` global — agent calls `rt.on_tool(...)` if set; delete the
   `_cancel_flag` import (uses `rt.cancel`); `_TURN_TIMEOUT`,
   `_MAX_CONSEC_TOOL_FAILURES`, XML-parsing helpers stay module-level.
3. `sessions.py`: all functions rt-first per Interfaces; session/history read
   from `rt.session`/`rt.history`; `compress_now(rt, threshold=None)`;
   summarize/auto_title take rt (LLM via `client.chat_completion(rt, ...)`).
4. `trace.py`: `trace(rt, **event)`, `read_trace(rt, n)`, `trace_count(rt)`;
   path derived from `rt.data_dir` (keep `_TRACE_MAX_LINES`/`_TRACE_MAX_BYTES`
   constants).
5. `alvaagent/__init__.py` — adapters: `chat_completion(messages, **kw)` →
   `client.chat_completion(_get_rt(), messages, **kw)`;
   `chat_completion_stream(messages, **kw)`;
   `fetch_models(base_url, api_key)`; `run_agent(messages_json, cfg_json)` →
   `rt = _get_rt(); rt.cfg = json.loads(cfg_json); return agent.run_agent(rt, json.loads(messages_json))`;
   `run_agent_stream(messages, cfg)` similarly; `cancel_agent()` (flat) →
   `client.cancel_agent(_get_rt())`. Re-export `context_usage(rt, history)` /
   `save_session(rt, ...)` / `compress_now(rt, threshold=None)` module
   functions (write-through keeps patching them for the tests). Update the
   facade re-export blocks to the rt signatures.
6. Verify: `python3 test_tui.py` → `ALL TESTS PASSED ✓`; pyflakes clean on
   client/agent/sessions/trace; smoke: `python3 -c "import alvaagent as pa; print(pa.run_agent)"`.
7. Commit:

```bash
git add alvaagent/client.py alvaagent/agent.py alvaagent/sessions.py alvaagent/trace.py alvaagent/util.py alvaagent/__init__.py
git commit -m "refactor: thread Runtime through client/agent/sessions/trace (rt.cancel, rt.on_tool); facade adapters"
```

---

- [ ] **Step 4: Thread `tui`/`commands`/`repl` + `main()` + their facade adapters**

1. `tui.py`: `set_active_skin(rt)`, `run_agent_tui(rt)`,
   `render_status_bar(rt, session, elapsed, tools, history)`,
   `print_user_turn(rt, ...)`; tool open/close operate on `rt.spinner`; delete
   `_UI` global; skin/color helpers read `rt.skin` (CUR_SKIN/COLOR stay as
   derived module-level render values for pure helpers OR move to a
   `_resolve_skin(rt)` helper — keep pure render functions `(skin, ...)`
   unchanged, only call sites resolve skin from rt). `on_tool` no longer lives
   in tui; `banner(rt)`, `ask_permission(rt, ...)` reads `rt.skin`.
2. `commands.py`: every `cmd_*(rt, ...)`; `ask_permission(rt, ...)`;
   `pick_model(rt, ...)`; helpers take rt where they read state; the
   `_SLASH_COMMANDS` dict stays.
3. `repl.py`: `send_message(rt, text) -> str` (reads `rt.history`,
   `rt.session`, `rt.active_cfg`, writes `rt.last_turn`, returns session);
   `repl(rt)`; `main()`:

```python
def main():
    rt = build_runtime()
    rt.on_permission = ask_permission
    rt.on_tool = on_tool
    set_active_skin(rt)
    # ... existing signal + alternate-screen handling verbatim ...
    banner(rt)
    repl(rt)
```

   Delete the `_last_turn` module global (→ `rt.last_turn`). Import
   `build_runtime`, `set_active_skin`, `on_tool` from the leaves.
4. `alvaagent/__init__.py` — adapters: `send_message(text, history, state,
   session)` → `rt = _get_rt(); rt.history = history; rt.cfg = state; rt.session = session; return repl.send_message(rt, text)`;
   `cmd_provider(state, args)` → `rt = _get_rt(); rt.cfg = state; return commands.cmd_provider(rt, args)`;
   `cmd_trace(n)` → `commands.cmd_trace(_get_rt(), n)`;
   `ask_permission(desc)` → `commands.ask_permission(_get_rt(), desc)`;
   `run_agent_tui(history, cfg)` → build rt, `tui.run_agent_tui(rt)`;
   `print_user_turn(...)`/`render_agent_panel(...)`/`render_status_bar(...)` →
   rt-first. Keep the shim + `_Facade` intact.
5. Verify:
   - `python3 test_tui.py` → `ALL TESTS PASSED ✓`
   - Both import orders + app entry: `timeout 20 python3 -c "import alvaagent_tui; import alvaagent as pa; pa.main()" < /dev/null` → banner + prompt + exit 0; `python3 -m alvaagent` starts
   - pyflakes clean on tui/commands/repl
   - `grep -rn '^_store\b\|^_TOOLS_MODE\|^_APPROVED_SET\|^ON_PERMISSION\|^ON_TOOL\|^_last_turn\|^_cancel_flag\|^_UI ' alvaagent/*.py` → no hits in leaf modules
6. Commit:

```bash
git add alvaagent/tui.py alvaagent/commands.py alvaagent/repl.py alvaagent/__init__.py
git commit -m "refactor: thread Runtime through tui/commands/repl; main() builds runtime and threads it; send_message(rt, text)"
```

---

- [ ] **Step 5: Phase A full verification + final commit**

1. Suite ×2: `python3 test_tui.py` twice → `ALL TESTS PASSED ✓` both runs.
2. Smoke probes:

```bash
python3 -c "import alvaagent as pa; rt=pa.build_runtime(); print(pa.tool_calculator('2+3'))"
timeout 20 python3 -c "import alvaagent_tui; import alvaagent as pa; pa.main()" < /dev/null   # banner + prompt + bye, exit 0
python3 -m alvaagent   # starts (kill after banner)
```

3. Write-through still reaches rt-backed functions:

```bash
python3 -c "
import alvaagent as pa, sys
_old = pa.save_session
pa.save_session = lambda *a, **k: None
assert sys.modules['alvaagent.sessions'].save_session is pa.save_session
pa.save_session = _old
print('write-through ok')"
```

4. pyflakes all modules: only sanctioned re-export F401 noise in
   `alvaagent/__init__.py`; 0 findings elsewhere.
5. `git status` clean except intended; commit any stragglers (none expected).
6. Record the Phase A summary in `.superpowers/sdd/.../progress.md`.

---

## Phase C — Runtime migration (suite rewrite + retirement)

### Task 15: Migrate `test_tui.py` to the rt API; retire the facade bridge + proxy

**Goal:** the package becomes a clean rt API: the suite is pytest-style with
per-test isolation, and the facade bridge (default rt, accessors, adapters,
`_Facade` proxy, write-through) is deleted.

**Files:**
- Modify: `test_tui.py` (full conversion to pytest-style functions + rt API)
- Modify: `alvaagent/__init__.py` (delete bridge; plain module re-exports)
- Modify: `alvaagent_tui.py` (unchanged shim; verify)
- Test: `python3 test_tui.py` (bundled zero-dep runner) — new pytest-style suite green; pytest-collection-compatible

**Interfaces (spec §4):** `mkrt(data_dir=None)` helper; per-test fresh rt/DATA;
mock server session fixture; architecture checks.

- [ ] **Step 1: Add `mkrt` + session fixture + convert the harness**

Restructure `test_tui.py`:
- Keep the top (PORT, BASE, MOCK, mock-server management) but move DATA setup
  into per-test code: `def mkrt(data_dir=None): return pa.build_runtime(data_dir=data_dir)`.
- Convert each inline check block into `def test_<name>():` functions. Stateful
  blocks (permission cache, store, todo/memory) build their own rt and DATA.
- Keep `assert_ok` semantics; add a summary.
- Add the bundled runner at the bottom:

```python
if __name__ == "__main__":
    import traceback as _tb
    _names = [n for n in sorted(dir()) if n.startswith("test_") and callable(globals()[n])]
    _fails = 0
    for _n in _names:
        _nf_before = failures
        try:
            globals()[_n]()
        except Exception as _e:
            _tb.print_exc()
            failures += 1
        _fails += failures - _nf_before
    print("\n%d tests, %d failures" % (len(_names), _fails))
    sys.exit(1 if _fails else 0)
```

  Output must still end with `ALL TESTS PASSED ✓` on success.

- [ ] **Step 2: Migrate every flat call site to the rt API**

Rewrite each test to the rt signatures, preserving assertions 1:1:
- `pa.tool_calculator(x)` → `pa.Tools(rt).calculator({"expression": x})` (and
  every other tool, mapping args exactly as in the old plan's Step 6 list)
- `pa.send_message(text, history, state, session)` → `pa.send_message(rt, text)`
  (rt built with the needed history/session/state)
- `pa.run_agent(h_json, cfg_json)` → `pa.run_agent(rt, json.loads(h_json))`
- `pa.chat_completion(messages, **kw)` → `pa.chat_completion(rt, messages, **kw)`
- `pa.cmd_provider(state, args)` → `pa.cmd_provider(rt, args)`;
  `pa.cmd_trace("3")` → `pa.cmd_trace(rt, "3")`
- `pa._permission(desc)` → `pa.request_permission(rt, desc)`;
  `pa._APPROVED_SET` → `rt.approved`; `pa._store` → `rt.store`;
  `pa._save_store()` → `pa.store_save(rt)` (or `store.save(rt)`)
- DI seams: `pa._sleep_retry = …` → `client._sleep_retry = …`;
  `pa._raw_fetch = …` → `util._raw_fetch = …` (qualified-name patches)
- `pa.ON_PERMISSION = …` → `rt.on_permission = …`
- `pa.active_cfg = …` → write into `rt.cfg`'s active profile

- [ ] **Step 3: Delete the bridge**

`alvaagent/__init__.py`: remove `_get_rt()`, the `_Facade` class, all
accessors/adapters, the write-through, the `_Facade._tui` read-forward, and
the `__class__` swap. Make `alvaagent/__init__.py` a plain module re-exporting
`Runtime`, `build_runtime`, `Tools`, `dispatch_tool`, `visible`, `set_mode`,
and the rt-based module functions (config/store/permissions/skills/tools/
client/agent/sessions/trace/tui/commands/repl surfaces). Keep the shim
(`alvaagent_tui.py`) re-exporting the repl surface for `python3
alvaagent_tui.py`. Verify both import orders and app entry still work.

- [ ] **Step 4: Add the architecture tests**

- No-import-cycles: import every `alvaagent.*` module in sequence; must not raise.
- Facade surface: `Runtime`, `build_runtime`, `Tools`, `dispatch_tool`,
  `TOOLS`, `SKINS`, `AgentWriter`, `Spinner`, `chat_completion`,
  `chat_completion_stream`, `run_agent`, `run_agent_stream`,
  `classify_command`, `classify_python`, `load_session`, `main` all exist.
- No retired globals: assert none of `_store`, `_TOOLS_MODE`, `_APPROVED_SET`,
  `_cancel_flag`, `ON_PERMISSION`, `ON_TOOL`, `_last_turn`, `_UI` exist as
  leaf-module module-globals (iterate `alvaagent.*` module dicts).

- [ ] **Step 5: Verify + commit**

- `python3 test_tui.py` → green, ends `ALL TESTS PASSED ✓`
- `python3 -m pyflakes alvaagent/*.py` → only sanctioned facade F401 noise
- both import orders + `python3 alvaagent_tui.py` start
- `git grep -n '_TOOLS_MODE\|^_store\|_APPROVED_SET' alvaagent/` → only
  docs/comments
- Commit:

```bash
git add test_tui.py alvaagent/__init__.py
git commit -m "refactor: retire facade bridge and proxy; test suite uses explicit Runtime (pytest-style)"
```

---
### Task 16: Documentation

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `docs/superpowers/specs/2026-08-14-package-rearchitecture-design.md` (update Status to "Implemented")

- [ ] **Step 1: Update README.md**

- Change the intro/Features framing from "single-file Python TUI" to "a stdlib-only Python package (`alvaagent/`) plus a one-file launcher shim (`alvaagent_tui.py`)". Keep the "Zero pip installs" promise.
- In "Install on Termux": `python3 alvaagent_tui.py` and `python3 -m alvaagent` both work; the `alvaagent` symlink and `bash start.sh tui` are unchanged.
- In "Tests": `python3 test_tui.py` unchanged.
- In "Files": replace the `alvaagent_tui.py — the harness` line with a short list of the package modules (`context.py`, `config.py`, `store.py`, `permissions.py`, `skills.py`, `tools.py`, `client.py`, `agent.py`, `sessions.py`, `tui.py`, `commands.py`, `repl.py`) + "`alvaagent_tui.py` — one-line launcher shim".
- Update "Updating" (unchanged behavior; note git pull still never touches `.alvaagent/`).

- [ ] **Step 2: Update AGENTS.md**

- "Single-file Python TUI" → "Python package (`alvaagent/`) with a one-file shim (`alvaagent_tui.py`) for the historical entry points."
- **Commands:** keep `python3 test_tui.py`; launch via `python3 alvaagent_tui.py`, `python3 -m alvaagent`, `bash start.sh tui`, or `alvaagent`.
- **Architecture:** replace the "one file, three roles" paragraph with the module map + the Runtime context object + the downward dependency rule. New tool = a `Tools` method + a schema entry in `TOOLS`. Keep the streaming/session-model/turn-safety bullet points (they still hold — adjust any line references to modules).
- **Persistence, Skills, Self-modification hygiene, Git:** update paths (`alvaagent/store.py`, `alvaagent/config.py`, etc.) where they name the single file. The hygiene rules (clean tree, `git add <file>`, no stray files) stay identical.
- Add a one-line note: module globals are banned except `tui.py`'s current-skin; state lives on `Runtime`.

- [ ] **Step 3: Mark the spec implemented**

Edit the spec header: `Status: Approved design (spec for implementation)` → `Status: Implemented`.

- [ ] **Step 4: Final verification**

Run `python3 test_tui.py` → `ALL TESTS PASSED ✓`. Run:

```bash
git status
```

Expected: only the files from this task are modified/untracked. Then:

```bash
git add README.md AGENTS.md docs/superpowers/specs/2026-08-14-package-rearchitecture-design.md
git commit -m "docs: document the package layout, Runtime context, and updated entry points (README/AGENTS)"
```

---

## Self-Review Notes (checked before saving)

- **Spec coverage:** every spec section maps to tasks — layout (Tasks 1-13), Runtime (Task 14), dependency rules (enforced in each extraction + Task 14), back-compat facade/shim (Tasks 1, 13), testing strategy (Task 15, plus the `ALL TESTS PASSED` gate on every task), sequencing (Tasks 1-16), docs (Task 16), risks (mitigations embedded: test-green checkpoints per task, bottom-up extraction to avoid cycles, comprehensive facade).
- **Placeholder scan:** no TBD/TODO; every task has concrete steps with code or exact move instructions and a commit.
- **Type consistency:** `dispatch_tool(rt, name, args)`, `Tools(rt)`, `build_runtime(data_dir=...)`, `request_permission(rt, ...)`, `send_message(rt, text)`, `set_active_skin(rt)`, `visible(rt)`, `set_mode(rt, mode)` are used consistently across Tasks 14-15.
