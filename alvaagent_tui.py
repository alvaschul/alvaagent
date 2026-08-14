#!/usr/bin/env python3
# ============================================================
#  alvaagent_tui.py - alvaagent terminal chat client
#
#  Same agent harness ported to plain Python so it runs entirely inside
#  Termux - no browser, no web server, nothing to disconnect when you
#  switch apps.
#
#  Uses only the Python standard library plus `rich` (pure-Python, pip-installs
#  on Termux - see alva_fix.sh). `rich` backs the Hermes-style panels.
#
#  Run:
#    python3 alvaagent_tui.py     (or:  bash start.sh tui)
#
#  Providers: each setup (/provider <name>) is saved as its own named profile
#  in ./.alvaagent/config.json next to this script (e.g. on Android shared
#  storage); /provider <name> adds a new profile or switches to an existing
#  one. Env vars ALVA_BASE_URL, ALVA_API_KEY, ALVA_MODEL, ALVA_TEMPERATURE
#  (POCKET_* names also still accepted) override the ACTIVE profile at start.
#
#  Todos & memory facts persist to ./.alvaagent/store.json next to this
#  script (override the folder with ALVA_DATA_DIR).
#
#  Commands:
#    /help /config /provider /models /test /tools /trace /todos /todo /memory
#    /skin /sessions /session /new /clear /context /compress /multi /export
#    /stop /exit /quit
#
#  Tool selection: by default the model sees a curated CORE tool set (~15);
#  /tools full advertises all tools, and any advanced tool call auto-enables
#  full mode. The choice persists in config.json ("tool_mode").
#    Ctrl+C cancels a running request | Tab completes slash commands
#    (at the api key prompt, type 'none' to clear the key)
#
#  Sessions: conversations are saved to store.json and resumed on restart.
#    /sessions lists them | /session <name> switches/creates | /new starts fresh.
#  Context: the footer shows a live ctx meter (est. tokens / model window) and
#    auto-compresses older messages into a summary near the limit so long chats
#    don't drift out of context. /context shows the numbers | /compress forces it.
#  Skins: /skin lists & switches the color theme (midnight | ember | ocean |
#  daylight). Skins persist to config.json. The layout echoes the Hermes agent
#  TUI (banner + bordered message blocks + tool blocks + status chips) but with
#  its own palettes, a footer status line instead of a persistent bottom bar,
#  tab-completion instead of a dropdown, and the alvaagent ⚡ brand.
#
#  Note: single-line input - a multi-line paste submits only its first
#  line (the soft keyboard's Enter sends each line).
# ============================================================
import os
import readline
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

# PyYAML is OPTIONAL: it powers full YAML parsing of skill frontmatter, but the
# harness ships a tiny fallback parser/serializer for the simple key:value +
# list format it writes, so the TUI stays runnable with zero pip installs
# (stdlib only, as the README promises).
try:
    import yaml
except Exception:
    yaml = None


from alvaagent.util import (  # noqa: E402,F401
    _env, now_iso, _fmt_k, _atomic_write, _looks_like_html, _raw_fetch,
    mask_key, _parse_frontmatter, _frontmatter_load, _frontmatter_dump,
    _mini_yaml, _mini_scalar, _finish_block, _cancel_flag,
)

# paths / config moved to alvaagent/config.py (Task 3)
from alvaagent.config import (  # noqa: E402,F401
    data_dir, DATA_DIR, _LEGACY_DIRS, CONFIG_PATH, STORE_PATH, HISTORY_PATH,
    TRACE_PATH, PROVIDERS, DEFAULT_CFG, FIRST_RUN_CFG, DEFAULT_SKIN,
    SKIN_NAMES, ALVA_VERSION, DEFAULT_CONTEXT_WINDOW, MODEL_CONTEXT,
    TOOL_MODES, _tool_mode_of, _skin_of, _normalize_state, load_state,
    save_state, active_cfg,
)

# store moved to alvaagent/store.py (Task 4)
from alvaagent.store import (  # noqa: E402,F401
    _store, _migrate_legacy_dir, _load_store, _save_store,
    _store_get, _store_set, TODO_KEY, MEM_PREFIX, FEEDBACK_KEY,
    IMPROVEMENT_KEY, HISTORY_KEY, SESSION_KEY, ACTIVE_SESSION_KEY, MAX_SESSIONS,
)

