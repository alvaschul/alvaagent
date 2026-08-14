"""alvaagent — on-device AI agent harness for Termux (stdlib-only).

Package layout replaces the original single-file alvaagent_tui.py. This
facade re-exports the old flat API so `import alvaagent as pa` behaves like
the original module.
"""
import os  # noqa: F401
import json as _json
import subprocess  # noqa: F401  (read via the facade fallback so pa.subprocess.run monkeypatches keep working)
import urllib.error  # noqa: F401
import urllib.request  # noqa: F401
try:
    import yaml  # noqa: F401
except ImportError:
    yaml = None
import sys as _sys

# alvaagent_tui is the compatibility shim (re-exports the repl surface from
# alvaagent.repl). Two import orders need to work:
#   1. `import alvaagent` first (the test suite): alvaagent_tui is not loaded
#      yet — import it here; the proxy below forwards reads to it.
#   2. `import alvaagent_tui` first: it is mid-import (its `from
#      alvaagent.repl import ...` pulled this package in before the shim
#      finished loading) — the proxy below still forwards reads to it once it
#      finishes loading. The facade itself never imports from alvaagent.repl
#      directly (that would re-enter the partial import in order 2).
_tui = _sys.modules.get("alvaagent_tui")
if _tui is None:
    import alvaagent_tui
    _tui = _sys.modules["alvaagent_tui"]
from alvaagent.util import (  # noqa: F401
    _env, now_iso, _fmt_k, _atomic_write, _looks_like_html, _raw_fetch,
    mask_key, _parse_frontmatter, _frontmatter_dump, _SKILL_FM_RE,
    _SKILL_FM_DEFAULT, _VALID_FM_KEYS, _SKILL_RAW_MAX,
    _mini_yaml, _mini_scalar, _finish_block,
)
from alvaagent.context import (  # noqa: F401
    Runtime, build_runtime as _build_runtime, default_rt as _get_rt,
)
build_runtime = _build_runtime
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
    tool_skill_list as _skills_list, tool_skill_read as _skills_read,
    tool_skill_remove as _skills_remove, tool_skill_save as _skills_save,
    tool_skill_install as _skills_install, tool_skill_sync_repo as _skills_sync,
)
from alvaagent.tools import (  # noqa: F401
    _PY_RUN_TIMEOUT, _PY_MAX_BYTES, _PY_MAX_CHARS, _CALC_ALLOWED,
    _CORE_TOOL_NAMES, _ADVANCED_TOOL_NAMES, _TOOL_ERROR_HINTS,
    Tools, visible, set_mode, sync_tool_mode, maybe_enable_full,
    tool_self_test, tool_count, classify_python,
    tool_calculator as _tools_calculator, tool_run_python as _tools_run_python,
    tool_web_fetch as _tools_web_fetch, tool_get_time as _tools_get_time,
    tool_run_command as _tools_run_command,
    tool_file_read as _tools_file_read, tool_file_write as _tools_file_write,
    tool_file_edit as _tools_file_edit, tool_file_list as _tools_file_list,
    tool_file_search as _tools_file_search,
    tool_todo_list as _tools_todo_list, tool_todo_add as _tools_todo_add,
    tool_todo_toggle as _tools_todo_toggle, tool_todo_remove as _tools_todo_remove,
    tool_memory_save as _tools_memory_save, tool_memory_recall as _tools_memory_recall,
    tool_memory_list as _tools_memory_list, tool_memory_search as _tools_memory_search,
    dispatch_tool as _tools_dispatch_tool, self_test as _tools_self_test,
    TOOLS, _safe_factorial, _calc_eval, _fmt_num,
)
from alvaagent.client import (  # noqa: F401
    SYSTEM_PROMPT,
    _MAX_RETRIES, _RETRY_BACKOFF, _STREAM_IDLE_LIMIT, _STREAM_POLL,
    _readable_error, _retryable_status, _sleep_retry, _Cancelled,
)
import alvaagent.client as _client_mod
from alvaagent.sessions import (  # noqa: F401
    context_window_for, estimate_tokens, estimate_message_tokens, context_usage,
    sessions_map, load_session, save_session, delete_session, _find_session,
    _rename_session_in_store, auto_title, _unique_session_name,
    summarize_with_llm, _fallback_summary, compress_history,
    trim_history, new_session_name,
)
# Flat adapters for the retired module globals: they read/write the default
# runtime's state so `pa._permission`, `pa._store_get`, `pa._store_set`,
# `pa._save_store`, ... keep working exactly like the old globals.
def _permission(desc):
    return request_permission(_get_rt(), desc)


