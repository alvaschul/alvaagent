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
    agent.py        run_agent, run_agent_stream, _repair_tool_pairs, trace
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

Dependency direction: `util` → `config`/`store` → `permissions`/`skills` → `tools` → `client`/`agent`/`sessions` → `tui` → `commands` → `repl`.

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
```

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
- `PROVIDERS`, `DEFAULT_CFG`, `FIRST_RUN_CFG`, `DEFAULT_SKIN`, `SKIN_NAMES`, `ALVA_VERSION`, `DEFAULT_CONTEXT_WINDOW`, `MODEL_CONTEXT` (lines ~139-181, verbatim).
- `TOOL_MODES = ("core", "full")` (move the tuple; **delete it from the tools section of `alvaagent_tui.py` now**).
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
    data_dir, CONFIG_PATH, STORE_PATH, HISTORY_PATH, TRACE_PATH,
    PROVIDERS, DEFAULT_CFG, FIRST_RUN_CFG, DEFAULT_SKIN, SKIN_NAMES,
    ALVA_VERSION, DEFAULT_CONTEXT_WINDOW, MODEL_CONTEXT, TOOL_MODES,
    _tool_mode_of, _skin_of, _normalize_state, load_state, save_state, active_cfg,
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

- [ ] **Step 4: Re-export from the facade**

Add to `alvaagent/__init__.py`:

```python
from alvaagent.config import (  # noqa: F401
    data_dir, CONFIG_PATH, STORE_PATH, HISTORY_PATH, TRACE_PATH,
    PROVIDERS, DEFAULT_CFG, FIRST_RUN_CFG, DEFAULT_SKIN, SKIN_NAMES,
    ALVA_VERSION, DEFAULT_CONTEXT_WINDOW, MODEL_CONTEXT, TOOL_MODES,
    _tool_mode_of, _skin_of, _normalize_state, load_state, save_state, active_cfg,
)
```

- [ ] **Step 5: Run the tests**

Run: `python3 test_tui.py` — Expected: `ALL TESTS PASSED ✓`

- [ ] **Step 6: Commit**

```bash
git add alvaagent/config.py alvaagent/__init__.py alvaagent_tui.py
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
  - `_tokenize_shell(cmd) -> list`, `classify_command(cmd) -> "allow"|"ask"|"block"`
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

`classify_file_action`/`_in_project` use `DATA_DIR`/project dir — check the bodies: `_in_project` compares against the project folder. In `permissions.py` compute the project root from the package path the same way `config.data_dir()` does (repo root = package parent). Keep the allow/ask semantics byte-identical.

- [ ] **Step 2: Patch `alvaagent_tui.py`**

Delete the moved block and add the import:

