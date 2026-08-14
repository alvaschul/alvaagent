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
    get as store_get, set as store_set, TODO_KEY, MEM_PREFIX, FEEDBACK_KEY,
    IMPROVEMENT_KEY, HISTORY_KEY, ACTIVE_SESSION_KEY,
)
from alvaagent.permissions import classify_command, PROJECT_DIR
from alvaagent.skills import (
    tool_skill_list, tool_skill_read, tool_skill_install, tool_skill_sync_repo,
)
from alvaagent.tools import (
    _ADVANCED_TOOL_NAMES, visible, TOOLS,
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


def ask_permission(rt, desc):
    """Interactive y/N/a prompt used as rt.on_permission in the REPL.

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
        rt.approved.add(desc)
        return True
    return False


# ---------------- slash commands ----------------
def pick_model(rt, base_url, api_key, current, fetch=True):
    """Fetch the models for an endpoint+key and let the user pick one.

    With fetch=False (endpoint unchanged), just ask for the model id directly.
    """
    if fetch:
        try:
            models = fetch_models(rt, base_url, api_key)
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


def cmd_models(rt):
    cfg = active_cfg(rt)
    if not (cfg.get("base_url") or "").rstrip("/"):
        p_err("no base url configured for '%s' - run /provider %s" % (rt.cfg["active"], rt.cfg["active"]))
        return
    cfg["model"] = pick_model(rt, cfg.get("base_url", ""), cfg.get("api_key", ""), cfg.get("model", ""))
    save_state(rt)
    p_ok("saved [OK]")


_SLASH_COMMANDS = [
    "/help", "/config", "/provider", "/models", "/test", "/skin",
    "/sessions", "/session", "/new", "/clear", "/context", "/compress",
    "/tools", "/trace", "/todos", "/todo", "/memory", "/skills", "/skill",
    "/install_skill", "/feedback", "/reflect", "/self-test", "/improve",
    "/multi", "/export", "/redo", "/stop", "/exit", "/quit",
]


def cmd_skin(rt, rest):
    """List or switch the UI skin (persisted in config.json)."""
    arg = rest.strip().lower()
    if not arg or arg in ("ls", "list"):
        print("  skins:")
        for name, sk in SKINS.items():
            mark = "   <- active" if (rt.cfg.get("skin") or DEFAULT_SKIN) == name else ""
            print("    %-10s %s%s" % (name, sk["desc"], mark))
        print("  usage: /skin <name>")
        return
    if arg not in SKINS:
        p_err("unknown skin '%s' - see /skin" % arg)
        return
    rt.cfg["skin"] = arg
    rt.skin = arg
    save_state(rt)
    set_active_skin(rt)
    p_ok("skin set to '%s' [OK]" % arg)


def cmd_sessions(rt):
    """List saved sessions (name, message count, last updated)."""
    sess = sessions_map()
    if not sess:
        print("  (no sessions yet - /session <name> starts one)")
        return
    active = store_get(rt, ACTIVE_SESSION_KEY, "default")
    print("  sessions (%d):" % len(sess))
    for name in sorted(sess, key=lambda n: (sess[n].get("updated") or ""), reverse=True):
        rec = sess[name]
        n = len(rec.get("messages") or [])
        upd = (rec.get("updated") or "")[11:16] or "?"
        mark = ">" if name == active else " "
        print("   %s %-22s %3d msgs | %s" % (mark, name[:22], n, upd))
    print("  usage: /session <name> | /session rm <name> | /session rename <old> <new>")


def cmd_context(rt, rest, history):
    """Context usage for the active provider + its settings."""
    cfg = active_cfg(rt)
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
        save_state(rt)
        p_ok("context window set to %s [OK]" % (_fmt_k(w) if w else "auto"))
        return
    if sub in ("auto", "autocompress", "auto-compress"):
        cfg["auto_compress"] = not cfg.get("auto_compress", True)
        save_state(rt)
        p_ok("auto-compress %s [OK]" % ("on" if cfg["auto_compress"] else "off"))
        return
    tokens, window = context_usage(history, cfg)
    pct = tokens * 100 // window if window else 0
    print("  context usage:")
    print("    system prompt : ~%s tokens" % _fmt_k(estimate_tokens(SYSTEM_PROMPT)))
    print("    conversation  : %s tokens (%d messages)"
          % (_fmt_k(tokens - estimate_tokens(SYSTEM_PROMPT)), len(history)))
    print("    total         : %s / %s  (%d%%)" % (_fmt_k(tokens), _fmt_k(window), pct))
    print("  settings ('%s'):" % rt.cfg["active"])
    print("    context window: %s   (/context window <n> to override)" % _fmt_k(window))
    print("    auto-compress : %s  at 75%% of the window   (/context autocompress toggles)"
          % ("on" if cfg.get("auto_compress", True) else "off"))
    if pct >= 85:
        p_warn("context is %d%% full - /new starts a fresh session | /compress summarizes now" % pct)


def cmd_compress(rt, history, session):
    """Manually summarize older messages to free context (persists immediately)."""
    if len(history) < 8:
        p_info("(conversation is short - nothing to compress)")
        return
    if compress_now(rt, history, force=True):
        save_session(session, history)


def cmd_self_test(rt):
    """Run the harness self-test suite and show results.

    Tests calculator, sandbox, todo, memory, skills, command classification,
    file tools, and the feedback/improvement/reflect tools.
    """
    tests = [
        ("calculator basic", lambda: _check(tool_calculator("2+2")["ok"])),
        ("calculator sqrt", lambda: _check(tool_calculator("sqrt(144)")["ok"])),
        ("sandbox rejects div0", lambda: _check(_raises(lambda: tool_calculator("1/0")))),
        ("sandbox rejects complex", lambda: _check(_raises(lambda: tool_calculator("(-8)**0.5")))),
        ("todo add+list+remove", lambda: _check(_todo_check(rt))),
        ("memory save+recall", lambda: _check(_mem_check(rt))),
        ("skills list+read", lambda: _check(_skill_check(rt))),
        ("classify allow: ls", lambda: _check(classify_command("ls -la") == "allow")),
        ("classify ask: rm -rf", lambda: _check(classify_command("rm -rf /") == "ask")),
        ("classify ask: subshell", lambda: _check(classify_command("echo $(whoami)") == "ask")),
        ("file_read in project", lambda: _check(tool_file_read(rt, __file__)["ok"])),
        ("file_write temp", lambda: _check(_file_write_check(rt))),
        ("file_edit temp", lambda: _check(_file_edit_check(rt))),
        ("feedback+improvement+reflect", lambda: _check(_feedback_check(rt))),
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


def _todo_check(rt):
    r = tool_todo_add(rt, "self-test-todo")
    if not r.get("ok"):
        return False
    items = tool_todo_list(rt).get("todos", [])
    found = any(t.get("text") == "self-test-todo" and not t.get("done") for t in items)
    tool_todo_remove(rt, len(items) - 1)
    return found


def _mem_check(rt):
    r = tool_memory_save(rt, "self-test-mem", "hello")
    if not r.get("ok"):
        return False
    v = tool_memory_recall(rt, "self-test-mem").get("value")
    tool_memory_save(rt, "self-test-mem", "")
    return v == "hello"


def _skill_check(rt):
    skills = tool_skill_list(rt).get("skills", [])
    if not skills:
        return True
    name = skills[0]["name"]
    r = tool_skill_read(rt, name)
    return r.get("ok") and r.get("content")


def _file_write_check(rt):
    # stay inside PROJECT_DIR: /tmp is out-of-project and would trigger a
    # permission prompt (or a headless deny), which a self-test shouldn't do
    tmp = os.path.join(PROJECT_DIR, ".alva_sst_write.txt")
    r = tool_file_write(rt, tmp, "test")
    if not r.get("ok"):
        return False
    content = tool_file_read(rt, tmp).get("content", "")
    try:
        os.remove(tmp)
    except OSError:
        pass
    return content == "test"


def _file_edit_check(rt):
    tmp = os.path.join(PROJECT_DIR, ".alva_sst_edit.txt")
    tool_file_write(rt, tmp, "hello world")
    r = tool_file_edit(rt, tmp, "hello", "goodbye")
    if not r.get("ok"):
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    content = tool_file_read(rt, tmp).get("content", "")
    try:
        os.remove(tmp)
    except OSError:
        pass
    return content == "goodbye world"


def _feedback_check(rt):
    r1 = tool_feedback(rt, "good", "self-test check")
    if not r1.get("ok"):
        return False
    fb = store_get(rt, FEEDBACK_KEY, [])
    if not fb or fb[-1].get("rating") != "good":
        return False
    r2 = tool_improvement_set(rt, "self-test-area", "fix something")
    if not r2.get("ok"):
        return False
    imps = store_get(rt, IMPROVEMENT_KEY, [])
    if not imps or imps[-1].get("area") != "self-test-area":
        return False
    r3 = tool_reflect(rt)
    if not r3.get("ok"):
        return False
    tool_improvement_done(rt, "self-test-area")
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


def cmd_config(rt):
    cfg = active_cfg(rt)
    print("  provider '%s' settings:" % rt.cfg["active"])
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
    cfg["model"] = pick_model(rt, base, key, cfg.get("model", ""), fetch=not unchanged)
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
    save_state(rt)
    p_ok("saved [OK]")


def _list_providers(rt):
    profiles = rt.cfg["profiles"]
    if not profiles:
        print("  (no providers configured)")
        return
    print("  providers:")
    for name, p in profiles.items():
        mark = "   <- active" if name == rt.cfg["active"] else ""
        print("    %-12s %s | model %s | key %s%s"
              % (name, p.get("base_url") or "(no base)",
                 p.get("model") or "-", mask_key(p.get("api_key", "")), mark))
    print("  usage: /provider <name> (add or switch) | /provider rm <name>")
    print("  presets: openai | groq | openrouter | gemini | custom   (any other name = custom endpoint)")


def cmd_provider(rt, rest):
    profiles = rt.cfg["profiles"]
    arg, _, sub = rest.strip().partition(" ")
    arg = arg.strip().lower()
    sub = sub.strip()

    if arg in ("ls", "list"):
        _list_providers(rt)
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
        if rt.cfg["active"] == sub:
            rt.cfg["active"] = next(iter(profiles)) if profiles else "openai"
            if rt.cfg["active"] not in profiles:
                profiles[rt.cfg["active"]] = dict(DEFAULT_CFG)
        save_state(rt)
        p_ok("removed '%s' [OK]" % sub)
        return
    if not arg:
        _list_providers(rt)
        return

    # switch to an existing provider
    if arg in profiles:
        rt.cfg["active"] = arg
        save_state(rt)
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
    prof["model"] = pick_model(rt, prof.get("base_url", ""), prof.get("api_key", ""), prof.get("model", ""))
    profiles[arg] = prof
    rt.cfg["active"] = arg
    save_state(rt)
    p_ok("added '%s' [OK]" % arg)


def cmd_test(rt):
    cfg = active_cfg(rt)
    base = (cfg.get("base_url") or "").rstrip("/")
    if not base:
        p_err("no base url configured for '%s' - run /provider %s" % (rt.cfg["active"], rt.cfg["active"]))
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


def cmd_tools(rt):
    """List the active tool set (only what the model can currently see)."""
    active = visible(rt)
    print("  tool mode: %s - %d/%d tools advertised to the model"
          % (rt.tool_mode, len(active), len(TOOLS)))
    for t in active:
        fn = t["function"]
        print("  %-14s %s" % (fn["name"], fn.get("description", "")))
    hidden = len(TOOLS) - len(active)
    if hidden:
        print("  hidden (%d advanced; /tools full to advertise, /tools core to revert): %s"
              % (hidden, ", ".join(sorted(_ADVANCED_TOOL_NAMES))))


def cmd_trace(rt, rest):
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


def cmd_todos(rt):
    lst = tool_todo_list(rt).get("todos", [])
    if not lst:
        print("  (empty)")
        return
    for i, t in enumerate(lst):
        mark = "[x]" if t.get("done") else "[ ]"
        print("  %d %s %s" % (i, mark, t.get("text", "")))


def cmd_todo(rt, rest):
    parts = rest.split(None, 1)
    op = parts[0].lower() if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""
    if not op or op in ("list", "ls", "show"):
        cmd_todos(rt)
        return
    if op in ("add", "a"):
        r = tool_todo_add(rt, arg)
        if r.get("ok"):
            p_ok("  added #%d: %s" % (r["index"], r["text"]))
        else:
            p_err("  " + r.get("error", "?"))
        return
    if op in ("done", "toggle", "t", "d"):
        try:
            r = tool_todo_toggle(rt, int(arg))
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
            r = tool_todo_remove(rt, int(arg))
        except ValueError:
            p_err("  need an index, e.g. /todo rm 0")
            return
        if r.get("ok"):
            p_ok("  removed #%d: %s" % (r.get("index", "?"), r.get("removed", {}).get("text", "")))
        else:
            p_err("  " + r.get("error", "?"))
        return
    if op == "clear":
        store_set(rt, TODO_KEY, [])
        p_ok("  list cleared")
        return
    p_err("  usage: /todo <text> | /todo done <i> | /todo rm <i> | /todo clear")


def cmd_memory(rt):
    facts = [(k[len(MEM_PREFIX):], v) for k, v in rt.store.items() if k.startswith(MEM_PREFIX)]
    if not facts:
        print("  (no saved facts)")
        return
    print("  %-16s %-20s %s" % ("Key", "Tags", "Value"))
    print("  " + "-"*50)
    for k, v in facts:
        val = v.get("value", v) if isinstance(v, dict) else v
        tags = ", ".join(v.get("tags", [])) if isinstance(v, dict) else ""
        print("  %-16s %-20s %s" % (k, tags, val))


def cmd_feedback(rt, rest):
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
    r = tool_feedback(rt, rating, notes or None)
    if r.get("ok"):
        p_ok("feedback recorded: %s%s" % (rating, " - " + notes if notes else ""))
    else:
        p_err("  " + r.get("error", "?"))


def cmd_skills(rt, rest=""):
    arg = (rest or "").strip()
    if arg.startswith("install "):
        target = arg[len("install "):].strip()
        if not target:
            p_err("usage: /skills install <url|path> [category]")
            return
        parts = target.split(None, 1)
        r = tool_skill_install(rt, parts[0], parts[1].strip() if len(parts) > 1 else None)
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
        r = tool_skill_sync_repo(rt, parts[0], parts[1].strip() if len(parts) > 1 else None)
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
        cmd_skill_category(rt, arg)
        return
    skills = tool_skill_list(rt).get("skills") or []
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


def cmd_skill_category(rt, rest):
    """List skills in a category, or list all categories."""
    arg = (rest or "").strip().lower()
    skills = tool_skill_list(rt).get("skills") or []
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
        cmd_skill_category(rt, "ls")
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


def cmd_reflect(rt):
    """Review all feedback + improvement areas and propose actions."""
    fb = store_get(rt, FEEDBACK_KEY, [])
    imps = store_get(rt, IMPROVEMENT_KEY, [])
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
def cmd_improve(rt, rest):
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
        items = store_get(rt, IMPROVEMENT_KEY, [])
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
        r = tool_improvement_set(rt, area, action)
        if r.get("ok"):
            p_ok("improvement recorded: %s" % area)
        else:
            p_err("  " + r.get("error", "?"))
    elif sub in ("done", "mark", "resolve"):
        if len(parts) < 2:
            p_err("usage: /improve done <area>")
            return
        r = tool_improvement_done(rt, parts[1])
        if r.get("ok"):
            p_ok("marked done: %s" % r.get("area", parts[1]))
        else:
            p_err("  " + r.get("error", "?"))
    else:
        p_err("unknown subcommand: %s (list/add/done)" % sub)


def cmd_install_skill(rt, rest):
    # Install a skill from a local .md file or a URL (delegates to
    # tool_skill_install so GitHub/blob URLs are auto-rewritten to raw).
    r = tool_skill_install(rt, rest.strip())
    if r.get("ok"):
        p_ok("installed skill '%s' [OK]" % r.get("name"))
        if r.get("category"):
            print("    category: %s" % r["category"])
    else:
        p_err("failed to install skill: %s" % r.get("error", "unknown error"))
def cmd_clear(rt, history):
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
    store_set(rt, HISTORY_KEY, [])
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
