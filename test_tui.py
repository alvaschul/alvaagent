#!/usr/bin/env python3
"""
test_tui.py — headless validation of the alvaagent package (rt-first API).

Exercises the harness against mock_llm_server.py (a fake OpenAI-compatible
API). Everything runs offline: web_fetch pulls the mock server's own
/mock-page.

The suite is pytest-compatible (every case is a test_* function) and also runs
standalone via a bundled runner:

    python3 test_tui.py          # -> prints "ALL TESTS PASSED ✓" and exits 0
    python3 -m pytest test_tui.py

Runtimes are fully isolated: every test builds its own Runtime through
`mkrt()` (a fresh temp data dir) so stores, config, skills and trace logs can
never collide. The mock LLM server is started lazily on first use and reused
for the whole process.
"""
import atexit
import builtins
import contextlib
import inspect
import io
import json
import os
import readline
import select
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PORT = 8210
BASE = "http://127.0.0.1:%d" % PORT
MOCK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mock_llm_server.py")

import alvaagent as pa
import alvaagent.agent as agent_mod
import alvaagent.client as client_mod
import alvaagent.config as config_mod
import alvaagent.skills as skills_mod
import alvaagent.tools as tools_mod
import alvaagent.tui as tui_mod
import alvaagent.util as util_mod

repl_mod = sys.modules["alvaagent.repl"]

# ---------------- helpers ---------------------------------------------------

_TMP_DIRS = []
_server = {"proc": None}


def _stop_server():
    proc = _server["proc"]
    if proc is not None:
        try:
            proc.kill()
        except Exception:
            pass
    _server["proc"] = None
    _kill_strays()


class _AdoptedServer:
    """Cache placeholder for a healthy mock server we did not start (an orphan
    from an interrupted run). poll() reports alive so the singleton keeps it
    for the rest of the process instead of restarting it."""

    def poll(self):
        return None


def _server_alive():
    """True when something is already answering as a mock server on PORT."""
    try:
        with urllib.request.urlopen(BASE + "/models", timeout=1) as r:
            return r.getcode() == 200
    except Exception:
        return False


