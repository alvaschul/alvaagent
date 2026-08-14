"""alvaagent — on-device AI agent harness for Termux (stdlib-only).

Package layout replaces the original single-file alvaagent_tui.py. This module
is the public facade: it re-exports the rt-first API surface from the package
leaf modules. The flat bridge/proxy layer (Phase A `_Facade`, `default_rt`,
flat tool/LLM/session adapters) is gone (Task 15 / Ruling 15): every entry
point takes the runtime explicitly, so tests and the app can build fully
isolated runtimes via `build_runtime(data_dir)`.
"""
import json as _json  # noqa: F401
import os  # noqa: F401
import signal  # noqa: F401
import subprocess  # noqa: F401
import urllib.error  # noqa: F401
import urllib.request  # noqa: F401
try:
    import yaml  # noqa: F401
except ImportError:
    yaml = None

from alvaagent.util import (  # noqa: F401
    _env, now_iso, _fmt_k, _atomic_write, _looks_like_html, _raw_fetch,
    mask_key, _parse_frontmatter, _frontmatter_dump, _SKILL_FM_RE,
    _SKILL_FM_DEFAULT, _VALID_FM_KEYS, _SKILL_RAW_MAX,
    _mini_yaml, _mini_scalar, _finish_block,
)
from alvaagent.context import (  # noqa: F401
    Runtime, build_runtime,
)
from alvaagent.config import (  # noqa: F401
    data_dir, DATA_DIR, _LEGACY_DIRS, CONFIG_PATH, STORE_PATH, HISTORY_PATH,
    TRACE_PATH, PROVIDERS, DEFAULT_CFG, FIRST_RUN_CFG, DEFAULT_SKIN,
    SKIN_NAMES, ALVA_VERSION, DEFAULT_CONTEXT_WINDOW, MODEL_CONTEXT,
    TOOL_MODES, _tool_mode_of, _skin_of, _normalize_state, load_state,
    save_state, active_cfg,
)
from alvaagent.trace import (  # noqa: F401
    trace, read_trace, trace_count, _TRACE_MAX_LINES, _TRACE_MAX_BYTES,
)
from alvaagent.store import (  # noqa: F401
    _migrate_legacy_dir, load as store_load, save as store_save,
    get as store_get, set as store_set, TODO_KEY, MEM_PREFIX, FEEDBACK_KEY,
    IMPROVEMENT_KEY, HISTORY_KEY, SESSION_KEY, ACTIVE_SESSION_KEY, MAX_SESSIONS,
)
from alvaagent.permissions import (  # noqa: F401
    _READONLY_PREFIXES, _RISKY_TOKENS, _RISKY_OPERATORS, _tokenize_shell,
    classify_command, PROJECT_DIR, _in_project, classify_file_action,
    request_permission,
)
from alvaagent.skills import (  # noqa: F401
    _skill_body_for_tool, _detect_category, _skill_filepath, _inside_skills,
    _resolve_skill_path, _skill_read, _scan_skill_files, _skill_list_all,
    skill_list, skill_read, skill_remove, skill_save, skill_install,
    skill_sync_repo,
)
from alvaagent.tools import (  # noqa: F401
    _PY_RUN_TIMEOUT, _PY_MAX_BYTES, _PY_MAX_CHARS, _CALC_ALLOWED,
    _CORE_TOOL_NAMES, _ADVANCED_TOOL_NAMES, _TOOL_ERROR_HINTS,
    Tools, visible, set_mode, sync_tool_mode, maybe_enable_full,
    tool_self_test, tool_count, classify_python,
    tool_calculator, tool_run_python, tool_web_fetch, tool_get_time,
    tool_run_command,
    tool_file_read, tool_file_write, tool_file_edit, tool_file_list,
    tool_file_search,
    tool_todo_list, tool_todo_add, tool_todo_toggle, tool_todo_remove,
    tool_memory_save, tool_memory_recall,
    tool_memory_list, tool_memory_search,
    dispatch_tool, self_test,
    TOOLS, _safe_factorial, _calc_eval, _fmt_num,
)
from alvaagent.client import (  # noqa: F401
    SYSTEM_PROMPT,
    _MAX_RETRIES, _RETRY_BACKOFF, _STREAM_IDLE_LIMIT, _STREAM_POLL,
    _readable_error, _retryable_status, _sleep_retry, _Cancelled,
    chat_completion, chat_completion_stream, fetch_models, cancel_agent,
)
from alvaagent.sessions import (  # noqa: F401
    context_window_for, estimate_tokens, estimate_message_tokens, context_usage,
    sessions_map, load_session, save_session, delete_session, find_session,
    rename_session, auto_title, unique_session_name,
    summarize_with_llm, _fallback_summary, compress_history,
    trim_history, new_session_name,
)
from alvaagent.agent import (  # noqa: F401
    MAX_STEPS, _TURN_TIMEOUT, _MAX_CONSEC_TOOL_FAILURES,
    _repair_tool_pairs, run_agent, run_agent_stream,
    _clean_segment, _strip_xml_blocks, _parse_xml_tool_calls,
    _strip_xml,
)
from alvaagent.tui import (  # noqa: F401
    SKINS, C, col, p_info, p_err, p_ok, p_warn, _term_width,
    _hrgb, _fgh, _rsth, _tool_line, print_user_turn, render_agent_panel,
    _md_attr_sgr, _has_ansi, _md_line, _md_prefix, style_inline, AgentWriter,
    fmt_args, tool_summary, Spinner, tool_open, tool_close, on_tool,
    _ANSI_RE, _MD_STYLE, COLOR, CUR_SKIN, _CON,
    Console, Panel, HORIZONTALS, banner, render_status_bar,
    ALVA_WORDMARK, _markup_safe, _banner_tools_lines, _banner_skills_lines,
    compress_now, set_active_skin, run_agent_tui,
)
from alvaagent.commands import (  # noqa: F401
    ask, parse_key, ask_key, pick_model, _SLASH_COMMANDS,
    cmd_models, cmd_skin, cmd_sessions, cmd_context, cmd_compress,
    cmd_self_test, cmd_help, cmd_config, cmd_test, cmd_tools,
    cmd_todos, cmd_todo, cmd_memory, cmd_feedback, cmd_skills,
    cmd_skill_category, cmd_reflect, cmd_improve, cmd_install_skill, cmd_clear,
    cmd_export, cmd_multi, cmd_provider, cmd_trace, ask_permission,
)
from alvaagent.repl import (  # noqa: F401
    setup_completion, save_completion_history, send_message, main, repl,
)