def _store_get(key, default=None):
    return store_get(_get_rt(), key, default)


def _store_set(key, value):
    store_set(_get_rt(), key, value)


def _save_store():
    store_save(_get_rt())


# The skills dir is derived from the default runtime's data dir (the module
# global in skills.py was retired when skills went rt-first).
SKILLS_DIR = os.path.join(DATA_DIR, "skills")


# Flat tool adapters: the tests (and legacy callers) invoke the tools with
# their old signatures; each adapter routes through the default rt.
def tool_calculator(expression):
    return _tools_calculator(expression)


def tool_run_python(code):
    return _tools_run_python(_get_rt(), code)


def tool_web_fetch(url):
    return _tools_web_fetch(_get_rt(), url)


def tool_get_time():
    return _tools_get_time()


def tool_run_command(command):
    return _tools_run_command(_get_rt(), command)


def tool_file_read(path):
    return _tools_file_read(_get_rt(), path)


def tool_file_write(path, content):
    return _tools_file_write(_get_rt(), path, content)


def tool_file_edit(path, old, new):
    return _tools_file_edit(_get_rt(), path, old, new)


def tool_file_list(path="."):
    return _tools_file_list(_get_rt(), path)


def tool_file_search(pattern, path=None, max_depth=None):
    return _tools_file_search(_get_rt(), pattern, path, max_depth)


def tool_todo_list():
    return _tools_todo_list(_get_rt())


def tool_todo_add(text):
    return _tools_todo_add(_get_rt(), text)


def tool_todo_toggle(index):
    return _tools_todo_toggle(_get_rt(), index)


def tool_todo_remove(index):
    return _tools_todo_remove(_get_rt(), index)


def tool_memory_save(key, value):
    return _tools_memory_save(_get_rt(), key, value)


def tool_memory_recall(key):
    return _tools_memory_recall(_get_rt(), key)


def tool_memory_list():
    return _tools_memory_list(_get_rt())


def tool_memory_search(query=""):
    return _tools_memory_search(_get_rt(), query)


def tool_skill_list():
    return _skills_list(_get_rt())


def tool_skill_read(name):
    return _skills_read(_get_rt(), name)


def tool_skill_remove(name):
    return _skills_remove(_get_rt(), name)


def tool_skill_save(name, content, category=None):
    return _skills_save(_get_rt(), name, content, category)


def tool_skill_install(source, category=None):
    return _skills_install(_get_rt(), source, category)


def tool_skill_sync_repo(repo, subdir=None):
    return _skills_sync(_get_rt(), repo, subdir)


# Dispatch is dual in Phase A: rt-first `(rt, name, args)` (from the surgical
# tiered-tool-selection tests and the Tools class) or flat `(name, args)`
# (from the agent's run loop). The flat form always routes through the
# default rt, which equals the threaded rt on every Phase A entry path.
def dispatch_tool(*a, **k):
    if a and isinstance(a[0], Runtime):
        return _tools_dispatch_tool(*a, **k)
    return _tools_dispatch_tool(_get_rt(), *a, **k)


def self_test():
    return _tools_self_test(_get_rt())


# Flat LLM/agent adapters: the tests (and legacy callers) invoke these with
# their old signatures; each adapter routes through the default rt.
def chat_completion(messages, config, tools=None):
    return _client_mod.chat_completion(_get_rt(), messages, config, tools=tools)


def chat_completion_stream(messages, config, tools=None):
    return _client_mod.chat_completion_stream(_get_rt(), messages, config, tools=tools)


def fetch_models(base_url, api_key, timeout=20):
    return _client_mod.fetch_models(_get_rt(), base_url, api_key, timeout=timeout)


def cancel_agent():
    return _client_mod.cancel_agent(_get_rt())


