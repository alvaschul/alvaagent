"""alvaagent — on-device AI agent harness for Termux (stdlib-only).

Package layout replaces the original single-file alvaagent_tui.py. This
facade re-exports the old flat API so `import alvaagent as pa` behaves like
the original module.
"""
import subprocess  # noqa: F401  (read via the facade fallback so pa.subprocess.run monkeypatches keep working)
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
        _store, _APPROVED_SET, _cancel_flag,
        ON_PERMISSION, ON_TOOL, _UI, CUR_SKIN, COLOR,
        _atomic_write, _find_session, _fmt_k, _looks_like_html, _md_line,
        _normalize_state, _parse_xml_tool_calls, _permission, _raw_fetch,
        _read_trace, _save_store, _sleep_retry, _store_get, _strip_xml,
        _unique_session_name, signal, urllib, time, yaml,
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
from alvaagent.tools import (  # noqa: F401
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
from alvaagent.client import (  # noqa: F401
    SYSTEM_PROMPT,
    _MAX_RETRIES, _RETRY_BACKOFF, _STREAM_IDLE_LIMIT, _STREAM_POLL,
    _readable_error, _retryable_status, _sleep_retry, _Cancelled,
    chat_completion, chat_completion_stream, fetch_models, cancel_agent,
)
from alvaagent.sessions import (  # noqa: F401
    context_window_for, estimate_tokens, estimate_message_tokens, context_usage,
    sessions_map, load_session, save_session, delete_session, _find_session,
    _rename_session_in_store, auto_title, _unique_session_name,
    summarize_with_llm, _fallback_summary, compress_history,
    trim_history, new_session_name,
)
from alvaagent.agent import (  # noqa: F401
    _TURN_TIMEOUT, _MAX_CONSEC_TOOL_FAILURES, ON_TOOL,
    _repair_tool_pairs, _report_tool,
    run_agent, _clean_segment, _strip_xml_blocks, _parse_xml_tool_calls,
    _strip_xml, run_agent_stream,
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
        try:
            return getattr(_Facade._tui, name)
        except AttributeError:
            # Names that have moved out of alvaagent_tui (and are not mirrored
            # back) live on in this facade's own namespace (e.g. SKILLS_DIR,
            # Task 6) or were set by the import system (util, config, ...).
            return super().__getattribute__(name)

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
