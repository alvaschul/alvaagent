# alvaagent: Single-File → Package Re-architecture

Date: 2026-08-14
Status: Approved design (spec for implementation)

## Problem

`alvaagent_tui.py` is a 5,212-line, 209-function single file. The
single-file shape was a deliberate product feature ("copy one file to a
phone, zero pip, survives a reinstall"), but the file has grown past the
point where it is efficient or safe to edit:

- Edits are error-prone at this size — especially AI-driven edits, which
  reason worse about a 5k-line file than about focused modules.
- 17 interleaved concerns (store, permissions, tools, LLM client, agent
  loop, sessions, compression, skins, TUI render, slash commands, REPL)
  all share one global namespace.
- Module-level mutable globals (`_store`, `_TOOLS_MODE`, `_APPROVED_SET`,
  `_cancel_flag`, `ON_PERMISSION`, `ON_TOOL`, `_UI`, `CUR_SKIN`, `COLOR`)
  couple everything implicitly.

## Goal

Refactor into a Python **package** with clean module boundaries and an
explicit **`Runtime` context object** replacing the module globals — while
preserving behavior exactly and keeping every entry point, launcher, and
test working.

Non-goals (this pass):
- No behavior/feature changes. All existing test checks must pass.
- No new runtime dependencies (stdlib-only promise stands).
- No change to the on-disk data formats (`.alvaagent/` layout, config.json,
  store.json, skill file layout).

## Target architecture

### Layout

```
alvaagent/
  alvaagent/
    __init__.py     facade: re-exports the old flat API
    __main__.py     `python3 -m alvaagent`
    context.py      Runtime dataclass (the app context object)
    util.py         pure helpers: _env, now_iso, atomic_write, _fmt_k,
                    mini-yaml frontmatter, mask_key, HTML sniff, regex consts
    config.py       paths/data_dir, load_state/save_state/active_cfg,
                    DEFAULT_CFG/FIRST_RUN_CFG, skin + tool_mode persistence,
                    known context windows
    store.py        store.json: _load_store/_save_store, namespaced
                    _store_get/_store_set, ACTIVE_SESSION_KEY
    permissions.py  classify_command, classify_python, classify_file_action,
                    request_permission (approved-set lives on Runtime)
    skills.py       frontmatter parse/dump, skill paths/resolution, scan/list,
                    skill save/read/remove/install/sync
    tools.py        tool_* implementations as methods on a `Tools` class,
                    TOOLS schemas, core/full selection, dispatch_tool,
                    self_test + tool_self_test
    client.py       chat_completion, chat_completion_stream, fetch_models,
                    retries/backoff, _Cancelled, stall watchdog, cancel hook
    agent.py        run_agent, run_agent_stream, _repair_tool_pairs, trace,
                    turn guards (_TURN_TIMEOUT, _MAX_CONSEC_TOOL_FAILURES)
    sessions.py     sessions_map, load/save/delete/rename, context_usage,
                    estimate_tokens, auto_title, compress/compress_now,
                    summarize_with_llm
    tui.py          SKINS, C, colors, print_user_turn, render_agent_panel,
                    render_status_bar, AgentWriter, Spinner, banner,
                    markdown line renderer
    commands.py     all cmd_* slash handlers + ask/ask_key/pick_model/
                    ask_permission UI prompts
    repl.py         setup_completion, save_completion_history, send_message,
                    repl, main, screen (alternate buffer) + signal handling
  alvaagent_tui.py  shim: `from alvaagent.repl import main; main()` — keeps
                    start.sh, the `alvaagent` launcher, README entry points,
                    and `import alvaagent_tui` working
  test_tui.py       updated to `import alvaagent as pa`
  mock_llm_server.py  unchanged
  start.sh / alvaagent launcher  unchanged (shim preserves them)
```

`__version__` lives in a leaf (config.py or util.py) so the facade never
creates an import cycle by being imported by its own modules. Modules must
not import `alvaagent` (the facade) — the facade only re-exports.

### Runtime context object (context.py)

A single dataclass built once in `main()` and threaded through tools →
agent → commands → repl. It replaces every mutable module global:

```python
@dataclass
class Runtime:
    data_dir: str                 # ALVA_DATA_DIR / default (was implicit)
    cfg: dict                     # active config (was `state`)
    store: dict                   # store.json contents (was `_store`)
    tool_mode: str = "core"       # was `_TOOLS_MODE`
    approved: set                 # session-approval cache (was `_APPROVED_SET`)
    cancel: threading.Event       # was `_cancel_flag`
    on_permission: Callable|None  # UI hook (was `ON_PERMISSION`); headless = deny
    on_tool: Callable|None        # UI hook (was `ON_TOOL`)
    spinner: Spinner|None         # was `_UI["spinner"]`
    skin: str                     # persisted skin name (was set via state)
    session: str                  # active session name
    history: list                 # active session messages
    last_turn: dict               # /redo state (was a repl local)
```

Rules:
- UI callbacks stay **hooks on `rt`** — this is what removes the old
  `ON_PERMISSION`/`ON_TOOL` globals without creating import cycles
  (repl.py sets them to tui/commands prompt helpers; headless runs leave
  them `None` → deny by default).
- Store/config/skills/sessions functions take `rt` and read/write
  `rt.store`, `rt.cfg`, and files derived from `rt.data_dir`.
- `send_message` / `run_agent` / `run_agent_tui` take `rt` and use
  `rt.history` / `rt.session` instead of separate args.
- The **only** surviving module-level state is `tui.py`'s current skin
  (UI-only rendering state, set once per run via a setter) — justified
  because the skin is effectively immutable during a session and threading
  it through ~20 render helpers adds noise, not clarity.

### Tools refactor

- `TOOLS` (the OpenAI-format schema registry) stays a module-level
  constant in `tools.py` — pure data, no deps.
- `tool_*` implementations become methods on a `Tools` class bound to a
  Runtime: `Tools(rt)`. Dispatch resolves name → bound method.
- `TOOL_IMPL` lambdas are replaced by a `name → method name` mapping
  (or equivalent) so `dispatch_tool(rt, name, args)` works the same way
  today's dispatch does (error hints, lazy full-mode enable).
- `self_test` and `tool_self_test` stay in `tools.py`: they only touch
  other tools, `permissions`, and (for `tool_self_test`) a `test_tui.py`
  subprocess — no agent/client imports, so no cycle.
- Permission-gated tools call `request_permission(rt, ...)`, which uses
  `rt.on_permission` when present and otherwise denies.

### Dependency rules

Dependencies point downward; no module may import from a higher layer:

```
util
  └→ config, store
        └→ permissions, skills
              └→ tools
                    └→ client, agent, sessions
                          └→ tui, commands, repl
```

Bidirectional needs are resolved one of two ways: a hook/field on `rt`
(UI callbacks, cancel) or moving the shared logic to a leaf module
(`self_test` → `tools.py`; regex/yaml helpers → `util.py`).

## Back-compat strategy

- `alvaagent/__init__.py` re-exports the full old flat API (~97 symbols
  the test suite touches: `TOOLS`, all `tool_*`, `SKINS`, `C`, `AgentWriter`,
  `Spinner`, constants, helpers). This makes `import alvaagent as pa`
  behave like the old module, so `test_tui.py` needs only a one-line
  import change plus direct top-level imports for stdlib aliases it used
  via `pa.signal`, `pa.subprocess`, `pa.urllib`, `pa.yaml`, `pa.time`.
- `alvaagent_tui.py` is a ~5-line shim so `start.sh`, the `alvaagent`
  launcher, and `python3 alvaagent_tui.py` keep working without edits.
- `.alvaagent/` data layout, config/store JSON shapes, and skill file
  layout are unchanged.

## Testing strategy

- `python3 test_tui.py` must print `ALL TESTS PASSED ✓` at every phase
  checkpoint. No single-test runner exists; the flat script runs all
  checks and spawns `mock_llm_server.py` (port 8210) itself.
- New checks added with the split:
  - package imports cleanly (no import cycle) and facade covers the old
    symbol surface,
  - `Runtime`/`Tools` construction and dispatch through `Tools`,
  - entry-point shim runs (import-only smoke test).
- Existing behavior checks (security classifiers, atomic store writes,
  ghost-duplicate regression, streaming fallback, tool tiering, etc.) all
  remain and must pass unchanged.

## Migration sequencing (one effort, test-green checkpoints)

1. Scaffold package + facade re-exporting from the single file; tests
   green (one-line import change in test_tui.py).
2. Move sections into modules by concern (mechanical, behavior-preserving),
   facade re-exports; tests green.
3. Introduce `Runtime`, thread it through tools/agent/commands/repl, retire
   the module globals; tests green.
4. Clean boundaries (resolve any remaining cycles), polish the shim, update
   README + AGENTS.md + `.gitignore` (`__pycache__/`), add the new tests;
   final `ALL TESTS PASSED ✓` + `git status` shows only intended files.

## Documentation updates

- README.md: replace the "single-file" framing with "package + one-file
  shim"; update the Files section; keep install/usage/security text intact.
- AGENTS.md: rewrite Architecture, Commands, and self-modification
  hygiene notes for the package layout; document the Runtime and the
  dependency rule.
- `.gitignore`: add `__pycache__/`, `*.pyc` (keep existing entries).

## Risks

- **Behavior drift** while threading `rt` through ~200 functions. Mitigated
  by: the full existing test suite, `self_test`, test-green phase
  checkpoints, and committing each phase separately (`git add <file>`,
  never `git add -A`).
- **Import cycles** during the move. Mitigated by the downward dependency
  rule, hooks on `rt`, and the facade never being imported by modules.
- **Test churn** from the facade surface. Mitigated by a comprehensive
  facade re-export so test edits stay minimal.

## Out of scope (later passes)

- Splitting tool classes further or grouping schemas.
- Data-format migrations.
- Feature work; the tool registry stays 30 tools.
