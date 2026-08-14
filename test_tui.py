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
import urllib.error

PORT = 8210
BASE = "http://127.0.0.1:%d" % PORT
MOCK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mock_llm_server.py")

# point the TUI at an isolated data dir BEFORE importing the module
DATA = tempfile.mkdtemp(prefix="alva_tui_test_")
os.environ["ALVA_DATA_DIR"] = DATA

import alvaagent as pa  # noqa: E402

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
    assert_ok(len(pa.TOOLS) == 30, "30 tools registered")

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
    ml = pa.tool_memory_list()
    assert_ok(ml.get("ok") is True and any(f["key"] == "testkey" for f in ml.get("facts", [])),
              "memory_list returns all saved facts")
    ms = pa.tool_memory_search("hello")
    assert_ok(ms.get("ok") is True and ms.get("count", 0) >= 1, "memory_search finds facts by value")
    ms2 = pa.tool_memory_search("testkey")
    assert_ok(ms2.get("ok") is True and any(f["key"] == "testkey" for f in ms2.get("facts", [])),
              "memory_search finds facts by key")
    assert_ok(pa.tool_memory_search("no-such-fact-xyz").get("count") == 0,
              "memory_search misses unmatched queries")

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
    assert_ok(pa.classify_command(r"find / -exec rm {} \;") == "ask",
              "classify: find -exec is rejected")
    # regression: quoted commands executed through `env` / shell interpreters
    # must NOT classify as read-only (quoted content hides the risky token)
    assert_ok(pa.classify_command("env sh -c 'rm -rf /'") == "ask",
              "classify: env executing a quoted shell command is rejected")
    assert_ok(pa.classify_command("env -S 'echo hi'") == "ask",
              "classify: env -S string-execution is rejected")
    assert_ok(pa.classify_command("sh -c 'rm -rf /'") == "ask",
              "classify: bare shell interpreter is rejected")
    assert_ok(pa.classify_command("bash -c 'rm -rf /'") == "ask",
              "classify: bash -c is rejected")
    assert_ok(pa.classify_command("env") == "ask",
              "classify: env alone is no longer treated as read-only")
    assert_ok(pa.classify_command("env X=1 date") == "ask",
              "classify: env with env-prefixed args is rejected")

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
    pa._APPROVED_SET.clear()

    # widened read-only allowlist (anti-nagging: dev-loop + inspection commands)
    assert_ok(pa.classify_command("python3 -m pyflakes alvaagent_tui.py") == "allow",
              "classify: pyflakes lint is read-only -> allow")
    assert_ok(pa.classify_command("python3 test_tui.py") == "allow",
              "classify: running the project test suite is read-only -> allow")
    assert_ok(pa.classify_command("python3 -m json.tool x.json") == "allow",
              "classify: json.tool pretty-print is read-only -> allow")
    assert_ok(pa.classify_command("ps aux") == "allow",
              "classify: ps is read-only -> allow")
    assert_ok(pa.classify_command("sort x.txt") == "allow",
              "classify: sort is read-only -> allow")
    assert_ok(pa.classify_command("git show HEAD") == "allow",
              "classify: git show is read-only -> allow")
    assert_ok(pa.classify_command("unzip -l a.zip") == "allow",
              "classify: unzip -l lists without extracting -> allow")
    assert_ok(pa.classify_command("cd /sdcard") == "allow",
              "classify: bare cd is read-only -> allow")
    # the widened list must NOT silently allow mutating/executing variants
    assert_ok(pa.classify_command("python3 -c 'print(1)'") == "ask",
              "classify: python3 -c arbitrary execution stays ask")
    assert_ok(pa.classify_command("python3 -m pip install x") == "ask",
              "classify: pip stays ask")
    assert_ok(pa.classify_command("unzip a.zip -d out") == "ask",
              "classify: unzip (extract) stays ask")
    assert_ok(pa.classify_command("tar -xf a.tar") == "ask",
              "classify: tar extract stays ask")
    assert_ok(pa.classify_command("cd /x && rm -rf /") == "ask",
              "classify: cd chained with a mutating command stays ask")
    assert_ok(pa.classify_command("git remote add o x") == "ask",
              "classify: git remote add mutates config -> ask")

    # session-remember cache: an approved action never prompts again
    pa._APPROVED_SET.clear()
    pa.ON_PERMISSION = lambda d: True
    assert_ok(pa.tool_run_command("touch /tmp/alva-cache-demo").get("ok") is True,
              "cache: first approval runs the command")
    pa.ON_PERMISSION = lambda d: False
    _cached = pa.tool_run_command("touch /tmp/alva-cache-demo")
    assert_ok(_cached.get("ok") is True,
              "cache: identical approved command reruns WITHOUT prompting (session)")
    _notcached = pa.tool_run_command("touch /tmp/alva-cache-demo-2")
    assert_ok(_notcached.get("ok") is False and "permission" in str(_notcached.get("error", "")),
              "cache: a different command still prompts")
    pa.ON_PERMISSION = None
    pa._APPROVED_SET.clear()

    # prompt accepts the 'a' (always) key and caches through ask_permission
    import builtins
    _orig_input = builtins.input
    try:
        builtins.input = lambda *a, **k: "a"
        assert_ok(pa.ask_permission("run command: python3 -m pyflakes demo.py") is True,
                  "ask_permission 'a' approves")
        builtins.input = lambda *a, **k: "n"
        assert_ok(pa.ask_permission("run command: python3 -m pyflakes demo2.py") is False,
                  "ask_permission 'n' denies")
    finally:
        builtins.input = _orig_input
    pa._APPROVED_SET.clear()
    # the cache itself is populated by _permission (the hook result feeds it)
    pa.ON_PERMISSION = lambda d: True
    assert_ok(pa._permission("run command: python3 -m pyflakes demo3.py") is True,
              "cache: _permission approves on first ask")
    assert_ok("run command: python3 -m pyflakes demo3.py" in pa._APPROVED_SET,
              "cache: approval is stored for the session")
    pa.ON_PERMISSION = lambda d: False
    assert_ok(pa._permission("run command: python3 -m pyflakes demo3.py") is True,
              "cache: stored approval reruns without re-asking")
    pa.ON_PERMISSION = None
    pa._APPROVED_SET.clear()

    # ---------- autonomy: files ----------
    proj_test = os.path.join(DATA, "proj-demo.txt")
    w = pa.tool_file_write(proj_test, "first line\nsecond")
    assert_ok(w.get("ok") is True and w.get("chars") == 17, "file_write creates a file")
    r = pa.tool_file_read(proj_test)
    assert_ok(r.get("ok") is True and "first line" in r.get("content", ""), "file_read round-trips content")
    e = pa.tool_file_edit(proj_test, "first", "FIRST")
    assert_ok(e.get("ok") is True and e.get("replaced") == 1, "file_edit replaces text (honest count)")
    assert_ok("FIRST line" in pa.tool_file_read(proj_test)["content"], "file_edit change persisted")
    pa.tool_file_write(proj_test, "a a a")
    e2 = pa.tool_file_edit(proj_test, "a", "b")
    assert_ok(e2.get("ok") is True and e2.get("replaced") == 1
              and pa.tool_file_read(proj_test)["content"] == "b a a",
              "file_edit replaces only the first occurrence")
    lst = pa.tool_file_list(DATA)
    assert_ok(lst.get("ok") is True and any(x["name"] == "proj-demo.txt" for x in lst.get("entries", [])),
              "file_list shows the created file")

    # ---------- file_search (glob) ----------
    fs = pa.tool_file_search("proj-demo.txt", path=DATA)
    assert_ok(fs.get("ok") is True and fs.get("count", 0) >= 1, "file_search finds an exact filename")
    fs2 = pa.tool_file_search("*.txt", path=DATA)
    assert_ok(fs2.get("ok") is True and any(m.get("path", "").endswith("proj-demo.txt") for m in fs2.get("matches", [])),
              "file_search glob matches *.txt")
    assert_ok(pa.tool_file_search("", path=DATA).get("ok") is False, "file_search rejects an empty pattern")
    assert_ok(pa.tool_file_search("*.md", path="/no/such/dir").get("ok") is False,
              "file_search rejects a missing base dir")

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
    skill_names = [s.get("name") for s in (pa.tool_skill_list().get("skills") or [])]
    assert_ok("test-skill" in skill_names, "skill_list shows saved skill")
    sr = pa.tool_skill_read("test-skill")
    assert_ok(sr.get("ok") is True and "check the time" in sr.get("content", ""), "skill_read returns skill body")
    assert_ok(pa.tool_skill_read("missing-skill").get("ok") is False, "skill_read reports missing skills")
    # categorized skills: save/read/list round-trip
    sc = pa.tool_skill_save("cat-skill", "Steps go here.", category="productivity")
    assert_ok(sc.get("ok") is True and sc.get("category") == "productivity",
              "skill_save places a categorized skill")
    scr = pa.tool_skill_read("productivity/cat-skill")
    assert_ok(scr.get("ok") is True and scr.get("category") == "productivity",
              "skill_read resolves category/name")
    # skills work even with NO PyYAML installed (frontmatter fallback)
    saved_yaml = pa.yaml
    try:
        pa.yaml = None
        sf = pa.tool_skill_save("no-yaml-skill", "---\ndescription: parsed without yaml\ntags:\n  - a\n  - b\n---\nbody here")
        assert_ok(sf.get("ok") is True, "skill_save works without PyYAML")
        sfr = pa.tool_skill_read("no-yaml-skill")
        assert_ok(sfr.get("ok") is True and sfr.get("description") == "parsed without yaml"
                  and sfr.get("tags") == ["a", "b"], "frontmatter fallback parses keys + lists")
        pa.tool_skill_remove("no-yaml-skill")
        # block-scalar frontmatter (description: >) + inline arrays without PyYAML
        sf2 = pa.tool_skill_save("block-scalar-skill",
                                 "---\ndescription: >\n  Procedure the agent follows\n  across folded lines\ntags: [alpha, beta]\nrelated_skills: []\n---\nbody")
        assert_ok(sf2.get("ok") is True, "skill_save accepts block-scalar frontmatter")
        sfr2 = pa.tool_skill_read("block-scalar-skill")
        assert_ok(sfr2.get("description") == "Procedure the agent follows across folded lines",
                  "mini-YAML folds '>' block scalars into a single line")
        assert_ok(sfr2.get("tags") == ["alpha", "beta"], "mini-YAML parses inline [a, b] arrays")
        assert_ok(sfr2.get("related_skills") == [], "mini-YAML parses empty inline arrays")
        pa.tool_skill_remove("block-scalar-skill")
    finally:
        pa.yaml = saved_yaml
    pa.tool_skill_remove("test-skill")
    pa.tool_skill_remove("productivity/cat-skill")

    # ---------- skills: install from file / URL / repo ----------
    _loc_skill = os.path.join(DATA, "loc-skill.md")
    with open(_loc_skill, "w", encoding="utf-8") as _f:
        _f.write("---\ndescription: A local skill\n---\nsteps here")
    _ri = pa.tool_skill_install(_loc_skill)
    assert_ok(_ri.get("ok") is True and _ri.get("name") == "loc-skill",
              "skill_install imports a local .md file")
    _rr = pa.tool_skill_read("loc-skill")
    assert_ok(_rr.get("ok") is True and _rr.get("description") == "A local skill",
              "installed skill is readable with its frontmatter")
    pa.tool_skill_remove("loc-skill")
    os.remove(_loc_skill)
    # GitHub blob URL -> raw.githubusercontent.com + category
    _fetched = {}
    _saved_raw = pa._raw_fetch
    pa._raw_fetch = lambda u: (_fetched.__setitem__("url", u),
                               "---\ndescription: Remote skill\n---\nbody")[1]
    try:
        _ru = pa.tool_skill_install(
            "https://github.com/alvaschul/skills/blob/main/skills/foo.md", "remote")
        assert_ok(_ru.get("ok") is True and _ru.get("name") == "foo"
                  and _ru.get("category") == "remote",
                  "skill_install fetches a GitHub URL and categorizes it")
        assert_ok(_fetched.get("url") ==
                  "https://raw.githubusercontent.com/alvaschul/skills/main/skills/foo.md",
                  "skill_install rewrites github.com blob URLs to raw")
    finally:
        pa._raw_fetch = _saved_raw
    pa.tool_skill_remove("remote/foo")
    # non-markdown page (HTML) is rejected, not imported
    assert_ok(pa._looks_like_html("<html><body>page</body></html>") is True,
              "HTML guard catches a repo page")
    assert_ok(pa._looks_like_html("---\ndescription: skill\n---\nbody") is False,
              "HTML guard lets real markdown through")
    _saved_raw = pa._raw_fetch
    pa._raw_fetch = lambda u: None  # simulate an un-fetchable/non-markdown URL
    try:
        _rh = pa.tool_skill_install("https://github.com/alvaschul/skills")
        assert_ok(_rh.get("ok") is False,
                  "skill_install rejects an un-fetchable (HTML) page")
    finally:
        pa._raw_fetch = _saved_raw
    # dispatch registration
    _disp = pa.dispatch_tool("skill_install", {"source": "/no/such/file.md"})
    assert_ok(_disp.get("ok") is False and "error" in _disp,
              "skill_install is registered in TOOL_IMPL (dispatchable)")
    # skill_sync_repo: permission-gated
    pa._APPROVED_SET.clear()
    _sync = pa.tool_skill_sync_repo("https://example.com/skills.git")
    assert_ok(_sync.get("ok") is False and "permission" in str(_sync.get("error", "")),
              "skill_sync_repo asks permission (headless denies)")
    # skill_sync_repo: fake a successful shallow clone and import every .md
    pa.ON_PERMISSION = lambda d: True
    _rc_obj = type("RC", (), {"returncode": 0, "stderr": "", "stdout": ""})()
    _saved_run = pa.subprocess.run

    def _fake_clone(args, **kw):
        tmp = args[-1]
        for rel, content in (("skills/prod/a.md", "---\ndescription: A\n---\nsteps"),
                             ("skills/research/b.md", "body b"),
                             ("skills/README.md", "docs to skip"),
                             ("skills/prod/.github/ci.md", "hidden")):
            p = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
        return _rc_obj

    pa.subprocess.run = _fake_clone
    try:
        _sy = pa.tool_skill_sync_repo("https://example.com/skills.git")
        assert_ok(_sy.get("ok") is True and _sy.get("count") == 2,
                  "skill_sync_repo imports every .md (%d)" % _sy.get("count"))
        _cats = sorted({s.get("category") for s in _sy.get("installed", [])})
        assert_ok(_cats == ["prod", "research"],
                  "skill_sync_repo uses folders as categories (%s)" % _cats)
        assert_ok(any(s == "README.md" for s in _sy.get("skipped", [])),
                  "skill_sync_repo skips README.md")
    finally:
        pa.subprocess.run = _saved_run
        pa.ON_PERMISSION = None
    pa.tool_skill_remove("prod/a")
    pa.tool_skill_remove("research/b")

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

    # ---------- session pruning (store.json stays bounded) ----------
    for i in range(pa.MAX_SESSIONS + 5):
        pa.save_session("prune-%02d" % i, [{"role": "user", "content": "m"}])
    assert_ok(len(pa.sessions_map()) <= pa.MAX_SESSIONS, "save_session prunes past MAX_SESSIONS")
    assert_ok("prune-%02d" % (pa.MAX_SESSIONS + 4) in pa.sessions_map(),
              "the newest session survives pruning")
    for i in range(pa.MAX_SESSIONS + 5):
        pa.delete_session("prune-%02d" % i)

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
    # regression: main() must never bind SIGINT to SIG_DFL - that skips
    # KeyboardInterrupt handling and the alt-screen _cleanup() on Ctrl+C
    import inspect as _inspect
    _main_src = _inspect.getsource(pa.main)
    assert_ok("signal.signal(pa.signal.SIGINT, pa.signal.SIG_DFL)" not in _main_src,
              "main() must not set SIGINT to SIG_DFL (breaks Ctrl+C cleanup)")
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

    # ---------- performance: chunked streaming parses SSE correctly ----------
    # Simulate an SSE byte stream and ensure the chunked reader yields the
    # same content as the old 1-byte reader would.
    import io as _io
    fake_sse = (
        "data: {\"choices\":[{\"delta\":{\"content\":\"Hello\"}}]}\n\n"
        "data: {\"choices\":[{\"delta\":{\"content\":\" world\"}}]}\n\n"
        "data: [DONE]\n\n"
    )
    # monkeypatch urlopen to return our fake stream for the streaming path
    _orig_urlopen = pa.urllib.request.urlopen
    class _FakeResp:
        def __init__(self, data):
            self._b = data.encode("utf-8")
            self._i = 0
        def read(self, n=1024):
            chunk = self._b[self._i:self._i + n]
            self._i += len(chunk)
            return chunk
    def _fake_urlopen(req, timeout=180):
        return _FakeResp(fake_sse)
    pa.urllib.request.urlopen = _fake_urlopen
    try:
        cfg_s = {"base_url": "http://x/v1", "api_key": "k", "model": "m", "temperature": 0.5}
        out = "".join(c for c, _ in pa.chat_completion_stream(
            [{"role": "user", "content": "hi"}], cfg_s))
        assert_ok(out == "Hello world", "chunked SSE reader reconstructs content correctly")
    finally:
        pa.urllib.request.urlopen = _orig_urlopen

    # ---------- UX: command history persists across restarts ----------
    # ---------- streaming: tool_call ids ----------
    id_sse = (
        "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"call_abc\","
        "\"function\":{\"name\":\"calculator\",\"arguments\":\"\"}}]}}]}\n\n"
        "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"call_abc\","
        "\"function\":{\"arguments\":\"{\\\"expression\\\":\\\"2+2\\\"}\"}}]}}]}\n\n"
        "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"tool_calls\"}]}\n\n"
    )
    def _fake_urlopen2(req, timeout=180):
        return _FakeResp(id_sse)
    pa.urllib.request.urlopen = _fake_urlopen2
    try:
        _events = list(pa.chat_completion_stream(
            [{"role": "user", "content": "calc"}], cfg_s))
        _tc = [tc for _, tcs in _events for tc in (tcs or [])]
        assert_ok(len(_tc) == 1 and _tc[0]["id"] == "call_abc",
                  "tool_call id is not concatenated across repeated deltas")
    finally:
        pa.urllib.request.urlopen = _orig_urlopen
    # tool_call id: no id in stream -> stable synthetic id
    noid_sse = (
        "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,"
        "\"function\":{\"name\":\"calculator\",\"arguments\":\"{\\\"expression\\\":\\\"1\\\"}\"}}]}}]}\n\n"
        "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"tool_calls\"}]}\n\n"
    )
    def _fake_urlopen3(req, timeout=180):
        return _FakeResp(noid_sse)
    pa.urllib.request.urlopen = _fake_urlopen3
    try:
        _events = list(pa.chat_completion_stream(
            [{"role": "user", "content": "calc"}], cfg_s))
        _tc = [tc for _, tcs in _events for tc in (tcs or [])]
        assert_ok(len(_tc) == 1 and _tc[0]["id"] == "call_0",
                  "tool_call falls back to a stable synthetic id")
    finally:
        pa.urllib.request.urlopen = _orig_urlopen

    # ---------- streaming: plain-JSON fallback (non-SSE gateways) ----------
    # Some gateways ignore "stream": true and answer with a plain JSON
    # completion (minified on one line, or pretty-printed). The stream reader
    # must parse the raw body directly instead of crashing on a str/bytes flush
    # or silently dropping the reply.
    _plain_msg = {"choices": [{"message": {"role": "assistant",
                                           "content": "plain-json-reply"},
                               "finish_reason": "stop"}]}
    for _label, _body in (("minified", json.dumps(_plain_msg)),
                          ("pretty", json.dumps(_plain_msg, indent=2))):
        def _fake_plain(req, timeout=180, _b=_body):
            return _FakeResp(_b)
        pa.urllib.request.urlopen = _fake_plain
        try:
            _events = list(pa.chat_completion_stream(
                [{"role": "user", "content": "hi"}], cfg_s))
            _text = "".join(c for c, _ in _events)
            assert_ok(_text == "plain-json-reply",
                      "plain-JSON (%s) fallback parses the response body" % _label)
        finally:
            pa.urllib.request.urlopen = _orig_urlopen

    # ---------- UX: dead turns (failed/empty) must not persist ghost messages ----------
    # A failed request or empty response previously left the unanswered user
    # message in the session; retrying the same message then stacked consecutive
    # duplicates. send_message must drop the user message (and any empty
    # assistant ghost) on such turns.
    _save_calls = []
    _orig_send_deps = {
        "active_cfg": pa.active_cfg,
        "run_agent_tui": pa.run_agent_tui,
        "compress_now": pa.compress_now,
        "render_agent_panel": pa.render_agent_panel,
        "render_status_bar": pa.render_status_bar,
        "print_user_turn": pa.print_user_turn,
        "context_usage": pa.context_usage,
        "save_session": pa.save_session,
    }
    pa.active_cfg = lambda st: {"auto_compress": False, "temperature": 0.7,
                                "base_url": "http://x/v1", "api_key": "k", "model": "m"}
    pa.compress_now = lambda *a, **k: False
    pa.render_agent_panel = lambda *a, **k: None
    pa.render_status_bar = lambda *a, **k: None
    pa.print_user_turn = lambda *a, **k: None
    pa.context_usage = lambda *a, **k: (0, 128000)
    pa.save_session = lambda name, msgs: _save_calls.append((name, [dict(m) for m in msgs]))

    def _fake_run(res):
        pa.run_agent_tui = lambda history, cfg: res

    _state = {"active": "p", "profiles": {"p": {}}}
    _session = "default"

    _sess_hist = [{"role": "user", "content": "helo"}]
    _failed_res = {"content": "error: LLM API unreachable: boom", "history": [{"role": "system", "content": "s"}] + _sess_hist,
                   "cancelled": False, "streamed": False, "tools": 0}
    _fake_run(_failed_res)
    _save_calls[:] = []
    pa.send_message("helo", _sess_hist, _state, _session)
    assert_ok(all(m.get("role") != "user" for m in _sess_hist),
              "failed turn drops the unanswered user message (no ghost duplicate)")

    _empty_hist = [{"role": "user", "content": "helo"},
                   {"role": "assistant", "content": ""}]
    _empty_res = {"content": "", "history": [{"role": "system", "content": "s"}] + _empty_hist,
                  "cancelled": False, "streamed": False, "tools": 0}
    _fake_run(_empty_res)
    _sess_hist = [{"role": "user", "content": "helo"},
                  {"role": "assistant", "content": ""}]
    pa.send_message("helo", _sess_hist, _state, _session)
    assert_ok(_sess_hist == [],
              "empty response drops both the user message and the empty assistant ghost")

    _good_hist = [{"role": "user", "content": "helo"}]
    _good_res = {"content": "Hey!", "history": [{"role": "system", "content": "s"}] + _good_hist +
                 [{"role": "assistant", "content": "Hey!"}],
                 "cancelled": False, "streamed": False, "tools": 0}
    _fake_run(_good_res)
    pa.send_message("helo", _good_hist, _state, _session)
    assert_ok(len(_good_hist) == 2 and _good_hist[0]["role"] == "user" and _good_hist[1]["content"] == "Hey!",
              "successful turn keeps user + assistant messages")

    for _k, _v in _orig_send_deps.items():
        setattr(pa, _k, _v)

    # ---------- resilience: retry/backoff on transient API failures ----------
    _orig_sleep = pa._sleep_retry
    pa._sleep_retry = lambda a: None
    class _Resp:
        def __init__(self, code, data):
            self._code, self._data = code, data
        def getcode(self):
            return self._code
        def read(self):
            return self._data
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    _attempts = [0]
    _good = json.dumps({"choices": [{"message": {"role": "assistant", "content": "retried-ok"}}]}).encode("utf-8")
    def _flaky(req, timeout=180):
        _attempts[0] += 1
        if _attempts[0] <= 2:
            raise pa.urllib.error.URLError("transient outage")
        return _Resp(200, _good)
    pa.urllib.request.urlopen = _flaky
    try:
        _d = pa.chat_completion([{"role": "user", "content": "hi"}],
                                {"base_url": "http://x/v1", "api_key": "k", "model": "m", "temperature": 0.5})
        assert_ok(_attempts[0] == 3 and _d["choices"][0]["message"]["content"] == "retried-ok",
                  "chat_completion retries transient failures (3 attempts)")
    finally:
        pa.urllib.request.urlopen = _orig_urlopen
    _attempts[0] = 0
    def _perm(req, timeout=180):
        _attempts[0] += 1
        return _Resp(400, json.dumps({"error": {"message": "nope"}}).encode("utf-8"))
    pa.urllib.request.urlopen = _perm
    try:
        try:
            pa.chat_completion([{"role": "user", "content": "hi"}],
                               {"base_url": "http://x/v1", "api_key": "k", "model": "m", "temperature": 0.5})
            _raised = False
        except RuntimeError:
            _raised = True
        assert_ok(_raised and _attempts[0] == 1,
                  "chat_completion does not retry permanent 4xx errors")
    finally:
        pa.urllib.request.urlopen = _orig_urlopen
        pa._sleep_retry = _orig_sleep
    # ---------- UX: agent reply renders code cleanly ----------
    import contextlib as _ctx, io as _iom
    _wbuf = _iom.StringIO()
    with _ctx.redirect_stdout(_wbuf):
        _w = pa.AgentWriter(pa.SKINS["midnight"], pa.SKINS["midnight"]["agent"])
        _w.feed("Here:\n\n```python\na = 1\n```\n\n```\necho hi\n```\nok\n")
        _w.close()
    _raw = _wbuf.getvalue()
    assert_ok("```" not in _raw, "agent reply shows no literal code fences")
    assert_ok("─ python" in _raw and "─ code" in _raw,
              "agent reply marks code blocks with a small language tag")
    assert_ok(all(ch not in _raw for ch in "╭╮╰╯"),
              "agent reply has no full-width box")
    assert_ok("▍ " in _raw, "agent reply uses a thin left accent bar")
    assert_ok("  a = 1" in _raw, "code lines are indented under the bar")

    # ---------- UX: Hermes XML function calling is hidden & executed ----------
    _xml = ("<tool_call>\n<function=calculator>\n<parameter=expression>6*7</parameter>\n"
            "</function>\n</tool_call>")
    _parsed = pa._parse_xml_tool_calls("Let me compute:\n" + _xml)
    assert_ok(_parsed == [("calculator", {"expression": "6*7"})],
              "_parse_xml_tool_calls parses the Hermes <tool_call> format")
    assert_ok(pa._parse_xml_tool_calls("no calls here") == [], "no tool_calls -> empty list")
    _stripped = pa._strip_xml("hi <think>secret</think> bye " + _xml + " end")
    assert_ok("think" not in _stripped and "secret" not in _stripped and "6*7" not in _stripped
              and "hi" in _stripped and "end" in _stripped, "_strip_xml drops think + tool_call blocks")
    _stray = pa._strip_xml("The user asks...\n</think>\n" + _xml + "\nnext")
    assert_ok("</think>" not in _stray and "tool_call" not in _stray and "The user asks..." in _stray
              and "next" in _stray, "_strip_xml drops stray </think> and tool_call blocks")
    _wbuf3 = _iom.StringIO()
    with _ctx.redirect_stdout(_wbuf3):
        _w3 = pa.AgentWriter(pa.SKINS["midnight"], pa.SKINS["midnight"]["agent"])
        for _chunk in ["Let me compute:\n<tool_ca", "ll>\n<function=calculator>\n<parameter=expression>6*7</parameter>\n"
                       "</function>\n</tool_call>\n", "<think>\nhmm\n</think>\n", "42 ok\n"]:
            _w3.feed(_chunk)
        _w3.close()
    _raw3 = _wbuf3.getvalue()
    assert_ok("<tool_call>" not in _raw3 and "<function" not in _raw3 and "6*7" not in _raw3
              and "<think>" not in _raw3 and "hmm" not in _raw3,
              "agent reply hides XML blocks even when they split across chunks")
    assert_ok("Let me compute:" in _raw3 and "42 ok" in _raw3,
              "visible text around hidden XML blocks survives")
    # stray </think> with no opening tag (reasoning models) must not render
    _wbuf4 = _iom.StringIO()
    with _ctx.redirect_stdout(_wbuf4):
        _w4 = pa.AgentWriter(pa.SKINS["midnight"], pa.SKINS["midnight"]["agent"])
        for _chunk4 in ["Reasoning about it.\n", "</think>\n", "<tool_call>\n<function=calculator>\n",
                        "<parameter=expression>2+2</parameter>\n</function>\n</tool_call>\n", "ok\n"]:
            _w4.feed(_chunk4)
        _w4.close()
    _raw4 = _wbuf4.getvalue()
    assert_ok("</think>" not in _raw4 and "<tool_call>" not in _raw4 and "2+2" not in _raw4,
              "agent reply hides stray </think> and split tool_call blocks")
    assert_ok("Reasoning about it." in _raw4 and "ok" in _raw4, "visible text survives stray tags")

    # ---------- UX: markdown formatting renders to ANSI styles ----------
    _old_color = pa.COLOR
    try:
        pa.COLOR = True
        _sk5 = pa.SKINS["midnight"]
        _rend, _ = pa._md_line("**bold** *italic* _it_ __also__ ~~gone~~", _sk5)
        assert_ok("\x1b[1m" in _rend and "\x1b[3m" in _rend and "\x1b[9m" in _rend
                  and "*" not in _rend and "_" not in _rend and "~" not in _rend,
                  "style_inline maps **bold**, *italic*, _italic_, __bold__, ~~strike~~")
        _rend2, _ = pa._md_line("nested **bold *italic* rest** end", _sk5)
        assert_ok("\x1b[1m" in _rend2 and "\x1b[1;3m" in _rend2 and "**" not in _rend2
                  and "*italic*" not in _rend2,
                  "nested emphasis keeps both styles with no stray markers")
        _rend3, _ = pa._md_line("use `cmd -x` here", _sk5)
        assert_ok("cmd -x" in _rend3 and "`" not in _rend3 and _sk5["code"] in _rend3,
                  "inline `code` is colored and backticks are hidden")
        _rend4, _ = pa._md_line("a * b * c", _sk5)
        assert_ok(_rend4 == "a * b * c", "space-flanked asterisks stay literal")
        _rend5, _ = pa._md_line("***both***", _sk5)
        assert_ok("\x1b[1;3m" in _rend5, "***both*** renders bold + italic")
    finally:
        pa.COLOR = _old_color
    assert_ok(pa._md_line("**raw**", pa.SKINS["midnight"])[0] == "**raw**",
              "style_inline passes markdown through untouched when colors are off")

    _old_color2 = pa.COLOR
    try:
        pa.COLOR = True
        _sk6 = pa.SKINS["midnight"]
        _r1, _p1 = pa._md_line("Some **bo", _sk6)
        _r2, _p2 = pa._md_line(_p1 + "ld** text", _sk6)
        assert_ok(_p1 == "**bo" and _r1 == "Some " and _p2 == "" and "\x1b[1m" in _r2
                  and "**" not in _r2 and "**bo" not in _r2,
                  "marker split across chunks is parked and merged into one bold span")
        _wbuf5 = _iom.StringIO()
        with _ctx.redirect_stdout(_wbuf5):
            _w5 = pa.AgentWriter(_sk6, _sk6["agent"])
            for _chunk5 in ["Result **4", "2**.\n", "## Head\n", "- [x] done *it*\n",
                            "- [ ] todo\n", "- plain\n", "> quote\n", "---\n"]:
                _w5.feed(_chunk5)
            _w5.close()
        _raw5 = _wbuf5.getvalue()
        assert_ok("\x1b[1m42\x1b[0m" in _raw5 and "**" not in _raw5,
                  "streamed bold split across feeds renders as one span")
        assert_ok("\x1b[38;5;81mHead" in _raw5 and "## " not in _raw5,
                  "## heading renders as a bold accent line")
        assert_ok("\x1b[38;5;114m✓ " in _raw5 and "\x1b[38;5;244m☐ " in _raw5,
                  "- [x]/- [ ] checkboxes render ✓ / ☐")
        assert_ok("\x1b[38;5;45m• " in _raw5, "- bullet renders with an accent marker")
        assert_ok("\x1b[38;5;240m│ " in _raw5, "> quote renders with a border bar")
        assert_ok("─" * 8 in _raw5 and "---" not in _raw5, "--- renders as a dim rule")
        assert_ok("*it*" not in _raw5 and "\x1b[3m" in _raw5,
                  "italic inside a checkbox item is styled")
    finally:
        pa.COLOR = _old_color2

    # end-to-end: XML tool_call gets executed and the result fed back
    _xml_sse = (
        'data: {"choices":[{"delta":{"content":"Let me compute:\\n<tool_call>\\n<function=calculator>\\n'
        '<parameter=expression>6*7</parameter>\\n</function>\\n</tool_call>\\n"}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    )
    _ans_sse = (
        'data: {"choices":[{"delta":{"content":"The answer is 42."}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    )
    _xml_attempt = [0]
    def _fake_xml_urlopen(req, timeout=180):
        _xml_attempt[0] += 1
        return _FakeResp(_xml_sse if _xml_attempt[0] == 1 else _ans_sse)
    pa.urllib.request.urlopen = _fake_xml_urlopen
    try:
        _evs = list(pa.run_agent_stream([{"role": "user", "content": "calc"}], cfg_s))
        _ts = [e for k, e in _evs if k == "tool_start"]
        _te = [e for k, e in _evs if k == "tool_end"]
        _dn = [e for k, e in _evs if k == "done"][0]
        assert_ok(len(_ts) == 1 and _ts[0]["name"] == "calculator"
                  and _ts[0]["args"] == {"expression": "6*7"},
                  "XML tool_call dispatches run_agent_stream tool_start")
        assert_ok(len(_te) == 1 and _te[0]["status"] == "done"
                  and _te[0]["result"].get("result") == 42,
                  "XML tool_call executes and returns its result")
        assert_ok(_dn["content"] == "The answer is 42." and "tool_call" not in _dn["content"],
                  "XML tool_call loop finishes with the model's clean follow-up")
    finally:
        pa.urllib.request.urlopen = _orig_urlopen

    # spinner can be permanently disabled once streaming starts
    _sp = pa.Spinner("thinking")
    _sp.start()
    _sp.disable()
    assert_ok(_sp._dead is True, "spinner.disable() permanently silences frames")
    _sp.stop()
    _sp2 = pa.Spinner("thinking")
    _sp2.start()
    _sp2.disable()
    _sp2.stop()
    assert_ok(_sp2._dead is True, "spinner.disable() stays dead across stop()")
    # ---------- UX: command history persists across restarts ----------
    import readline as _rl
    _htmp = os.path.join(DATA, "_hist_probe.txt")
    pa.HISTORY_PATH = _htmp
    pa.setup_completion()           # fresh load (no file yet)
    _rl.add_history("/provider add")
    _rl.add_history("remember my name is Alex")
    _rl.add_history("/help")
    pa.save_completion_history()
    assert_ok(os.path.exists(_htmp), "history file written to disk")
    # simulate a restart: re-load from disk
    pa.setup_completion()
    assert_ok(_rl.get_current_history_length() == 3, "history reloads after restart (no dupes)")
    # calling setup_completion again must not duplicate
    pa.setup_completion()
    assert_ok(_rl.get_current_history_length() == 3, "re-calling setup_completion does not duplicate history")
    os.remove(_htmp)

    # ---------- resilience: tool error recovery hints ----------
    # dispatch_tool must attach an actionable hint to failed tool results so
    # the agent can switch strategy instead of blindly retrying.
    _hint_ftp = pa.dispatch_tool("web_fetch", {"url": "ftp://x"})
    assert_ok(_hint_ftp.get("error") and "http" in _hint_ftp.get("error", "")
              and "hint" in _hint_ftp,
              "web_fetch error result carries a recovery hint")
    _hint_todo = pa.dispatch_tool("todo_toggle", {"index": 999})
    assert_ok(_hint_todo.get("ok") is False and "hint" in _hint_todo,
              "tool error dicts from the tool body get a hint too")
    _hint_calc = pa.dispatch_tool("calculator", {"expression": "1/0"})
    assert_ok(_hint_calc.get("error") and "hint" in _hint_calc,
              "tool exceptions are wrapped with a hint")
    _hint_unknown = pa.dispatch_tool("nope", {})
    assert_ok(_hint_unknown.get("error") and "unknown tool" in _hint_unknown.get("error", ""),
              "unknown tools still error cleanly (no hint needed)")

    # ---------- observability: trace log ----------
    # run_agent (used by the full-loop test above) should have written
    # turn_start / tool / turn_end JSON lines to trace.log.
    assert_ok(os.path.exists(pa.TRACE_PATH), "trace.log exists after agent turns")
    _trace_lines = pa._read_trace(500)
    _trace_all = "".join(_trace_lines)
    assert_ok(len(_trace_lines) > 0 and '"ts"' in _trace_all,
              "trace entries are JSON lines with a timestamp")
    assert_ok('"event": "turn_start"' in _trace_all, "trace records turn_start")
    assert_ok('"event": "tool"' in _trace_all, "trace records per-tool events")
    assert_ok('"event": "turn_end"' in _trace_all, "trace records turn_end")
    assert_ok(pa._trace_count() >= len(_trace_lines), "_trace_count agrees with _read_trace")

    # ---------- resilience: circuit breaker + turn timeout (run_agent) ----------
    # A tool that fails every time must stop the loop early instead of burning
    # MAX_STEPS API calls on a strategy that is not working.
    _orig_dispatch = pa.dispatch_tool
    _orig_chat = pa.chat_completion
    _orig_timeout = pa._TURN_TIMEOUT
    _chat_calls = []

    def _fail_dispatch(name, args):
        return {"error": "boom"}

    def _tool_chat(messages, config, tools=None):
        _chat_calls.append(1)
        return {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "t%d" % len(_chat_calls), "type": "function",
             "function": {"name": "get_time", "arguments": "{}"}}]}}]}

    pa.dispatch_tool = _fail_dispatch
    pa.chat_completion = _tool_chat
    try:
        _breaker = json.loads(pa.run_agent(
            json.dumps([{"role": "user", "content": "retry forever"}]), json.dumps(cfg)))
        assert_ok("stopped early" in str(_breaker.get("content", "")),
                  "circuit breaker stops after repeated tool failures")
        assert_ok(len(_chat_calls) == pa._MAX_CONSEC_TOOL_FAILURES,
                  "breaker fires at %d consecutive failures (not MAX_STEPS)"
                  % pa._MAX_CONSEC_TOOL_FAILURES)
    finally:
        pa.dispatch_tool = _orig_dispatch
        pa.chat_completion = _orig_chat
        pa._TURN_TIMEOUT = _orig_timeout

    # A turn that runs past the wall-clock budget must stop without another
    # API call (no runaway 25-step x long-timeout turn).
    pa._TURN_TIMEOUT = 0
    pa.dispatch_tool = _orig_dispatch
    pa.chat_completion = _tool_chat
    _chat_calls[:] = []
    try:
        _timed = json.loads(pa.run_agent(
            json.dumps([{"role": "user", "content": "slow"}]), json.dumps(cfg)))
        assert_ok("time budget" in str(_timed.get("content", "")),
                  "turn timeout stops a running turn")
        assert_ok(len(_chat_calls) == 0, "timeout fires before the first API call")
    finally:
        pa.chat_completion = _orig_chat
        pa._TURN_TIMEOUT = _orig_timeout

    # ---------- resilience: circuit breaker + timeout (run_agent_stream) ----------
    # Same guarantees on the streaming path used by the real TUI.
    _fail_sse = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_fail",'
        '"function":{"name":"calculator","arguments":"{\\"expression\\":\\"1\\"}"}}]}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
    )
    _sse_calls = []

    def _fail_urlopen(req, timeout=180):
        _sse_calls.append(1)
        return _FakeResp(_fail_sse)

    pa.dispatch_tool = _fail_dispatch
    pa.urllib.request.urlopen = _fail_urlopen
    try:
        _evs = list(pa.run_agent_stream([{"role": "user", "content": "loop"}], cfg_s))
        _dn = [e for k, e in _evs if k == "done"][0]
        assert_ok("stopped early" in str(_dn.get("content", "")),
                  "streaming loop stops early on repeated tool failures")
        assert_ok(len(_sse_calls) == pa._MAX_CONSEC_TOOL_FAILURES,
                  "streaming breaker fires at %d requests, not MAX_STEPS"
                  % pa._MAX_CONSEC_TOOL_FAILURES)
    finally:
        pa.dispatch_tool = _orig_dispatch
        pa.urllib.request.urlopen = _orig_urlopen

    pa._TURN_TIMEOUT = 0
    pa.urllib.request.urlopen = _fail_urlopen
    _sse_calls[:] = []
    try:
        _evs = list(pa.run_agent_stream([{"role": "user", "content": "slow"}], cfg_s))
        _dn = [e for k, e in _evs if k == "done"][0]
        assert_ok("time budget" in str(_dn.get("content", "")),
                  "streaming turn respects the wall-clock timeout")
        assert_ok(len(_sse_calls) == 0, "streaming timeout fires before any request")
    finally:
        pa.urllib.request.urlopen = _orig_urlopen
        pa._TURN_TIMEOUT = _orig_timeout

    # trace.log also carries the breaker/timeout events above
    _tail = pa._read_trace(50)
    assert_ok(any("circuit_breaker" in ln for ln in _tail),
              "trace logs the circuit-breaker stop reason")
    assert_ok(any("timeout" in ln for ln in _tail),
              "trace logs the timeout stop reason")

    # ---------- run_python: sandboxed subprocess tool ----------
    # The tool is registered (it was advertised to the model but not
    # dispatched - a real bug) and executes in a child process with a timeout,
    # an output cap, and the permission gate.
    _rp = pa.dispatch_tool("run_python", {"code": "print(6*7)"})
    assert_ok(_rp.get("ok") is True and "42" in _rp.get("output", ""),
              "run_python is dispatched and executes (regression: was unregistered)")
    _rp2 = pa.dispatch_tool("run_python", {"code": "import os; print(os.getcwd())"})
    assert_ok(_rp2.get("ok") is False and "permission" in _rp2.get("error", ""),
              "run_python asks permission for device-touching code (headless denies)")
    assert_ok(pa.classify_python("x = [i*i for i in range(10)]") == "allow",
              "classify_python allows pure computation")
    assert_ok(pa.classify_python("import shutil; shutil.rmtree('/x')") == "ask",
              "classify_python flags shutil/rmtree")
    assert_ok(pa.classify_python("open('/sdcard/x', 'w')") == "ask",
              "classify_python flags filesystem access")
    assert_ok(pa.classify_python("print(eval('2+2'))") == "ask",
              "classify_python flags eval")
    # infinite loop: killed by the timeout (not a hung agent)
    _orig_py_to = pa._PY_RUN_TIMEOUT
    pa._PY_RUN_TIMEOUT = 1
    try:
        _t0 = time.monotonic()
        _rp3 = pa.dispatch_tool("run_python", {"code": "while True: pass"})
        _dt3 = time.monotonic() - _t0
        assert_ok(_rp3.get("ok") is False and "timed out" in _rp3.get("error", "")
                  and _dt3 < 15,
                  "run_python kills runaway loops via the timeout (%.1fs)" % _dt3)
    finally:
        pa._PY_RUN_TIMEOUT = _orig_py_to
    # output flood: killed by the byte cap, truncated before it hits the agent
    _orig_py_max = pa._PY_MAX_BYTES
    pa._PY_MAX_BYTES = 1024
    try:
        _rp4 = pa.dispatch_tool("run_python", {"code": "print('x' * 100000)"})
        assert_ok(_rp4.get("ok") is False and "cap" in _rp4.get("error", ""),
                  "run_python caps runaway output")
    finally:
        pa._PY_MAX_BYTES = _orig_py_max
    # stdout truncation for sane-but-large outputs
    _rp5 = pa.dispatch_tool("run_python", {"code": "print('a' * 9000)"})
    assert_ok(_rp5.get("ok") is True and "... (truncated)" in _rp5.get("output", ""),
              "run_python truncates large outputs to _PY_MAX_CHARS")

    # ---------- tiered tool selection (core vs full) ----------
    _rt14 = pa.build_runtime()
    _saved_mode = _rt14.tool_mode
    try:
        _rt14.tool_mode = "core"
        _core = pa.visible(_rt14)
        _core_names = {t["function"]["name"] for t in _core}
        assert_ok(0 < len(_core) < len(pa.TOOLS),
                  "core mode advertises a curated subset (%d/%d)"
                  % (len(_core), len(pa.TOOLS)))
        assert_ok("run_command" in _core_names and "calculator" in _core_names,
                  "core set keeps the everyday tools")
        assert_ok("skill_list" not in _core_names and "self_test" not in _core_names,
                  "core set hides the advanced meta-tools")
        _rt14.tool_mode = "full"
        assert_ok(len(pa.visible(_rt14)) == len(pa.TOOLS),
                  "full mode advertises all tools")
    finally:
        _rt14.tool_mode = _saved_mode
    # lazy auto-enable: an advanced tool call flips the mode to full (one-way)
    _saved_mode = _rt14.tool_mode
    try:
        _rt14.tool_mode = "core"
        _r = pa.dispatch_tool(_rt14, "self_test", {})
        assert_ok(_rt14.tool_mode == "full",
                  "calling an advanced tool auto-enables full mode")
        assert_ok("Advanced tool set enabled" in _r.get("hint", ""),
                  "auto-enable tells the model the full set is now visible")
    finally:
        _rt14.tool_mode = _saved_mode
    # /trace plumbing: _read_trace + cmd_trace render without crashing
    try:
        import io as _io, contextlib as _cl
        _buf = _io.StringIO()
        with _cl.redirect_stdout(_buf):
            pa.cmd_trace("3")
        assert_ok(bool(_buf.getvalue().strip()),
                  "cmd_trace renders trace output")
    except Exception as _e:
        assert_ok(False, "cmd_trace crashed: %s" % _e)

    print("\nALL TESTS PASSED ✓" if failures == 0 else "\n%d TEST(S) FAILED ✗" % failures)
    sys.exit(0 if failures == 0 else 1)
finally:
    server.kill()