```python
# permissions moved to alvaagent/permissions.py (Task 5)
from alvaagent.permissions import (  # noqa: E402,F401
    _READONLY_PREFIXES, _RISKY_TOKENS, _RISKY_OPERATORS, _tokenize_shell,
    classify_command, _in_project, classify_file_action, _APPROVED_SET,
    _permission, ON_PERMISSION,
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

(The `ON_TOOL` global is still in `alvaagent_tui.py` until Task 11.)

- [ ] **Step 3: Re-export from the facade**

```python
from alvaagent.permissions import (  # noqa: F401
    _READONLY_PREFIXES, _RISKY_TOKENS, _RISKY_OPERATORS, _tokenize_shell,
    classify_command, _in_project, classify_file_action, _APPROVED_SET,
    _permission, ON_PERMISSION,
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
- Produces: `_SKILL_FM_RE`, `_SKILL_FM_DEFAULT`, `_VALID_FM_KEYS`, `_SKILL_RAW_MAX`, `_skill_body_for_tool`, `_detect_category`, `_skill_filepath`, `_inside_skills`, `_resolve_skill_path`, `_skill_read`, `_scan_skill_files`, `_skill_list_all`, `tool_skill_list`, `tool_skill_read`, `tool_skill_remove`, `tool_skill_save`, `tool_skill_install`, `tool_skill_sync_repo`.

- [ ] **Step 1: Create `alvaagent/skills.py`**

Move verbatim from `alvaagent_tui.py`: the block between `# ---------------- skills: Hermes-style frontmatter ...` and `# ---------------- tools ...` (roughly lines 635-1215), EXCEPT the mini-yaml helpers already moved to `util.py` in Task 2 (`_mini_scalar`, `_finish_block`, `_mini_yaml`, `_frontmatter_load`, `_frontmatter_dump`, `_parse_frontmatter` — do not copy them again; import them instead). Also exclude `_atomic_write` (Task 2), `_looks_like_html`, `_raw_fetch` (Task 2).

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
    _parse_frontmatter, _frontmatter_dump,
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

- [ ] **Step 3: Re-export from the facade**

Add the same names from `alvaagent.tools` to `alvaagent/__init__.py`.

- [ ] **Step 4: Run the tests**

Run: `python3 test_tui.py` — Expected: `ALL TESTS PASSED ✓` (calculator, sandbox, classifiers, tool tiering, run_python, skill tools, self-test path checks).

- [ ] **Step 5: Commit**

```bash
git add alvaagent/tools.py alvaagent/__init__.py alvaagent_tui.py
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
- Consumes: `config` (`active_cfg`, `MODEL_CONTEXT`, `DEFAULT_CONTEXT_WINDOW`, `data_dir`), `store` (`_store_get`, `_store_set`, `ACTIVE_SESSION_KEY`, `MAX_SESSIONS`, `SESSION_KEY`), `client` (`chat_completion`), `util` (`now_iso`, `_fmt_k`).
- Produces: `context_window_for(model)`, `estimate_tokens(text)`, `estimate_message_tokens(m)`, `context_usage(history, cfg)`, `sessions_map()`, `load_session(name)`, `save_session(name, history)`, `delete_session(name)`, `_find_session(name)`, `_rename_session_in_store(old, new)`, `auto_title(text)`, `_unique_session_name(base)`, `summarize_with_llm(history, cfg)`, `_fallback_summary(history)`, `compress_history(history, cfg)`, `compress_now(history, cfg, threshold=None)`, `trim_history(history)`.

- [ ] **Step 1: Create `alvaagent/sessions.py`**

Move verbatim from `alvaagent_tui.py`:
- the context/sessions block between `# ---------------- context tracking & sessions ...` and `# ---------------- skins ...` (roughly lines 2851-3110),
- `trim_history` (line ~3776, currently in the display section — a history-management helper),
- `new_session_name` (line ~4690, currently in the REPL section).

Header imports:

```python
import json
import re

from alvaagent.config import active_cfg, MODEL_CONTEXT, DEFAULT_CONTEXT_WINDOW
from alvaagent.store import (
    _store_get, _store_set, ACTIVE_SESSION_KEY, MAX_SESSIONS, SESSION_KEY,
)
from alvaagent.client import chat_completion
from alvaagent.util import now_iso, _fmt_k
```

Check the moved bodies for any `HISTORY_KEY` / `SESSION_KEY` usage — `SESSION_KEY` is the sessions map key; keep imports consistent with store.py's constants. `summarize_with_llm` calls `chat_completion(state, ...)` — pass the `cfg`/`state` through as the original did (verbatim signatures).

- [ ] **Step 2: Patch `alvaagent_tui.py`**

Delete the moved blocks and add:

```python
# sessions/context/compression moved to alvaagent/sessions.py (Task 9)
from alvaagent.sessions import (  # noqa: E402,F401
    context_window_for, estimate_tokens, estimate_message_tokens, context_usage,
    sessions_map, load_session, save_session, delete_session, _find_session,
    _rename_session_in_store, auto_title, _unique_session_name,
    summarize_with_llm, _fallback_summary, compress_history, compress_now,
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

### Task 10: Extract `agent.py` (agent loop + trace)

**Files:**
- Create: `alvaagent/agent.py`
- Modify: `alvaagent_tui.py`, `alvaagent/__init__.py`

**Interfaces:**
- Consumes: `config` (`TRACE_PATH`, `active_cfg`, `data_dir`), `store` (`_store_get`? check bodies), `client` (`chat_completion_stream`, `_Cancelled`, `_sleep_retry`, `_cancel_flag`), `tools` (`TOOLS`, `dispatch_tool`, `TOOL_IMPL`, `active_tools`, `_maybe_enable_full`, `_TOOL_ERROR_HINTS`), `util`.
- Produces: `_TURN_TIMEOUT`, `_MAX_CONSEC_TOOL_FAILURES`, `_TRACE_MAX_LINES`, `_TRACE_MAX_BYTES`, XML regex consts (`_XML_*`), `_repair_tool_pairs`, `_trace(event)`, `_read_trace(n)`, `_trace_count()`, `_report_tool(...)`, `run_agent(messages, cfg, ...)` (verbatim), `_clean_segment`, `_strip_xml_blocks`, `_parse_xml_tool_calls`, `_strip_xml`, `run_agent_stream(messages, cfg, ...)` (verbatim).

- [ ] **Step 1: Create `alvaagent/agent.py`**

Move verbatim from `alvaagent_tui.py`: the block between `# ---------------- agent loop ...` and `# ---------------- harness self-test ...` (roughly lines 2292-2682).

Header imports:

```python
import json
import re

from alvaagent.config import TRACE_PATH, active_cfg
from alvaagent.client import (
    chat_completion_stream, _Cancelled, _sleep_retry, _cancel_flag,
)
from alvaagent.tools import TOOLS, dispatch_tool, active_tools, _maybe_enable_full, TOOL_IMPL
from alvaagent.store import _store_get, _store_set
```

The `_trace` functions write to `TRACE_PATH` — imported from config. `run_agent_stream` references `_cancel_flag`, `_TURN_TIMEOUT`, `_MAX_CONSEC_TOOL_FAILURES`, `dispatch_tool`, `_trace`, `_repair_tool_pairs` — all local to this module after the move. Check for `_sleep_retry` usage.

- [ ] **Step 2: Patch `alvaagent_tui.py`**

Delete the block and add:

```python
# agent loop moved to alvaagent/agent.py (Task 10)
from alvaagent.agent import (  # noqa: E402,F401
    _TURN_TIMEOUT, _MAX_CONSEC_TOOL_FAILURES, _TRACE_MAX_LINES, _TRACE_MAX_BYTES,
    _repair_tool_pairs, _trace, _read_trace, _trace_count, _report_tool,
    run_agent, _clean_segment, _strip_xml_blocks, _parse_xml_tool_calls,
    _strip_xml, run_agent_stream,
)
```

- [ ] **Step 3: Re-export from the facade**

Add the same names from `alvaagent.agent` to `alvaagent/__init__.py`.

- [ ] **Step 4: Run the tests**

Run: `python3 test_tui.py` — Expected: `ALL TESTS PASSED ✓` (agent-loop + streaming tests, trace checks, ghost-duplicate regression).

- [ ] **Step 5: Commit**

```bash
git add alvaagent/agent.py alvaagent/__init__.py alvaagent_tui.py
git commit -m "refactor: extract agent.py (turn loop, runaway guards, XML tool-call parsing, trace)"
```

---

### Task 11: Extract `tui.py` (skins + rendering)

**Files:**
- Create: `alvaagent/tui.py`
- Modify: `alvaagent_tui.py`, `alvaagent/__init__.py`

**Interfaces:**
- Consumes: `config` (`SKIN_NAMES`, `DEFAULT_SKIN`, `_skin_of`, `data_dir`), `agent` (`run_agent_stream`, `_trace`? check bodies), `util` (`_fmt_k`, `now_iso`? check), `sessions` (`context_usage`? check `render_status_bar`).
- Produces: `SKINS`, `C`, `DEFAULT_SKIN` usage, `set_active_skin(state)`, `col`, `p_info`, `p_err`, `p_ok`, `p_warn`, `_term_width`, `_hrgb`, `_fgh`, `_rsth`, `_tool_line`, `print_user_turn`, `render_agent_panel`, `_md_attr_sgr`, `_has_ansi`, `_md_line`, `_md_prefix`, `style_inline`, `AgentWriter`, `fmt_args`, `tool_summary`, `Spinner`, `tool_open`, `tool_close`, `on_tool`, `run_agent_tui(history, cfg)`, `_ANSI_RE`, `_MD_STYLE`, `_UI`, `_CON`/`Panel`/`Console`/`HORIZONTALS`, `COLOR`/`CUR_SKIN` globals, `banner(state)`, `render_status_bar(...)`.

- [ ] **Step 1: Create `alvaagent/tui.py`**

Move verbatim from `alvaagent_tui.py`: the block between `# ---------------- skins ...` and `# ---------------- slash commands ...` (roughly lines 3094-3855), PLUS `banner` and `render_status_bar` (lines ~4811-4906, currently in the REPL section) and the rich fallback block (`_CON`, `_ShimPanel`, etc., lines ~84-110).

Header imports:

```python
import os
import sys
import threading
import time

from alvaagent.config import SKIN_NAMES, DEFAULT_SKIN, _skin_of, data_dir
from alvaagent.agent import run_agent_stream
from alvaagent.util import _fmt_k
```

Carry over the optional rich import exactly as it exists today (the `try: from rich... except:` fallback with `_ShimConsole`/`_ShimPanel`/`_ShimBox`). The `CUR_SKIN` / `COLOR` globals and `_UI = {"spinner": None}` stay module-level here (Runtime phase → `rt.skin` / `rt.spinner`). `set_active_skin(state)` stays as-is (reads `_skin_of(state)`, sets the module globals).

Check `run_agent_tui`'s body: it uses `AgentWriter`, `Spinner`, `run_agent_stream`, `_trace`(? if so import from agent), `tool_summary`, `_maybe_enable_full`(?) — import whatever the moved bodies reference from the modules above. `render_status_bar` uses `context_usage`/`_fmt_k` — import `context_usage` from sessions if present.

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
```

**Important:** the old `ON_TOOL = on_tool` global in `alvaagent_tui.py` was pointing at the local `on_tool` — after this move, `on_tool` comes from the import above, so the existing assignment still works. Leave it until the Runtime phase.

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

### Task 12: Extract `commands.py` (slash commands + prompts)

**Files:**
- Create: `alvaagent/commands.py`
- Modify: `alvaagent_tui.py`, `alvaagent/__init__.py`

**Interfaces:**
- Consumes: everything above — `config`, `store`, `permissions`, `skills`, `tools`, `client`, `sessions`, `agent`, `tui`.
- Produces: `mask_key`? (already util), `ask`, `parse_key`, `ask_key`, `ask_permission`, `pick_model`, and every `cmd_*`: `cmd_models`, `cmd_skin`, `cmd_sessions`, `cmd_context`, `cmd_compress`, `cmd_self_test`, `cmd_help`, `cmd_config`, `cmd_provider`, `cmd_test`, `cmd_tools`, `cmd_trace`, `cmd_todos`, `cmd_todo`, `cmd_memory`, `cmd_feedback`, `cmd_skills`, `cmd_skill_category`, `cmd_reflect`, `cmd_improve`, `cmd_install_skill`, `cmd_clear`, `cmd_export`, `cmd_multi` — signatures unchanged (they already take `state`/`history`/`rest` params as today).

- [ ] **Step 1: Create `alvaagent/commands.py`**

Move verbatim from `alvaagent_tui.py`: the block between `# ---------------- slash commands ...` and `# ---------------- REPL ...` (roughly lines 3856-4688). This includes `ask`, `parse_key`, `ask_key`, `ask_permission`, `pick_model`, and all `cmd_*`. (`mask_key` was already moved to util in Task 2 — import it.)

Header imports (add what the moved bodies reference):

```python
import json
import os

from alvaagent.config import (
    PROVIDERS, SKIN_NAMES, DEFAULT_SKIN, ALVA_VERSION, DEFAULT_CONTEXT_WINDOW,
    MODEL_CONTEXT, active_cfg, save_state, data_dir, load_state,
)
from alvaagent.store import (
    _store_get, _store_set, ACTIVE_SESSION_KEY, TODO_KEY, MEM_PREFIX,
)
from alvaagent.permissions import ask_permission
from alvaagent.skills import (
    tool_skill_list, tool_skill_read, tool_skill_remove, tool_skill_save,
    tool_skill_install, tool_skill_sync_repo, _skill_list_all,
)
from alvaagent.tools import (
    tool_todo_list, tool_todo_add, tool_todo_toggle, tool_todo_remove,
    tool_memory_save, tool_memory_recall, tool_memory_list, tool_memory_search,
    active_tools, _set_tool_mode, _TOOLS_MODE, _TOOL_MODES, TOOLS,
    tool_self_test, dispatch_tool,
)
from alvaagent.client import fetch_models
from alvaagent.sessions import (
    sessions_map, load_session, save_session, delete_session, _find_session,
    _rename_session_in_store, context_usage, compress_now, _unique_session_name,
    auto_title, new_session_name,
)
from alvaagent.agent import _trace, _read_trace, _trace_count
from alvaagent.tui import (
    p_info, p_err, p_ok, p_warn, col, C, CUR_SKIN, COLOR,
    set_active_skin, banner, print_user_turn, render_agent_panel,
    render_status_bar, _md_line, AgentWriter, Spinner,
)
from alvaagent.util import mask_key, _fmt_k, now_iso, _raw_fetch
```

Where a moved body references bare names (e.g. `pick_model`, `_check`, `_raises`, `_todo_check`, `_mem_check`, `_skill_check`, `_file_write_check`, `_file_edit_check`, `_feedback_check`), they stay local to `commands.py`. If a body references a name that lives in another moved section, add it to the import above. `ask_permission`/`ask_key`/`pick_model` are defined in this module but `permissions.ask_permission` is the *hook* name — the hook is set in `main()` via the permissions module; the command handlers call the local `ask_permission` prompt directly. Verify the original call graph: `cmd_*` functions call `ask_permission`/`_permission` — `_permission` comes from `permissions`. If any `cmd_*` calls `ON_PERMISSION`, import it from permissions.

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

### Task 13: Extract `repl.py` + make `alvaagent_tui.py` a shim

**Files:**
- Create: `alvaagent/repl.py`
- Modify: `alvaagent_tui.py` (becomes the shim), `alvaagent/__init__.py` (stops importing from the shim), `alvaagent/__main__.py`

**Interfaces:**
- Consumes: everything — `config`, `store`, `permissions`, `skills`, `tools`, `client`, `sessions`, `agent`, `tui`, `commands`.
- Produces: `setup_completion()`, `save_completion_history()`, `send_message(text, history, state, session)`, `repl()`, `main()`, `_slash_complete`.

- [ ] **Step 1: Create `alvaagent/repl.py`**

Move verbatim from `alvaagent_tui.py`: the block between `# ---------------- REPL ...` and end of file (roughly lines 4689-5212): `new_session_name` (already moved to sessions — skip it), `setup_completion`, `save_completion_history`, `_slash_complete`, `_markup_safe` (TUI helper — moves here with the REPL), `send_message`, `repl`, `main`.

Header imports:

```python
import os
import readline
import signal
import sys
import threading

from alvaagent.config import load_state, save_state, active_cfg, data_dir
from alvaagent.store import _store_get, _store_set, ACTIVE_SESSION_KEY
from alvaagent.permissions import ON_PERMISSION
from alvaagent.tools import _sync_tool_mode, _set_tool_mode, _TOOLS_MODE, _TOOL_MODES, active_tools
from alvaagent.client import cancel_agent
from alvaagent.sessions import (
    load_session, save_session, delete_session, _find_session,
    _rename_session_in_store, compress_now, context_usage, _unique_session_name,
    auto_title, new_session_name, trim_history,
)
from alvaagent.agent import _trace
from alvaagent.tui import (
    set_active_skin, col, C, COLOR, CUR_SKIN, p_info, p_err, p_ok, p_warn,
    print_user_turn, render_agent_panel, render_status_bar, banner, run_agent_tui,
)
from alvaagent.commands import (
    cmd_help, cmd_config, cmd_provider, cmd_test, cmd_tools, cmd_trace,
    cmd_models, cmd_skin, cmd_sessions, cmd_context, cmd_compress, cmd_clear,
    cmd_multi, cmd_install_skill, cmd_self_test, cmd_improve, cmd_skills,
    cmd_memory, cmd_export, ask_permission, pick_model, ask_key,
)
```

Inside `main()`, the hook assignments become:

```python
    import alvaagent.permissions as _perms
    import alvaagent.tui as _tui
    _perms.ON_PERMISSION = ask_permission
    _tui.ON_TOOL = on_tool
```

`on_tool` is imported from `tui`. Verify `main()`'s signal/screen handling, `_cleanup`, `_restored`, and `banner(state)`/`repl()` calls move verbatim.

- [ ] **Step 2: Turn `alvaagent_tui.py` into the shim**

Replace the entire contents of `alvaagent_tui.py` with:

```python
#!/usr/bin/env python3
"""Compatibility shim — the real app lives in the alvaagent package.

Keeps the historical entry points working unchanged:
    python3 alvaagent_tui.py   (start.sh and the `alvaagent` launcher)
    import alvaagent_tui       (old docs / external scripts)
"""
from alvaagent.repl import main  # noqa: E402,F401

if __name__ == "__main__":
    main()
```

Delete the old header comment block? No — replace the whole file (the docstring above is the new header). Use the write/overwrite step.

- [ ] **Step 3: Update `__main__.py`**

```python
from alvaagent.repl import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Rewrite the facade to stop importing the shim**

In `alvaagent/__init__.py`, remove the `from alvaagent_tui import *` and the `from alvaagent_tui import (...)`, and the `from alvaagent_tui import main` if present. Rebuild the re-exports so they come ONLY from the modules (they already do, from Tasks 2-12). Then add:

```python
from alvaagent.repl import main  # noqa: F401
```

Update the facade docstring to say the flat API is now re-exported from the package modules.

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

## Phase B — Runtime threading (retire the globals)

### Task 14: `Runtime` context object threaded through everything

**Files:**
- Create: `alvaagent/context.py` (replace placeholder)
- Modify: every module — `util` (none), `config`, `store`, `permissions`, `skills`, `tools`, `client`, `sessions`, `agent`, `tui`, `commands`, `repl`
- Modify: `test_tui.py` (tool call sites + a Runtime builder helper)

**Interfaces:**

The `Runtime` dataclass (full definition):

```python
"""Runtime context object — replaces the single-file module globals."""
from __future__ import annotations

from dataclasses import dataclass, field
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
    def active_cfg(self):
        return self.cfg["profiles"][self.cfg["active"]]
```

Builder (in `context.py`):

```python
def build_runtime(data_dir=None):
    import alvaagent.config as config
    import alvaagent.store as store
    if data_dir is None:
        data_dir = config.data_dir()
    rt = Runtime(data_dir=data_dir)
    rt.cfg = config.load_state()          # returns the normalized state dict
    rt.tool_mode = rt.cfg.get("tool_mode", "core")
    rt.skin = rt.cfg.get("skin", "midnight")
    store.load(rt)                         # store module function now takes rt
    return rt
```

**Global → Runtime mapping (apply every row):**

| Old global / param | Becomes | Notes |
|---|---|---|
| `store._store` | `rt.store` | `store.get(rt, k, d)` / `store.set(rt, k, v)` / `store.load(rt)` / `store.save(rt)` |
| `tools._TOOLS_MODE` | `rt.tool_mode` | `tools.set_mode(rt, mode)`, `tools.visible(rt)`, dispatch auto-enables via `rt` |
| `permissions._APPROVED_SET` | `rt.approved` | `request_permission(rt, ...)` |
| `client._cancel_flag` | `rt.cancel` (`threading.Event`) | `cancel_agent(rt)` → `rt.cancel.set()`; stream checks `rt.cancel.is_set()` |
| `permissions.ON_PERMISSION` | `rt.on_permission` | `request_permission` uses it; `build_runtime` leaves `None` (headless deny) |
| `tui.ON_TOOL` (was `alvaagent_tui.ON_TOOL`) | `rt.on_tool` | agent loop calls it if set |
| `tui._UI["spinner"]` | `rt.spinner` | `tool_open`/`tool_close` operate on `rt.spinner` |
| `tui.CUR_SKIN` + `tui.COLOR` | `rt.skin` | tui keeps module-level current-skin via `set_active_skin(rt)`; render helpers unchanged |
| `repl._last_turn` | `rt.last_turn` | `/redo` reads it |
| `state` param | `rt.cfg` | `active_cfg(state)` → `rt.active_cfg` |
| `session`, `history` params | `rt.session`, `rt.history` | `send_message(rt, text)` returns session name |

**Key signature changes (apply exactly):**

```python
# config.py
def load_state() -> dict                        # unchanged; no global side effects
def save_state(rt)                              # was save_state(state); persists rt.cfg
def active_cfg(rt) -> dict                      # was active_cfg(state)
# store.py
def load(rt) / save(rt) / get(rt, key, default=None) / set(rt, key, value)
# permissions.py
def request_permission(rt, action, ok=True, hint="")  # was _permission(action, ok, hint)
def classify_file_action(rt, path, mode)               # was classify_file_action(path, mode)
# skills.py
def skill_list(rt) / skill_read(rt, name) / skill_save(rt, name, content, category=None)
def skill_remove(rt, name) / skill_install(rt, source, category=None) / skill_sync_repo(rt, repo, subdir=None)
# tools.py
class Tools:
    def __init__(self, rt): self.rt = rt
    def calculator(self, args): ...              # args is the full params dict
    def run_python(self, args): ...
    # ... one method per tool name in TOOLS, named exactly the schema name
def dispatch_tool(rt, name, args): ...
def visible(rt) -> list                          # was active_tools()
def set_mode(rt, mode) -> None                   # was _set_tool_mode(state, mode)
def maybe_enable_full(rt, name) -> bool          # was _maybe_enable_full(name)
def self_test(rt) -> str                         # was self_test()
# client.py
def chat_completion(rt, messages, **kw)          # was chat_completion(state, messages, **kw)
def chat_completion_stream(rt, messages, **kw)
def fetch_models(rt)                             # was fetch_models(state)
def cancel_agent(rt)                             # rt.cancel.set()
# agent.py
def run_agent(rt, messages)                      # thread rt; drop separate cfg
def run_agent_stream(rt, messages)
def trace(rt, **event) / read_trace(rt, n) / trace_count(rt)   # was _trace/_read_trace/_trace_count
# sessions.py
def sessions_map(rt) / load_session(rt) / save_session(rt)
def delete_session(rt, name) / rename_session(rt, old, new) / find_session(rt, name)
def context_usage(rt, history) / compress_now(rt, threshold=None)
# tui.py
def set_active_skin(rt)                          # reads rt.skin
def run_agent_tui(rt)                            # was run_agent_tui(history, cfg)
def render_status_bar(rt, session, elapsed, tools, history)
# commands.py
def cmd_*(rt, ...)                               # every cmd_* takes rt first; the old
                                                 #   `state`/`history` params come from rt
# repl.py
def send_message(rt, text) -> str                # returns session name
def repl(rt) / main()                            # main(): rt = build_runtime(); wire hooks
```

**Dispatch through `Tools` (replaces `TOOL_IMPL`):**

Delete the `TOOL_IMPL` lambda dict in `tools.py`. Dispatch becomes:

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

Each `Tools` method takes the full `args` dict and plucks what it needs, e.g.:

```python
    def calculator(self, args):
        return tool_calculator(self.rt, args.get("expression"))
```

Keep the existing `tool_*` logic as private functions taking `(rt, ...)` (or fold inline — your choice, behavior first). `self_test` becomes `self_test(rt)` and `tool_self_test(rt)` runs the external harness against the repo root as in Task 7.

- [ ] **Step 1: Replace `alvaagent/context.py` with the full Runtime + builder** (code above). Add `__version__`-adjacent sanity: none needed.

- [ ] **Step 2: Thread rt through the leaf modules** — `store`, `config` (`save_state(rt)`, `active_cfg(rt)`), `permissions` (`request_permission(rt, ...)`, `classify_file_action(rt, ...)`). Update each module's internals: reads of `_store` become `rt.store`; `_save_store()` becomes `save(rt)`; `_APPROVED_SET` becomes `rt.approved`; `ON_PERMISSION` becomes `rt.on_permission`. Remove the now-dead module globals (`_store`, `_APPROVED_SET`, `ON_PERMISSION`). Drop the import-time `_load_store()` — store now loads in `build_runtime()`.

- [ ] **Step 3: Thread rt through `skills` and `tools`** — `skill_*(rt, ...)`; `Tools` class; `dispatch_tool(rt, ...)`; `visible(rt)`/`set_mode(rt, ...)`; remove `_TOOLS_MODE`. Update `self_test(rt)`.

- [ ] **Step 4: Thread rt through `client`, `agent`, `sessions`** — remove `_cancel_flag` (use `rt.cancel`); `trace(rt, ...)`; `run_agent(rt, messages)`; session funcs read `rt.session`/`rt.history`.

- [ ] **Step 5: Thread rt through `tui`, `commands`, `repl`** — `set_active_skin(rt)`, `run_agent_tui(rt)`, `cmd_*(rt, ...)`, `send_message(rt, text)`, `repl(rt)`. `main()` becomes:

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

- [ ] **Step 6: Update `test_tui.py`** — add a builder helper near the top:

```python
def mkrt():
    return pa.build_runtime(data_dir=DATA)
```

Replace the tool-call sites (all of these) from flat calls to `Tools` methods:

- `pa.tool_calculator(x)` → `pa.Tools(mkrt()).calculator({"expression": x})`
- `pa.tool_run_command(cmd)` → `pa.Tools(mkrt()).run_command({"command": cmd})`
- `pa.tool_run_python(code)` → `pa.Tools(mkrt()).run_python({"code": code})`
- `pa.tool_web_fetch(url)` → `pa.Tools(mkrt()).web_fetch({"url": url})`
- `pa.tool_get_time()` → `pa.Tools(mkrt()).get_time({})`
- `pa.tool_file_read(p)` → `pa.Tools(mkrt()).file_read({"path": p})`
- `pa.tool_file_write(p, c)` → `pa.Tools(mkrt()).file_write({"path": p, "content": c})`
- `pa.tool_file_edit(p, o, n)` → `pa.Tools(mkrt()).file_edit({"path": p, "old": o, "new": n})`
- `pa.tool_file_list(p)` → `pa.Tools(mkrt()).file_list({"path": p})`
- `pa.tool_file_search(pat, p, d)` → `pa.Tools(mkrt()).file_search({"pattern": pat, "path": p, "max_depth": d})`
- `pa.tool_memory_save(k, v)` / `tool_memory_recall(k)` / `tool_memory_search(q)` / `tool_memory_list()` → `pa.Tools(mkrt()).memory_*({...})`
- `pa.tool_todo_add(t)` / `tool_todo_list()` / `tool_todo_toggle(i)` / `tool_todo_remove(i)` → `pa.Tools(mkrt()).todo_*({...})`
- `pa.tool_skill_list()` / `tool_skill_read(n)` / `tool_skill_save(n, c, category=...)` / `tool_skill_remove(n)` / `tool_skill_install(s, cat)` / `tool_skill_sync_repo(r, s)` → `pa.Tools(mkrt()).skill_*({...})`
- `pa.self_test()` → `pa.self_test(mkrt())` (facade exposes the module function taking rt — see Step 7)
- `pa._permission(...)` → `pa.request_permission(mkrt(), ...)` (check the actual call signatures in the test and adapt the arg order)
- `pa._store`, `pa._save_store`, `pa._store_get`, `pa._APPROVED_SET`, `pa._TOOLS_MODE`, `pa.ON_PERMISSION`, `pa._cancel_flag` → rewrite those assertions against `mkrt().store`, `pa.store.save(rt)`, `pa.store.get(rt, ...)`, `mkrt().approved`, `mkrt().tool_mode`, `mkrt().on_permission`, `mkrt().cancel`. Read each test before editing and preserve intent.
- `pa._MAX_CONSEC_TOOL_FAILURES`, `pa._TURN_TIMEOUT`, `pa._PY_MAX_BYTES`, `pa._PY_RUN_TIMEOUT`, `pa.TRACE_PATH` → unchanged (module constants still on the facade).
- `pa.chat_completion(...)`/`pa.chat_completion_stream(...)`/`pa.fetch_models(...)` → pass `mkrt()` as first arg.
- `pa.run_agent(...)`/`pa.run_agent_stream(...)`/`pa.cmd_provider(...)`/`pa.cmd_trace(...)`/`pa.send_message(...)` → pass `mkrt()` and drop the old `state`/`history`/`session` args per the new signatures.

- [ ] **Step 7: Update the facade re-exports** — drop the retired globals (`_store`, `_TOOLS_MODE`, `_APPROVED_SET`, `_cancel_flag`, `ON_PERMISSION`, `ON_TOOL`, `_UI`, `CUR_SKIN`, `COLOR`, `_TOOL_IMPL`, `_sync_tool_mode`, `tool_*` flat names). Add `Runtime`, `build_runtime`, `request_permission`, `visible`, `set_mode`, `dispatch_tool`, `Tools`, and the new `store`/`config`/`sessions`/`agent`/`tui`/`commands`/`repl` names the tests use. **Keep** `TRACE_PATH`, `_MAX_CONSEC_TOOL_FAILURES`, `_TURN_TIMEOUT`, `_PY_MAX_BYTES`, `_PY_RUN_TIMEOUT`, `SKINS`, `TOOLS`, `AgentWriter`, `Spinner`, `C`, `DEFAULT_CONTEXT_WINDOW`, `FIRST_RUN_CFG`, `HISTORY_PATH`, `MAX_SESSIONS`, `ACTIVE_SESSION_KEY`, `COLOR`-adjacent display names if tests still reference them (re-check with `grep`).

- [ ] **Step 8: Run the tests**

Run: `python3 test_tui.py` — Expected: `ALL TESTS PASSED ✓`. Iterate on failures: any missed global read or stale signature shows up here. Also run:

```bash
python3 -c "import alvaagent as pa; print(pa.build_runtime(data_dir='/tmp/alva_rt_check'))"
```

(creates nothing but loads config; must not raise).

- [ ] **Step 9: Commit**

```bash
git add alvaagent/context.py alvaagent/*.py test_tui.py
git commit -m "refactor: introduce Runtime context object; retire module globals (store, tool mode, approvals, cancel, UI hooks, skin)"
```

---

## Phase C — Hardening

### Task 15: New architecture tests + facade coverage

**Files:**
- Modify: `test_tui.py`

- [ ] **Step 1: Add a no-cycle import test**

```python
def check_no_cycles():
    import alvaagent.context
    import alvaagent.util
    import alvaagent.config
    import alvaagent.store
    import alvaagent.permissions
    import alvaagent.skills
    import alvaagent.tools
    import alvaagent.client
    import alvaagent.agent
    import alvaagent.sessions
    import alvaagent.tui
    import alvaagent.commands
    import alvaagent.repl
    return True
assert_ok(check_no_cycles(), "all package modules import cleanly (no import cycles)")
```

- [ ] **Step 2: Add a facade-surface test**

```python
missing = []
for _name in ["Runtime", "build_runtime", "Tools", "dispatch_tool", "TOOLS",
              "SKINS", "AgentWriter", "Spinner", "chat_completion",
              "chat_completion_stream", "run_agent", "run_agent_stream",
              "classify_command", "classify_python", "load_session", "main"]:
    if not hasattr(pa, _name):
        missing.append(_name)
assert_ok(not missing, "facade exposes the architecture API (missing: %s)" % (", ".join(missing) or "none"))
```

(Locate this snippet near the other module-level checks in `test_tui.py`; `pa` and `assert_ok` are already defined above it.)

- [ ] **Step 3: Add a dispatch-through-Tools test**

```python
def check_tools_dispatch():
    rt = mkrt()
    r = pa.dispatch_tool(rt, "calculator", {"expression": "2+3*4"})
    return r.get("result") == 14
assert_ok(check_tools_dispatch(), "dispatch_tool routes through Tools class")
```

- [ ] **Step 4: Add an entry-point shim test**

```python
def check_shim():
    import importlib
    m = importlib.import_module("alvaagent_tui")
    return callable(getattr(m, "main", None))
assert_ok(check_shim(), "alvaagent_tui.py shim exposes main()")
```

- [ ] **Step 5: Run the tests and commit**

Run: `python3 test_tui.py` — Expected: `ALL TESTS PASSED ✓`

```bash
git add test_tui.py
git commit -m "test: add package-architecture checks (no cycles, facade surface, Tools dispatch, shim)"
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