# ---------------- autonomy: permissions ----------------
# (classifiers moved to alvaagent/permissions.py (Task 5))
from alvaagent.permissions import (  # noqa: E402,F401
    _READONLY_PREFIXES, _RISKY_TOKENS, _RISKY_OPERATORS, _tokenize_shell,
    classify_command, PROJECT_DIR, _in_project, classify_file_action,
    _APPROVED_SET, _permission, ON_PERMISSION,
)

# tools moved to alvaagent/tools.py (Task 7)
import alvaagent.tools as _tools  # noqa: E402
from alvaagent.tools import (  # noqa: E402,F401
    _PY_RUN_TIMEOUT, _PY_MAX_BYTES, _PY_MAX_CHARS, _CALC_ALLOWED,
    _CORE_TOOL_NAMES, _ADVANCED_TOOL_NAMES,
    active_tools, _maybe_enable_full, _set_tool_mode, _sync_tool_mode,
    tool_run_command, tool_file_read, tool_file_write, tool_file_edit,
    tool_file_list, tool_file_search, tool_todo_list, tool_todo_add,
    tool_todo_toggle, tool_todo_remove, tool_memory_save, tool_memory_recall,
    tool_memory_list, tool_memory_search, tool_get_time, tool_feedback,
    tool_improvement_set, tool_improvement_list, tool_improvement_done,
    tool_reflect, tool_web_fetch, _safe_factorial, _calc_eval, _fmt_num,
    tool_calculator, classify_python, tool_run_python,
    TOOLS, TOOL_IMPL, _TOOL_ERROR_HINTS, dispatch_tool, self_test, tool_self_test,
)

# skills moved to alvaagent/skills.py (Task 6)
from alvaagent.skills import (  # noqa: E402,F401
    _SKILL_FM_RE, _SKILL_FM_DEFAULT, _VALID_FM_KEYS, _SKILL_RAW_MAX,
    _skill_body_for_tool, _detect_category, _skill_filepath, _inside_skills,
    _resolve_skill_path, _skill_read, _scan_skill_files, _skill_list_all,
    tool_skill_list, tool_skill_read, tool_skill_remove, tool_skill_save,
    tool_skill_install, tool_skill_sync_repo,
)

# LLM client moved to alvaagent/client.py (Task 8)
from alvaagent.client import (  # noqa: E402,F401
    SYSTEM_PROMPT,
    _MAX_RETRIES, _RETRY_BACKOFF, _STREAM_IDLE_LIMIT, _STREAM_POLL,
    _readable_error, _retryable_status, _sleep_retry, _Cancelled,
    chat_completion, chat_completion_stream, fetch_models, cancel_agent,
)
# sessions/context/compression moved to alvaagent/sessions.py (Task 9)
from alvaagent.sessions import (  # noqa: E402,F401
    context_window_for, estimate_tokens, estimate_message_tokens, context_usage,
    sessions_map, load_session, save_session, delete_session, _find_session,
    _rename_session_in_store, auto_title, _unique_session_name,
    summarize_with_llm, _fallback_summary, compress_history,
    trim_history, new_session_name,
)

# agent loop moved to alvaagent/agent.py (Task 10)
from alvaagent.agent import (  # noqa: E402,F401
    _TURN_TIMEOUT, _MAX_CONSEC_TOOL_FAILURES, ON_TOOL,
    _repair_tool_pairs, _report_tool,
    run_agent, _clean_segment, _strip_xml_blocks, _parse_xml_tool_calls,
    _strip_xml, run_agent_stream,
)

# trace helpers moved to alvaagent/trace.py (Task 3)
from alvaagent.trace import _read_trace  # noqa: E402,F401

# TUI rendering moved to alvaagent/tui.py (Task 11)
from alvaagent.tui import (  # noqa: E402,F401
    SKINS, C, set_active_skin, col, p_info, p_err, p_ok, p_warn, _term_width,
    _hrgb, _fgh, _rsth, _tool_line, print_user_turn, render_agent_panel,
    _md_attr_sgr, _has_ansi, _md_line, _md_prefix, style_inline, AgentWriter,
    fmt_args, tool_summary, Spinner, tool_open, tool_close, on_tool,
    run_agent_tui, _ANSI_RE, _MD_STYLE, _UI, COLOR, CUR_SKIN, _CON,
    Console, Panel, HORIZONTALS, banner, render_status_bar,
    compress_now,
)
import alvaagent.tui as _tui  # noqa: E402

