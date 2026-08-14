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
import datetime
import json
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
)
import alvaagent.tui as _tui  # noqa: E402


def compress_now(history, cfg, threshold=0.75, force=False):
    """If usage exceeds the threshold (or force=True), summarize older messages
    in place. Returns True when a compression happened; never raises on failure."""
    tokens, window = context_usage(history, cfg)
    if window <= 0:
        p_info("(no context window configured)")
        return False
    if not force and tokens <= int(window * threshold):
        return False
    p_info("context %d%% of %s - compressing older messages..."
           % (tokens * 100 // window, _fmt_k(window)))
    sp = Spinner("compressing")
    _UI["spinner"] = sp
    sp.start()
    try:
        new, stats = compress_history(history, cfg)
    except KeyboardInterrupt:
        p_info("compression cancelled")
        return False
    except Exception as e:
        p_info("compression failed: %s" % e)
        return False
    finally:
        sp.stop()
        _UI["spinner"] = None
    if not stats:
        if tokens > int(window * 0.6):
            p_info("(nothing to compress - a single message dominates the window; consider /new)")
        else:
            p_info("(nothing to compress)")
        return False
    history[:] = new
    p_ok("[OK] context compressed | %d earlier message%s -> summary"
         % (stats["dropped"], "" if stats["dropped"] == 1 else "s"))
    if stats.get("mode") == "fallback":
        p_info("  (offline summary - the model call failed, kept a basic note)")
    return True


def ask(label, current):
    try:
        v = input("    %s [%s]: " % (label, current)).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return current
    return v if v else current


def parse_key(v, current):
    """Resolve a key-prompt answer: '' keeps, 'none'/'clear'/'-' clears, else replaces."""
    if v == "":
        return current
    if v.lower() in ("none", "clear", "-"):
        return ""
    return v


def ask_key(current):
    """Ask for an API key; 'none' / 'clear' / '-' empties it, Enter keeps."""
    print("    api key [%s]   ('none' clears it)" % mask_key(current))
    try:
        v = input("    > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return current
    return parse_key(v, current)


def ask_permission(desc):
    """Interactive y/N/a prompt used as ON_PERMISSION in the REPL.

    y or a approve (and are remembered for this session, so the same action
    won't prompt again); anything else denies.
    """
    sp = _UI.get("spinner")
    was_running = sp is not None
    if sp:
        sp.stop()
    print()
    print(col(_tui.CUR_SKIN["err"], "  [!] permission needed") + "  " + desc)
    try:
        v = input("    allow? [y/N/a]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        if was_running and sp:
            sp.start()
        return False
    if was_running and sp:
        sp.start()
    if v in ("y", "yes", "a", "allow", "always"):
        print("    (remembered for this session - restart to reset)")
        return True
    return False


# ---------------- slash commands ----------------
def pick_model(base_url, api_key, current, fetch=True):
    """Fetch the models for an endpoint+key and let the user pick one.

    With fetch=False (endpoint unchanged), just ask for the model id directly.
    """
    if fetch:
        try:
            models = fetch_models(base_url, api_key)
        except Exception as e:
            p_info("  (couldn't fetch models: %s)" % e)
            return ask("model", current)
        if not models:
            p_info("  (endpoint reported no models)")
            return ask("model", current)
        print("  available models:")
        for i, m in enumerate(models):
            mark = "   <- current" if m == current else ""
            print("    %2d) %s%s" % (i, m, mark))
        keep = current or "(none)"
        try:
            choice = input("    pick a model [0-%d, Enter=keep %s]: " % (len(models) - 1, keep)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return current
        if choice == "":
            return current
        if choice in models:
            return choice
        try:
            idx = int(choice)
            if 0 <= idx < len(models):
                return models[idx]
        except ValueError:
            pass
        p_info("  invalid choice - keeping %s" % keep)
        return current
    return ask("model", current)


def cmd_models(state):
    cfg = active_cfg(state)
    if not (cfg.get("base_url") or "").rstrip("/"):
        p_err("no base url configured for '%s' - run /provider %s" % (state["active"], state["active"]))
        return
    cfg["model"] = pick_model(cfg.get("base_url", ""), cfg.get("api_key", ""), cfg.get("model", ""))
    save_state(state)
    p_ok("saved [OK]")


_SLASH_COMMANDS = [
    "/help", "/config", "/provider", "/models", "/test", "/skin",
    "/sessions", "/session", "/new", "/clear", "/context", "/compress",
    "/tools", "/trace", "/todos", "/todo", "/memory", "/skills", "/skill",
    "/install_skill", "/feedback", "/reflect", "/self-test", "/improve",
    "/multi", "/export", "/redo", "/stop", "/exit", "/quit",
]


def cmd_skin(state, rest):
    """List or switch the UI skin (persisted in config.json)."""
    arg = rest.strip().lower()
    if not arg or arg in ("ls", "list"):
        print("  skins:")
        for name, sk in SKINS.items():
            mark = "   <- active" if (state.get("skin") or DEFAULT_SKIN) == name else ""
            print("    %-10s %s%s" % (name, sk["desc"], mark))
        print("  usage: /skin <name>")
        return
    if arg not in SKINS:
        p_err("unknown skin '%s' - see /skin" % arg)
        return
    state["skin"] = arg
    save_state(state)
    set_active_skin(state)
    p_ok("skin set to '%s' [OK]" % arg)


def cmd_sessions():
    """List saved sessions (name, message count, last updated)."""
    sess = sessions_map()
    if not sess:
        print("  (no sessions yet - /session <name> starts one)")
        return
    active = _store_get(ACTIVE_SESSION_KEY, "default")
    print("  sessions (%d):" % len(sess))
    for name in sorted(sess, key=lambda n: (sess[n].get("updated") or ""), reverse=True):
        rec = sess[name]
        n = len(rec.get("messages") or [])
        upd = (rec.get("updated") or "")[11:16] or "?"
        mark = ">" if name == active else " "
        print("   %s %-22s %3d msgs | %s" % (mark, name[:22], n, upd))
    print("  usage: /session <name> | /session rm <name> | /session rename <old> <new>")


def cmd_context(state, rest, history):
    """Context usage for the active provider + its settings."""
    cfg = active_cfg(state)
    parts = rest.strip().split(None, 1)
    sub = parts[0].lower() if parts else ""
    val = parts[1].strip() if len(parts) > 1 else ""
    if sub == "window":
        if not val:
            p_err("usage: /context window <tokens>  (0 = auto-detect from the model)")
            return
        try:
            w = int(float(val))
        except (ValueError, OverflowError):
            p_err("usage: /context window <tokens>  (0 = auto-detect from the model)")
            return
        cfg["context_window"] = w
        save_state(state)
        p_ok("context window set to %s [OK]" % (_fmt_k(w) if w else "auto"))
        return
    if sub in ("auto", "autocompress", "auto-compress"):
        cfg["auto_compress"] = not cfg.get("auto_compress", True)
        save_state(state)
        p_ok("auto-compress %s [OK]" % ("on" if cfg["auto_compress"] else "off"))
        return
    tokens, window = context_usage(history, cfg)
    pct = tokens * 100 // window if window else 0
    print("  context usage:")
    print("    system prompt : ~%s tokens" % _fmt_k(estimate_tokens(SYSTEM_PROMPT)))
    print("    conversation  : %s tokens (%d messages)"
          % (_fmt_k(tokens - estimate_tokens(SYSTEM_PROMPT)), len(history)))
    print("    total         : %s / %s  (%d%%)" % (_fmt_k(tokens), _fmt_k(window), pct))
    print("  settings ('%s'):" % state["active"])
    print("    context window: %s   (/context window <n> to override)" % _fmt_k(window))
    print("    auto-compress : %s  at 75%% of the window   (/context autocompress toggles)"
          % ("on" if cfg.get("auto_compress", True) else "off"))
    if pct >= 85:
        p_warn("context is %d%% full - /new starts a fresh session | /compress summarizes now" % pct)


def cmd_compress(history, state, session):
    """Manually summarize older messages to free context (persists immediately)."""
    if len(history) < 8:
        p_info("(conversation is short - nothing to compress)")
        return
    if compress_now(history, active_cfg(state), force=True):
        save_session(session, history)


def cmd_self_test():
    """Run the harness self-test suite and show results.

    Tests calculator, sandbox, todo, memory, skills, command classification,
    file tools, and the feedback/improvement/reflect tools.
    """
    tests = [
        ("calculator basic", lambda: _check(tool_calculator("2+2")["ok"])),
        ("calculator sqrt", lambda: _check(tool_calculator("sqrt(144)")["ok"])),
        ("sandbox rejects div0", lambda: _check(_raises(lambda: tool_calculator("1/0")))),
        ("sandbox rejects complex", lambda: _check(_raises(lambda: tool_calculator("(-8)**0.5")))),
        ("todo add+list+remove", lambda: _check(_todo_check())),
        ("memory save+recall", lambda: _check(_mem_check())),
        ("skills list+read", lambda: _check(_skill_check())),
        ("classify allow: ls", lambda: _check(classify_command("ls -la") == "allow")),
        ("classify ask: rm -rf", lambda: _check(classify_command("rm -rf /") == "ask")),
        ("classify ask: subshell", lambda: _check(classify_command("echo $(whoami)") == "ask")),
        ("file_read in project", lambda: _check(tool_file_read(__file__)["ok"])),
        ("file_write temp", lambda: _check(_file_write_check())),
        ("file_edit temp", lambda: _check(_file_edit_check())),
        ("feedback+improvement+reflect", lambda: _check(_feedback_check())),
    ]
    total = len(tests)
    passed = 0
    for name, fn in tests:
        try:
            ok = fn()
        except Exception as e:
            ok = False
            print("  [FAIL] %-35s -> %s" % (name, e))
            continue
        if ok:
            passed += 1
            print("  [PASS] %-35s" % name)
        else:
            print("  [FAIL] %-35s" % name)
    print("\n  %d/%d tests passed" % (passed, total))
    if passed == total:
        print("  self-test: ALL PASSED")
    else:
        print("  self-test: %d FAILED" % (total - passed))
    return {"passed": passed, "total": total}


def _check(cond):
    return bool(cond)


def _raises(fn):
    """True when calling fn() raises an exception (used by self-test entries
    that assert a tool rejects bad input)."""
    try:
        fn()
    except Exception:
        return True
    return False


def _todo_check():
    r = tool_todo_add("self-test-todo")
    if not r.get("ok"):
        return False
    items = tool_todo_list().get("todos", [])
    found = any(t.get("text") == "self-test-todo" and not t.get("done") for t in items)
    tool_todo_remove(len(items) - 1)
    return found


def _mem_check():
    r = tool_memory_save("self-test-mem", "hello")
    if not r.get("ok"):
        return False
    v = tool_memory_recall("self-test-mem").get("value")
    tool_memory_save("self-test-mem", "")
    return v == "hello"


def _skill_check():
    skills = tool_skill_list().get("skills", [])
    if not skills:
        return True
    name = skills[0]["name"]
    r = tool_skill_read(name)
    return r.get("ok") and r.get("content")


def _file_write_check():
    # stay inside PROJECT_DIR: /tmp is out-of-project and would trigger a
    # permission prompt (or a headless deny), which a self-test shouldn't do
    tmp = os.path.join(PROJECT_DIR, ".alva_sst_write.txt")
    r = tool_file_write(tmp, "test")
    if not r.get("ok"):
        return False
    content = tool_file_read(tmp).get("content", "")
    try:
        os.remove(tmp)
    except OSError:
        pass
    return content == "test"


def _file_edit_check():
    tmp = os.path.join(PROJECT_DIR, ".alva_sst_edit.txt")
    tool_file_write(tmp, "hello world")
    r = tool_file_edit(tmp, "hello", "goodbye")
    if not r.get("ok"):
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    content = tool_file_read(tmp).get("content", "")
    try:
        os.remove(tmp)
    except OSError:
        pass
    return content == "goodbye world"


def _feedback_check():
    r1 = tool_feedback("good", "self-test check")
    if not r1.get("ok"):
        return False
    fb = _store_get(FEEDBACK_KEY, [])
    if not fb or fb[-1].get("rating") != "good":
        return False
    r2 = tool_improvement_set("self-test-area", "fix something")
    if not r2.get("ok"):
        return False
    imps = _store_get(IMPROVEMENT_KEY, [])
    if not imps or imps[-1].get("area") != "self-test-area":
        return False
    r3 = tool_reflect()
    if not r3.get("ok"):
        return False
    tool_improvement_done("self-test-area")
    return True


def cmd_help():
    print("  commands:")
    print("    /help /?               this help")
    print("    /sessions              list saved sessions (name | messages | updated)")
    print("    /session <name>        switch to (or create) a session")
    print("    /session rm <name>     delete a session      /session rename <old> <new>")
    print("    /new                   start a fresh session (the current one is saved)")
    print("    /clear                 wipe the current conversation")
    print("    /context               context meter + settings (window, auto-compress)")
    print("    /compress              summarize older messages to free context now")
    print("    /multi                 multi-line input ('.' on its own line submits)")
    print("    /export                save the conversation as a text file")
    print("    /redo                  re-run the last request (regenerates the answer)")
    print("    /provider [name]       list / add / switch provider profiles")
    print("    /provider rm <name>    delete a provider")
    print("    /config                edit the ACTIVE provider (base url, key, model, temp)")
    print("    /models                fetch + pick a model for the active provider")
    print("    /test                  test the active provider's connection")
    print("    /skin [name]           list / switch the UI skin (midnight, ember, ocean, daylight)")
    print("    (at the api key prompt, type 'none' to clear the key)")
    print("    /tools                 list the agent's tools (and switch mode: /tools full|core)")
    print("    /trace [n]             show the last n agent-trace lines (default 15)")
    print("    /todos                 show the to-do list")
    print("    /todo <text>           add a task")
    print("    /todo done <i>         toggle task i      /todo rm <i>   remove task i")
    print("    /todo clear            empty the list")
    print("    /memory                show saved memory facts")
    print("    /skills                list saved skills (grouped by category)")
    print("    /skills install <u>    install a skill from a URL or local .md [category]")
    print("    /skills sync <repo>    bulk-import a whole skills git repo [subdir]")
    print("    /skill rm <name>       delete a skill (name or category/name)")
    print("    /skill category [n]    list categories / show skills in category n")
    print("    /install_skill <u>     install a skill from a URL or local .md file")
    print("    /feedback <good|bad>   record feedback on the last response")
    print("    /reflect               review feedback + improvements, propose actions")
    print("    /improve               manage self-improvement areas (list/add/done)")
    print("    /self-test             run harness self-test suite (validate after edits)")
    print("    /stop                  cancel the running request")
    print("    /exit  /quit           leave the agent")
    print("    Tab                    completes slash commands")
    print("    Ctrl+C                 while a request runs: cancel it (same as /stop)")


def cmd_config(state):
    cfg = active_cfg(state)
    print("  provider '%s' settings:" % state["active"])
    print("    base url    : " + cfg.get("base_url", ""))
    print("    api key     : " + mask_key(cfg.get("api_key", "")))
    print("    model       : " + cfg.get("model", ""))
    print("    temperature : " + str(cfg.get("temperature", 0.7)))
    print("    context     : " + (_fmt_k(cfg.get("context_window", 0)) if cfg.get("context_window") else "auto"))
    print("    auto-compress: " + ("on" if cfg.get("auto_compress", True) else "off"))
    print("  (Enter keeps the current value; at the api key prompt type 'none' to clear it)")
    base = ask("base url", cfg.get("base_url", ""))
    key = ask_key(cfg.get("api_key", ""))
    unchanged = (base == (cfg.get("base_url") or "") and key == (cfg.get("api_key") or "") and bool(cfg.get("model")))
    cfg["base_url"], cfg["api_key"] = base, key
    cfg["model"] = pick_model(base, key, cfg.get("model", ""), fetch=not unchanged)
    try:
        cfg["temperature"] = float(ask("temperature", str(cfg.get("temperature", 0.7))))
    except ValueError:
        pass
    try:
        cfg["context_window"] = int(float(ask("context window (tokens, 0=auto)", str(cfg.get("context_window") or 0))))
    except (ValueError, OverflowError):
        pass
    ac = ask("auto-compress near the context limit (y/n)",
             "y" if cfg.get("auto_compress", True) else "n").strip().lower()
    cfg["auto_compress"] = ac in ("y", "yes", "on", "1", "true")
    save_state(state)
    p_ok("saved [OK]")


def _list_providers(state):
    profiles = state["profiles"]
    if not profiles:
        print("  (no providers configured)")
        return
    print("  providers:")
    for name, p in profiles.items():
        mark = "   <- active" if name == state["active"] else ""
        print("    %-12s %s | model %s | key %s%s"
              % (name, p.get("base_url") or "(no base)",
                 p.get("model") or "-", mask_key(p.get("api_key", "")), mark))
    print("  usage: /provider <name> (add or switch) | /provider rm <name>")
    print("  presets: openai | groq | openrouter | gemini | custom   (any other name = custom endpoint)")


def cmd_provider(state, rest):
    profiles = state["profiles"]
    arg, _, sub = rest.strip().partition(" ")
    arg = arg.strip().lower()
    sub = sub.strip()

    if arg in ("ls", "list"):
        _list_providers(state)
        return
    if arg in ("rm", "remove", "del", "delete"):
        sub = sub.lower()
        if not sub:
            p_err("usage: /provider rm <name>")
            return
        if sub not in profiles:
            p_err("no provider named '%s'" % sub)
            return
        del profiles[sub]
        if state["active"] == sub:
            state["active"] = next(iter(profiles)) if profiles else "openai"
            if state["active"] not in profiles:
                profiles[state["active"]] = dict(DEFAULT_CFG)
        save_state(state)
        p_ok("removed '%s' [OK]" % sub)
        return
    if not arg:
        _list_providers(state)
        return

    # switch to an existing provider
    if arg in profiles:
        state["active"] = arg
        save_state(state)
        p_ok("switched to '%s' [OK]" % arg)
        return

    # add a new provider - a fresh profile, never inherits another provider's key
    p = PROVIDERS.get(arg)
    prof = dict(FIRST_RUN_CFG)
    if p:
        print("  " + p["label"])
        prof["base_url"] = p["base"]
        if p["model"]:
            prof["model"] = p["model"]
    if arg == "custom" or p is None:
        prof["base_url"] = ask("base url", prof.get("base_url", ""))
    prof["api_key"] = ask_key("")
    prof["model"] = pick_model(prof.get("base_url", ""), prof.get("api_key", ""), prof.get("model", ""))
    profiles[arg] = prof
    state["active"] = arg
    save_state(state)
    p_ok("added '%s' [OK]" % arg)


def cmd_test(state):
    cfg = active_cfg(state)
    base = (cfg.get("base_url") or "").rstrip("/")
    if not base:
        p_err("no base url configured for '%s' - run /provider %s" % (state["active"], state["active"]))
        return
    req = urllib.request.Request(
        base + "/models",
        headers={"Authorization": "Bearer " + (cfg.get("api_key") or ""),
                 "Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        n = len(data.get("data") or [])
        p_ok("Connected [OK] | %d model%s available" % (n, "" if n == 1 else "s"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        p_err("HTTP %d - %s" % (e.code, _readable_error(e.code, body)))
    except Exception as e:
        p_err("cannot reach API: %s" % e)


def cmd_tools():
    """List the active tool set (only what the model can currently see)."""
    active = active_tools()
    print("  tool mode: %s - %d/%d tools advertised to the model"
          % (_tools._TOOLS_MODE, len(active), len(TOOLS)))
    for t in active:
        fn = t["function"]
        print("  %-14s %s" % (fn["name"], fn.get("description", "")))
    hidden = len(TOOLS) - len(active)
    if hidden:
        print("  hidden (%d advanced; /tools full to advertise, /tools core to revert): %s"
              % (hidden, ", ".join(sorted(_ADVANCED_TOOL_NAMES))))


def cmd_trace(rest):
    """Print the last n lines of trace.log, oldest first (default 15)."""
    try:
        n = max(1, min(200, int(str(rest or "15").strip())))
    except ValueError:
        n = 15
    lines = _read_trace(n)
    if not lines:
        print("  (trace.log is empty - run some turns first)")
        return
    for ln in lines:
        try:
            rec = json.loads(ln)
        except Exception:
            print("  " + ln[:200])
            continue
        ev = rec.get("event", "?")
        rest_rec = {k: v for k, v in rec.items() if k not in ("event", "ts")}
        print("  %s %s" % (ev, json.dumps(rest_rec, ensure_ascii=False)[:220]))


def cmd_todos():
    lst = tool_todo_list().get("todos", [])
    if not lst:
        print("  (empty)")
        return
    for i, t in enumerate(lst):
        mark = "[x]" if t.get("done") else "[ ]"
        print("  %d %s %s" % (i, mark, t.get("text", "")))


def cmd_todo(rest):
    parts = rest.split(None, 1)
    op = parts[0].lower() if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""
    if not op or op in ("list", "ls", "show"):
        cmd_todos()
        return
    if op in ("add", "a"):
        r = tool_todo_add(arg)
        if r.get("ok"):
            p_ok("  added #%d: %s" % (r["index"], r["text"]))
        else:
            p_err("  " + r.get("error", "?"))
        return
    if op in ("done", "toggle", "t", "d"):
        try:
            r = tool_todo_toggle(int(arg))
        except ValueError:
            p_err("  need an index, e.g. /todo done 0")
            return
        if r.get("ok"):
            p_ok("  #%d %s: %s" % (r["index"], "done" if r.get("done") else "undone", r.get("text", "")))
        else:
            p_err("  " + r.get("error", "?"))
        return
    if op in ("rm", "remove", "del", "delete"):
        try:
            r = tool_todo_remove(int(arg))
        except ValueError:
            p_err("  need an index, e.g. /todo rm 0")
            return
        if r.get("ok"):
            p_ok("  removed #%d: %s" % (r.get("index", "?"), r.get("removed", {}).get("text", "")))
        else:
            p_err("  " + r.get("error", "?"))
        return
    if op == "clear":
        _store_set(TODO_KEY, [])
        p_ok("  list cleared")
        return
    p_err("  usage: /todo <text> | /todo done <i> | /todo rm <i> | /todo clear")


def cmd_memory():
    facts = [(k[len(MEM_PREFIX):], v) for k, v in _store.items() if k.startswith(MEM_PREFIX)]
    if not facts:
        print("  (no saved facts)")
        return
    print("  %-16s %-20s %s" % ("Key", "Tags", "Value"))
    print("  " + "-"*50)
    for k, v in facts:
        val = v.get("value", v) if isinstance(v, dict) else v
        tags = ", ".join(v.get("tags", [])) if isinstance(v, dict) else ""
        print("  %-16s %-20s %s" % (k, tags, val))


def cmd_feedback(rest):
    """Record feedback on the agent's last response.

    Usage: /feedback good | /feedback bad <notes> | /feedback neutral
    """
    parts = rest.strip().split(None, 1)
    if not parts:
        p_err("usage: /feedback <good|bad|neutral> [notes]")
        return
    rating = parts[0].lower()
    if rating not in ("good", "bad", "neutral"):
        p_err("rating must be good, bad, or neutral")
        return
    notes = parts[1] if len(parts) > 1 else ""
    r = tool_feedback(rating, notes or None)
    if r.get("ok"):
        p_ok("feedback recorded: %s%s" % (rating, " - " + notes if notes else ""))
    else:
        p_err("  " + r.get("error", "?"))


def cmd_skills(rest=""):
    arg = (rest or "").strip()
    if arg.startswith("install "):
        target = arg[len("install "):].strip()
        if not target:
            p_err("usage: /skills install <url|path> [category]")
            return
        parts = target.split(None, 1)
        r = tool_skill_install(parts[0], parts[1].strip() if len(parts) > 1 else None)
        if r.get("ok"):
            p_ok("installed skill '%s' [OK]" % r.get("name"))
            if r.get("category"):
                print("    category: %s" % r["category"])
        else:
            p_err("  " + r.get("error", "?"))
        return
    if arg.startswith("sync "):
        parts = arg[len("sync "):].strip().split(None, 1)
        if not parts:
            p_err("usage: /skills sync <repo-url> [subdir]")
            return
        r = tool_skill_sync_repo(parts[0], parts[1].strip() if len(parts) > 1 else None)
        if r.get("ok"):
            p_ok("synced %d skills from repo [OK]" % r.get("count"))
            for s in r.get("installed", []):
                print("    - %s%s" % (s["name"], (" (" + s["category"] + ")") if s.get("category") else ""))
            if r.get("errors"):
                for name, err in r.get("errors", []):
                    p_err("    %s: %s" % (name, err))
        else:
            p_err("  " + r.get("error", "?"))
        return
    if arg:
        cmd_skill_category(arg)
        return
    skills = tool_skill_list().get("skills") or []
    if not skills:
        print("  (no skills yet - ask the agent to save one)")
        return
    # group by category
    by_cat = {}
    for s in skills:
        cat = s.get("category") or "(flat)"
        by_cat.setdefault(cat, []).append(s)
    print("  skills (%d):" % len(skills))
    for cat in sorted(by_cat):
        entries = by_cat[cat]
        print("    [%s] %d" % (cat, len(entries)))
        for s in entries:
            desc = s.get("description") or ""
            if desc:
                desc = "  " + desc[:55]
            tags = s.get("tags") or []
            tagstr = ("  " + ", ".join(str(t) for t in tags)) if tags else ""
            print("      - %s%s%s" % (col(C.BOLD, s["name"]),
                                      col(C.DIM, desc),
                                      col(C.DIM, tagstr)))


def cmd_skill_category(rest):
    """List skills in a category, or list all categories."""
    arg = (rest or "").strip().lower()
    skills = tool_skill_list().get("skills") or []
    if arg in ("ls", "list", "show"):
        # list categories
        cats = {}
        for s in skills:
            c = s.get("category") or "(flat)"
            cats.setdefault(c, 0)
            cats[c] += 1
        if not cats:
            print("  (no skills yet)")
            return
        print("  categories (%d):" % len(cats))
        for c in sorted(cats):
            print("    %s  (%d skill%s)" % (col(C.BOLD, c if c != "(flat)" else "flat"),
                                             cats[c], "" if cats[c] == 1 else "s"))
        return
    if not arg:
        # no arg: list all categories
        cmd_skill_category("ls")
        return
    # show skills in one category
    cat_skills = [s for s in skills if (s.get("category") or "(flat)") == arg]
    if not cat_skills:
        p_err("no skills in category '%s'" % arg)
        return
    print("  category '%s' (%d skill%s):" % (arg, len(cat_skills),
                                              "" if len(cat_skills) == 1 else "s"))
    for s in cat_skills:
        desc = s.get("description") or ""
        if desc:
            desc = "  " + desc[:50]
        tags = s.get("tags") or []
        tagstr = ("  " + ", ".join(str(t) for t in tags)) if tags else ""
        print("      - %s%s%s" % (col(C.BOLD, s["name"]),
                                  col(C.DIM, desc),
                                  col(C.DIM, tagstr)))


def cmd_reflect():
    """Review all feedback + improvement areas and propose actions."""
    fb = _store_get(FEEDBACK_KEY, [])
    imps = _store_get(IMPROVEMENT_KEY, [])
    if not fb and not imps:
        p_info("(no feedback or improvements yet)")
        return
    if fb:
        print("  --- recent feedback (%d entries) ---" % len(fb))
        for e in fb[-10:]:
            tag = {"good": "+", "bad": "-", "neutral": "~"}.get(e["rating"], "?")
            note = " - " + e["notes"] if e.get("notes") else ""
            print("    %s [%s]%s" % (tag, e["rating"], note))
    if imps:
        print("  --- improvement areas (%d) ---" % len(imps))
        for it in imps:
            mark = "[x]" if it.get("done") else "[ ]"
            print("    %s %s" % (mark, it["area"]))
            print("        -> %s" % it["action"])
    print("  --- suggested actions ---")
    bad = [e for e in fb if e["rating"] == "bad"]
    if bad:
        print("    - review %d negative feedback entries above" % len(bad))
    open_imgs = [it for it in imps if not it.get("done")]
    if open_imgs:
        print("    - %d improvement area(s) still open - act on them" % len(open_imgs))
    if not bad and not open_imgs:
        print("    - no open issues - keep doing what works")
    print("  (re-run /reflect after making changes to mark them done)")
def cmd_improve(rest):
    """Manage self-improvement areas.

    Usage:
      /improve list          - show all pending + done improvements
      /improve add <area> <action>  - record a new area to improve
      /improve done <area>   - mark an area as resolved
    """
    parts = rest.strip().split(None, 2)
    if not parts:
        print("  Usage:")
        print("    /improve list")
        print("    /improve add <area> <action>")
        print("    /improve done <area>")
        return
    sub = parts[0].lower()
    if sub in ("list", "ls", "show"):
        items = _store_get(IMPROVEMENT_KEY, [])
        if not items:
            p_info("(no improvement areas yet)")
            return
        pending = [it for it in items if not it.get("done")]
        done = [it for it in items if it.get("done")]
        if pending:
            print("  --- pending (%d) ---" % len(pending))
            for it in pending:
                print("    [ ] %s" % it["area"])
                print("        -> %s" % it["action"])
        if done:
            print("  --- done (%d) ---" % len(done))
            for it in done:
                print("    [x] %s" % it["area"])
        print("  (use /improve done <area> to mark resolved)")
    elif sub in ("add", "set"):
        if len(parts) < 3:
            p_err("usage: /improve add <area> <action>")
            return
        area = parts[1]
        action = parts[2]
        r = tool_improvement_set(area, action)
        if r.get("ok"):
            p_ok("improvement recorded: %s" % area)
        else:
            p_err("  " + r.get("error", "?"))
    elif sub in ("done", "mark", "resolve"):
        if len(parts) < 2:
            p_err("usage: /improve done <area>")
            return
        r = tool_improvement_done(parts[1])
        if r.get("ok"):
            p_ok("marked done: %s" % r.get("area", parts[1]))
        else:
            p_err("  " + r.get("error", "?"))
    else:
        p_err("unknown subcommand: %s (list/add/done)" % sub)


def cmd_install_skill(rest):
    # Install a skill from a local .md file or a URL (delegates to
    # tool_skill_install so GitHub/blob URLs are auto-rewritten to raw).
    r = tool_skill_install(rest.strip())
    if r.get("ok"):
        p_ok("installed skill '%s' [OK]" % r.get("name"))
        if r.get("category"):
            print("    category: %s" % r["category"])
    else:
        p_err("failed to install skill: %s" % r.get("error", "unknown error"))
def cmd_clear(history):
    if not history:
        p_info("(conversation is already empty)")
        return
    try:
        v = input("  clear %d messages? [y/N]: " % len(history)).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if v not in ("y", "yes"):
        p_info("(cleared skipped)")
        return
    history.clear()
    _store_set(HISTORY_KEY, [])
    p_ok("conversation cleared")


def cmd_export(history):
    """Export the conversation as plain text."""
    if not history:
        p_info("(no conversation to export)")
        return
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = os.path.join(DATA_DIR, "conversation_%s.txt" % ts)
    lines = []
    for m in history:
        role = m.get("role", "?")
        content = m.get("content", "")
        if role == "user" and content.startswith("[summary of earlier conversation]"):
            lines.append("## summary (compressed)\n%s\n" % content)
        elif role == "user":
            lines.append("## you\n%s\n" % content)
        elif role == "assistant":
            lines.append("## agent\n%s\n" % content)
        elif role == "tool":
            lines.append("## tool (%s)\n%s\n" % (m.get("tool_call_id", "?"), content[:500]))
    try:
        with open(fname, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        p_ok("exported to %s (%d messages)" % (fname, len(history)))
    except Exception as e:
        p_err("export failed: %s" % e)


def cmd_multi():
    """Collect multi-line input until '.' on its own line or Ctrl+D."""
    print(col(C.DIM, "  (multi-line mode - type '.' alone to submit, Ctrl+C to cancel)"))
    lines = []
    try:
        while True:
            line = input("  > ")
            if line.strip() == ".":
                break
            lines.append(line)
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not lines:
        return None
    return "\n".join(lines)


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
