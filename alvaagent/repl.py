import os
import readline
import signal
import sys
import threading

from alvaagent.config import HISTORY_PATH, TOOL_MODES, active_cfg
from alvaagent.context import build_runtime
from alvaagent.store import get as store_get, ACTIVE_SESSION_KEY
from alvaagent.client import cancel_agent
from alvaagent.sessions import (
    context_usage, delete_session, find_session, load_session,
    new_session_name, rename_session, save_session, trim_history,
    unique_session_name,
)
from alvaagent.tools import (
    set_mode, sync_tool_mode, visible, skill_remove,
)
from alvaagent.tui import (
    C, COLOR, banner, col, compress_now, on_tool, p_err, p_info, p_ok, p_warn,
    print_user_turn, render_agent_panel, render_status_bar, run_agent_tui,
    set_active_skin,
)
from alvaagent.commands import (
    _SLASH_COMMANDS, ask_permission,
    cmd_clear, cmd_compress, cmd_config, cmd_context, cmd_export, cmd_help,
    cmd_scroll,
    cmd_improve, cmd_install_skill, cmd_memory, cmd_models, cmd_multi,
    cmd_provider, cmd_self_test, cmd_sessions, cmd_skill_category, cmd_skills,
    cmd_skin, cmd_test, cmd_tools, cmd_trace,
)
from alvaagent.util import _fmt_k
import alvaagent.tui as _tui
from alvaagent.scrollback import (StreamTee, LineReader, ScrollView,
                                  MOUSE_DISABLE)
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


_TEE = None


def _history_file_lines():
    """Load persisted input history as plain lines (for the LineReader)."""
    lines = []
    try:
        if os.path.exists(HISTORY_PATH):
            with open(HISTORY_PATH, encoding="utf-8") as f:
                lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    except Exception:
        pass
    return lines


def _prompt(rt):
    return col(_tui.CUR_SKIN["accent"], "> ") if COLOR else "> "


def _slash_complete(text, state):
    if text.startswith("/"):
        opts = [c for c in _SLASH_COMMANDS if c.startswith(text.lower())]
        return opts[state] if state < len(opts) else None
    return None


def send_message(rt, text):
    """Render the 'you' bubble, run the agent, manage context + sessions.

    Returns the (possibly auto-renamed) session name.
    """
    history = rt.history
    session = rt.session
    cfg = active_cfg(rt)
    # auto-title a fresh placeholder session from the first user message
    if session.startswith("sess-"):
        new_name = unique_session_name(rt, text)
        if new_name != session:
            rename_session(rt, session, new_name)
            session = new_name
    print()
    print_user_turn(text)
    history.append({"role": "user", "content": text})
    trim_history(history)
    # pre-turn safety: only act if the window is nearly full (0.9) - the post-turn
    # check (0.75) is the normal compressor, so both rarely fire in one turn
    if cfg.get("auto_compress", True):
        compress_now(rt, history, threshold=0.9)
    try:
        res = run_agent_tui(rt, history)
    except KeyboardInterrupt:
        cancel_agent(rt)
        p_info("cancelled")
        if history:
            history.pop()  # drop the unanswered message
        save_session(rt, session, history)
        return session
    except Exception as e:
        p_err("agent error: %s" % e)
        if history:
            history.pop()
        save_session(rt, session, history)
        return session
    # drop the internal system prompt that run_agent_stream prepends
    history[:] = [m for m in res["history"] if m.get("role") != "system"]
    if res.get("cancelled"):
        p_info("(request stopped)")
        save_session(rt, session, history)
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
        save_session(rt, session, history)
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
        compressed = compress_now(rt, history)
    tokens, window = context_usage(rt, history)
    render_status_bar(rt, session, res.get("elapsed", 0.0), res.get("tools", 0), history)
    pct = tokens * 100 // window if window else 0
    if not compressed and window and pct >= 85:
        p_warn("context at %d%% of %s - /new starts a fresh session | /compress summarizes older messages"
               % (pct, _fmt_k(window)))
    save_session(rt, session, history)
    return session