# slash commands moved to alvaagent/commands.py (Task 12)
from alvaagent.commands import (  # noqa: E402,F401
    _SLASH_COMMANDS, ask_permission,
    cmd_models, cmd_skin, cmd_sessions, cmd_context, cmd_compress,
    cmd_self_test, cmd_help, cmd_config, cmd_provider, cmd_test, cmd_tools,
    cmd_trace, cmd_memory, cmd_export, cmd_multi,
    cmd_install_skill, cmd_improve, cmd_skills, cmd_skill_category, cmd_clear,
)

# ---------------- REPL ----------------


def setup_completion():
    """Tab-complete slash commands via readline (Hermes-style autocomplete,
    adapted to a line-oriented Termux prompt). Also loads + persists input
    history so up-arrow recall survives restarts (important on Termux, where
    you launch the TUI fresh each time you open the app)."""
    try:
        # Clear any in-memory history first so re-loading the file doesn't
        # duplicate entries (readline appends on read_history_file).
        readline.clear_history()
        # Load persisted command history (silent if absent)
        if os.path.exists(HISTORY_PATH):
            readline.read_history_file(HISTORY_PATH)
        readline.set_history_length(2000)  # keep last 2000 entries
        # '/' is a completer delimiter by default, so typing /he<Tab> hands the
        # completer 'he' and _slash_complete's startswith("/") check never
        # fires. Remove it so slash commands actually complete.
        delims = readline.get_completer_delims().replace("/", "")
        readline.set_completer_delims(delims)
        readline.set_completer(_slash_complete)
        readline.parse_and_bind("tab: complete")
    except Exception as e:
        p_info("(tab completion unavailable: %s)" % e)


def save_completion_history():
    """Flush readline history to disk (called on exit and after each turn)."""
    try:
        readline.write_history_file(HISTORY_PATH)
    except Exception:
        pass


def _slash_complete(text, state):
    if text.startswith("/"):
        opts = [c for c in _SLASH_COMMANDS if c.startswith(text.lower())]
        return opts[state] if state < len(opts) else None
    return None


def send_message(text, history, state, session):
    """Render the 'you' bubble, run the agent, manage context + sessions.

    Returns the (possibly auto-renamed) session name.
    """
    cfg = active_cfg(state)
    # auto-title a fresh placeholder session from the first user message
    if session.startswith("sess-"):
        new_name = _unique_session_name(auto_title(text))
        if new_name != session:
            _rename_session_in_store(session, new_name)
            session = new_name
    print()
    print_user_turn(text)
    history.append({"role": "user", "content": text})
    trim_history(history)
    # pre-turn safety: only act if the window is nearly full (0.9) - the post-turn
    # check (0.75) is the normal compressor, so both rarely fire in one turn
    if cfg.get("auto_compress", True):
        compress_now(history, cfg, threshold=0.9)
    try:
        res = run_agent_tui(history, cfg)
    except KeyboardInterrupt:
        cancel_agent()
        p_info("cancelled")
        if history:
            history.pop()  # drop the unanswered message
        save_session(session, history)
        return session
    except Exception as e:
        p_err("agent error: %s" % e)
        if history:
            history.pop()
        save_session(session, history)
        return session
    # drop the internal system prompt that run_agent_stream prepends
    history[:] = [m for m in res["history"] if m.get("role") != "system"]
    if res.get("cancelled"):
        p_info("(request stopped)")
        save_session(session, history)
        return session
    streamed = bool(res.get("streamed"))
    tools = res.get("tools", 0)
    content = (res.get("content") or "").strip()
    if not streamed and not tools:
        # Dead turn: no text streamed and no tool calls. This is either a
        # failed request (run_agent_stream caught a RuntimeError and yielded
        # content="error: ...") or a gateway that answered with empty content.
        # Remove the unanswered user message (and any trailing empty assistant
        # ghost) so failed/empty turns don't pile up as consecutive duplicates
        # in the session - retrying via Up+Enter previously stacked one ghost
        # per attempt, which looked like the message was being duplicated.
        while history and history[-1].get("role") == "assistant":
            if history[-1].get("content") or history[-1].get("tool_calls"):
                break
            history.pop()
        if history and history[-1].get("role") == "user":
            history.pop()
        save_session(session, history)
        if content.startswith("error:"):
            render_agent_panel(content)
            p_warn("the turn failed - your message was not saved (retry when the endpoint is back)")
        else:
            p_warn("the model returned an empty response - is the endpoint/streaming working?"
                   "  (your message was not saved - retry)")
        return session
    if content and not streamed:
        render_agent_panel(content)
    # post-turn: auto-compress if the response pushed us past the threshold
    compressed = False
    if cfg.get("auto_compress", True):
        compressed = compress_now(history, cfg)
    tokens, window = context_usage(history, cfg)
    render_status_bar(state, session, res.get("elapsed", 0.0), res.get("tools", 0), history)
    pct = tokens * 100 // window if window else 0
    if not compressed and window and pct >= 85:
        p_warn("context at %d%% of %s - /new starts a fresh session | /compress summarizes older messages"
               % (pct, _fmt_k(window)))
    save_session(session, history)
    return session