def _kill_strays():
    """Best-effort: kill any mock server process from an earlier run that may
    still hold PORT. Matches both the full script path and its basename, since
    strays may have been launched either way (pkill ships with procps on
    Termux; if it is missing, the adopt fallback in _mock_server covers
    healthy strays)."""
    for pat in (MOCK, os.path.basename(MOCK)):
        try:
            subprocess.run(["pkill", "-f", pat], timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def _mock_server():
    """Lazily start (or reuse) the mock LLM server singleton.

    Survives interrupted runs: an orphaned server from a previous process may
    still be bound to PORT, which would make a fresh server exit immediately
    with 'Address already in use'. Kill the port holder before starting; only
    if that fails do we fall back to adopting whatever is already answering.
    """
    proc = _server["proc"]
    if proc is not None and proc.poll() is None:
        return proc
    _kill_strays()
    if _server_alive():
        _server["proc"] = _AdoptedServer()
        return _server["proc"]
    proc = subprocess.Popen(
        [sys.executable, MOCK, str(PORT)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    _server["proc"] = proc
    ready = False
    deadline = time.time() + 10
    while time.time() < deadline and proc.poll() is None:
        rlist, _, _ = select.select([proc.stdout], [], [], 0.2)
        if rlist and "READY" in proc.stdout.readline():
            ready = True
            break
    if proc.poll() is not None:
        raise RuntimeError("mock server exited early")
    if not ready:
        raise RuntimeError("mock server did not become ready")
    return proc


def mkrt(data_dir=None):
    """Build a fully isolated Runtime in a fresh temp data dir."""
    if data_dir is None:
        data_dir = tempfile.mkdtemp(prefix="alva_test_")
        _TMP_DIRS.append(data_dir)
    return pa.build_runtime(data_dir)


def mock_rt(data_dir=None):
    """A runtime pointed at the offline mock LLM server."""
    rt = mkrt(data_dir)
    rt.cfg = pa._normalize_state(
        {"provider": "mock", "base_url": BASE + "/v1",
         "api_key": "test-key", "model": "mock-model", "temperature": 0.5})
    return rt


class _FakeResp:
    """Minimal urllib response for faked streaming bodies."""

    def __init__(self, data):
        self._b = data.encode("utf-8")
        self._i = 0

    def fileno(self):
        raise AttributeError("fake socket")

    def read(self, n=1024):
        chunk = self._b[self._i:self._i + n]
        self._i += len(chunk)
        return chunk

    def close(self):
        pass


class _Resp:
    """Minimal context-managed urllib response for plain completions."""

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


def _cfg():
    return {"base_url": "http://x/v1", "api_key": "k", "model": "m",
            "temperature": 0.5}


# ---------------- tools -----------------------------------------------------

def test_tools_registered():
    assert len(pa.TOOLS) == 36


def test_calculator():
    assert pa.tool_calculator("6*7")["result"] == 42
    assert pa.tool_calculator("sqrt(16) + 2**3")["result"] == 12
    assert pa.tool_calculator("(2 + 3) * 4")["result"] == 20
    assert pa.tool_calculator("floor(pi * 3)")["result"] == 9
    assert pa.tool_calculator("10 % 3")["result"] == 1

    def calc_raises(expr, frag=None):
        try:
            pa.tool_calculator(expr)
            return False
        except Exception as e:
            return frag is None or frag in str(e)

    assert calc_raises("__import__('os').listdir('.')")
    assert calc_raises("1/0", "division by zero")
    assert calc_raises("'a'.upper()")
    assert calc_raises("1e400", "infinite")
    assert calc_raises("2**1000000", "exponent too large")
    assert calc_raises("9**9**9", "exponent too large")
    assert calc_raises("factorial(20000)")
    assert pa.tool_calculator("factorial(5)")["result"] == 120
    assert len(str(pa.tool_calculator("2**500")["result"])) > 100


def test_todos():
    rt = mock_rt()
    add1 = pa.tool_todo_add(rt, "unit test task")
    assert add1["ok"] is True
    add2 = pa.tool_todo_add(rt, "second task")
    lst = pa.tool_todo_list(rt)
    assert lst["count"] == 2 and lst["todos"][1]["text"] == "second task"
    tog = pa.tool_todo_toggle(rt, add1["index"])
    assert tog["done"] is True
    rem = pa.tool_todo_remove(rt, add2["index"])
    assert rem["ok"] is True and pa.tool_todo_list(rt)["count"] == 1
    assert pa.tool_todo_toggle(rt, 99)["ok"] is False


def test_memory():
    rt = mock_rt()
    assert pa.tool_memory_save(rt, "testkey", "hello world")["ok"] is True
    rec = pa.tool_memory_recall(rt, "testkey")
    assert rec["found"] is True and rec["value"] == "hello world"
    assert pa.tool_memory_recall(rt, "missing_key")["found"] is False
    ml = pa.tool_memory_list(rt)
    assert ml.get("ok") is True and any(f["key"] == "testkey" for f in ml.get("facts", []))
    ms = pa.tool_memory_search(rt, "hello")
    assert ms.get("ok") is True and ms.get("count", 0) >= 1
    ms2 = pa.tool_memory_search(rt, "testkey")
    assert ms2.get("ok") is True and any(f["key"] == "testkey" for f in ms2.get("facts", []))
    assert pa.tool_memory_search(rt, "no-such-fact-xyz").get("count") == 0


def test_clock():
    t = pa.tool_get_time()
    assert isinstance(t.get("iso"), str) and len(t["iso"]) > 10


# ---------------- autonomy: permissions ------------------------------------

def test_classify_command():
    assert pa.classify_command("ls -la") == "allow"
    assert pa.classify_command("python3 -m py_compile x.py") == "allow"
    assert pa.classify_command("echo hi") == "allow"
    assert pa.classify_command("git status") == "allow"
    assert pa.classify_command("rm -rf /") == "ask"
    assert pa.classify_command("sudo apt update") == "ask"
    assert pa.classify_command("pkg install python") == "ask"
    assert pa.classify_command("weird-thing") == "ask"
    assert pa.classify_command("") == "deny"
    # regression: shell substitution / metachar bypasses must NOT be allow
    assert pa.classify_command("echo $(touch /tmp/x)") == "ask"
    assert pa.classify_command("echo `touch /tmp/x`") == "ask"
    assert pa.classify_command("cat /etc/passwd $(whoami)") == "ask"
    assert pa.classify_command("ls -la; rm -rf /") == "ask"
    assert pa.classify_command("echo hi & whoami") == "ask"
    assert pa.classify_command("ls || whoami") == "ask"
    assert pa.classify_command("env X=1 rm -rf /") == "ask"
    assert pa.classify_command("git push") == "ask"
    assert pa.classify_command("catastrophe --version") == "ask"
    assert pa.classify_command("python3 -m py_compile x.py") == "allow"
    assert pa.classify_command("git status --short") == "allow"
    assert pa.classify_command("find / -name x") == "allow"
    assert pa.classify_command("find / -delete") == "ask"
    assert pa.classify_command(r"find / -exec rm {} \;") == "ask"
    # quoted commands executed through `env` / shell interpreters stay ask
    assert pa.classify_command("env sh -c 'rm -rf /'") == "ask"
    assert pa.classify_command("env -S 'echo hi'") == "ask"
    assert pa.classify_command("sh -c 'rm -rf /'") == "ask"
    assert pa.classify_command("bash -c 'rm -rf /'") == "ask"
    assert pa.classify_command("env") == "ask"
    assert pa.classify_command("env X=1 date") == "ask"


def test_command_permission_gate():
    rt = mock_rt()
    rt.on_permission = lambda d: False
    denied = pa.tool_run_command(rt, "touch /tmp/should-not-exist-alva")
    assert denied.get("ok") is False and "permission" in str(denied.get("error", ""))
    allowed = pa.tool_run_command(rt, "echo hello-from-alva")
    assert allowed.get("ok") is True and "hello-from-alva" in allowed.get("stdout", "")
    rt.on_permission = lambda d: True
    ok2 = pa.tool_run_command(rt, "echo approved-run")
    assert ok2.get("ok") is True and "approved-run" in ok2.get("stdout", "")
    rt.on_permission = None
    rt.approved.clear()


def test_widened_allowlist():
    # read-only dev-loop + inspection commands never nag
    assert pa.classify_command("python3 -m pyflakes alvaagent_tui.py") == "allow"
    assert pa.classify_command("python3 test_tui.py") == "allow"
    assert pa.classify_command("python3 -m json.tool x.json") == "allow"
    assert pa.classify_command("ps aux") == "allow"
    assert pa.classify_command("sort x.txt") == "allow"
    assert pa.classify_command("git show HEAD") == "allow"
    assert pa.classify_command("unzip -l a.zip") == "allow"
    assert pa.classify_command("cd /sdcard") == "allow"
    # mutating/executing variants stay ask
    assert pa.classify_command("python3 -c 'print(1)'") == "ask"
    assert pa.classify_command("python3 -m pip install x") == "ask"
    assert pa.classify_command("unzip a.zip -d out") == "ask"
    assert pa.classify_command("tar -xf a.tar") == "ask"
    assert pa.classify_command("cd /x && rm -rf /") == "ask"
    assert pa.classify_command("echo hi & whoami") == "ask"
    assert pa.classify_command("ls || whoami") == "ask"
    assert pa.classify_command("git remote add o x") == "ask"


def test_permission_cache():
    # an approved action never prompts again (exact-match, session-scoped)
    rt = mock_rt()
    rt.approved.clear()
    rt.on_permission = lambda d: True
    assert pa.tool_run_command(rt, "touch /tmp/alva-cache-demo").get("ok") is True
    rt.on_permission = lambda d: False
    _cached = pa.tool_run_command(rt, "touch /tmp/alva-cache-demo")
    assert _cached.get("ok") is True
    _notcached = pa.tool_run_command(rt, "touch /tmp/alva-cache-demo-2")
    assert _notcached.get("ok") is False and "permission" in str(_notcached.get("error", ""))
    rt.on_permission = None
    rt.approved.clear()


def test_ask_permission_keys():
    # prompt accepts the 'a' (always) key and denies on 'n'
    rt = mock_rt()
    _orig_input = builtins.input
    try:
        builtins.input = lambda *a, **k: "a"
        assert pa.ask_permission(rt, "run command: python3 -m pyflakes demo.py") is True
        builtins.input = lambda *a, **k: "n"
        assert pa.ask_permission(rt, "run command: python3 -m pyflakes demo2.py") is False
    finally:
        builtins.input = _orig_input
    rt.approved.clear()
    # request_permission feeds the session cache from the hook result
    rt.on_permission = lambda d: True
    assert pa.request_permission(rt, "run command: python3 -m pyflakes demo3.py") is True
    assert "run command: python3 -m pyflakes demo3.py" in rt.approved
    rt.on_permission = lambda d: False
    assert pa.request_permission(rt, "run command: python3 -m pyflakes demo3.py") is True
    rt.on_permission = None
    rt.approved.clear()


# ---------------- autonomy: files ------------------------------------------

def test_file_tools():
    rt = mock_rt()
    proj_test = os.path.join(rt.data_dir, "proj-demo.txt")
    w = pa.tool_file_write(rt, proj_test, "first line\nsecond")
    assert w.get("ok") is True and w.get("chars") == 17
    r = pa.tool_file_read(rt, proj_test)
    assert r.get("ok") is True and "first line" in r.get("content", "")
    e = pa.tool_file_edit(rt, proj_test, "first", "FIRST")
    assert e.get("ok") is True and e.get("replaced") == 1
    assert "FIRST line" in pa.tool_file_read(rt, proj_test)["content"]
    pa.tool_file_write(rt, proj_test, "a a a")
    e2 = pa.tool_file_edit(rt, proj_test, "a", "b")
    assert e2.get("ok") is True and e2.get("replaced") == 1 \
        and pa.tool_file_read(rt, proj_test)["content"] == "b a a"
    lst = pa.tool_file_list(rt, rt.data_dir)
    assert lst.get("ok") is True and any(x["name"] == "proj-demo.txt" for x in lst.get("entries", []))
    # file_search (glob)
    fs = pa.tool_file_search(rt, "proj-demo.txt", path=rt.data_dir)
    assert fs.get("ok") is True and fs.get("count", 0) >= 1
    fs2 = pa.tool_file_search(rt, "*.txt", path=rt.data_dir)
    assert fs2.get("ok") is True and any(m.get("path", "").endswith("proj-demo.txt") for m in fs2.get("matches", []))
    assert pa.tool_file_search(rt, "", path=rt.data_dir).get("ok") is False
    assert pa.tool_file_search(rt, "*.md", path="/no/such/dir").get("ok") is False
    # outside the data dir -> permission gate
    rt.on_permission = lambda d: False
    denied_w = pa.tool_file_write(rt, "/tmp/alva-outside-write.txt", "nope")
    assert denied_w.get("ok") is False and "permission" in str(denied_w.get("error", ""))
    denied_r = pa.tool_file_read(rt, "/etc/hostname")
    assert denied_r.get("ok") is False and "permission" in str(denied_r.get("error", ""))
    rt.on_permission = None


# ---------------- autonomy: skills -----------------------------------------

def test_skill_save_read_list():
    rt = mock_rt()
    sk = pa.skill_save(rt, "test-skill", "Always check the time before planning.")
    assert sk.get("ok") is True
    skill_names = [s.get("name") for s in (pa.skill_list(rt).get("skills") or [])]
    assert "test-skill" in skill_names
    sr = pa.skill_read(rt, "test-skill")
    assert sr.get("ok") is True and "check the time" in sr.get("content", "")
    assert pa.skill_read(rt, "missing-skill").get("ok") is False
    # categorized skills round-trip
    sc = pa.skill_save(rt, "cat-skill", "Steps go here.", category="productivity")
    assert sc.get("ok") is True and sc.get("category") == "productivity"
    scr = pa.skill_read(rt, "productivity/cat-skill")
    assert scr.get("ok") is True and scr.get("category") == "productivity"
    # skills work even with NO PyYAML installed (frontmatter fallback)
    saved_yaml = util_mod.yaml
    try:
        util_mod.yaml = None
        sf = pa.skill_save(rt, "no-yaml-skill",
                           "---\ndescription: parsed without yaml\ntags:\n  - a\n  - b\n---\nbody here")
        assert sf.get("ok") is True
        sfr = pa.skill_read(rt, "no-yaml-skill")
        assert sfr.get("ok") is True and sfr.get("description") == "parsed without yaml" \
            and sfr.get("tags") == ["a", "b"]
        pa.skill_remove(rt, "no-yaml-skill")
        # block-scalar frontmatter + inline arrays without PyYAML
        sf2 = pa.skill_save(rt, "block-scalar-skill",
                            "---\ndescription: >\n  Procedure the agent follows\n"
                            "  across folded lines\ntags: [alpha, beta]\nrelated_skills: []\n---\nbody")
        assert sf2.get("ok") is True
        sfr2 = pa.skill_read(rt, "block-scalar-skill")
        assert sfr2.get("description") == "Procedure the agent follows across folded lines"
        assert sfr2.get("tags") == ["alpha", "beta"]
        assert sfr2.get("related_skills") == []
        pa.skill_remove(rt, "block-scalar-skill")
    finally:
        util_mod.yaml = saved_yaml
    pa.skill_remove(rt, "test-skill")
    pa.skill_remove(rt, "productivity/cat-skill")


def test_skill_install():
    rt = mock_rt()
    _loc_skill = os.path.join(rt.data_dir, "loc-skill.md")
    with open(_loc_skill, "w", encoding="utf-8") as _f:
        _f.write("---\ndescription: A local skill\n---\nsteps here")
    _ri = pa.skill_install(rt, _loc_skill)
    assert _ri.get("ok") is True and _ri.get("name") == "loc-skill"
    _rr = pa.skill_read(rt, "loc-skill")
    assert _rr.get("ok") is True and _rr.get("description") == "A local skill"
    pa.skill_remove(rt, "loc-skill")
    os.remove(_loc_skill)
    # GitHub blob URL -> raw.githubusercontent.com + category
    _fetched = {}
    _saved_raw = skills_mod._raw_fetch
    skills_mod._raw_fetch = lambda u: (_fetched.__setitem__("url", u),
                                       "---\ndescription: Remote skill\n---\nbody")[1]
    try:
        _ru = pa.skill_install(
            rt, "https://github.com/alvaschul/skills/blob/main/skills/foo.md", "remote")
        assert _ru.get("ok") is True and _ru.get("name") == "foo" \
            and _ru.get("category") == "remote"
        assert _fetched.get("url") == \
            "https://raw.githubusercontent.com/alvaschul/skills/main/skills/foo.md"
    finally:
        skills_mod._raw_fetch = _saved_raw
    pa.skill_remove(rt, "remote/foo")
    # non-markdown page (HTML) is rejected, not imported
    assert pa._looks_like_html("<html><body>page</body></html>") is True
    assert pa._looks_like_html("---\ndescription: skill\n---\nbody") is False
    _saved_raw2 = skills_mod._raw_fetch
    skills_mod._raw_fetch = lambda u: None
    try:
        _rh = pa.skill_install(rt, "https://github.com/alvaschul/skills")
        assert _rh.get("ok") is False
    finally:
        skills_mod._raw_fetch = _saved_raw2


def test_skill_install_dispatch():
    rt = mock_rt()
    _disp = pa.dispatch_tool(rt, "skill_install", {"source": "/no/such/file.md"})
    assert _disp.get("ok") is False and "error" in _disp


def test_skill_sync_repo():
    rt = mock_rt()
    rt.approved.clear()
    _sync = pa.skill_sync_repo(rt, "https://example.com/skills.git")
    assert _sync.get("ok") is False and "permission" in str(_sync.get("error", ""))
    # fake a successful shallow clone and import every .md
    rt.on_permission = lambda d: True
    _rc_obj = type("RC", (), {"returncode": 0, "stderr": "", "stdout": ""})()
    _saved_run = subprocess.run

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

    subprocess.run = _fake_clone
    try:
        _sy = pa.skill_sync_repo(rt, "https://example.com/skills.git")
        assert _sy.get("ok") is True and _sy.get("count") == 2
        _cats = sorted({s.get("category") for s in _sy.get("installed", [])})
        assert _cats == ["prod", "research"]
        assert any(s == "README.md" for s in _sy.get("skipped", []))
    finally:
        subprocess.run = _saved_run
        rt.on_permission = None
    pa.skill_remove(rt, "prod/a")
    pa.skill_remove(rt, "research/b")


# ---------------- web + models ---------------------------------------------

def test_web_fetch():
    _mock_server()
    rt = mock_rt()
    wf = pa.tool_web_fetch(rt, BASE + "/mock-page")
    assert wf.get("ok") is True and wf.get("status") == 200
    assert "Mock Page" in wf.get("snippet", "")


def test_web_head():
    _mock_server()
    rt = mock_rt()
    _h = pa.tool_web_head(rt, BASE + "/mock-head")
    assert _h.get("ok") is True and _h.get("status") == 200
    assert "text/plain" in _h.get("content_type", "")
    assert _h.get("content_length") == str(len(b"mock head body\n"))
    # HEAD is rejected (501) -> GET fallback; redirects are followed and the
    # final URL reported
    _r = pa.tool_web_head(rt, BASE + "/mock-redirect")
    assert _r.get("ok") is True and _r.get("status") == 200
    assert _r.get("url", "").endswith("/mock-page")


def test_web_json():
    _mock_server()
    rt = mock_rt()
    _j = pa.tool_web_json(rt, BASE + "/mock-json")
    assert _j.get("ok") is True and _j.get("status") == 200
    assert _j.get("data", {}).get("status") == "ok"
    _sub = pa.tool_web_json(rt, BASE + "/mock-json", "items")
    assert _sub.get("data") == [1, 2, 3]
    _miss = pa.tool_web_json(rt, BASE + "/mock-json", "nope")
    assert _miss.get("ok") is False and "path" in _miss.get("error", "")


def test_web_markdown():
    _mock_server()
    rt = mock_rt()
    _m = pa.tool_web_markdown(rt, BASE + "/mock-markdown")
    assert _m.get("ok") is True and _m.get("status") == 200
    md = _m.get("markdown", "")
    assert "# Mock Markdown" in md
    assert "[Link Text](http://example.com)" in md
    assert "**markdown**" in md


def test_web_search():
    _mock_server()
    rt = mock_rt()
    _orig_req = tools_mod._web_req

    def _local_req(url, **kw):
        url = url.replace("https://lite.duckduckgo.com/lite/?q=",
                          BASE + "/mock-search?q=")
        return _orig_req(url, **kw)

    tools_mod._web_req = _local_req
    try:
        _s = pa.tool_web_search(rt, "mock query")
        assert _s.get("ok") is True and _s.get("count") == 2
        first = _s.get("results", [])[0]
        assert first.get("title") == "Mock Result One"
        assert first.get("url") == "http://example.com/one"
        assert "first snippet" in first.get("snippet", "")
        # num_results caps the returned list
        _one = pa.tool_web_search(rt, "mock query", 1)
        assert len(_one.get("results", [])) == 1
    finally:
        tools_mod._web_req = _orig_req


def test_web_download():
    _mock_server()
    rt = mock_rt()
    dest = os.path.join(rt.data_dir, "dl.bin")
    _d = pa.tool_web_download(rt, BASE + "/mock-download", dest)
    assert _d.get("ok") is True and _d.get("bytes") == len(b"\x00\x01MOCKBYTES\xff")
    with open(dest, "rb") as f:
        assert f.read() == b"\x00\x01MOCKBYTES\xff"


def test_web_post():
    _mock_server()
    rt = mock_rt()
    rt.on_permission = lambda desc: True
    _p = pa.tool_web_post(rt, BASE + "/mock-post", {"hello": "world"})
    assert _p.get("ok") is True and _p.get("status") == 200
    assert _p.get("response", {}).get("echo", {}).get("hello") == "world"
    # headless default denies outbound POSTs
    _deny = mkrt()
    _pd = pa.tool_web_post(_deny, BASE + "/mock-post", {})
    assert _pd.get("ok") is False and "permission" in _pd.get("error", "")


def test_fetch_models():
    _mock_server()
    rt = mock_rt()
    models = pa.fetch_models(rt, BASE + "/v1", "test-key")
    assert models == ["mock-model", "another-mock"]


def test_provider_profiles():
    st = pa._normalize_state({"provider": "groq", "base_url": "http://x/v1", "api_key": "k",
                              "model": "m", "temperature": 0.3})
    assert st["active"] == "groq" and "groq" in st["profiles"]
    assert pa.active_cfg(st)["api_key"] == "k"
    rt = mkrt()
    st2 = {"active": "a", "profiles": {"a": dict(pa.DEFAULT_CFG), "b": dict(pa.DEFAULT_CFG)}}
    rt.cfg = st2
    pa.cmd_provider(rt, "rm a")
    assert "a" not in st2["profiles"] and st2["active"] in st2["profiles"]
    pa.cmd_provider(rt, "b")
    assert st2["active"] == "b"


# ---------------- context tracking -----------------------------------------

def test_context_tracking():
    rt = mock_rt()
    est = pa.estimate_tokens("hello world")
    assert isinstance(est, int) and est >= 1
    tok, win = pa.context_usage(rt, [{"role": "user", "content": "hi"}])
    assert win == pa.DEFAULT_CONTEXT_WINDOW
    assert isinstance(tok, int) and tok > 0
    ctx_cfg = {"base_url": BASE + "/v1", "api_key": "test-key",
               "model": "mock-model", "temperature": 0.5}
    cfg2 = dict(ctx_cfg)
    cfg2["model"] = "gpt-4o"
    assert pa.context_window_for(cfg2) == 128000
    cfg3 = dict(ctx_cfg)
    cfg3["context_window"] = 4000
    assert pa.context_window_for(cfg3) == 4000
    assert pa._fmt_k(12345) == "12.3k"


# ---------------- sessions --------------------------------------------------

def test_sessions():
    rt = mock_rt()
    pa.save_session(rt, "test-sess", [{"role": "user", "content": "a"}])
    assert "test-sess" in pa.sessions_map(rt)
    assert len(pa.load_session(rt, "test-sess")) == 1
    assert pa.find_session(rt, "TEST-SESS") == "test-sess"
    assert pa.store_get(rt, pa.ACTIVE_SESSION_KEY) == "test-sess"
    pa.delete_session(rt, "test-sess")
    assert "test-sess" not in pa.sessions_map(rt)
    assert pa.auto_title("   hello   world  ") == "hello world"
    assert pa.unique_session_name(rt, "x") == "x"


def test_session_pruning():
    rt = mock_rt()
    for i in range(pa.MAX_SESSIONS + 5):
        pa.save_session(rt, "prune-%02d" % i, [{"role": "user", "content": "m"}])
    assert len(pa.sessions_map(rt)) <= pa.MAX_SESSIONS
    assert "prune-%02d" % (pa.MAX_SESSIONS + 4) in pa.sessions_map(rt)
    for i in range(pa.MAX_SESSIONS + 5):
        pa.delete_session(rt, "prune-%02d" % i)


def test_auto_compression():
    rt = mock_rt()
    ctx_cfg = {"base_url": BASE + "/v1", "api_key": "test-key",
               "model": "mock-model", "temperature": 0.5}
    big = [{"role": "user" if i % 2 == 0 else "assistant", "content": "m" * 4000}
           for i in range(12)]
    tiny = dict(ctx_cfg)
    tiny["context_window"] = 4000
    new, stats = pa.compress_history(rt, big, tiny, summarizer=lambda msgs, c: "SUMMARY")
    assert stats is not None and stats["dropped"] == 4 and stats["kept"] == 8
    assert new[0]["content"].startswith("[summary of earlier conversation]")
    assert new[0]["role"] == "user" and len(new) == 9
    small = [{"role": "user", "content": "hi"}]
    _, stats2 = pa.compress_history(rt, small, tiny, summarizer=lambda msgs, c: "S")
    assert stats2 is None
    _, stats3 = pa.compress_history(rt, big, ctx_cfg, summarizer=lambda msgs, c: "S")
    assert stats3 is None
    # structured summary sections are preserved verbatim
    def structured_summarizer(msgs, c):
        return "- GOALS: build a thing\n- DECISIONS: used sqlite\n- FACTS: user is Alex\n- ACTIONS: wrote code\n- OPEN: deploy it"
    new_struct, stats_struct = pa.compress_history(rt, big, tiny, summarizer=structured_summarizer)
    assert stats_struct is not None and "GOALS" in new_struct[0]["content"]
    assert all(s in new_struct[0]["content"] for s in ("DECISIONS", "FACTS", "ACTIONS", "OPEN"))
    # chatty preamble is stripped
    def chatty_summarizer(msgs, c):
        return "Here is your summary:\n- GOALS: x"
    new_chatty, _ = pa.compress_history(rt, big, tiny, summarizer=chatty_summarizer)
    assert not new_chatty[0]["content"].lower().startswith("here")
    # empty LLM result falls back to a marker summary
    newf, statsf = pa.compress_history(rt, big, tiny, summarizer=lambda msgs, c: "")
    assert statsf is not None and statsf["mode"] == "fallback" and len(newf) == 9
    # the compressed tail must never start mid-tool-sequence
    seq = [{"role": "user", "content": "u" * 4000}]
    for k in range(6):
        seq.append({"role": "assistant", "content": "a" * 2000, "tool_calls": [{"id": "c%d" % k}]})
        seq.append({"role": "tool", "tool_call_id": "c%d" % k, "content": "r" * 2000})
    newb, statsb = pa.compress_history(rt, seq, tiny, summarizer=lambda msgs, c: "S")
    assert statsb is not None and newb[1]["role"] != "tool"
    # the newest user message always survives
    big2 = big + [{"role": "user", "content": "FRESH QUESTION"}]
    new3, _ = pa.compress_history(rt, big2, tiny, summarizer=lambda msgs, c: "S")
    assert new3[-1]["content"] == "FRESH QUESTION"
    # trim_history protects a leading compression summary
    trimmed = [{"role": "user", "content": "[summary of earlier conversation]\nold stuff"}]
    for k in range(130):
        trimmed.append({"role": "user", "content": "msg %d" % k})
    pa.trim_history(trimmed)
    assert trimmed[0]["content"].startswith("[summary") and len(trimmed) == 121


def test_key_parsing():
    assert pa.parse_key("", "old") == "old"
    assert pa.parse_key("none", "old") == ""
    assert pa.parse_key("clear", "old") == ""
    assert pa.parse_key("sk-123", "old") == "sk-123"
    assert pa.FIRST_RUN_CFG["api_key"] == ""
    first = pa._normalize_state({})
    assert first["active"] == "default" and first["profiles"]["default"]["api_key"] == ""


# ---------------- full agent loop vs the mock server -----------------------

def test_agent_loop_mock():
    _mock_server()
    rt = mock_rt()
    history = [{"role": "user", "content": "use all your tools"}]
    res1 = json.loads(pa.run_agent(rt, json.dumps(history)))
    print("  [final answer] " + str(res1.get("content", ""))[:220])
    assert "AGENT_LOOP_OK" in str(res1.get("content", ""))
    assert isinstance(res1.get("history"), list) and len(res1["history"]) >= 10
    # persisted todos / memory from the loop
    with open(os.path.join(rt.data_dir, "store.json")) as _fh:
        store = json.load(_fh)
    assert any(t.get("text") == "buy milk" for t in store.get("alvaagent.todos", []))
    assert store.get("alvaagent.mem.name") == "Alex"
    # observability: the loop wrote trace entries into this runtime's trace.log
    lines = pa.read_trace(rt, 500)
    assert len(lines) > 0 and '"ts"' in "".join(lines)
    assert any('"event": "turn_start"' in ln for ln in lines)
    assert any('"event": "tool"' in ln for ln in lines)
    assert any('"event": "turn_end"' in ln for ln in lines)
    assert pa.trace_count(rt) >= len(lines)


def test_plain_path():
    _mock_server()
    rt = mock_rt()
    history2 = [{"role": "user", "content": "[plain] say hi"}]
    res2 = json.loads(pa.run_agent(rt, json.dumps(history2)))
    assert "PLAIN_OK" in str(res2.get("content", ""))


def test_self_test_harness():
    rt = mock_rt()
    results = json.loads(pa.self_test(rt))
    assert all(v is True for v in results.values()), json.dumps(results)


def test_atomic_store_writes():
    rt = mkrt()
    rt.store["alvaagent.todos"] = [{"text": "atomic test", "done": False}]
    pa.store_save(rt)
    sp = os.path.join(rt.data_dir, "store.json")
    assert os.path.exists(sp)
    try:
        with open(sp) as _fh:
            _reloaded = json.load(_fh)
        assert isinstance(_reloaded, dict)
    except Exception as e:
        raise AssertionError("store.json corrupted: %s" % e)
    # back-to-back saves must not corrupt (temp+rename is atomic on POSIX)
    rt.store["alvaagent.mem.x"] = "v1"
    pa.store_save(rt)
    rt.store["alvaagent.mem.x"] = "v2"
    pa.store_save(rt)
    with open(sp) as _fh:
        _reloaded2 = json.load(_fh)
    assert _reloaded2.get("alvaagent.mem.x") == "v2"
    _leftover = [f for f in os.listdir(rt.data_dir)
                 if f.startswith(".store.") or f.startswith(".tmp.") or f.endswith(".tmp")]
    assert not _leftover, _leftover
    # _atomic_write helper works
    _ap = os.path.join(rt.data_dir, "_atomic_probe.txt")
    pa._atomic_write(_ap, "hello")
    assert open(_ap).read() == "hello"
    os.remove(_ap)


def test_main_no_sig_dfl():
    # main() must never bind SIGINT to SIG_DFL - that skips KeyboardInterrupt
    # handling and the alt-screen _cleanup() on Ctrl+C
    _main_src = inspect.getsource(pa.main)
    assert "signal.signal(pa.signal.SIGINT, pa.signal.SIG_DFL)" not in _main_src


# ---------------- performance: streaming -----------------------------------

def test_chunked_sse():
    rt = mkrt()
    fake_sse = (
        "data: {\"choices\":[{\"delta\":{\"content\":\"Hello\"}}]}\n\n"
        "data: {\"choices\":[{\"delta\":{\"content\":\" world\"}}]}\n\n"
        "data: [DONE]\n\n"
    )
    _orig_urlopen = urllib.request.urlopen
    urllib.request.urlopen = lambda req, timeout=180: _FakeResp(fake_sse)
    try:
        cfg_s = _cfg()
        out = "".join(c for c, _ in pa.chat_completion_stream(
            rt, [{"role": "user", "content": "hi"}], cfg_s))
        assert out == "Hello world"
    finally:
        urllib.request.urlopen = _orig_urlopen


def test_tool_call_ids():
    rt = mkrt()
    _orig_urlopen = urllib.request.urlopen
    cfg_s = _cfg()
    # id must not be concatenated across repeated deltas
    id_sse = (
        "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"call_abc\","
        "\"function\":{\"name\":\"calculator\",\"arguments\":\"\"}}]}}]}\n\n"
        "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"call_abc\","
        "\"function\":{\"arguments\":\"{\\\"expression\\\":\\\"2+2\\\"}\"}}]}}]}\n\n"
        "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"tool_calls\"}]}\n\n"
    )
    urllib.request.urlopen = lambda req, timeout=180: _FakeResp(id_sse)
    try:
        _events = list(pa.chat_completion_stream(rt, [{"role": "user", "content": "calc"}], cfg_s))
        _tc = [tc for _, tcs in _events for tc in (tcs or [])]
        assert len(_tc) == 1 and _tc[0]["id"] == "call_abc"
    finally:
        urllib.request.urlopen = _orig_urlopen
    # no id in stream -> stable synthetic id
    noid_sse = (
        "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,"
        "\"function\":{\"name\":\"calculator\",\"arguments\":\"{\\\"expression\\\":\\\"1\\\"}\"}}]}}]}\n\n"
        "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"tool_calls\"}]}\n\n"
    )
    urllib.request.urlopen = lambda req, timeout=180: _FakeResp(noid_sse)
    try:
        _events = list(pa.chat_completion_stream(rt, [{"role": "user", "content": "calc"}], cfg_s))
        _tc = [tc for _, tcs in _events for tc in (tcs or [])]
        assert len(_tc) == 1 and _tc[0]["id"] == "call_0"
    finally:
        urllib.request.urlopen = _orig_urlopen


def test_plain_json_fallback():
    # gateways that ignore "stream": true answer with a plain JSON completion;
    # the stream reader must parse the body instead of crashing.
    rt = mkrt()
    cfg_s = _cfg()
    _orig_urlopen = urllib.request.urlopen
    _plain_msg = {"choices": [{"message": {"role": "assistant",
                                           "content": "plain-json-reply"},
                               "finish_reason": "stop"}]}
    for _label, _body in (("minified", json.dumps(_plain_msg)),
                          ("pretty", json.dumps(_plain_msg, indent=2))):
        def _fake_plain(req, timeout=180, _b=_body):
            return _FakeResp(_b)
        urllib.request.urlopen = _fake_plain
        try:
            _events = list(pa.chat_completion_stream(
                rt, [{"role": "user", "content": "hi"}], cfg_s))
            _text = "".join(c for c, _ in _events)
            assert _text == "plain-json-reply", "plain-JSON (%s) fallback" % _label
        finally:
            urllib.request.urlopen = _orig_urlopen


# ---------------- UX: dead turns -------------------------------------------

def test_dead_turn_no_ghost_messages():
    # failed/empty turns must not persist ghost user messages
    rt = mock_rt()
    rt.session = "default"
    _save_calls = []
    _orig = {
        "active_cfg": repl_mod.active_cfg,
        "run_agent_tui": repl_mod.run_agent_tui,
        "compress_now": repl_mod.compress_now,
        "render_agent_panel": repl_mod.render_agent_panel,
        "render_status_bar": repl_mod.render_status_bar,
        "print_user_turn": repl_mod.print_user_turn,
        "context_usage": repl_mod.context_usage,
        "save_session": repl_mod.save_session,
    }
    repl_mod.active_cfg = lambda st: {"auto_compress": False, "temperature": 0.7,
                                      "base_url": "http://x/v1", "api_key": "k", "model": "m"}
    repl_mod.compress_now = lambda *a, **k: False
    repl_mod.render_agent_panel = lambda *a, **k: None
    repl_mod.render_status_bar = lambda *a, **k: None
    repl_mod.print_user_turn = lambda *a, **k: None
    repl_mod.context_usage = lambda *a, **k: (0, 128000)
    repl_mod.save_session = lambda rt, name, msgs: _save_calls.append((name, [dict(m) for m in msgs]))

    def _fake_run(res):
        repl_mod.run_agent_tui = lambda rt, history: res

    # failed request: the unanswered user message is dropped
    _sess_hist = [{"role": "user", "content": "helo"}]
    _failed_res = {"content": "error: LLM API unreachable: boom",
                   "history": [{"role": "system", "content": "s"}] + _sess_hist,
                   "cancelled": False, "streamed": False, "tools": 0}
    _fake_run(_failed_res)
    rt.history = _sess_hist
    _save_calls[:] = []
    repl_mod.send_message(rt, "helo")
    assert all(m.get("role") != "user" for m in _sess_hist)

    # empty response: drops the user message and the empty assistant ghost
    _empty_hist = [{"role": "user", "content": "helo"},
                   {"role": "assistant", "content": ""}]
    _empty_res = {"content": "", "history": [{"role": "system", "content": "s"}] + _empty_hist,
                  "cancelled": False, "streamed": False, "tools": 0}
    _fake_run(_empty_res)
    _sess_hist = [{"role": "user", "content": "helo"},
                  {"role": "assistant", "content": ""}]
    rt.history = _sess_hist
    repl_mod.send_message(rt, "helo")
    assert _sess_hist == []

    # successful turn keeps user + assistant messages
    _good_hist = [{"role": "user", "content": "helo"}]
    _good_res = {"content": "Hey!",
                 "history": [{"role": "system", "content": "s"}] + _good_hist +
                 [{"role": "assistant", "content": "Hey!"}],
                 "cancelled": False, "streamed": False, "tools": 0}
    _fake_run(_good_res)
    rt.history = _good_hist
    repl_mod.send_message(rt, "helo")
    assert len(_good_hist) == 2 and _good_hist[0]["role"] == "user" \
        and _good_hist[1]["content"] == "Hey!"

    for _k, _v in _orig.items():
        setattr(repl_mod, _k, _v)


# ---------------- resilience: retry/backoff --------------------------------

def test_retry_backoff():
    rt = mkrt()
    _orig_sleep = client_mod._sleep_retry
    _orig_urlopen = urllib.request.urlopen
    client_mod._sleep_retry = lambda a: None
    _attempts = [0]
    _good = json.dumps({"choices": [{"message": {"role": "assistant", "content": "retried-ok"}}]}).encode("utf-8")

    def _flaky(req, timeout=180):
        _attempts[0] += 1
        if _attempts[0] <= 2:
            raise urllib.error.URLError("transient outage")
        return _Resp(200, _good)

    urllib.request.urlopen = _flaky
    try:
        _d = pa.chat_completion(rt, [{"role": "user", "content": "hi"}], _cfg())
        assert _attempts[0] == 3 and _d["choices"][0]["message"]["content"] == "retried-ok"
    finally:
        urllib.request.urlopen = _orig_urlopen
    _attempts[0] = 0

    def _perm(req, timeout=180):
        _attempts[0] += 1
        return _Resp(400, json.dumps({"error": {"message": "nope"}}).encode("utf-8"))

    urllib.request.urlopen = _perm
    try:
        try:
            pa.chat_completion(rt, [{"role": "user", "content": "hi"}], _cfg())
            _raised = False
        except RuntimeError:
            _raised = True
        assert _raised and _attempts[0] == 1
    finally:
        urllib.request.urlopen = _orig_urlopen
        client_mod._sleep_retry = _orig_sleep


# ---------------- UX: rendering --------------------------------------------

def test_agent_render_code():
    _wbuf = io.StringIO()
    with contextlib.redirect_stdout(_wbuf):
        _w = pa.AgentWriter(pa.SKINS["midnight"], pa.SKINS["midnight"]["agent"])
        _w.feed("Here:\n\n```python\na = 1\n```\n\n```\necho hi\n```\nok\n")
        _w.close()
    _raw = _wbuf.getvalue()
    assert "```" not in _raw
    assert "─ python" in _raw and "─ code" in _raw
    assert all(ch not in _raw for ch in "╭╮╰╯")
    assert "▍ " in _raw
    assert "  a = 1" in _raw


def test_xml_tool_calls():
    _xml = ("<tool_call>\n<function=calculator>\n<parameter=expression>6*7</parameter>\n"
            "</function>\n</tool_call>")
    _parsed = pa._parse_xml_tool_calls("Let me compute:\n" + _xml)
    assert _parsed == [("calculator", {"expression": "6*7"})]
    assert pa._parse_xml_tool_calls("no calls here") == []
    _stripped = pa._strip_xml("hi <think>secret</think> bye " + _xml + " end")
    assert "think" not in _stripped and "secret" not in _stripped and "6*7" not in _stripped \
        and "hi" in _stripped and "end" in _stripped
    _stray = pa._strip_xml("The user asks...\n</think>\n" + _xml + "\nnext")
    assert "</think>" not in _stray and "tool_call" not in _stray \
        and "The user asks..." in _stray and "next" in _stray
    # AgentWriter hides XML blocks even when they split across chunks
    _wbuf3 = io.StringIO()
    with contextlib.redirect_stdout(_wbuf3):
        _w3 = pa.AgentWriter(pa.SKINS["midnight"], pa.SKINS["midnight"]["agent"])
        for _chunk in ["Let me compute:\n<tool_ca", "ll>\n<function=calculator>\n<parameter=expression>6*7</parameter>\n"
                       "</function>\n</tool_call>\n", "<think>\nhmm\n</think>\n", "42 ok\n"]:
            _w3.feed(_chunk)
        _w3.close()
    _raw3 = _wbuf3.getvalue()
    assert "<tool_call>" not in _raw3 and "<function" not in _raw3 and "6*7" not in _raw3 \
        and "<think>" not in _raw3 and "hmm" not in _raw3
    assert "Let me compute:" in _raw3 and "42 ok" in _raw3
    # stray </think> with no opening tag (reasoning models) must not render
    _wbuf4 = io.StringIO()
    with contextlib.redirect_stdout(_wbuf4):
        _w4 = pa.AgentWriter(pa.SKINS["midnight"], pa.SKINS["midnight"]["agent"])
        for _chunk4 in ["Reasoning about it.\n", "</think>\n", "<tool_call>\n<function=calculator>\n",
                        "<parameter=expression>2+2</parameter>\n</function>\n</tool_call>\n", "ok\n"]:
            _w4.feed(_chunk4)
        _w4.close()
    _raw4 = _wbuf4.getvalue()
    assert "</think>" not in _raw4 and "<tool_call>" not in _raw4 and "2+2" not in _raw4
    assert "Reasoning about it." in _raw4 and "ok" in _raw4


def test_markdown_ansi():
    _old_color = tui_mod.COLOR
    try:
        tui_mod.COLOR = True
        _sk5 = pa.SKINS["midnight"]
        _rend, _ = pa._md_line("**bold** *italic* _it_ __also__ ~~gone~~", _sk5)
        assert "\x1b[1m" in _rend and "\x1b[3m" in _rend and "\x1b[9m" in _rend \
            and "*" not in _rend and "_" not in _rend and "~" not in _rend
        _rend2, _ = pa._md_line("nested **bold *italic* rest** end", _sk5)
        assert "\x1b[1m" in _rend2 and "\x1b[1;3m" in _rend2 and "**" not in _rend2 \
            and "*italic*" not in _rend2
        _rend3, _ = pa._md_line("use `cmd -x` here", _sk5)
        assert "cmd -x" in _rend3 and "`" not in _rend3 and _sk5["code"] in _rend3
        _rend4, _ = pa._md_line("a * b * c", _sk5)
        assert _rend4 == "a * b * c"
        _rend5, _ = pa._md_line("***both***", _sk5)
        assert "\x1b[1;3m" in _rend5
    finally:
        tui_mod.COLOR = _old_color
    assert pa._md_line("**raw**", pa.SKINS["midnight"])[0] == "**raw**"

    _old_color2 = tui_mod.COLOR
    try:
        tui_mod.COLOR = True
        _sk6 = pa.SKINS["midnight"]
        _r1, _p1 = pa._md_line("Some **bo", _sk6)
        _r2, _p2 = pa._md_line(_p1 + "ld** text", _sk6)
        assert _p1 == "**bo" and _r1 == "Some " and _p2 == "" and "\x1b[1m" in _r2 \
            and "**" not in _r2 and "**bo" not in _r2
        _wbuf5 = io.StringIO()
        with contextlib.redirect_stdout(_wbuf5):
            _w5 = pa.AgentWriter(_sk6, _sk6["agent"])
            for _chunk5 in ["Result **4", "2**.\n", "## Head\n", "- [x] done *it*\n",
                            "- [ ] todo\n", "- plain\n", "> quote\n", "---\n"]:
                _w5.feed(_chunk5)
            _w5.close()
        _raw5 = _wbuf5.getvalue()
        assert "\x1b[1m42\x1b[0m" in _raw5 and "**" not in _raw5
        assert "\x1b[38;5;81mHead" in _raw5 and "## " not in _raw5
        assert "\x1b[38;5;114m✓ " in _raw5 and "\x1b[38;5;244m☐ " in _raw5
        assert "\x1b[38;5;45m• " in _raw5
        assert "\x1b[38;5;240m│ " in _raw5
        assert "─" * 8 in _raw5 and "---" not in _raw5
        assert "*it*" not in _raw5 and "\x1b[3m" in _raw5
    finally:
        tui_mod.COLOR = _old_color2


def test_xml_stream_end_to_end():
    # end-to-end: an XML tool_call stream gets executed and fed back
    rt = mock_rt()
    _orig_urlopen = urllib.request.urlopen
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

    urllib.request.urlopen = _fake_xml_urlopen
    try:
        _evs = list(pa.run_agent_stream(rt, [{"role": "user", "content": "calc"}]))
        _ts = [e for k, e in _evs if k == "tool_start"]
        _te = [e for k, e in _evs if k == "tool_end"]
        _dn = [e for k, e in _evs if k == "done"][0]
        assert len(_ts) == 1 and _ts[0]["name"] == "calculator" \
            and _ts[0]["args"] == {"expression": "6*7"}
        assert len(_te) == 1 and _te[0]["status"] == "done" \
            and _te[0]["result"].get("result") == 42
        assert _dn["content"] == "The answer is 42." and "tool_call" not in _dn["content"]
    finally:
        urllib.request.urlopen = _orig_urlopen


def test_spinner():
    _sp = pa.Spinner("thinking")
    _sp.start()
    _sp.disable()
    assert _sp._dead is True
    _sp.stop()
    _sp2 = pa.Spinner("thinking")
    _sp2.start()
    _sp2.disable()
    _sp2.stop()
    assert _sp2._dead is True


def test_history_persistence():
    rt = mkrt()
    _htmp = os.path.join(rt.data_dir, "_hist_probe.txt")
    _saved_hp = repl_mod.HISTORY_PATH
    _saved_chp = config_mod.HISTORY_PATH
    repl_mod.HISTORY_PATH = _htmp
    config_mod.HISTORY_PATH = _htmp
    try:
        repl_mod.setup_completion()           # fresh load (no file yet)
        readline.add_history("/provider add")
        readline.add_history("remember my name is Alex")
        readline.add_history("/help")
        repl_mod.save_completion_history()
        assert os.path.exists(_htmp)
        # simulate a restart: re-load from disk
        repl_mod.setup_completion()
        assert readline.get_current_history_length() == 3
        # calling setup_completion again must not duplicate
        repl_mod.setup_completion()
        assert readline.get_current_history_length() == 3
    finally:
        repl_mod.HISTORY_PATH = _saved_hp
        config_mod.HISTORY_PATH = _saved_chp


def test_error_hints():
    rt = mkrt()
    _hint_ftp = pa.dispatch_tool(rt, "web_fetch", {"url": "ftp://x"})
    assert _hint_ftp.get("error") and "http" in _hint_ftp.get("error", "") \
        and "hint" in _hint_ftp
    _hint_todo = pa.dispatch_tool(rt, "todo_toggle", {"index": 999})
    assert _hint_todo.get("ok") is False and "hint" in _hint_todo
    _hint_calc = pa.dispatch_tool(rt, "calculator", {"expression": "1/0"})
    assert _hint_calc.get("error") and "hint" in _hint_calc
    _hint_unknown = pa.dispatch_tool(rt, "nope", {})
    assert _hint_unknown.get("error") and "unknown tool" in _hint_unknown.get("error", "")


def test_mock_server_recovers_stale():
    # An interrupted run can leave an untracked mock server still bound to
    # PORT. _mock_server must recover from that: kill the stray and start
    # fresh (or, when the stray-killer is unavailable, adopt the healthy
    # stray) - never raise "exited early".
    global _kill_strays
    _stop_server()

    def _spawn_orphan():
        return subprocess.Popen(
            [sys.executable, MOCK, str(PORT)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def _wait_ready(p):
        deadline = time.time() + 10
        while time.time() < deadline and p.poll() is None:
            rlist, _, _ = select.select([p.stdout], [], [], 0.2)
            if rlist and "READY" in p.stdout.readline():
                return True
        return p.poll() is None

    def _alive():
        try:
            with urllib.request.urlopen(BASE + "/models", timeout=1) as r:
                return r.getcode() == 200
        except Exception:
            return False

    # Path 1: stray-killer works -> stray killed, fresh server starts
    _orphan = _spawn_orphan()
    _server["proc"] = None
    try:
        assert _wait_ready(_orphan), "orphan failed to start"
        _mock_server()
        assert _alive()
    finally:
        _orphan.kill()
    # Path 2: stray-killer unavailable -> healthy orphan adopted, not crashed
    _stop_server()
    _orphan = _spawn_orphan()
    _server["proc"] = None
    _orig_kill = _kill_strays
    try:
        assert _wait_ready(_orphan), "orphan failed to start"
        _kill_strays = lambda: None
        _mock_server()
        assert _alive()
        assert _server["proc"] is not None and _server["proc"].poll() is None
    finally:
        _kill_strays = _orig_kill
        _orphan.kill()
        _server["proc"] = None


# ---------------- resilience: circuit breaker + timeouts -------------------

def test_circuit_breaker():
    # a tool that fails every time stops the loop early (not MAX_STEPS)
    rt = mock_rt()
    _chat_calls = []

    def _fail_dispatch(rt, name, args):
        return {"error": "boom"}

    def _tool_chat(rt, messages, config, tools=None):
        _chat_calls.append(1)
        return {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "t%d" % len(_chat_calls), "type": "function",
             "function": {"name": "get_time", "arguments": "{}"}}]}}]}

    _orig_dispatch = agent_mod.dispatch_tool
    _orig_chat = agent_mod.chat_completion
    _orig_timeout = agent_mod._TURN_TIMEOUT
    agent_mod.dispatch_tool = _fail_dispatch
    agent_mod.chat_completion = _tool_chat
    try:
        _breaker = json.loads(pa.run_agent(rt, json.dumps([{"role": "user", "content": "retry forever"}])))
        assert "stopped early" in str(_breaker.get("content", ""))
        assert len(_chat_calls) == agent_mod._MAX_CONSEC_TOOL_FAILURES
    finally:
        agent_mod.dispatch_tool = _orig_dispatch
        agent_mod.chat_completion = _orig_chat
        agent_mod._TURN_TIMEOUT = _orig_timeout
    assert any("circuit_breaker" in ln for ln in pa.read_trace(rt, 50))