from alvaagent.agent import (  # noqa: F401
    _TURN_TIMEOUT, _MAX_CONSEC_TOOL_FAILURES,
    _repair_tool_pairs,
    _clean_segment, _strip_xml_blocks, _parse_xml_tool_calls,
    _strip_xml,
)
import alvaagent.agent as _agent_mod

from alvaagent.tui import (  # noqa: F401
    SKINS, C, col, p_info, p_err, p_ok, p_warn, _term_width,
    _hrgb, _fgh, _rsth, _tool_line, print_user_turn, render_agent_panel,
    _md_attr_sgr, _has_ansi, _md_line, _md_prefix, style_inline, AgentWriter,
    fmt_args, tool_summary, Spinner, tool_open, tool_close, on_tool,
    _ANSI_RE, _MD_STYLE, _UI, COLOR, CUR_SKIN, _CON,
    Console, Panel, HORIZONTALS, banner, render_status_bar,
    ALVA_WORDMARK, _markup_safe, _banner_tools_lines, _banner_skills_lines,
    compress_now,
)
from alvaagent.commands import (  # noqa: F401
    ask, parse_key, ask_key, pick_model, _SLASH_COMMANDS,
    cmd_models, cmd_skin, cmd_sessions, cmd_context, cmd_compress,
    cmd_self_test, cmd_help, cmd_config, cmd_test, cmd_tools,
    cmd_todos, cmd_todo, cmd_memory, cmd_feedback, cmd_skills,
    cmd_skill_category, cmd_reflect, cmd_improve, cmd_install_skill, cmd_clear,
    cmd_export, cmd_multi,
)
import alvaagent.tui as _tui_mod
import alvaagent.commands as _commands_mod


# Flat command/TUI adapters (legacy flat arity): the test suite calls these
# with their old signatures; each routes through the default runtime.
def set_active_skin(state):
    rt = _get_rt()
    rt.skin = (state or {}).get("skin") or DEFAULT_SKIN
    _tui_mod.set_active_skin(rt)


def run_agent_tui(history, cfg):
    rt = _get_rt()
    rt.cfg = cfg
    return _tui_mod.run_agent_tui(rt, history)


def cmd_provider(state, rest):
    rt = _get_rt()
    rt.cfg = state
    return _commands_mod.cmd_provider(rt, rest)


def cmd_trace(rest):
    return _commands_mod.cmd_trace(_get_rt(), rest)


def ask_permission(desc):
    return _commands_mod.ask_permission(_get_rt(), desc)

# The split modules read module globals (ON_PERMISSION, _TOOLS_MODE, ...). The
# test suite monkeypatches them through `pa.<name> = ...`. A write to the
# facade must land on the facade + the shim (alvaagent_tui) + every loaded
# alvaagent.* submodule that exposes the name (the def-owner plus any module
# that imported it by name). Reads forward to the shim first (the repl surface:
# send_message, setup_completion, main, ...), then fall back to this facade's
# own re-exported namespace (the flat API imported from the package modules).
import types as _types


def run_agent(history_json, config_json):
    rt = _get_rt()
    rt.cfg = _normalize_state(_json.loads(config_json))
    return _agent_mod.run_agent(rt, history_json)


def run_agent_stream(history, config):
    rt = _get_rt()
    rt.cfg = _normalize_state(config)
    return _agent_mod.run_agent_stream(rt, history)


class _Facade(_types.ModuleType):
    _tui = None

    # Bridge properties for the retired module globals (`pa._store`,
    # `pa._APPROVED_SET`, `pa.ON_PERMISSION`): they resolve through the
    # default runtime. Being data descriptors on the class, BOTH reads
    # (`__getattribute__`) and writes (`__setattr__`) honor them.
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
        if isinstance(value, _types.ModuleType):
            # Import-system submodule registration (e.g. setattr(alvaagent,
            # "repl", <module alvaagent.repl>)) must not be treated as a
            # monkeypatch: propagating it would overwrite the `repl` function
            # re-exported into the shim and into alvaagent.repl itself.
            return
        setattr(_Facade._tui, name, value)
        for _mname, _mod in _sys.modules.items():
            if _mname.startswith("alvaagent.") and hasattr(_mod, name):
                setattr(_mod, name, value)


_Facade._tui = _tui
_sys.modules[__name__].__class__ = _Facade