def repl():
    state = load_state()
    _sync_tool_mode(state)
    set_active_skin(state)
    # resume the last active session (conversations persist across restarts)
    session = _store_get(ACTIVE_SESSION_KEY) or "default"
    history = load_session(session)
    # last completed turn, for /redo (session-scoped so it can't leak across
    # a /session switch)
    _last_turn = {"session": None, "text": None, "pre": None}
    while True:
        try:
            prompt = col(_tui.CUR_SKIN["accent"], "> ") if COLOR else "> "
            line = input(prompt)
        except EOFError:
            print()
            save_completion_history()
            break
        except KeyboardInterrupt:
            print()
            save_completion_history()
            break
        line = line.strip()
        if not line:
            continue

        if line.startswith("/"):
            cmd, _, rest = line.partition(" ")
            c = cmd.lower()
            if c in ("/help", "/h", "/?"):
                cmd_help()
            elif c == "/config":
                cmd_config(state)
            elif c == "/provider":
                cmd_provider(state, rest)
            elif c == "/test":
                cmd_test(state)
            elif c == "/tools":
                arg = rest.strip().lower()
                if arg in TOOL_MODES:
                    _set_tool_mode(state, arg)
                    p_ok("tool mode: %s (%d tools advertised to the model)"
                         % (_tools._TOOLS_MODE, len(active_tools())))
                else:
                    cmd_tools()
            elif c == "/trace":
                cmd_trace(rest)
            elif c == "/models":
                cmd_models(state)
            elif c == "/skin":
                cmd_skin(state, rest)
            elif c == "/sessions":
                cmd_sessions()
            elif c == "/session":
                arg, _, sub = rest.strip().partition(" ")
                arg = arg.strip().lower()
                sub = sub.strip()
                if not arg or arg in ("ls", "list", "show"):
                    cmd_sessions()
                elif arg in ("rm", "remove", "del", "delete"):
                    target = _find_session(sub)
                    if not sub:
                        p_err("usage: /session rm <name>")
                    elif target is None:
                        p_err("no session named '%s'" % sub)
                    elif target.lower() == session.lower():
                        p_err("that's the active session - switch first (/session <name>)")
                    else:
                        delete_session(target)
                        p_ok("deleted session '%s' [OK]" % target)
                elif arg in ("rename", "mv"):
                    old, _, new = sub.partition(" ")
                    old, new = old.strip(), new.strip()
                    target = _find_session(old)
                    if not old or not new:
                        p_err("usage: /session rename <old> <new>")
                    elif target is None:
                        p_err("no session named '%s'" % old)
                    elif _find_session(new):
                        p_err("a session named '%s' already exists" % new)
                    else:
                        _rename_session_in_store(target, new)
                        if session.lower() == target.lower():
                            session = new
                        p_ok("renamed '%s' -> '%s' [OK]" % (target, new))
                else:
                    save_session(session, history)  # persist the outgoing session
                    target = _find_session(arg)
                    if target is None:
                        target = arg
                        p_info("(new session '%s')" % target)
                    history[:] = load_session(target)
                    session = target
                    save_session(session, history)  # mark active + refresh timestamp
                    p_ok("switched to session '%s' | %d messages" % (session, len(history)))
            elif c == "/context":
                cmd_context(state, rest, history)
            elif c == "/compress":
                cmd_compress(history, state, session)
            elif c == "/new":
                save_session(session, history)
                cmd_clear(history)
                session = new_session_name()
                save_session(session, history)
                p_ok("new session: " + session)
            elif c == "/clear":
                cmd_clear(history)
            elif c == "/multi":
                text = cmd_multi()
                if text and text.strip():
                    session = send_message(text.strip(), history, state, session)
            elif c == "/install_skill":
                cmd_install_skill(rest)
            elif c == "/self-test":
                cmd_self_test()
            elif c == "/improve":
                cmd_improve(rest)
            elif c == "/skills":
                cmd_skills(rest)
            elif c == "/skill":
                op, _, arg = rest.strip().partition(" ")
                op = op.strip().lower()
                if op in ("rm", "remove", "del", "delete"):
                    if not arg:
                        p_err("usage: /skill rm <name>")
                        continue
                    r = tool_skill_remove(arg)
                    if r.get("ok"):
                        p_ok("removed skill '%s' (category: %s) [OK]"
                             % (r.get("name", "?"), r.get("category") or "(flat)"))
                    else:
                        p_err("  " + r.get("error", "?"))
                elif op in ("cat", "category", "cats", "categories"):
                    cmd_skill_category(arg)
                else:
                    p_err("usage: /skill rm <name>  |  /skill category [name]")
                continue
            elif c == "/memory":
                cmd_memory()
            elif c == "/export":
                cmd_export(history)
            elif c == "/stop":
                cancel_agent()
                p_info("stopping...")
            elif c == "/redo":
                if _last_turn.get("session") != session or _last_turn.get("text") is None:
                    p_err("nothing to redo - send a message first (in this session)")
                    continue
                history[:] = _last_turn["pre"]
                p_info("(re-running: %s)" % _last_turn["text"][:80])
                session = send_message(_last_turn["text"], history, state, session)
            elif c in ("/exit", "/quit", "/q"):
                break
            else:
                p_err("unknown command: " + c + "   (/help for the list)")
            continue

        _last_turn["session"] = session
        _last_turn["text"] = line
        _last_turn["pre"] = list(history)
        session = send_message(line, history, state, session)
        save_completion_history()  # persist input history after each turn
    save_session(session, history)
    save_completion_history()  # flush readline history to disk on exit
    print(col(C.DIM, "bye"))