def test_turn_timeout():
    # a turn past the wall-clock budget stops without another API call
    rt = mock_rt()
    _chat_calls = []

    def _tool_chat(rt, messages, config, tools=None):
        _chat_calls.append(1)
        return {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "t%d" % len(_chat_calls), "type": "function",
             "function": {"name": "get_time", "arguments": "{}"}}]}}]}

    _orig_chat = agent_mod.chat_completion
    _orig_timeout = agent_mod._TURN_TIMEOUT
    agent_mod._TURN_TIMEOUT = 0
    agent_mod.chat_completion = _tool_chat
    try:
        _timed = json.loads(pa.run_agent(rt, json.dumps([{"role": "user", "content": "slow"}])))
        assert "time budget" in str(_timed.get("content", ""))
        assert len(_chat_calls) == 0
    finally:
        agent_mod.chat_completion = _orig_chat
        agent_mod._TURN_TIMEOUT = _orig_timeout
    assert any("timeout" in ln for ln in pa.read_trace(rt, 50))


def test_stream_breaker():
    # same guarantee on the streaming path used by the real TUI
    rt = mock_rt()
    _fail_sse = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_fail",'
        '"function":{"name":"calculator","arguments":"{\\"expression\\":\\"1\\"}"}}]}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
    )
    _sse_calls = []

    def _fail_dispatch(rt, name, args):
        return {"error": "boom"}

    def _fail_urlopen(req, timeout=180):
        _sse_calls.append(1)
        return _FakeResp(_fail_sse)

    _orig_dispatch = agent_mod.dispatch_tool
    _orig_urlopen = urllib.request.urlopen
    agent_mod.dispatch_tool = _fail_dispatch
    urllib.request.urlopen = _fail_urlopen
    try:
        _evs = list(pa.run_agent_stream(rt, [{"role": "user", "content": "loop"}]))
        _dn = [e for k, e in _evs if k == "done"][0]
        assert "stopped early" in str(_dn.get("content", ""))
        assert len(_sse_calls) == agent_mod._MAX_CONSEC_TOOL_FAILURES
    finally:
        agent_mod.dispatch_tool = _orig_dispatch
        urllib.request.urlopen = _orig_urlopen
    assert any("circuit_breaker" in ln for ln in pa.read_trace(rt, 50))


