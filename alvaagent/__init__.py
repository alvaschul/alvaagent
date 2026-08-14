"""alvaagent — on-device AI agent harness for Termux (stdlib-only).

Package layout replaces the original single-file alvaagent_tui.py. This
facade re-exports the old flat API so `import alvaagent as pa` behaves like
the original module.
"""
import sys as _sys

# The eager re-export must only run against a fully loaded alvaagent_tui.
# Three import orders all need to work:
#   1. `import alvaagent` first (the test suite): alvaagent_tui is not loaded
#      yet — import it here, then re-export.
#   2. `import alvaagent_tui` first: it is mid-import (its own
#      `from alvaagent.util import ...` pulled this package in before `_store`
#      is defined) — skip the re-export; the proxy below forwards reads to it
#      once it finishes loading.
#   3. `python3 alvaagent_tui.py` script mode: same as 1 (the facade imports it
#      as a module) — unchanged.
_tui = _sys.modules.get("alvaagent_tui")
if _tui is None:
    import alvaagent_tui
    _tui = _sys.modules["alvaagent_tui"]
if "_store" in _tui.__dict__:
    from alvaagent_tui import *  # noqa: F401,F403
    from alvaagent_tui import (  # noqa: F401
        _store, _TOOLS_MODE, _APPROVED_SET, _cancel_flag,
        ON_PERMISSION, ON_TOOL, _UI, CUR_SKIN, COLOR,
        _atomic_write, _find_session, _fmt_k, _looks_like_html, _md_line,
        _normalize_state, _parse_xml_tool_calls, _permission, _raw_fetch,
        _read_trace, _save_store, _sleep_retry, _store_get, _strip_xml,
        _trace_count, _unique_session_name, signal, subprocess, urllib, time, yaml,
    )
from alvaagent.util import (  # noqa: F401
    _env, now_iso, _fmt_k, _atomic_write, _looks_like_html, _raw_fetch,
    mask_key, _parse_frontmatter, _frontmatter_load, _frontmatter_dump,
    _mini_yaml, _mini_scalar, _finish_block,
)
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
from alvaagent.store import (  # noqa: F401
    _store, _migrate_legacy_dir, _load_store, _save_store,
    _store_get, _store_set, TODO_KEY, MEM_PREFIX, FEEDBACK_KEY,
    IMPROVEMENT_KEY, HISTORY_KEY, SESSION_KEY, ACTIVE_SESSION_KEY, MAX_SESSIONS,
)
from alvaagent.permissions import (  # noqa: F401
    _READONLY_PREFIXES, _RISKY_TOKENS, _RISKY_OPERATORS, _tokenize_shell,
    classify_command, PROJECT_DIR, _in_project, classify_file_action,
    _APPROVED_SET, _permission, ON_PERMISSION,
)
from alvaagent.skills import (  # noqa: F401
    SKILLS_DIR,
    _SKILL_FM_RE, _SKILL_FM_DEFAULT, _VALID_FM_KEYS, _SKILL_RAW_MAX,
    _skill_body_for_tool, _detect_category, _skill_filepath, _inside_skills,
    _resolve_skill_path, _skill_read, _scan_skill_files, _skill_list_all,
    tool_skill_list, tool_skill_read, tool_skill_remove, tool_skill_save,
    tool_skill_install, tool_skill_sync_repo,
)

# The single file's functions read module globals (ON_PERMISSION, _TOOLS_MODE,
# _raw_fetch, ...). The test suite monkeypatches them through `pa.<name> = ...`.
# As the mechanical split moves readers into alvaagent.* submodules, a write to
# the facade must land in every loaded module that exposes the name (the
# def-owner plus any module that imported it by name). Reads forward to
# alvaagent_tui, which re-imports the full surface until Task 13.
import types as _types


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


_Facade._tui = _tui
_sys.modules[__name__].__class__ = _Facade