def main():
    import alvaagent.agent as _agent
    _load_store()
    setup_completion()
    _agent.ON_TOOL = on_tool        # live tool-progress blocks
    import alvaagent.permissions as _perms
    _perms.ON_PERMISSION = ask_permission  # interactive y/N for risky actions
    state = load_state()
    _sync_tool_mode(state)
    set_active_skin(state)

    # Guarantee screen restoration even on SIGTERM / OOM kill / crash.
    # SIGTERM and SIGINT both route through _cleanup so the alternate-screen
    # escape always lands; without this, `kill` from another session leaves the
    # stale TUI buffer on screen (the issue the user hit).
    _restored = threading.Event()

    def _cleanup(signum=None, frame=None):
        if _restored.is_set():
            return
        _restored.set()
        try:
            sys.stdout.write("\x1b[?1049l")
            sys.stdout.write("\n")
            sys.stdout.flush()
        except Exception:
            pass
        if signum is not None:
            sys.exit(signum)

    def _on_sigterm(signum, frame):
        _cleanup(signum, frame)

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except Exception:
        pass
    # SIGINT is deliberately left untouched (Python's default raises
    # KeyboardInterrupt): the REPL's `except KeyboardInterrupt` (Ctrl+C during
    # input), the agent's `except KeyboardInterrupt` (Ctrl+C during a network
    # call), and any library that catches it all work as expected, and the
    # `finally: _cleanup()` below still runs on every exit path. Do NOT set
    # SIGINT to SIG_DFL here — that kills the process outright, skipping both
    # KeyboardInterrupt handling and _cleanup(), leaving the terminal stuck in
    # the alternate-screen buffer.

    # Alternate-screen buffer: take over the whole terminal like Hermes' TUI
    # (prior scrollback hidden on launch, restored on exit). Emit the enter
    # code, run, and always emit the leave code (even on Ctrl-C / error).
    sys.stdout.write("\x1b[?1049h")
    sys.stdout.flush()
    try:
        banner(state)
        repl()
    finally:
        _cleanup()


if __name__ == "__main__":
    main()