def test_stream_timeout():
    rt = mock_rt()
    _fail_sse = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_fail",'
        '"function":{"name":"calculator","arguments":"{\\"expression\\":\\"1\\"}"}}]}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
    )
    _sse_calls = []

    def _fail_urlopen(req, timeout=180):
        _sse_calls.append(1)
        return _FakeResp(_fail_sse)

    _orig_urlopen = urllib.request.urlopen
    _orig_timeout = agent_mod._TURN_TIMEOUT
    agent_mod._TURN_TIMEOUT = 0
    urllib.request.urlopen = _fail_urlopen
    try:
        _evs = list(pa.run_agent_stream(rt, [{"role": "user", "content": "slow"}]))
        _dn = [e for k, e in _evs if k == "done"][0]
        assert "time budget" in str(_dn.get("content", ""))
        assert len(_sse_calls) == 0
    finally:
        urllib.request.urlopen = _orig_urlopen
        agent_mod._TURN_TIMEOUT = _orig_timeout
    assert any("timeout" in ln for ln in pa.read_trace(rt, 50))


# ---------------- run_python: sandboxed subprocess tool --------------------

def test_run_python():
    rt = mock_rt()
    _rp = pa.dispatch_tool(rt, "run_python", {"code": "print(6*7)"})
    assert _rp.get("ok") is True and "42" in _rp.get("output", "")
    _rp2 = pa.dispatch_tool(rt, "run_python", {"code": "import os; print(os.getcwd())"})
    assert _rp2.get("ok") is False and "permission" in _rp2.get("error", "")
    assert pa.classify_python("x = [i*i for i in range(10)]") == "allow"
    assert pa.classify_python("import shutil; shutil.rmtree('/x')") == "ask"
    assert pa.classify_python("open('/sdcard/x', 'w')") == "ask"
    assert pa.classify_python("print(eval('2+2'))") == "ask"
    # infinite loop: killed by the timeout (not a hung agent)
    _orig_py_to = tools_mod._PY_RUN_TIMEOUT
    tools_mod._PY_RUN_TIMEOUT = 1
    try:
        _t0 = time.monotonic()
        _rp3 = pa.dispatch_tool(rt, "run_python", {"code": "while True: pass"})
        _dt3 = time.monotonic() - _t0
        assert _rp3.get("ok") is False and "timed out" in _rp3.get("error", "") and _dt3 < 15
    finally:
        tools_mod._PY_RUN_TIMEOUT = _orig_py_to
    # output flood: killed by the byte cap
    _orig_py_max = tools_mod._PY_MAX_BYTES
    tools_mod._PY_MAX_BYTES = 1024
    try:
        _rp4 = pa.dispatch_tool(rt, "run_python", {"code": "print('x' * 100000)"})
        assert _rp4.get("ok") is False and "cap" in _rp4.get("error", "")
    finally:
        tools_mod._PY_MAX_BYTES = _orig_py_max
    # stdout truncation for sane-but-large outputs
    _rp5 = pa.dispatch_tool(rt, "run_python", {"code": "print('a' * 9000)"})
    assert _rp5.get("ok") is True and "... (truncated)" in _rp5.get("output", "")


