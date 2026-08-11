#!/usr/bin/env python3
"""
test_tui.py — headless validation of alvaagent_tui.py (plain-Python TUI).

Exercises the ported harness against mock_llm_server.py (a fake
OpenAI-compatible API). Everything runs offline: web_fetch pulls the mock
server's own /mock-page.

Run:  python3 test_tui.py
"""
import json
import os
import select
import subprocess
import sys
import tempfile
import time

PORT = 8210
BASE = "http://127.0.0.1:%d" % PORT
MOCK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mock_llm_server.py")

# point the TUI at an isolated data dir BEFORE importing the module
DATA = tempfile.mkdtemp(prefix="alva_tui_test_")
os.environ["ALVA_DATA_DIR"] = DATA

import alvaagent_tui as pa  # noqa: E402

failures = 0


def assert_ok(cond, msg):
    global failures
    print(("  ok  - " if cond else "  FAIL - ") + msg)
    if not cond:
        failures += 1


# ---------- start the mock LLM server ----------
server = subprocess.Popen(
    [sys.executable, MOCK, str(PORT)],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

try:
    ready = False
    deadline = time.time() + 10
    while time.time() < deadline and server.poll() is None:
        rlist, _, _ = select.select([server.stdout], [], [], 0.2)
        if rlist and "READY" in server.stdout.readline():
            ready = True
            break
    if server.poll() is not None:
        print("FATAL: mock server exited early")
        sys.exit(1)
    if not ready:
        print("FATAL: mock server did not become ready")
        sys.exit(1)
    print("[mock server ready]")

    # ---------- tools registered ----------
    assert_ok(len(pa.TOOLS) == 17, "17 tools registered (9 base + shell/files/skills)")

    # ---------- calculator ----------
    assert_ok(pa.tool_calculator("6*7")["result"] == 42, "calculator: 6*7 = 42")
    assert_ok(pa.tool_calculator("sqrt(16) + 2**3")["result"] == 12, "calculator: sqrt(16) + 2**3 = 12")
    assert_ok(pa.tool_calculator("(2 + 3) * 4")["result"] == 20, "calculator: (2+3)*4 = 20")
    assert_ok(pa.tool_calculator("floor(pi * 3)")["result"] == 9, "calculator: floor(pi*3) = 9")
    assert_ok(pa.tool_calculator("10 % 3")["result"] == 1, "calculator: 10 % 3 = 1")

    def calc_raises(expr, frag=None):
        try:
            pa.tool_calculator(expr)
            return False
        except Exception as e:
            return frag is None or frag in str(e)

    assert_ok(calc_raises("__import__('os').listdir('.')"), "calculator sandbox: rejects __import__")
    assert_ok(calc_raises("1/0", "division by zero"), "calculator: 1/0 raises division-by-zero")
    assert_ok(calc_raises("'a'.upper()"), "calculator sandbox: rejects attribute access")
    assert_ok(calc_raises("1e400", "infinite"), "calculator: 1e400 (infinity) is rejected cleanly")
    assert_ok(calc_raises("2**1000000", "exponent too large"), "calculator: huge exponents are rejected")
    assert_ok(calc_raises("9**9**9", "exponent too large"), "calculator: 9**9**9 is rejected without hanging")
    assert_ok(calc_raises("factorial(20000)"), "calculator: factorial of huge values is rejected")
    assert_ok(pa.tool_calculator("factorial(5)")["result"] == 120, "calculator: factorial(5) = 120")
    assert_ok(len(str(pa.tool_calculator("2**500")["result"])) > 100, "calculator: 2**500 works (big but sane result)")

    # ---------- todo tools ----------
    add1 = pa.tool_todo_add("unit test task")
    assert_ok(add1["ok"] is True, "todo_add returns ok")
    add2 = pa.tool_todo_add("second task")
    lst = pa.tool_todo_list()
    assert_ok(lst["count"] == 2 and lst["todos"][1]["text"] == "second task", "todo_list shows both tasks")
    tog = pa.tool_todo_toggle(add1["index"])
    assert_ok(tog["done"] is True, "todo_toggle marks done")
    rem = pa.tool_todo_remove(add2["index"])
    assert_ok(rem["ok"] is True and pa.tool_todo_list()["count"] == 1, "todo_remove deletes the task")
    assert_ok(pa.tool_todo_toggle(99)["ok"] is False, "todo_toggle rejects out-of-range index")

    # ---------- memory tools ----------
    assert_ok(pa.tool_memory_save("testkey", "hello world")["ok"] is True, "memory_save returns ok")
    rec = pa.tool_memory_recall("testkey")
    assert_ok(rec["found"] is True and rec["value"] == "hello world", "memory_recall round-trips value")
    assert_ok(pa.tool_memory_recall("missing_key")["found"] is False, "memory_recall reports missing keys")

    # ---------- clock ----------
    t = pa.tool_get_time()
    assert_ok(isinstance(t.get("iso"), str) and len(t["iso"]) > 10, "get_time returns ISO timestamp")

    # ---------- autonomy: command classification + permission gate ----------
    assert_ok(pa.classify_command("ls -la") == "allow", "classify: ls is read-only -> allow")
    assert_ok(pa.classify_command("python3 -m py_compile x.py") == "allow", "classify: py_compile is read-only -> allow")
    assert_ok(pa.classify_command("echo hi") == "allow", "classify: echo is read-only -> allow")
    assert_ok(pa.classify_command("git status") == "allow", "classify: git status -> allow")
    assert_ok(pa.classify_command("rm -rf /") == "ask", "classify: rm is risky -> ask")
    assert_ok(pa.classify_command("sudo apt update") == "ask", "classify: sudo/apt -> ask")
    assert_ok(pa.classify_command("pkg install python") == "ask", "classify: pkg install -> ask")
    assert_ok(pa.classify_command("weird-thing") == "ask", "classify: unknown -> ask (safe default)")
    assert_ok(pa.classify_command("") == "deny", "classify: empty -> deny")
    # regression: shell substitution / metachar bypasses must NOT classify as allow
    assert_ok(pa.classify_command("echo $(touch /tmp/x)") == "ask",
              "classify: command substitution $() is rejected")
    assert_ok(pa.classify_command("echo `touch /tmp/x`") == "ask",
              "classify: backtick substitution is rejected")
    assert_ok(pa.classify_command("cat /etc/passwd $(whoami)") == "ask",
              "classify: substitution anywhere in the command is rejected")
    assert_ok(pa.classify_command("ls -la; rm -rf /") == "ask",
              "classify: semicolon chaining is rejected")
    assert_ok(pa.classify_command("env X=1 rm -rf /") == "ask",
              "classify: risky token in a later position is rejected")
    assert_ok(pa.classify_command("git push") == "ask",
              "classify: risky multi-word command (git push) is rejected")
    assert_ok(pa.classify_command("catastrophe --version") == "ask",
              "classify: prefix collision (cat vs catastrophe) is rejected")
    assert_ok(pa.classify_command("python3 -m py_compile x.py") == "allow",
              "classify: allowlisted multi-word command still allowed")
    assert_ok(pa.classify_command("git status --short") == "allow",
              "classify: allowlisted git status with args still allowed")
    assert_ok(pa.classify_command("find / -name x") == "allow",
              "classify: plain find search stays allowed")
    assert_ok(pa.classify_command("find / -delete") == "ask",
              "classify: find -delete is rejected")
    assert_ok(pa.classify_command("find / -exec rm {} \;") == "ask",
              "classify: find -exec is rejected")

    pa.ON_PERMISSION = lambda d: False
    denied = pa.tool_run_command("touch /tmp/should-not-exist-alva")
    assert_ok(denied.get("ok") is False and "permission" in str(denied.get("error", "")),
              "run_command: risky command denied when user says no")
    allowed = pa.tool_run_command("echo hello-from-alva")
    assert_ok(allowed.get("ok") is True and "hello-from-alva" in allowed.get("stdout", ""),
              "run_command: read-only echo runs without prompting")
    pa.ON_PERMISSION = lambda d: True
    ok2 = pa.tool_run_command("echo approved-run")
    assert_ok(ok2.get("ok") is True and "approved-run" in ok2.get("stdout", ""),
              "run_command: risky command runs when user approves")
    pa.ON_PERMISSION = None

    # ---------- autonomy: files ----------
    proj_test = os.path.join(DATA, "proj-demo.txt")
    w = pa.tool_file_write(proj_test, "first line\nsecond")
    assert_ok(w.get("ok") is True and w.get("chars") == 17, "file_write creates a file")
    r = pa.tool_file_read(proj_test)
    assert_ok(r.get("ok") is True and "first line" in r.get("content", ""), "file_read round-trips content")
    e = pa.tool_file_edit(proj_test, "first", "FIRST")
    assert_ok(e.get("ok") is True and e.get("replaced") >= 1, "file_edit replaces text")
    assert_ok("FIRST line" in pa.tool_file_read(proj_test)["content"], "file_edit change persisted")
    lst = pa.tool_file_list(DATA)
    assert_ok(lst.get("ok") is True and any(x["name"] == "proj-demo.txt" for x in lst.get("entries", [])),
              "file_list shows the created file")

    pa.ON_PERMISSION = lambda d: False
    denied_w = pa.tool_file_write("/tmp/alva-outside-write.txt", "nope")
    assert_ok(denied_w.get("ok") is False and "permission" in str(denied_w.get("error", "")),
              "file_write outside project asks permission")
    denied_r = pa.tool_file_read("/etc/hostname")
    assert_ok(denied_r.get("ok") is False and "permission" in str(denied_r.get("error", "")),
              "file_read outside project asks permission (exfiltration guard)")
    pa.ON_PERMISSION = None

    # ---------- autonomy: skills ----------
    sk = pa.tool_skill_save("test-skill", "Always check the time before planning.")
    assert_ok(sk.get("ok") is True, "skill_save writes a skill")
    assert_ok("test-skill" in (pa.tool_skill_list().get("skills") or []), "skill_list shows saved skill")
    sr = pa.tool_skill_read("test-skill")
    assert_ok(sr.get("ok") is True and "check the time" in sr.get("content", ""), "skill_read returns skill body")
    assert_ok(pa.tool_skill_read("missing-skill").get("ok") is False, "skill_read reports missing skills")

    # ---------- web_fetch (offline: the mock's own /mock-page) ----------
    wf = pa.tool_web_fetch(BASE + "/mock-page")
    assert_ok(wf.get("ok") is True and wf.get("status") == 200, "web_fetch returns ok for mock page")
    assert_ok("Mock Page" in wf.get("snippet", ""), "web_fetch strips HTML to text")

    # ---------- model listing (powers the setup autofill) ----------
    models = pa.fetch_models(BASE + "/v1", "test-key")
    assert_ok(models == ["mock-model", "another-mock"], "fetch_models lists the endpoint's models")

    # ---------- provider profiles (add / switch / remove) ----------
    st = pa._normalize_state({"provider": "groq", "base_url": "http://x/v1", "api_key": "k",
                              "model": "m", "temperature": 0.3})
    assert_ok(st["active"] == "groq" and "groq" in st["profiles"], "legacy flat config migrates to profiles")
    assert_ok(pa.active_cfg(st)["api_key"] == "k", "active_cfg returns the active profile")
    st2 = {"active": "a", "profiles": {"a": dict(pa.DEFAULT_CFG), "b": dict(pa.DEFAULT_CFG)}}
    pa.cmd_provider(st2, "rm a")
    assert_ok("a" not in st2["profiles"] and st2["active"] in st2["profiles"], "provider rm removes and fixes active")
    pa.cmd_provider(st2, "b")
    assert_ok(st2["active"] == "b", "provider switches to an existing profile")

    # ---------- context tracking (meter + window detection) ----------
    ctx_cfg = {"base_url": BASE + "/v1", "api_key": "test-key",
                "model": "mock-model", "temperature": 0.5}
    est = pa.estimate_tokens("hello world")
    assert_ok(isinstance(est, int) and est >= 1, "estimate_tokens returns a positive int")
    tok, win = pa.context_usage([{"role": "user", "content": "hi"}], ctx_cfg)
    assert_ok(win == pa.DEFAULT_CONTEXT_WINDOW, "unknown model falls back to the default window")
    assert_ok(isinstance(tok, int) and tok > 0, "context_usage returns positive tokens")
    cfg2 = dict(ctx_cfg)
    cfg2["model"] = "gpt-4o"
    assert_ok(pa.context_window_for(cfg2) == 128000, "known model maps to its window")
    cfg3 = dict(ctx_cfg)
    cfg3["context_window"] = 4000
    assert_ok(pa.context_window_for(cfg3) == 4000, "explicit context_window wins")
    assert_ok(pa._fmt_k(12345) == "12.3k", "fmt_k formats thousands")

    # ---------- sessions (save / load / list / delete) ----------
    pa.save_session("test-sess", [{"role": "user", "content": "a"}])
    assert_ok("test-sess" in pa.sessions_map(), "save_session persists a session")
    assert_ok(len(pa.load_session("test-sess")) == 1, "load_session restores messages")
    assert_ok(pa._find_session("TEST-SESS") == "test-sess", "session lookup is case-insensitive")
    assert_ok(pa._store_get(pa.ACTIVE_SESSION_KEY) == "test-sess", "saving marks the session active")
    pa.delete_session("test-sess")
    assert_ok("test-sess" not in pa.sessions_map(), "delete_session removes a session")
    assert_ok(pa.auto_title("   hello   world  ") == "hello world", "auto_title normalizes text")
    assert_ok(pa._unique_session_name("x") == "x", "unique name passes through when free")

    # ---------- auto-compression (injected summarizer, no network) ----------
    big = [{"role": "user" if i % 2 == 0 else "assistant", "content": "m" * 4000}
           for i in range(12)]
    tiny = dict(ctx_cfg)
    tiny["context_window"] = 4000
    new, stats = pa.compress_history(big, tiny, summarizer=lambda msgs, c: "SUMMARY")
    assert_ok(stats is not None and stats["dropped"] == 4 and stats["kept"] == 8,
              "compress_history keeps a recent tail and summarizes the head")
    assert_ok(new[0]["content"].startswith("[summary of earlier conversation]"),
              "compressed history starts with the summary marker")
    assert_ok(new[0]["role"] == "user" and len(new) == 9, "summary message + 8-message tail")
    small = [{"role": "user", "content": "hi"}]
    _, stats2 = pa.compress_history(small, tiny, summarizer=lambda msgs, c: "S")
    assert_ok(stats2 is None, "short conversations are never compressed")
    _, stats3 = pa.compress_history(big, ctx_cfg, summarizer=lambda msgs, c: "S")
    assert_ok(stats3 is None, "conversations inside the default window are not compressed")
    # smarter summarizer: structured sections are preserved verbatim
    def structured_summarizer(msgs, c):
        return "- GOALS: build a thing\n- DECISIONS: used sqlite\n- FACTS: user is Alex\n- ACTIONS: wrote code\n- OPEN: deploy it"
    new_struct, stats_struct = pa.compress_history(big, tiny, summarizer=structured_summarizer)
    assert_ok(stats_struct is not None and "GOALS" in new_struct[0]["content"],
              "structured summary sections are preserved in the compression marker")
    assert_ok(all(s in new_struct[0]["content"] for s in ("DECISIONS", "FACTS", "ACTIONS", "OPEN")),
              "all five summary sections survive compression")
    # summarizer preamble guard: a leading 'Here is...' is stripped
    def chatty_summarizer(msgs, c):
        return "Here is your summary:\n- GOALS: x"
    new_chatty, _ = pa.compress_history(big, tiny, summarizer=chatty_summarizer)
    assert_ok(not new_chatty[0]["content"].lower().startswith("here"),
              "summarizer strips chatty preamble before persisting")
    newf, statsf = pa.compress_history(big, tiny, summarizer=lambda msgs, c: "")
    assert_ok(statsf is not None and statsf["mode"] == "fallback" and len(newf) == 9,
              "fallback summary used when the LLM returns nothing")
    # tool-heavy history: the compressed tail must never start with a tool message
    seq = [{"role": "user", "content": "u" * 4000}]
    for k in range(6):
        seq.append({"role": "assistant", "content": "a" * 2000, "tool_calls": [{"id": "c%d" % k}]})
        seq.append({"role": "tool", "tool_call_id": "c%d" % k, "content": "r" * 2000})
    newb, statsb = pa.compress_history(seq, tiny, summarizer=lambda msgs, c: "S")
    assert_ok(statsb is not None and newb[1]["role"] != "tool",
              "compression never leaves the tail starting mid-tool-sequence")
    # the newest user message must always survive compression
    big2 = big + [{"role": "user", "content": "FRESH QUESTION"}]
    new3, _ = pa.compress_history(big2, tiny, summarizer=lambda msgs, c: "S")
    assert_ok(new3[-1]["content"] == "FRESH QUESTION",
              "the newest user message is never summarized away")
    # trim_history keeps a leading compression summary instead of dropping it
    trimmed = [{"role": "user", "content": "[summary of earlier conversation]\nold stuff"}]
    for k in range(130):
        trimmed.append({"role": "user", "content": "msg %d" % k})
    pa.trim_history(trimmed)
    assert_ok(trimmed[0]["content"].startswith("[summary") and len(trimmed) == 121,
              "trim_history protects the leading compression summary")

    # ---------- key input parsing + first-run defaults (the user-reported issues) ----------
    assert_ok(pa.parse_key("", "old") == "old", "empty key input keeps the current key")
    assert_ok(pa.parse_key("none", "old") == "", "typing 'none' clears the key")
    assert_ok(pa.parse_key("clear", "old") == "", "typing 'clear' clears the key")
    assert_ok(pa.parse_key("sk-123", "old") == "sk-123", "typing a key replaces it")
    assert_ok(pa.FIRST_RUN_CFG["api_key"] == "", "fresh profiles start with no api key")
    first = pa._normalize_state({})
    assert_ok(first["active"] == "default" and first["profiles"]["default"]["api_key"] == "",
              "first run creates a neutral keyless profile")

    # ---------- full agent loop against the mock server ----------
    cfg = {"base_url": BASE + "/v1", "api_key": "test-key", "model": "mock-model", "temperature": 0.5}
    history = [{"role": "user", "content": "use all your tools"}]
    res1 = json.loads(pa.run_agent(json.dumps(history), json.dumps(cfg)))
    print("  [final answer] " + str(res1.get("content", ""))[:220])
    assert_ok("AGENT_LOOP_OK" in str(res1.get("content", "")), "agent loop completed with ALL tool results verified")
    assert_ok(isinstance(res1.get("history"), list) and len(res1["history"]) >= 10, "history grew with assistant + tool messages")

    # persisted todos / memory from the loop
    store = json.load(open(os.path.join(DATA, "store.json")))
    assert_ok(any(t.get("text") == "buy milk" for t in store.get("alvaagent.todos", [])),
              "todo from agent loop persisted to store.json")
    assert_ok(store.get("alvaagent.mem.name") == "Alex", "memory fact from agent loop persisted to store.json")

    # ---------- plain-text path (no tools) ----------
    history2 = res1["history"] + [{"role": "user", "content": "[plain] say hi"}]
    res2 = json.loads(pa.run_agent(json.dumps(history2), json.dumps(cfg)))
    assert_ok("PLAIN_OK" in str(res2.get("content", "")), "plain request answered directly without tools")

    # ---------- harness self-test ----------
    results = json.loads(pa.self_test())
    assert_ok(all(v is True for v in results.values()),
              "harness self_test passes all checks: " + json.dumps(results))

    # ---------- reliability: atomic store writes ----------
    # A store save must leave VALID json even under a racing second write.
    pa._store["alvaagent.todos"] = [{"text": "atomic test", "done": False}]
    pa._save_store()
    sp = os.path.join(DATA, "store.json")
    assert_ok(os.path.exists(sp), "store.json exists after save")
    try:
        _reloaded = json.load(open(sp))
        assert_ok(isinstance(_reloaded, dict), "store.json is valid JSON after save")
    except Exception as e:
        assert_ok(False, "store.json corrupted: %s" % e)
    # back-to-back saves must not corrupt (temp+rename is atomic on POSIX)
    pa._store["alvaagent.mem.x"] = "v1"
    pa._save_store()
    pa._store["alvaagent.mem.x"] = "v2"
    pa._save_store()
    _reloaded2 = json.load(open(sp))
    assert_ok(_reloaded2.get("alvaagent.mem.x") == "v2", "second store save wins (no corruption)")
    # no leftover temp files
    _leftover = [f for f in os.listdir(DATA) if f.startswith(".store.") or f.startswith(".tmp.") or f.endswith(".tmp")]
    assert_ok(not _leftover, "no leftover temp files after atomic writes (%s)" % _leftover)
    # _atomic_write helper works
    _ap = os.path.join(DATA, "_atomic_probe.txt")
    pa._atomic_write(_ap, "hello")
    assert_ok(open(_ap).read() == "hello", "_atomic_write writes content")
    os.remove(_ap)

    print("\nALL TESTS PASSED ✓" if failures == 0 else "\n%d TEST(S) FAILED ✗" % failures)
    sys.exit(0 if failures == 0 else 1)
finally:
    server.kill()