def repl(rt):
    sync_tool_mode(rt)
    set_active_skin(rt)
    # resume the last active session (conversations persist across restarts)
    session = store_get(rt, ACTIVE_SESSION_KEY) or "default"
    history = load_session(rt, session)
    rt.history = history
    rt.session = session
    # last completed turn, for /redo (session-scoped so it can't leak across
    # a /session switch)
    rt.last_turn = {"session": None, "text": None, "pre": None}
    while True:
        try:
            prompt = _prompt(rt)
            global _TEE
            _TEE = StreamTee()
            _TEE.install()
            _reader = LineReader(_TEE, _history_file_lines(), prompt=prompt)
            _reader.on_scroll(lambda d: _handle_scroll(d, rt, _reader))
            line = _reader.read_line()
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
                cmd_config(rt)
            elif c == "/provider":
                cmd_provider(rt, rest)
            elif c == "/test":
                cmd_test(rt)
            elif c == "/tools":
                arg = rest.strip().lower()
                if arg in TOOL_MODES:
                    set_mode(rt, arg)
                    p_ok("tool mode: %s (%d tools advertised to the model)"
                         % (rt.tool_mode, len(visible(rt))))
                else:
                    cmd_tools(rt)
            elif c == "/trace":
                cmd_trace(rt, rest)
            elif c == "/models":
                cmd_models(rt)
            elif c == "/skin":
                cmd_skin(rt, rest)
            elif c == "/sessions":
                cmd_sessions(rt)
            elif c == "/session":
                arg, _, sub = rest.strip().partition(" ")
                arg = arg.strip().lower()
                sub = sub.strip()
                if not arg or arg in ("ls", "list", "show"):
                    cmd_sessions(rt)
                elif arg in ("rm", "remove", "del", "delete"):
                    target = find_session(rt, sub)
                    if not sub:
                        p_err("usage: /session rm <name>")
                    elif target is None:
                        p_err("no session named '%s'" % sub)
                    elif target.lower() == session.lower():
                        p_err("that's the active session - switch first (/session <name>)")
                    else:
                        delete_session(rt, target)
                        p_ok("deleted session '%s' [OK]" % target)
                elif arg in ("rename", "mv"):
                    old, _, new = sub.partition(" ")
                    old, new = old.strip(), new.strip()
                    target = find_session(rt, old)
                    if not old or not new:
                        p_err("usage: /session rename <old> <new>")
                    elif target is None:
                        p_err("no session named '%s'" % old)
                    elif find_session(rt, new):
                        p_err("a session named '%s' already exists" % new)
                    else:
                        rename_session(rt, target, new)
                        if session.lower() == target.lower():
                            session = new
                        p_ok("renamed '%s' -> '%s' [OK]" % (target, new))
                else:
                    save_session(rt, session, history)  # persist the outgoing session
                    target = find_session(rt, arg)
                    if target is None:
                        target = arg
                        p_info("(new session '%s')" % target)
                    history[:] = load_session(rt, target)
                    session = target
                    rt.session = session
                    save_session(rt, session, history)  # mark active + refresh timestamp
                    p_ok("switched to session '%s' | %d messages" % (session, len(history)))
            elif c == "/context":
                cmd_context(rt, rest, history)
            elif c == "/compress":
                cmd_compress(rt, history, session)
            elif c == "/new":
                save_session(rt, session, history)
                cmd_clear(rt, history)
                session = new_session_name()
                rt.session = session
                save_session(rt, session, history)
                p_ok("new session: " + session)
            elif c == "/clear":
                cmd_clear(rt, history)
            elif c == "/multi":
                text = cmd_multi()
                if text and text.strip():
                    session = send_message(rt, text.strip())
                    rt.session = session
            elif c == "/install_skill":
                cmd_install_skill(rt, rest)
            elif c == "/self-test":
                cmd_self_test(rt)
            elif c == "/improve":
                cmd_improve(rt, rest)
            elif c == "/skills":
                cmd_skills(rt, rest)
            elif c == "/skill":
                op, _, arg = rest.strip().partition(" ")
                op = op.strip().lower()
                if op in ("rm", "remove", "del", "delete"):
                    if not arg:
                        p_err("usage: /skill rm <name>")
                        continue
                    r = skill_remove(rt, arg)
                    if r.get("ok"):
                        p_ok("removed skill '%s' (category: %s) [OK]"
                             % (r.get("name", "?"), r.get("category") or "(flat)"))
                    else:
                        p_err("  " + r.get("error", "?"))
                elif op in ("cat", "category", "cats", "categories"):
                    cmd_skill_category(rt, arg)
                else:
                    p_err("usage: /skill rm <name>  |  /skill category [name]")
                continue
            elif c == "/memory":
                cmd_memory(rt)
            elif c == "/export":
                cmd_export(rt, history)
            elif c == "/scroll":
                cmd_scroll(rt, history)
            elif c == "/stop":
                cancel_agent(rt)
                p_info("stopping...")
            elif c == "/redo":
                if rt.last_turn.get("session") != session or rt.last_turn.get("text") is None:
                    p_err("nothing to redo - send a message first (in this session)")
                    continue
                history[:] = rt.last_turn["pre"]
                p_info("(re-running: %s)" % rt.last_turn["text"][:80])
                session = send_message(rt, rt.last_turn["text"])
                rt.session = session
            elif c in ("/exit", "/quit", "/q"):
                break
            else:
                p_err("unknown command: " + c + "   (/help for the list)")
            continue

        rt.last_turn["session"] = session
        rt.last_turn["text"] = line
        rt.last_turn["pre"] = list(history)
        session = send_message(rt, line)
        rt.session = session
        save_completion_history()  # persist input history after each turn
    save_session(rt, session, history)
    save_completion_history()  # flush readline history to disk on exit
    print(col(C.DIM, "bye"))


def _handle_scroll(direction, rt, reader):
    """Enter the scroll view; restore the live screen when it exits."""
    sv = ScrollView(list(rt.history))
    if not sv.total_lines():
        return
    reader.run_scroll_loop(sv, sv.page_count() - 1)
    if _TEE is not None:
        _TEE.restore()


def main():
    rt = build_runtime()
    rt.on_permission = lambda desc: ask_permission(rt, desc)  # interactive y/N for risky actions
    rt.on_tool = lambda t_id, n, a, r, s: on_tool(rt, t_id, n, a, r, s)  # live tool-progress blocks
    setup_completion()

    # _cleanup is idempotent and always runs (even on SIGTERM / OOM / crash) so
    # the shell prompt is left on a clean line; the guard event makes repeated
    # cleanup from signal + finally harmless.
    _restored = threading.Event()

    def _cleanup(signum=None, frame=None):
        if _restored.is_set():
            return
        _restored.set()
        try:
            sys.stdout.write(MOUSE_DISABLE)
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
        banner(rt)
        repl(rt)
    finally:
        _cleanup()