# ---------------- tiered tool selection ------------------------------------

def test_tiered_tools():
    rt = mkrt()
    _saved_mode = rt.tool_mode
    try:
        rt.tool_mode = "core"
        _core = pa.visible(rt)
        _core_names = {t["function"]["name"] for t in _core}
        assert 0 < len(_core) < len(pa.TOOLS)
        assert "run_command" in _core_names and "calculator" in _core_names
        assert "web_search" in _core_names and "web_head" in _core_names
        assert "web_download" not in _core_names and "web_post" not in _core_names
        assert "skill_list" not in _core_names and "self_test" not in _core_names
        rt.tool_mode = "full"
        assert len(pa.visible(rt)) == len(pa.TOOLS)
    finally:
        rt.tool_mode = _saved_mode
    # lazy auto-enable: an advanced tool call flips the mode to full (one-way)
    _saved_mode = rt.tool_mode
    _orig_st = tools_mod.tool_self_test
    tools_mod.tool_self_test = lambda rt: {"ok": True, "tests": [], "all_passed": True}
    try:
        rt.tool_mode = "core"
        _r = pa.dispatch_tool(rt, "self_test", {})
        assert rt.tool_mode == "full"
        assert "Advanced tool set enabled" in _r.get("hint", "")
    finally:
        tools_mod.tool_self_test = _orig_st
        rt.tool_mode = _saved_mode


def test_cmd_trace():
    rt = mkrt()
    pa.trace(rt, {"event": "probe", "detail": "x"})
    _buf = io.StringIO()
    with contextlib.redirect_stdout(_buf):
        pa.cmd_trace(rt, "3")
    assert bool(_buf.getvalue().strip())


# ---------------- architecture: rt-first facade (plan Task 15 step 4) ------

_FACADE_SURFACE = (
    "Runtime", "build_runtime", "Tools", "dispatch_tool", "TOOLS",
    "SKINS", "AgentWriter", "Spinner",
    "chat_completion", "chat_completion_stream", "run_agent", "run_agent_stream",
    "classify_command", "classify_python", "load_session", "main",
    "context_usage", "compress_history", "sessions_map", "read_trace",
    "trace_count", "trace", "skill_list", "skill_save", "store_save",
    "store_get", "store_set", "visible", "set_mode", "sync_tool_mode",
    "request_permission", "ask_permission", "save_session",
)


def test_facade_surface():
    for name in _FACADE_SURFACE:
        assert hasattr(pa, name), "facade missing %r" % name


def test_no_retired_globals():
    for name in ("default_rt", "_get_rt", "_APPROVED_SET", "ON_PERMISSION",
                 "ON_TOOL", "_cancel_flag", "_last_turn", "_TOOLS_MODE",
                 "_UI", "_store", "_TRACE_PATH", "_read_trace", "_trace_count",
                 "tool_skill_list", "tool_skill_save", "tool_skill_read",
                 "tool_skill_remove", "tool_skill_install", "tool_skill_sync_repo"):
        assert not hasattr(pa, name), "retired global %r leaked onto the facade" % name


def test_no_facade_class():
    assert "_Facade" not in dir(pa)


def test_no_import_cycles():
    # both import orders must work, and every leaf must import standalone
    for order in (("import alvaagent", "import alvaagent_tui"),
                  ("import alvaagent_tui", "import alvaagent")):
        code = "import sys\n" + "\n".join(order) + "\nprint('OK')\n"
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True)
        assert r.returncode == 0 and "OK" in r.stdout, r.stderr
    for mod in ("alvaagent.context", "alvaagent.config", "alvaagent.store",
                "alvaagent.permissions", "alvaagent.sessions", "alvaagent.trace",
                "alvaagent.skills", "alvaagent.tools", "alvaagent.client",
                "alvaagent.agent", "alvaagent.tui", "alvaagent.commands",
                "alvaagent.repl", "alvaagent.util"):
        r = subprocess.run([sys.executable, "-c", "import %s" % mod],
                           capture_output=True, text=True)
        assert r.returncode == 0, (mod, r.stderr)


def test_cli_smoke():
    # `python3 -m alvaagent` boots and exits cleanly on EOF stdin. Runs with
    # ALVA_DATA_DIR pointed at a temp dir so the REPL never touches the real
    # ~/.alvaagent store/sessions.
    _tmp = tempfile.mkdtemp(prefix="alva_cli_")
    _TMP_DIRS.append(_tmp)
    _env = dict(os.environ)
    _env["ALVA_DATA_DIR"] = _tmp
    r = subprocess.run([sys.executable, "-m", "alvaagent"],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True,
                       timeout=30, env=_env)
    assert r.returncode == 0, (r.returncode, r.stdout[-500:], r.stderr[-500:])


# ---------------- bundled runner (plan Task 15 step 1) ---------------------

def _run_all():
    tests = sorted(n for n in globals().keys() if n.startswith("test_") and callable(globals()[n]))
    failures = 0
    for name in tests:
        fn = globals()[name]
        try:
            fn()
            print("  ok  - %s" % name)
        except Exception as e:
            failures += 1
            print("  FAIL - %s: %s: %s" % (name, type(e).__name__, e))
    print("\nALL TESTS PASSED ✓" if failures == 0 else "\n%d TEST(S) FAILED ✗" % failures)
    return 0 if failures == 0 else 1


def _cleanup():
    _stop_server()
    for d in list(_TMP_DIRS):
        shutil.rmtree(d, ignore_errors=True)


def _on_exit():
    try:
        _cleanup()
    except Exception:
        pass


if __name__ == "__main__":
    atexit.register(_on_exit)
    try:
        _mock_server()
        print("[mock server ready]")
        code = _run_all()
    finally:
        _cleanup()
    sys.exit(code)
