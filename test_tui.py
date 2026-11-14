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
never collide. The mock LLM server is started lazily on an OS-assigned port,
owned by this process, reused for the suite, and stopped during cleanup.
"""
import atexit
import builtins
import contextlib
import hashlib
import inspect
import io
import json
import os
import pty
import readline
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = None
BASE = None
MOCK = os.path.join(ROOT, "mock_llm_server.py")

import alvaagent as pa
from alvaagent import _PY_MAX_CHARS
import alvaagent.agent as agent_mod
import alvaagent.client as client_mod
import alvaagent.commands as commands_mod
import alvaagent.config as config_mod
import alvaagent.skills as skills_mod
import alvaagent.tools as tools_mod
import alvaagent.tui as tui_mod
import alvaagent.util as util_mod
import alvaagent.notes_summary as notes_mod

repl_mod = sys.modules["alvaagent.repl"]

# ---------------- helpers ---------------------------------------------------
_TMP_DIRS = []
_server = {"proc": None}


def _stop_server():
    global PORT, BASE
    proc = _server["proc"]
    if proc is not None:
        if proc.poll() is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()
    _server["proc"] = None
    PORT = None
    BASE = None


def _mock_server():
    """Lazily start and reuse an owned mock server on an OS-assigned port."""
    global PORT, BASE
    proc = _server["proc"]
    if proc is not None and proc.poll() is None:
        return proc
    if proc is not None:
        _stop_server()
    proc = subprocess.Popen(
        [sys.executable, MOCK, "0"], cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    _server["proc"] = proc
    port = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and proc.poll() is None:
        rlist, _, _ = select.select([proc.stdout], [], [], 0.2)
        if rlist:
            line = proc.stdout.readline()
            if line.startswith("READY port="):
                try:
                    port = int(line.split("=", 1)[1])
                except ValueError:
                    pass
                if port:
                    break
    if port is None:
        detail = ""
        if proc.poll() is not None and proc.stdout is not None:
            detail = proc.stdout.read().strip()
        _stop_server()
        message = "mock server exited before reporting its port" if detail else "mock server did not become ready"
        if detail:
            message += ": " + detail[-500:]
        raise RuntimeError(message)
    PORT = port
    BASE = "http://127.0.0.1:%d" % PORT
    return proc


def mkrt(data_dir=None):
    """Build a fully isolated Runtime in a fresh temp data dir."""
    if data_dir is None:
        data_dir = tempfile.mkdtemp(prefix="alva_test_")
        _TMP_DIRS.append(data_dir)
    return pa.build_runtime(data_dir)


def mock_rt(data_dir=None):
    """A runtime pointed at the offline mock LLM server."""
    _mock_server()
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
    return {"base_url": "http://127.0.0.1/v1", "api_key": "k", "model": "m",
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
    # use a real scratch dir: some proot environments have no /tmp (they use
    # $TMPDIR), so a hardcoded /tmp path would make `touch` fail before the
    # permission logic is even exercised.
    scratch = tempfile.mkdtemp(prefix="alva_perm_")
    try:
        cmd1 = "touch %s/a" % scratch
        cmd2 = "touch %s/b" % scratch
        rt.on_permission = lambda d: True
        assert pa.tool_run_command(rt, cmd1).get("ok") is True
        rt.on_permission = lambda d: False
        _cached = pa.tool_run_command(rt, cmd1)
        assert _cached.get("ok") is True
        _notcached = pa.tool_run_command(rt, cmd2)
        assert _notcached.get("ok") is False and "permission" in str(_notcached.get("error", ""))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
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
    rt.approved_keys.clear()
    # request_permission feeds the session cache from the hook result
    rt.on_permission = lambda d: True
    assert pa.request_permission(rt, "run command: python3 -m pyflakes demo3.py") is True
    assert "run command: python3 -m pyflakes demo3.py" in rt.approved
    rt.on_permission = lambda d: False
    assert pa.request_permission(rt, "run command: python3 -m pyflakes demo3.py") is True
    rt.on_permission = None
    rt.approved.clear()
    rt.approved_keys.clear()


def test_permission_session_grants():
    # 'a' remembers the KIND of action for the session (permission_key), so
    # a long task stops re-prompting for every near-identical action, while
    # 'y' still remembers only the exact action.
    rt = mock_rt()
    _orig_input = builtins.input
    try:
        # key derivation: program + subcommand / module / folder / host
        assert pa.permission_key("run command: git commit -m x") == \
            "run command: git commit"
        assert pa.permission_key("run command: git push") == \
            "run command: git push"
        assert pa.permission_key("run command: python3 -m pyflakes a.py") == \
            "run command: python3 -m pyflakes"
        assert pa.permission_key("run command: touch /a/b") == \
            "run command: touch"
        assert pa.permission_key("POST to https://api.example.com/v1/x") == \
            "POST to api.example.com"
        assert pa.permission_key("clone skills repo: https://x/y") == \
            "clone skills repo: https://x/y"
        # command kind: one 'a' covers later commits, never a push
        builtins.input = lambda *a, **k: "a"
        assert pa.ask_permission(rt, "run command: git commit -m one") is True
        assert "run command: git commit" in rt.approved_keys
        rt.on_permission = lambda d: False  # the hook would deny...
        assert pa.request_permission(rt, "run command: git commit -m two") is True
        assert pa.request_permission(rt, "run command: git push") is False
        # python -m: the module is part of the kind
        builtins.input = lambda *a, **k: "a"
        assert pa.ask_permission(rt, "run command: python3 -m pyflakes a.py") is True
        assert pa.request_permission(rt, "run command: python3 -m pyflakes b.py") is True
        assert pa.request_permission(rt, "run command: python3 -m pip install x") is False
        # file kind: the action + the folder
        scratch = tempfile.mkdtemp(prefix="alva_grant_")
        try:
            builtins.input = lambda *a, **k: "a"
            assert pa.ask_permission(
                rt, "write file: %s" % os.path.join(scratch, "a.txt")) is True
            assert pa.request_permission(
                rt, "write file: %s" % os.path.join(scratch, "b.txt")) is True
            assert pa.request_permission(rt, "write file: /other-dir/c.txt") is False
            # a different action in the same folder is a different kind
            assert pa.request_permission(
                rt, "read file: %s" % os.path.join(scratch, "a.txt")) is False
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        # 'y' remembers only the exact action, never the kind
        rt.approved.clear()
        rt.approved_keys.clear()
        builtins.input = lambda *a, **k: "y"
        assert pa.ask_permission(rt, "run command: touch /somewhere/x") is True
        assert pa.request_permission(rt, "run command: touch /somewhere/x") is True
        assert pa.request_permission(rt, "run command: touch /somewhere/y") is False
    finally:
        builtins.input = _orig_input
        rt.on_permission = None
        rt.approved.clear()
        rt.approved_keys.clear()


def test_permission_selector_logic():
    # the modal's selection loop is pure logic: it starts on No (the safe
    # default), clamps at both ends, and honors enter / direct keys / esc.
    seen = []

    def run(keys):
        it = iter(keys)
        return commands_mod._perm_select(["y", "a", "n"],
                                         lambda: next(it), seen.append)

    assert run(["enter"]) == "n"                    # default row is No
    assert run(["up", "enter"]) == "a"
    assert run(["up", "up", "enter"]) == "y"
    assert run(["up", "up", "up", "enter"]) == "y"  # clamped at the top
    assert run(["down", "enter"]) == "n"            # clamped at the bottom
    assert run(["y"]) == "y"                        # direct keys answer at once
    assert run(["a"]) == "a"
    assert run(["esc"]) is None
    assert run([""]) is None
    assert seen[0] == 2                             # first paint selects row 2


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


def test_file_read_truncation():
    # file_read must cap while reading, not after slurping a huge file into
    # memory (that can OOM-kill the app on a phone).
    rt = mock_rt()
    big = os.path.join(rt.data_dir, "big-demo.txt")
    small = os.path.join(rt.data_dir, "small-demo.txt")
    try:
        pa.tool_file_write(rt, big, "x" * (pa._FILE_READ_MAX + 500))
        r = pa.tool_file_read(rt, big)
        assert r.get("ok") is True and r.get("truncated") is True
        assert r.get("content", "").endswith("... [truncated]")
        assert len(r.get("content", "")) < pa._FILE_READ_MAX + 40
        pa.tool_file_write(rt, small, "hello")
        s = pa.tool_file_read(rt, small)
        assert s.get("ok") is True and s.get("truncated") is False
        assert s.get("content") == "hello"
    finally:
        for p in (big, small):
            try:
                os.remove(p)
            except OSError:
                pass


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


class _ProviderTestTTY:
    def isatty(self):
        return True


def _run_provider_add(rt, answers, discover=None, probe_results=None):
    """Run the wizard with controlled prompts; the fixture key is never emitted."""
    fake_key = "provider-test-fixture-only"
    answer_iter = iter(answers)
    output = io.StringIO()
    old_input = builtins.input
    old_getpass = commands_mod.getpass.getpass
    old_stdin = sys.stdin
    old_discover = commands_mod.fetch_models
    old_probe = commands_mod._test_provider_model
    probes = []

    def fake_input(prompt=""):
        print(prompt, end="")
        try:
            return next(answer_iter)
        except StopIteration:
            raise AssertionError("provider wizard requested an unexpected answer")

    def fake_getpass(prompt=""):
        print(prompt, end="")
        return fake_key

    def fake_probe(base_url, api_key, model):
        probes.append(model)
        if not probe_results:
            return True, "ok"
        result = probe_results.pop(0)
        return result

    builtins.input = fake_input
    commands_mod.getpass.getpass = fake_getpass
    sys.stdin = _ProviderTestTTY()
    if discover is not None:
        commands_mod.fetch_models = discover
    if probe_results is not None:
        commands_mod._test_provider_model = fake_probe
    try:
        with contextlib.redirect_stdout(output):
            commands_mod.cmd_provider(rt, "add")
    finally:
        builtins.input = old_input
        commands_mod.getpass.getpass = old_getpass
        sys.stdin = old_stdin
        commands_mod.fetch_models = old_discover
        commands_mod._test_provider_model = old_probe
    return output.getvalue(), probes


def _assert_fixture_key_not_echoed(output):
    if "provider-test-fixture-only" in output:
        raise AssertionError("hidden API-key input was echoed")


def test_ask_key_compat_wrapper_uses_hidden_input():
    old_stdin = sys.stdin
    old_getpass = commands_mod.getpass.getpass
    prompts = []
    commands_mod.getpass.getpass = lambda prompt: prompts.append(prompt) or "none"
    sys.stdin = _ProviderTestTTY()
    try:
        result = pa.ask_key("provider-test-fixture-only")
    finally:
        sys.stdin = old_stdin
        commands_mod.getpass.getpass = old_getpass
    assert result == ""
    assert len(prompts) == 1 and "input hidden" in prompts[0]


def _run_config_key_edit(key_answer):
    rt = mkrt()
    cfg = pa.active_cfg(rt)
    cfg.update(base_url="https://provider.example/v1",
               api_key="provider-test-fixture-only", model="model-one")
    answers = iter(["", "", "0.7", "0", "y"])
    output = io.StringIO()
    old_input, old_stdin = builtins.input, sys.stdin
    old_getpass = commands_mod.getpass.getpass
    old_discover = commands_mod.fetch_models

    def fake_input(prompt=""):
        print(prompt, end="")
        return next(answers)

    def fake_getpass(prompt=""):
        print(prompt, end="")
        return key_answer

    builtins.input = fake_input
    commands_mod.getpass.getpass = fake_getpass
    commands_mod.fetch_models = lambda _rt, _base, _key: ["model-one"]
    sys.stdin = _ProviderTestTTY()
    try:
        with contextlib.redirect_stdout(output):
            commands_mod.cmd_config(rt)
    finally:
        builtins.input, sys.stdin = old_input, old_stdin
        commands_mod.getpass.getpass = old_getpass
        commands_mod.fetch_models = old_discover
    return rt, output.getvalue()


def test_config_hidden_key_blank_keeps_current():
    rt, output = _run_config_key_edit("")
    _assert_fixture_key_not_echoed(output)
    assert pa.active_cfg(rt)["api_key"]


def test_config_hidden_key_none_clears_current():
    rt, output = _run_config_key_edit("none")
    _assert_fixture_key_not_echoed(output)
    assert not pa.active_cfg(rt)["api_key"]


def test_config_hidden_key_refuses_non_tty_without_changes():
    rt = mkrt()
    cfg = pa.active_cfg(rt)
    cfg.update(base_url="https://provider.example/v1",
               api_key="provider-test-fixture-only", model="model-one")
    old_input, old_stdin = builtins.input, sys.stdin
    old_getpass = commands_mod.getpass.getpass
    output = io.StringIO()

    def fake_input(prompt=""):
        print(prompt, end="")
        return ""

    def unexpected_getpass(_prompt=""):
        raise AssertionError("non-TTY config must not read a key")

    builtins.input = fake_input
    commands_mod.getpass.getpass = unexpected_getpass
    sys.stdin = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            commands_mod.cmd_config(rt)
    finally:
        builtins.input, sys.stdin = old_input, old_stdin
        commands_mod.getpass.getpass = old_getpass
    _assert_fixture_key_not_echoed(output.getvalue())
    assert "no key was read" in output.getvalue()
    assert pa.active_cfg(rt)["api_key"]
    assert pa.active_cfg(rt)["model"] == "model-one"


def test_config_rejects_external_http_before_key_prompt():
    rt = mkrt()
    cfg = pa.active_cfg(rt)
    cfg.update(base_url="https://provider.example/v1",
               api_key="provider-test-fixture-only", model="model-one")
    old_input, old_stdin = builtins.input, sys.stdin
    old_getpass = commands_mod.getpass.getpass
    output = io.StringIO()

    def fake_input(prompt=""):
        print(prompt, end="")
        return "http://provider.example/v1"

    def unexpected_getpass(_prompt=""):
        raise AssertionError("remote HTTP must be rejected before key entry")

    builtins.input = fake_input
    commands_mod.getpass.getpass = unexpected_getpass
    sys.stdin = _ProviderTestTTY()
    try:
        with contextlib.redirect_stdout(output):
            commands_mod.cmd_config(rt)
    finally:
        builtins.input, sys.stdin = old_input, old_stdin
        commands_mod.getpass.getpass = old_getpass
    text = output.getvalue()
    _assert_fixture_key_not_echoed(text)
    assert "HTTPS is required" in text
    assert pa.active_cfg(rt)["base_url"] == "https://provider.example/v1"
    assert pa.active_cfg(rt)["api_key"]


def test_provider_url_requires_https_except_local_http():
    assert client_mod.provider_url_error("https://provider.example/v1") is None
    for local_url in ("http://localhost:11434/v1", "http://127.0.0.1:12345/v1",
                      "http://[::1]:12345/v1", "http://192.168.1.20:8000/v1",
                      "http://llm.local/v1"):
        assert client_mod.provider_url_error(local_url) is None
    assert "HTTPS is required" in client_mod.provider_url_error("http://provider.example/v1")
    assert "must not contain credentials" in client_mod.provider_url_error(
        "https://user:pass@provider.example/v1")

    rt = mkrt()
    opened = []
    old_open = client_mod._open_provider_request
    client_mod._open_provider_request = lambda req, timeout: opened.append(req.full_url)
    try:
        try:
            client_mod.fetch_models(rt, "http://provider.example/v1", "local-dummy-key")
            rejected = False
        except RuntimeError as exc:
            rejected = "HTTPS is required" in str(exc)
    finally:
        client_mod._open_provider_request = old_open
    assert rejected and not opened


def test_provider_add_rejects_external_http_custom_endpoint():
    rt = mkrt()
    answers = iter(["custom", "cleartext", "http://provider.example/v1"])
    output = io.StringIO()
    old_input, old_stdin = builtins.input, sys.stdin
    old_getpass = commands_mod.getpass.getpass

    def fake_input(prompt=""):
        print(prompt, end="")
        return next(answers)

    def unexpected_getpass(_prompt=""):
        raise AssertionError("insecure endpoint must be rejected before key entry")

    builtins.input = fake_input
    commands_mod.getpass.getpass = unexpected_getpass
    sys.stdin = _ProviderTestTTY()
    try:
        with contextlib.redirect_stdout(output):
            commands_mod.cmd_provider(rt, "add")
    finally:
        builtins.input, sys.stdin = old_input, old_stdin
        commands_mod.getpass.getpass = old_getpass
    assert "HTTPS is required" in output.getvalue()
    assert "cleartext" not in rt.cfg["profiles"]


def test_provider_cross_origin_redirect_does_not_forward_bearer():
    received_auth = []

    class DestinationHandler(BaseHTTPRequestHandler):
        def _reply(self, payload):
            received_auth.append(self.headers.get("Authorization"))
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._reply({"object": "list", "data": [{"id": "model-one"}]})

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self._reply({"choices": [{"message": {"content": "ok"}}]})

        def log_message(self, *_args):
            pass

    destination = ThreadingHTTPServer(("127.0.0.1", 0), DestinationHandler)
    destination_base = "http://127.0.0.1:%d" % destination.server_port

    class RedirectHandler(BaseHTTPRequestHandler):
        def _redirect(self):
            self.send_response(302)
            self.send_header("Location", destination_base + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            self._redirect()

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self._redirect()

        def log_message(self, *_args):
            pass

    source = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    source_base = "http://127.0.0.1:%d/v1" % source.server_port
    servers = (destination, source)
    threads = [threading.Thread(target=server.serve_forever, daemon=True)
               for server in servers]
    for thread in threads:
        thread.start()
    rt = mkrt()
    try:
        try:
            client_mod.fetch_models(rt, source_base, "local-dummy-key")
            discovery_rejected = False
        except RuntimeError as exc:
            discovery_rejected = "HTTP 302" in str(exc)
        passed, reason = commands_mod._test_provider_model(
            source_base, "local-dummy-key", "model-one")
        assert discovery_rejected
        assert not passed and reason == "HTTP 302"
        assert received_auth == []
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


def test_provider_requests_work_over_loopback_http():
    received_auth = []

    class ProviderHandler(BaseHTTPRequestHandler):
        def _reply(self, payload):
            received_auth.append(bool(self.headers.get("Authorization")))
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._reply({"object": "list", "data": [{"id": "local-model"}]})

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self._reply({"choices": [{"message": {"content": "ok"}}]})

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = "http://127.0.0.1:%d/v1" % server.server_port
    rt = mkrt()
    try:
        models = client_mod.fetch_models(rt, base_url, "local-dummy-key")
        passed, reason = commands_mod._test_provider_model(
            base_url, "local-dummy-key", "local-model")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert models == ["local-model"]
    assert passed and reason == "ok"
    assert received_auth == [True, True]


def test_provider_add_guided_success_and_masked_key():
    rt = mkrt()
    output, _ = _run_provider_add(
        rt, ["custom", "saved-active", "https://provider.example/v1", "0", "yes"],
        discover=lambda _rt, _base, _key: ["model-one"],
        probe_results=[(True, "ok")])
    _assert_fixture_key_not_echoed(output)
    assert rt.cfg["active"] == "saved-active"
    assert pa.active_cfg(rt)["base_url"] == "https://provider.example/v1"
    assert pa.active_cfg(rt)["model"] == "model-one"
    assert pa.active_cfg(rt)["api_key"]
    with open(os.path.join(rt.data_dir, "config.json"), encoding="utf-8") as saved_file:
        saved = json.load(saved_file)
    assert saved["active"] == "saved-active"
    assert saved["profiles"]["saved-active"] == pa.active_cfg(rt)
    assert "provider profile 'saved-active' saved and now active" in output


def test_provider_add_early_cancel_leaves_config_unchanged():
    rt = mkrt()
    rt.cfg["profiles"]["existing"] = dict(pa.DEFAULT_CFG)
    rt.cfg["profiles"]["existing"].update(
        model="existing-model", api_key="existing-key")
    rt.cfg["active"] = "existing"
    pa.save_state(rt)
    before_cfg = json.loads(json.dumps(rt.cfg))
    with open(os.path.join(rt.data_dir, "config.json"), encoding="utf-8") as saved_file:
        before_file = json.load(saved_file)

    output, probes = _run_provider_add(rt, ["cancel"], probe_results=[(True, "ok")])

    assert rt.cfg == before_cfg
    with open(os.path.join(rt.data_dir, "config.json"), encoding="utf-8") as saved_file:
        assert json.load(saved_file) == before_file
    assert probes == []
    assert "provider setup cancelled; settings and active provider were not changed" in output


def test_provider_add_decline_charge_saves_nothing():
    rt = mkrt()
    pa.save_state(rt)
    before_cfg = json.loads(json.dumps(rt.cfg))
    with open(os.path.join(rt.data_dir, "config.json"), encoding="utf-8") as saved_file:
        before_file = json.load(saved_file)

    output, probes = _run_provider_add(
        rt, ["openai", "declined", "0", "no"],
        discover=lambda _rt, _base, _key: ["model-one"], probe_results=[])

    assert rt.cfg == before_cfg
    with open(os.path.join(rt.data_dir, "config.json"), encoding="utf-8") as saved_file:
        assert json.load(saved_file) == before_file
    assert rt.cfg["active"] == before_cfg["active"]
    assert "declined" not in rt.cfg["profiles"]
    assert probes == []
    assert "may incur a usage charge" in output
    assert "provider setup cancelled; settings and active provider were not changed" in output


def test_provider_add_refuses_echoing_non_tty():
    rt = mkrt()
    answers = iter(["openai", "non-tty"])
    output = io.StringIO()
    old_input, old_stdin = builtins.input, sys.stdin

    def fake_input(prompt=""):
        print(prompt, end="")
        return next(answers)

    builtins.input = fake_input
    sys.stdin = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            commands_mod.cmd_provider(rt, "add")
    finally:
        builtins.input, sys.stdin = old_input, old_stdin
    assert "no key was read" in output.getvalue()
    assert "non-tty" not in rt.cfg["profiles"]


def test_provider_add_discovery_failure_manual_model():
    rt = mkrt()

    def fail_discovery(_rt, _base, _key):
        raise RuntimeError("fixture error")

    output, probes = _run_provider_add(
        rt, ["custom", "manual", "https://provider.example/v1", "m", "manual-model", "yes"],
        discover=fail_discovery, probe_results=[(True, "ok")])
    _assert_fixture_key_not_echoed(output)
    assert "model discovery failed" in output
    assert "saved and now active" in output
    assert probes == ["manual-model"]
    assert pa.active_cfg(rt)["model"] == "manual-model"


def test_provider_add_test_retry_then_save():
    rt = mkrt()
    results = [(False, "HTTP 401"), (True, "ok")]
    output, probes = _run_provider_add(
        rt, ["openai", "retry-profile", "0", "yes", "r", "yes"],
        discover=lambda _rt, _base, _key: ["model-one"],
        probe_results=results)
    _assert_fixture_key_not_echoed(output)
    assert "connection test failed (HTTP 401); profile was not saved" in output
    assert "saved and now active" in output
    assert probes == ["model-one", "model-one"]
    assert rt.cfg["active"] == "retry-profile"


def test_provider_add_failed_manual_model_does_not_save():
    rt = mkrt()
    results = [(False, "HTTP 400"), (False, "HTTP 400")]
    output, probes = _run_provider_add(
        rt, ["openai", "not-saved", "0", "yes", "m", "other-model", "yes", "c"],
        discover=lambda _rt, _base, _key: ["model-one"],
        probe_results=results)
    _assert_fixture_key_not_echoed(output)
    assert "connection test failed" in output
    assert "connection test passed" not in output
    assert "profile saved" not in output
    assert "not-saved" not in rt.cfg["profiles"]
    assert rt.cfg["active"] != "not-saved"
    assert probes == ["model-one", "other-model"]


def test_provider_add_save_failure_not_reported_as_saved():
    rt = mkrt()
    old_save = commands_mod.save_state
    commands_mod.save_state = lambda _rt: None
    try:
        output, _ = _run_provider_add(
            rt, ["openai", "write-failed", "0", "yes"],
            discover=lambda _rt, _base, _key: ["model-one"],
            probe_results=[(True, "ok")])
    finally:
        commands_mod.save_state = old_save
    _assert_fixture_key_not_echoed(output)
    assert "connection test passed, but the profile could not be saved" in output
    assert "provider profile saved" not in output
    assert "write-failed" not in rt.cfg["profiles"]
    assert rt.cfg["active"] != "write-failed"


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
    _orig_provider_open = client_mod._open_provider_request
    client_mod._open_provider_request = lambda req, timeout=180: _FakeResp(fake_sse)
    try:
        cfg_s = _cfg()
        out = "".join(c for c, _ in pa.chat_completion_stream(
            rt, [{"role": "user", "content": "hi"}], cfg_s))
        assert out == "Hello world"
    finally:
        client_mod._open_provider_request = _orig_provider_open


def test_tool_call_ids():
    rt = mkrt()
    _orig_provider_open = client_mod._open_provider_request
    cfg_s = _cfg()
    # id must not be concatenated across repeated deltas
    id_sse = (
        "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"call_abc\","
        "\"function\":{\"name\":\"calculator\",\"arguments\":\"\"}}]}}]}\n\n"
        "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"call_abc\","
        "\"function\":{\"arguments\":\"{\\\"expression\\\":\\\"2+2\\\"}\"}}]}}]}\n\n"
        "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"tool_calls\"}]}\n\n"
    )
    client_mod._open_provider_request = lambda req, timeout=180: _FakeResp(id_sse)
    try:
        _events = list(pa.chat_completion_stream(rt, [{"role": "user", "content": "calc"}], cfg_s))
        _tc = [tc for _, tcs in _events for tc in (tcs or [])]
        assert len(_tc) == 1 and _tc[0]["id"] == "call_abc"
    finally:
        client_mod._open_provider_request = _orig_provider_open
    # no id in stream -> stable synthetic id
    noid_sse = (
        "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,"
        "\"function\":{\"name\":\"calculator\",\"arguments\":\"{\\\"expression\\\":\\\"1\\\"}\"}}]}}]}\n\n"
        "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"tool_calls\"}]}\n\n"
    )
    client_mod._open_provider_request = lambda req, timeout=180: _FakeResp(noid_sse)
    try:
        _events = list(pa.chat_completion_stream(rt, [{"role": "user", "content": "calc"}], cfg_s))
        _tc = [tc for _, tcs in _events for tc in (tcs or [])]
        assert len(_tc) == 1 and _tc[0]["id"] == "call_0"
    finally:
        client_mod._open_provider_request = _orig_provider_open


def test_plain_json_fallback():
    # gateways that ignore "stream": true answer with a plain JSON completion;
    # the stream reader must parse the body instead of crashing.
    rt = mkrt()
    cfg_s = _cfg()
    _orig_provider_open = client_mod._open_provider_request
    _plain_msg = {"choices": [{"message": {"role": "assistant",
                                           "content": "plain-json-reply"},
                               "finish_reason": "stop"}]}
    for _label, _body in (("minified", json.dumps(_plain_msg)),
                          ("pretty", json.dumps(_plain_msg, indent=2))):
        def _fake_plain(req, timeout=180, _b=_body):
            return _FakeResp(_b)
        client_mod._open_provider_request = _fake_plain
        try:
            _events = list(pa.chat_completion_stream(
                rt, [{"role": "user", "content": "hi"}], cfg_s))
            _text = "".join(c for c, _ in _events)
            assert _text == "plain-json-reply", "plain-JSON (%s) fallback" % _label
        finally:
            client_mod._open_provider_request = _orig_provider_open


def test_stream_plain_json_real_socket():
    # A real socket answering with a plain JSON body (the mock ignores
    # "stream": true). Reading the full Content-Length body closes the
    # connection, so the client must treat the dead socket as EOF instead of
    # selecting on it (which raised OSError EBADF -> "Bad file descriptor").
    rt = mock_rt()
    cfg = dict(rt.cfg)
    cfg["base_url"] = BASE + "/v1"
    events = list(client_mod.chat_completion_stream(
        rt, [{"role": "user", "content": "hi [plain]"}], cfg))
    assert events == [("PLAIN_OK this is a direct answer, no tools were used.", None)], events


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
    _orig_provider_open = client_mod._open_provider_request
    client_mod._sleep_retry = lambda a: None
    _attempts = [0]
    _good = json.dumps({"choices": [{"message": {"role": "assistant", "content": "retried-ok"}}]}).encode("utf-8")

    def _flaky(req, timeout=180):
        _attempts[0] += 1
        if _attempts[0] <= 2:
            raise urllib.error.URLError("transient outage")
        return _Resp(200, _good)

    client_mod._open_provider_request = _flaky
    try:
        _d = pa.chat_completion(rt, [{"role": "user", "content": "hi"}], _cfg())
        assert _attempts[0] == 3 and _d["choices"][0]["message"]["content"] == "retried-ok"
    finally:
        client_mod._open_provider_request = _orig_provider_open
    _attempts[0] = 0

    def _perm(req, timeout=180):
        _attempts[0] += 1
        return _Resp(400, json.dumps({"error": {"message": "nope"}}).encode("utf-8"))

    client_mod._open_provider_request = _perm
    try:
        try:
            pa.chat_completion(rt, [{"role": "user", "content": "hi"}], _cfg())
            _raised = False
        except RuntimeError:
            _raised = True
        assert _raised and _attempts[0] == 1
    finally:
        client_mod._open_provider_request = _orig_provider_open
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
    tui_mod.COLOR = False
    try:
        assert pa._md_line("**raw**", pa.SKINS["midnight"])[0] == "**raw**"
    finally:
        tui_mod.COLOR = _old_color

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
    _orig_provider_open = client_mod._open_provider_request
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

    client_mod._open_provider_request = _fake_xml_urlopen
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
        client_mod._open_provider_request = _orig_provider_open


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


def test_mock_server_lifecycle():
    first = _mock_server()
    try:
        assert first is _mock_server()
        assert PORT and BASE == "http://127.0.0.1:%d" % PORT
        with urllib.request.urlopen(BASE + "/models", timeout=2) as response:
            assert response.getcode() == 200
            assert json.loads(response.read())['data'][0]['id'] == "mock-model"

        _stop_server()
        assert first.poll() is not None
        second = _mock_server()
        assert second is not first and second.poll() is None
        with urllib.request.urlopen(BASE + "/models", timeout=2) as response:
            assert response.getcode() == 200
    finally:
        _stop_server()


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
    _orig_provider_open = client_mod._open_provider_request
    agent_mod.dispatch_tool = _fail_dispatch
    client_mod._open_provider_request = _fail_urlopen
    try:
        _evs = list(pa.run_agent_stream(rt, [{"role": "user", "content": "loop"}]))
        _dn = [e for k, e in _evs if k == "done"][0]
        assert "stopped early" in str(_dn.get("content", ""))
        assert len(_sse_calls) == agent_mod._MAX_CONSEC_TOOL_FAILURES
    finally:
        agent_mod.dispatch_tool = _orig_dispatch
        client_mod._open_provider_request = _orig_provider_open
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

    _orig_provider_open = client_mod._open_provider_request
    _orig_timeout = agent_mod._TURN_TIMEOUT
    agent_mod._TURN_TIMEOUT = 0
    client_mod._open_provider_request = _fail_urlopen
    try:
        _evs = list(pa.run_agent_stream(rt, [{"role": "user", "content": "slow"}]))
        _dn = [e for k, e in _evs if k == "done"][0]
        assert "time budget" in str(_dn.get("content", ""))
        assert len(_sse_calls) == 0
    finally:
        client_mod._open_provider_request = _orig_provider_open
        agent_mod._TURN_TIMEOUT = _orig_timeout
    assert any("timeout" in ln for ln in pa.read_trace(rt, 50))


# ---------------- run_python: bounded AST interpreter ----------------------

def test_run_python():
    rt = mock_rt()
    launches = []
    permissions = []
    _orig_popen = tools_mod.subprocess.Popen
    _orig_run = tools_mod.subprocess.run
    _orig_permission = tools_mod.request_permission

    def _blocked_launcher(*args, **kwargs):
        launches.append((args, kwargs))
        raise AssertionError("run_python must not use a process launcher")

    def _unexpected_permission(*args, **kwargs):
        permissions.append((args, kwargs))
        return True

    tools_mod.subprocess.Popen = _blocked_launcher
    tools_mod.subprocess.run = _blocked_launcher
    tools_mod.request_permission = _unexpected_permission
    try:
        _rp = pa.dispatch_tool(rt, "run_python", {"code": "print(6*7)"})
        assert _rp.get("ok") is True and _rp.get("output") == "42\n"

        _transform = (
            "nums = [n*n for n in range(5)]\n"
            "total = 0\n"
            "for n in nums:\n"
            "    if n % 2 == 0:\n"
            "        total += n\n"
            "print(nums)\n"
            "print(total)"
        )
        _rp2 = pa.dispatch_tool(rt, "run_python", {"code": _transform})
        assert _rp2.get("ok") is True
        assert _rp2.get("output") == "[0, 1, 4, 9, 16]\n20\n"
        _rp3 = pa.dispatch_tool(rt, "run_python", {
            "code": "rows = {'a': 3, 'b': 4}\nprint(rows['a'] + rows['b'])"})
        assert _rp3.get("ok") is True and _rp3.get("output") == "7\n"
        assert pa.classify_python("x = [i*i for i in range(10)]") == "allow"

        rejected = (
            "import os; print(os.getcwd())",
            "open('/sdcard/x', 'w')",
            "print(eval('2+2'))",
            "print((1).__class__)",
            "while condition: pass",
            "__import__('os')",
        )
        for code in rejected:
            assert pa.classify_python(code) == "deny", code
            result = pa.dispatch_tool(rt, "run_python", {"code": code})
            assert result.get("ok") is False, (code, result)
        assert launches == []
        assert permissions == []

        _over_limit = pa.dispatch_tool(rt, "run_python", {
            "code": "items = [0] * 1001"})
        assert _over_limit.get("ok") is False
        _over_budget = pa.dispatch_tool(rt, "run_python", {
            "code": "for x in range(100):\n    for y in range(100):\n        z = x + y"})
        assert _over_budget.get("ok") is False
        _large_output = pa.dispatch_tool(rt, "run_python", {
            "code": "print('a' * 9000)"})
        assert _large_output.get("ok") is True
        assert len(_large_output.get("output", "")) <= _PY_MAX_CHARS
        assert "... (truncated)" in _large_output.get("output", "")
        assert launches == []
    finally:
        tools_mod.subprocess.Popen = _orig_popen
        tools_mod.subprocess.run = _orig_run
        tools_mod.request_permission = _orig_permission


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
    "SKINS", "AgentWriter", "Spinner", "_PY_MAX_CHARS",
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
                           capture_output=True, text=True, cwd=ROOT)
        assert r.returncode == 0 and "OK" in r.stdout, r.stderr
    for mod in ("alvaagent.context", "alvaagent.config", "alvaagent.store",
                "alvaagent.permissions", "alvaagent.sessions", "alvaagent.trace",
                "alvaagent.skills", "alvaagent.tools", "alvaagent.client",
                "alvaagent.agent", "alvaagent.tui", "alvaagent.commands",
                "alvaagent.repl", "alvaagent.util"):
        r = subprocess.run([sys.executable, "-c", "import %s" % mod],
                           capture_output=True, text=True, cwd=ROOT)
        assert r.returncode == 0, (mod, r.stderr)


def test_render_conversation():
    from alvaagent.commands import render_conversation
    assert render_conversation([]) == ""
    hist = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi back"},
        {"role": "user", "content": "[summary of earlier conversation] old stuff"},
        {"role": "tool", "tool_call_id": "t1", "content": "tool out"},
    ]
    text = render_conversation(hist)
    for want in ("## you", "## agent", "## summary (compressed)", "## tool (t1)"):
        assert want in text
    assert (text.index("hello") < text.index("hi back")
            < text.index("old stuff") < text.index("tool out"))
    long = render_conversation([{"role": "tool", "tool_call_id": "t", "content": "x" * 600}])
    assert "x" * 500 in long and "x" * 501 not in long


def test_cmd_scroll():
    import alvaagent.commands as cmds
    rt = mock_rt()
    pages = []
    orig = cmds._page
    cmds._page = lambda t: pages.append(t)
    try:
        cmds.cmd_scroll(rt, [{"role": "user", "content": "hello"}])
        assert pages and "## you" in pages[0] and "hello" in pages[0]
        pages.clear()
        cmds.cmd_scroll(rt, [])
        assert pages == []
    finally:
        cmds._page = orig
    assert "/scroll" in cmds._SLASH_COMMANDS


def test_stream_tee_capture():
    from alvaagent.scrollback import StreamTee
    from io import StringIO
    sink = StringIO()
    tee = StreamTee(stream=sink)
    tee.write("hello\n")
    tee.write("\x1b[38;5;45mcolored\x1b[0m line\n")
    tee.write("partial")
    assert tee.captured_lines() == ["hello", "\x1b[38;5;45mcolored\x1b[0m line"]
    assert tee.partial_line() == "partial"
    assert sink.getvalue() == "hello\n\x1b[38;5;45mcolored\x1b[0m line\npartial"


def test_stream_tee_filters_private_modes():
    from alvaagent.scrollback import StreamTee
    from io import StringIO
    sink = StringIO()
    tee = StreamTee(stream=sink)
    tee.write("\x1b[?1049h")
    tee.write("\x1b[?1002h")
    tee.write("\n")
    tee.write("banner\n")
    assert tee.captured_lines() == ["banner"]


def test_stream_tee_restore_replays():
    from alvaagent.scrollback import StreamTee
    from io import StringIO
    sink = StringIO()
    tee = StreamTee(stream=sink)
    tee.write("line one\nline two\n")
    tee.restore()
    out = sink.getvalue()
    assert "\x1b[2J\x1b[H" in out
    # the replay uses \r\n line endings: restore() runs under the
    # LineReader's raw mode, where a bare \n leaves each replayed line
    # starting wherever the previous one ended (scattered text)
    assert "line one\r\nline two\r\n" in out
    assert out.count("line one") == 2 and out.count("line two") == 2


def test_stream_tee_bounded():
    from alvaagent.scrollback import StreamTee
    from io import StringIO
    tee = StreamTee(stream=StringIO(), max_lines=3)
    for i in range(6):
        tee.write("line%d\n" % i)
    assert tee.captured_lines() == ["line3", "line4", "line5"]


def test_stream_tee_write_direct_not_captured():
    from alvaagent.scrollback import StreamTee
    from io import StringIO
    sink = StringIO()
    tee = StreamTee(stream=sink)
    tee.write("live\n")
    tee.write_direct("scroll page\n")
    tee.write("after\n")
    assert tee.captured_lines() == ["live", "after"]
    assert sink.getvalue() == "live\nscroll page\nafter\n"


def test_mouse_parse():
    from alvaagent.scrollback import (parse_mouse, is_wheel_up, is_wheel_down)
    up = parse_mouse(b"\x1b[<64;20;5M")
    assert up == {"button": 64, "col": 20, "row": 5, "kind": "press"}
    assert is_wheel_up(up) and not is_wheel_down(up)
    down = parse_mouse(b"\x1b[<65;20;5M")
    assert is_wheel_down(down)
    rel = parse_mouse(b"\x1b[<0;1;1m")
    assert rel["kind"] == "release" and rel["button"] == 0
    assert parse_mouse(b"garbage") is None
    assert parse_mouse(b"\x1b[<64;x;5M") is None
    assert parse_mouse(b"\x1b[<64;20;5X") is None
    x10up = parse_mouse(b"\x1b[M\x60\x21\x21")  # X10 wheel up @ (1,1)
    assert x10up == {"button": 64, "col": 1, "row": 1, "kind": "press"}
    assert is_wheel_up(x10up)
    x10down = parse_mouse(b"\x1b[M\x61\x21\x21")  # X10 wheel down @ (1,1)
    assert is_wheel_down(x10down)
    x10release = parse_mouse(b"\x1b[M\x23\x21\x21")  # X10 button 3 = release
    assert x10release["kind"] == "release"


def test_scroll_view_wrap():
    from alvaagent.scrollback import wrap_to
    assert wrap_to("one two three four", 10) == ["one two", "three four"]
    long_word = "x" * 30
    assert wrap_to("a " + long_word, 10) == ["a", long_word[:10], long_word[10:20], long_word[20:30]]


def test_scroll_view_pages():
    from alvaagent.scrollback import ScrollView
    history = [{"role": "user", "content": "hello"},
               {"role": "assistant", "content": "a" * 60}]
    sv = ScrollView(history, columns=20, rows=12)
    assert sv.window() == 10
    assert sv.total_lines() > 0
    assert sv.page_count() >= 1
    page0 = sv.page_text(0)
    assert "## you" in page0
    assert "hello" in page0
    assert "page 1/%d" % sv.page_count() in page0
    assert "◀ older" in page0 and "▼ newer" in page0 and "⏎ return" in page0
    lines = page0.split("\n")
    assert len(lines) == sv.window() + 1


def test_scroll_view_empty():
    from alvaagent.scrollback import ScrollView
    sv = ScrollView([], columns=20, rows=12)
    assert sv.page_count() == 1
    assert "⏎ return" in sv.page_text(0)


def _pty_run(script):
    """Run `script` in a pty child; return (pid, fd) for the parent."""
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(ROOT)
        os.execv(sys.executable, [sys.executable, "-c", script])
        os._exit(127)
    os.set_blocking(fd, False)
    return pid, fd


def _pty_drain(fd, seconds):
    import select
    out = b""
    end = time.time() + seconds
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        else:
            time.sleep(0.05)
    return out


def _pty_drain_until(fd, marker, timeout):
    """Drain fd until `marker` appears in the accumulated output."""
    import select
    out = b""
    end = time.time() + timeout
    while time.time() < end and marker not in out:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
    return out


def _pty_wait_raw(fd, timeout):
    """Poll the pty until the child's raw mode is active (echo off)."""
    import termios
    end = time.time() + timeout
    while time.time() < end:
        try:
            if not (termios.tcgetattr(fd)[3] & termios.ECHO):
                return True
        except OSError:
            return False
        time.sleep(0.05)
    return False


def test_hidden_api_key_pty_no_echo():
    # Exercise the real getpass/TTY path with a dummy value only. The child
    # calls the prompt helper directly; it never contacts a provider.
    script = (
        "import os\n"
        "from alvaagent.commands import _ask_hidden_api_key\n"
        "value = _ask_hidden_api_key()\n"
        "os.write(1, b'\\nKEY_ACCEPTED\\n' if value == 'dummy-pty-key' else b'\\nKEY_REJECTED\\n')\n"
    )
    pid, fd = _pty_run(script)
    status = None
    try:
        out = _pty_drain_until(fd, b"API key (input hidden;", 8.0)
        assert b"API key (input hidden;" in out, "hidden-key prompt did not appear"
        assert _pty_wait_raw(fd, 8.0), "PTY did not disable input echo"
        os.write(fd, b"dummy-pty-key\n")
        out += _pty_drain_until(fd, b"KEY_ACCEPTED", 8.0)
        assert b"KEY_ACCEPTED" in out, "hidden-key prompt did not accept the input"
        assert b"dummy-pty-key" not in out, "dummy key appeared in PTY output"
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, "PTY child failed"
    finally:
        if status is None:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
            try:
                os.waitpid(pid, 0)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass


def test_line_reader_basic():
    script = (
        "import os\n"
        "import sys\n"
        "from io import StringIO\n"
        "from alvaagent.scrollback import StreamTee, LineReader\n"
        "os.write(1, b'READY\\n')\n"
        "tee = StreamTee(stream=StringIO())\n"
        "sys.stdout = tee\n"
        "r = LineReader(tee, [], prompt='> ')\n"
        "line = r.read_line()\n"
        "os.write(1, b'RESULT=' + repr(line).encode() + b'\\n')\n"
    )
    pid, fd = _pty_run(script)
    out = _pty_drain_until(fd, b"READY", 8.0)
    assert b"READY" in out, out[-500:]
    assert _pty_wait_raw(fd, 8.0)
    os.write(fd, b"hi there\n")
    out += _pty_drain_until(fd, b"RESULT=", 5.0)
    assert b"RESULT='hi there'" in out, out[-500:]
    try:
        os.kill(pid, 15)
    except OSError:
        pass
    try:
        os.waitpid(pid, os.WNOHANG)
    except OSError:
        pass


def test_line_reader_eof_and_interrupt():
    script = (
        "import os\n"
        "import sys\n"
        "from io import StringIO\n"
        "from alvaagent.scrollback import StreamTee, LineReader\n"
        "os.write(1, b'READY\\n')\n"
        "tee = StreamTee(stream=StringIO())\n"
        "sys.stdout = tee\n"
        "r = LineReader(tee, [], prompt='> ')\n"
        "try:\n"
        "    r.read_line()\n"
        "    os.write(1, b'NO-RAISE\\n')\n"
        "except EOFError:\n"
        "    os.write(1, b'GOT-EOF\\n')\n"
    )
    pid, fd = _pty_run(script)
    out = _pty_drain_until(fd, b"READY", 8.0)
    assert b"READY" in out, out[-500:]
    assert _pty_wait_raw(fd, 8.0)
    os.write(fd, b"\x04")
    out += _pty_drain_until(fd, b"GOT-EOF", 5.0)
    assert b"GOT-EOF" in out, out[-500:]
    try:
        os.kill(pid, 15)
    except OSError:
        pass
    try:
        os.waitpid(pid, os.WNOHANG)
    except OSError:
        pass


def test_line_reader_x10_noleak():
    # Termux falls back to X10 mouse events (ESC [ M + 3 bytes) when SGR
    # (1006) is not in effect. The reader must consume the whole sequence so
    # no trailing bytes leak into the input echo as printable garbage.
    script = (
        "import os\n"
        "import sys\n"
        "from alvaagent.scrollback import StreamTee, LineReader\n"
        "os.write(1, b'READY\\n')\n"
        "tee = StreamTee(stream=sys.stdout)\n"
        "sys.stdout = tee\n"
        "r = LineReader(tee, [], prompt='> ')\n"
        "hits = []\n"
        "r.on_scroll(hits.append)\n"
        "line = r.read_line()\n"
        "os.write(1, b'RESULT=' + repr(line).encode() + b'\\n')\n"
        "os.write(1, b'HITS=' + repr(hits).encode() + b'\\n')\n"
        "os.write(1, b'CAP=' + repr(tee.captured_lines()).encode() + b'\\n')\n"
    )
    pid, fd = _pty_run(script)
    out = _pty_drain_until(fd, b"READY", 8.0)
    assert b"READY" in out, out[-500:]
    assert _pty_wait_raw(fd, 8.0)
    os.write(fd, b"\x1b[M\x60\x21\x21")  # X10 wheel up @ (1,1); leaks '`', '!', '!'
    os.write(fd, b"ok\n")
    out += _pty_drain_until(fd, b"CAP=", 5.0)
    assert b"RESULT='ok'" in out, out[-500:]
    assert b"!`" not in out and b"`!" not in out, out[-500:]
    assert b"HITS=['older']" in out, out[-500:]  # wheel-up triggered a scroll
    try:
        os.kill(pid, 15)
    except OSError:
        pass
    try:
        os.waitpid(pid, os.WNOHANG)
    except OSError:
        pass


def test_scroll_loop_paging():
    # Wheel up (swipe up) moves to older pages, wheel down to newer pages,
    # Enter returns to live; the footer page number must track the motion.
    script = (
        "import os\n"
        "import sys\n"
        "from alvaagent.scrollback import StreamTee, LineReader, ScrollView\n"
        "tee = StreamTee()\n"
        "sys.stdout = tee\n"
        "r = LineReader(tee, [], prompt='> ')\n"
        "hist = []\n"
        "for i in range(30):\n"
        "    hist.append({'role': 'user', 'content': 'question %d' % i})\n"
        "    hist.append({'role': 'assistant', 'content': 'answer %d' % i})\n"
        "sv = ScrollView(hist, columns=40, rows=12)\n"
        "r.run_scroll_loop(sv, sv.page_count() - 1)\n"
        "os.write(1, b'DONE\\n')\n"
    )
    pid, fd = _pty_run(script)
    assert _pty_wait_raw(fd, 8.0)

    def page_num(buf):
        return int(buf.rsplit(b"page ", 1)[-1].split(b"/", 1)[0])

    out = _pty_drain(fd, 1.5)
    p0 = page_num(out)
    assert p0 >= 2, out[-300:]
    os.write(fd, b"\x1b[<64;1;1M")  # wheel up == swipe up -> older
    out += _pty_drain(fd, 1.0)
    p1 = page_num(out)
    assert p1 == p0 - 1, (p0, p1, out[-300:])
    os.write(fd, b"\x1b[<65;1;1M")  # wheel down == swipe down -> newer
    out += _pty_drain(fd, 1.0)
    p2 = page_num(out)
    assert p2 == p1 + 1, (p1, p2, out[-300:])
    os.write(fd, b"\r")
    out += _pty_drain_until(fd, b"DONE", 3.0)
    assert b"DONE" in out, out[-300:]
    try:
        os.kill(pid, 15)
    except OSError:
        pass
    try:
        os.waitpid(pid, os.WNOHANG)
    except OSError:
        pass


def test_scroll_from_lines():
    # ScrollView.from_lines pages already-rendered screen lines verbatim
    # (no conversation wrapping) - the source the swipe view is built from.
    script = (
        "import os\n"
        "import sys\n"
        "from alvaagent.scrollback import StreamTee, LineReader, ScrollView\n"
        "tee = StreamTee()\n"
        "sys.stdout = tee\n"
        "r = LineReader(tee, [], prompt='> ')\n"
        "sv = ScrollView.from_lines(['line %d' % i for i in range(20)], columns=40, rows=10)\n"
        "assert sv.total_lines() == 20, sv.total_lines()\n"
        "assert sv.page_count() == 3, sv.page_count()\n"
        "assert 'line 0' in sv.page_text(0), sv.page_text(0)\n"
        "assert 'line 17' not in sv.page_text(0), sv.page_text(0)\n"
        "os.write(1, b'DONE\\n')\n"
    )
    pid, fd = _pty_run(script)
    out = _pty_drain_until(fd, b"DONE", 8.0)
    assert b"DONE" in out, out[-300:]
    try:
        os.kill(pid, 15)
    except OSError:
        pass
    try:
        os.waitpid(pid, os.WNOHANG)
    except OSError:
        pass


def test_scroll_e2e():
    # Boot the real app against the offline mock server with a seeded PAST
    # session, send one real message so the screen holds this run's content,
    # then enter the conversation explicitly, send one message, and feed a
    # wheel-up (swipe up) event: the scroll view must page through exactly
    # what was printed THIS run - never the seeded past session.
    # Enter returns to live, then /exit.
    _mock_server()
    data_dir = tempfile.mkdtemp(prefix="alva_scrolle2e_")
    _TMP_DIRS.append(data_dir)
    store = {
        "alvaagent.sessions": {
            "past": {"name": "past", "created": "2026-08-15T00:00:00",
                     "updated": "2026-08-15T00:00:00",
                     "messages": [
                         {"role": "user", "content": "PAST_SESSION_CONTENT_XXX"},
                     ]}
        },
        "alvaagent.active_session": "default",
    }
    with open(os.path.join(data_dir, "store.json"), "w") as f:
        json.dump(store, f)
    with open(os.path.join(data_dir, "config.json"), "w") as f:
        json.dump({"provider": "mock", "base_url": BASE + "/v1",
                   "api_key": "test-key", "model": "mock-model",
                   "temperature": 0.5}, f)
    env = dict(os.environ)
    env["ALVA_DATA_DIR"] = data_dir
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(ROOT)
        os.execve(sys.executable, [sys.executable, "-m", "alvaagent"], env)
        os._exit(127)
    os.set_blocking(fd, False)
    out = b""
    try:
        out += _pty_drain_until(fd, b"> ", 15.0)
        assert b"> " in out, out[-400:]  # prompt live
        assert b"PAST_SESSION_CONTENT_XXX" not in out, out[-400:]
        os.write(fd, b"/chat\n")
        out += _pty_drain_until(fd, b"chat> ", 10.0)
        assert b"Conversation" in out
        os.write(fd, b"hello [plain]\n")  # a real turn against the mock server
        out += _pty_drain_until(fd, b"PLAIN_OK", 20.0)
        assert b"PLAIN_OK" in out, out[-800:]  # answer streamed onto the screen
        out += _pty_drain_until(fd, b"chat> ", 10.0)  # live chat prompt after the turn
        os.write(fd, b"\x1b[<64;1;1M")  # wheel up == swipe up on Termux
        out += _pty_drain(fd, 3.0)
        assert b"hello [plain]" in out, out[-800:]  # user turn printed this run
        assert b"PLAIN_OK" in out, out[-800:]       # streamed answer printed this run
        assert b"PAST_SESSION_CONTENT_XXX" not in out, out[-800:]  # not past history
        os.write(fd, b"\r")             # Enter: return to live
        out += _pty_drain_until(fd, b"chat> ", 4.0)
        assert b"chat> " in out[-400:], out[-400:]  # live prompt restored
        os.write(fd, b"/exit\n")
        out += _pty_drain_until(fd, b"bye", 5.0)
        assert b"bye" in out, out[-400:]
    finally:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except OSError:
            pass


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
                       timeout=30, env=_env, cwd=ROOT)
    assert r.returncode == 0, (r.returncode, r.stdout[-500:], r.stderr[-500:])


def _notes_test_root(files=None):
    root = tempfile.mkdtemp(prefix="alva_notes_task_")
    _TMP_DIRS.append(root)
    os.mkdir(os.path.join(root, "notes"))
    for name, content in (files if files is not None else {"one.md": "SYNTHETIC_NOTE"}).items():
        path = os.path.join(root, "notes", name)
        raw = content if isinstance(content, bytes) else content.encode("utf-8")
        with open(path, "wb") as stream:
            stream.write(raw)
    return root


def _notes_test_runtime():
    rt = mkrt()
    rt.cfg = config_mod._normalize_state({
        "provider": "synthetic-test-provider",
        "base_url": "https://provider.invalid/v1",
        "api_key": "SYNTHETIC_TEST_KEY",
        "model": "synthetic-model",
        "temperature": 0.2,
    })
    rt.history = [
        {"role": "user", "content": "UNRELATED_CHAT_HISTORY_SENTINEL"},
        {"role": "tool", "content": "UNRELATED_PRIOR_TOOL_OUTPUT_SENTINEL"},
    ]
    rt.store["test.unrelated_tool_result"] = "UNRELATED_STORED_TOOL_OUTPUT"
    return rt


def _notes_run(rt, root, answers=(), action=""):
    old_cwd = os.getcwd()
    old_input = notes_mod.builtins.input
    answers = iter(answers)
    prompts = []
    out = io.StringIO()

    def fake_input(prompt=""):
        prompts.append(prompt)
        try:
            return next(answers)
        except StopIteration:
            raise AssertionError("unexpected notes workflow input prompt: %s" % prompt)

    try:
        os.chdir(root)
        notes_mod.builtins.input = fake_input
        with contextlib.redirect_stdout(out):
            notes_mod.cmd_notes(rt, action)
    finally:
        notes_mod.builtins.input = old_input
        os.chdir(old_cwd)
    return out.getvalue(), prompts


def _notes_run_with_pty_control(rt, root, answers, entered, control="s", action=""):
    import fcntl
    import struct
    import termios

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 40, 0, 0))
    slave_stream = os.fdopen(slave, "r", buffering=1, encoding="utf-8")
    old_stdin = notes_mod.sys.stdin
    old_columns, old_lines = os.environ.get("COLUMNS"), os.environ.get("LINES")
    notes_mod.sys.stdin = slave_stream
    os.environ["COLUMNS"] = "40"
    os.environ["LINES"] = "24"
    result = {}

    def run_task():
        try:
            result["value"] = _notes_run(rt, root, answers, action)
        except BaseException as exc:
            result["error"] = exc

    task = threading.Thread(target=run_task, name="notes-pty-task", daemon=True)
    started = time.monotonic()
    task.start()
    try:
        deadline = time.monotonic() + 5.0
        while not entered.is_set() and task.is_alive() and time.monotonic() < deadline:
            entered.wait(0.02)
        if not entered.is_set():
            raise AssertionError("synthetic stall was not reached before PTY control input")
        os.write(master, (control + "\n").encode("ascii"))
        task.join(timeout=5.0)
        if task.is_alive():
            raise AssertionError("notes task did not respond to real PTY control input")
        if "error" in result:
            raise result["error"]
        output, prompts = result["value"]
        return output, prompts, time.monotonic() - started
    finally:
        if task.is_alive():
            try:
                os.write(master, b"s\n")
            except OSError:
                pass
            task.join(timeout=1.0)
        notes_mod.sys.stdin = old_stdin
        if old_columns is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = old_columns
        if old_lines is None:
            os.environ.pop("LINES", None)
        else:
            os.environ["LINES"] = old_lines
        slave_stream.close()
        os.close(master)


def _notes_fake_provider(response, calls=None):
    def fake(messages, cfg, before_dispatch=None, cancel_handle=None):
        if before_dispatch:
            before_dispatch()
        if calls is not None:
            calls.append((messages, cfg))
        return response
    return fake


def _notes_start_stall_server(mode):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(0.1)
    entered = threading.Event()
    peer_closed = threading.Event()
    stop = threading.Event()
    request_count = [0]
    connections = []

    def serve():
        conn = None
        try:
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                    break
                except socket.timeout:
                    continue
            if conn is None:
                return
            connections.append(conn)
            conn.settimeout(0.1)
            if mode == "tls":
                entered.set()
            else:
                data = bytearray()
                while b"\r\n\r\n" not in data and not stop.is_set():
                    try:
                        chunk = conn.recv(65536)
                    except socket.timeout:
                        continue
                    if not chunk:
                        return
                    data.extend(chunk)
                header, _, body = bytes(data).partition(b"\r\n\r\n")
                length = 0
                for line in header.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1].strip())
                body = bytearray(body)
                while len(body) < length and not stop.is_set():
                    try:
                        chunk = conn.recv(65536)
                    except socket.timeout:
                        continue
                    if not chunk:
                        return
                    body.extend(chunk)
                request_count[0] += 1
                if mode == "body":
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n{")
                entered.set()
            while not stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    peer_closed.set()
                    return
        except OSError:
            peer_closed.set()
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass
            try:
                listener.close()
            except OSError:
                pass

    thread = threading.Thread(target=serve, name="synthetic-provider-server", daemon=True)
    thread.start()
    scheme = "https" if mode == "tls" else "http"
    base = "%s://127.0.0.1:%d/v1" % (scheme, listener.getsockname()[1])

    def cleanup():
        stop.set()
        try:
            listener.close()
        except OSError:
            pass
        for conn in connections:
            try:
                conn.close()
            except OSError:
                pass
        thread.join(timeout=2)

    return base, entered, peer_closed, request_count, thread, cleanup


class _NotesFakeTTY:
    def isatty(self):
        return True

    def readline(self):
        return ""


def test_notes_provider_request_bypasses_environment_proxies():
    class ProviderHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.server.requests += 1
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            body = json.dumps({"choices": [{"message": {"content": "Synthetic direct response."}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.server.requests += 1
            self.send_error(502, "synthetic proxy should not receive this request")

        def log_message(self, *args):
            pass

    provider = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    provider.requests = 0
    proxy.requests = 0
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    provider_thread.start()
    proxy_thread.start()
    proxy_url = "http://127.0.0.1:%d" % proxy.server_address[1]
    proxy_names = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
                   "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")
    saved_env = {name: os.environ.get(name) for name in proxy_names}
    try:
        for name in proxy_names[:6]:
            os.environ[name] = proxy_url
        os.environ["NO_PROXY"] = ""
        os.environ["no_proxy"] = ""
        assert urllib.request.getproxies().get("http") == proxy_url
        assert not urllib.request.proxy_bypass("127.0.0.1")
        result = notes_mod._single_provider_completion(
            notes_mod._task_messages([("one.md", "SYNTHETIC_NOTE")]),
            {
                "base_url": "http://127.0.0.1:%d/v1" % provider.server_address[1],
                "api_key": "SYNTHETIC_PROVIDER_KEY",
                "model": "synthetic-model",
                "temperature": 0.2,
            },
        )
        assert result == "Synthetic direct response."
        assert provider.requests == 1 and proxy.requests == 0
    finally:
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        provider.shutdown()
        proxy.shutdown()
        provider.server_close()
        proxy.server_close()
        provider_thread.join(timeout=2)
        proxy_thread.join(timeout=2)


def test_notes_provider_response_is_streamed_with_hard_2mib_cap():
    class OversizedResponse:
        def __init__(self, declared_length=None):
            self.declared_length = declared_length
            self.returned = 0
            self.max_requested = 0
            self.read_calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def getcode(self):
            return 200

        def getheader(self, name):
            return str(self.declared_length) if name.lower() == "content-length" and self.declared_length is not None else None

        def read(self, size=-1):
            assert size is not None and size >= 0, "response reader used an unbounded read"
            self.read_calls += 1
            self.max_requested = max(self.max_requested, size)
            remaining = notes_mod._MAX_OUTPUT_BYTES + 1 - self.returned
            count = min(size, remaining)
            self.returned += count
            return b"x" * count

    response = OversizedResponse()
    old_open = notes_mod._open_provider_once
    notes_mod._open_provider_once = lambda *args, **kwargs: response
    try:
        try:
            notes_mod._single_provider_completion(
                notes_mod._task_messages([("one.md", "SYNTHETIC_NOTE")]),
                {"base_url": "https://provider.invalid/v1", "api_key": "SYNTHETIC_KEY",
                 "model": "synthetic-model", "temperature": 0.2},
            )
            raise AssertionError("oversized provider response was accepted")
        except notes_mod.NotesSummaryError as exc:
            assert "provider response exceeds the 2 MiB safety limit" in str(exc)
    finally:
        notes_mod._open_provider_once = old_open
    assert response.returned == notes_mod._MAX_OUTPUT_BYTES + 1
    assert response.max_requested <= 65536
    assert response.read_calls <= (notes_mod._MAX_OUTPUT_BYTES // 65536) + 2

    advertised = OversizedResponse(declared_length=notes_mod._MAX_OUTPUT_BYTES + 1)
    old_open = notes_mod._open_provider_once
    notes_mod._open_provider_once = lambda *args, **kwargs: advertised
    try:
        try:
            notes_mod._single_provider_completion(
                notes_mod._task_messages([("one.md", "SYNTHETIC_NOTE")]),
                {"base_url": "https://provider.invalid/v1", "api_key": "SYNTHETIC_KEY",
                 "model": "synthetic-model", "temperature": 0.2},
            )
            raise AssertionError("oversized declared provider response was accepted")
        except notes_mod.NotesSummaryError as exc:
            assert "provider response exceeds the 2 MiB safety limit" in str(exc)
    finally:
        notes_mod._open_provider_once = old_open
    assert advertised.returned == 0 and advertised.read_calls == 0


def test_notes_task_scoped_payload_and_40_column_flow():
    long_name = "a" * 72 + ".md"
    root = _notes_test_root({
        long_name: "SYNTHETIC_SELECTED_NOTE_ONLY",
        "z-unselected.md": "SYNTHETIC_UNSELECTED_NOTE_SENTINEL",
    })
    rt = _notes_test_runtime()
    calls = []

    def fake_provider(messages, cfg, before_dispatch=None, cancel_handle=None):
        if before_dispatch:
            before_dispatch()
        calls.append((messages, cfg))
        return "Synthetic summary of selected note."

    old_provider = notes_mod._single_provider_completion
    old_width = notes_mod.shutil.get_terminal_size
    notes_mod._single_provider_completion = fake_provider
    notes_mod.shutil.get_terminal_size = lambda fallback: os.terminal_size((40, 24))
    try:
        output, prompts = _notes_run(rt, root, ["1", "y", "", "s", "", ""])
    finally:
        notes_mod._single_provider_completion = old_provider
        notes_mod.shutil.get_terminal_size = old_width

    assert len(calls) == 1
    messages, cfg = calls[0]
    assert len(messages) == 2
    assert [m["role"] for m in messages] == ["system", "user"]
    payload = "\n".join(m["content"] for m in messages)
    assert "SYNTHETIC_SELECTED_NOTE_ONLY" in payload
    assert "SYNTHETIC_UNSELECTED_NOTE_SENTINEL" not in payload
    assert "UNRELATED_CHAT_HISTORY_SENTINEL" not in payload
    assert "UNRELATED_PRIOR_TOOL_OUTPUT_SENTINEL" not in payload
    assert "UNRELATED_STORED_TOOL_OUTPUT" not in payload
    assert cfg["api_key"] == "SYNTHETIC_TEST_KEY"
    assert open(os.path.join(root, "summary.md"), encoding="utf-8").read() == "Synthetic summary of selected note.\n"
    assert "complete" in output and "[find]" in output and "[summarize]" in output
    assert "[write]" in output and "[verify]" in output
    normalized_output = " ".join(output.split())
    assert "Sends the task prompt and only these selected file contents. No prior chat or tool results are sent." in normalized_output
    assert "Type s + Enter to send; o = offline; n = cancel" in normalized_output
    assert "sending may incur provider usage charges" in normalized_output
    assert "[Send these files]" not in output and "[Stay offline]" not in output
    assert max(map(len, output.splitlines())) <= 40, output
    assert all(len(prompt) <= 40 for prompt in prompts), prompts
    checkpoint = open(os.path.join(rt.data_dir, notes_mod._STATE_NAME), "rb").read()
    assert b"SYNTHETIC_TEST_KEY" not in checkpoint
    assert b"SYNTHETIC_SELECTED_NOTE_ONLY" not in checkpoint
    assert b"UNRELATED_CHAT_HISTORY_SENTINEL" not in checkpoint
    assert not any(name.startswith(".notes-summary-") and name.endswith(".draft")
                   for name in os.listdir(rt.data_dir))
    assert len(tools_mod.TOOLS) == 36  # no broad tool catalog changes


def test_notes_decline_and_offline_send_zero_requests():
    old_provider = notes_mod._single_provider_completion
    calls = []
    notes_mod._single_provider_completion = lambda *a, **k: calls.append((a, k))
    try:
        for choice, expected in (("n", "declined"), ("o", "offline_unavailable")):
            root = _notes_test_root({"one.md": "SYNTHETIC_NOTE"})
            rt = _notes_test_runtime()
            output, _ = _notes_run(rt, root, ["1", "y", "", choice])
            state = json.loads(open(os.path.join(rt.data_dir, notes_mod._STATE_NAME), encoding="utf-8").read())
            assert state["status"] == expected, (state, output)
            assert "no provider request was sent" in " ".join(output.split()), output
    finally:
        notes_mod._single_provider_completion = old_provider
    assert calls == [], calls


def test_notes_overwrite_decline_and_approval_are_exact_target_only():
    old_provider = notes_mod._single_provider_completion
    notes_mod._single_provider_completion = _notes_fake_provider("Synthetic replacement.")
    try:
        for answer in ("n", "y"):
            root = _notes_test_root({"one.md": "SYNTHETIC_NOTE"})
            with open(os.path.join(root, "summary.md"), "w", encoding="utf-8") as stream:
                stream.write("ORIGINAL_TARGET_SENTINEL")
            rt = _notes_test_runtime()
            answers = ["1", "y", "", "s", "", answer]
            if answer == "y":
                answers.append("")  # continue from write to verification
            output, _ = _notes_run(rt, root, answers)
            target = open(os.path.join(root, "summary.md"), encoding="utf-8").read()
            if answer == "n":
                assert target == "ORIGINAL_TARGET_SENTINEL"
                assert "overwrite declined" in output
            else:
                assert target == "Synthetic replacement.\n"
                assert "verified ./summary.md" in output
            assert not os.path.exists(os.path.join(root, "notes", "summary.md"))
    finally:
        notes_mod._single_provider_completion = old_provider


def test_notes_stop_resume_before_and_after_atomic_write():
    root = _notes_test_root({"one.md": "SYNTHETIC_NOTE"})
    rt = _notes_test_runtime()
    provider_calls = []
    replace_calls = []
    old_provider = notes_mod._single_provider_completion
    old_replace = notes_mod._atomic_replace

    fake_provider = _notes_fake_provider("Synthetic resumable summary.", provider_calls)

    def count_replace(*args, **kwargs):
        replace_calls.append(1)
        return old_replace(*args, **kwargs)

    notes_mod._single_provider_completion = fake_provider
    notes_mod._atomic_replace = count_replace
    try:
        output, _ = _notes_run(rt, root, ["1", "y", "", "s", "s"])
        state = json.loads(open(os.path.join(rt.data_dir, notes_mod._STATE_NAME), encoding="utf-8").read())
        assert state["status"] == "stopped" and state["stage"] == "ready_to_write"
        draft_path = notes_mod._draft_path(rt, state["task_id"])
        assert os.stat(draft_path).st_mode & 0o777 == 0o600
        assert not os.path.exists(os.path.join(root, "summary.md"))
        _notes_run(rt, root, [""], "resume")
        assert open(os.path.join(root, "summary.md"), encoding="utf-8").read() == "Synthetic resumable summary.\n"
        assert len(provider_calls) == 1 and len(replace_calls) == 1

        root2 = _notes_test_root({"one.md": "SYNTHETIC_NOTE_2"})
        rt2 = _notes_test_runtime()
        output2, _ = _notes_run(rt2, root2, ["1", "y", "", "s", "", "s"])
        state2 = json.loads(open(os.path.join(rt2.data_dir, notes_mod._STATE_NAME), encoding="utf-8").read())
        assert state2["status"] == "stopped" and state2["stage"] == "verify"
        before = open(os.path.join(root2, "summary.md"), "rb").read()
        _notes_run(rt2, root2, [], "resume")
        after = open(os.path.join(root2, "summary.md"), "rb").read()
        assert after == before
        assert len(provider_calls) == 2 and len(replace_calls) == 2
        assert "checkpoint saved" in output and "checkpoint saved" in output2
    finally:
        notes_mod._single_provider_completion = old_provider
        notes_mod._atomic_replace = old_replace


def test_notes_uncertain_provider_request_is_never_repeated_on_resume():
    root = _notes_test_root({"one.md": "SYNTHETIC_NOTE"})
    rt = _notes_test_runtime()
    old_cwd = os.getcwd()
    try:
        os.chdir(root)
        _, hashes = notes_mod._read_selected(root, ["one.md"])
    finally:
        os.chdir(old_cwd)
    state = notes_mod._new_state(root, ["one.md"], hashes)
    state["provider_status"] = "sent_outcome_unknown"
    state["provider_name"] = "synthetic-test-provider"
    state["model"] = "synthetic-model"
    state["stage"] = "ready_to_summarize"
    state["status"] = "stopped"
    notes_mod._save_state(rt, state)
    calls = []
    old_provider = notes_mod._single_provider_completion
    notes_mod._single_provider_completion = _notes_fake_provider("Fresh synthetic summary.", calls)
    try:
        output, _ = _notes_run(rt, root, ["l"], "resume")
        saved = json.loads(open(os.path.join(rt.data_dir, notes_mod._STATE_NAME), encoding="utf-8").read())
        assert saved["status"] == "stopped" and saved["provider_status"] == "sent_outcome_unknown"
        assert saved["stage"] == "ready_to_summarize" and saved["files"] == ["one.md"]
        assert saved["content_snapshot_sha256"] == state["content_snapshot_sha256"]
        assert calls == []
        assert "The provider may already have received the selected notes. The result is unknown; no automatic retry will happen." in " ".join(output.split())
        assert "Type r + Enter to review" in output

        # Reload from the same private data directory, then explicitly review,
        # recheck, and consent to one fresh request for the same exact snapshot.
        reloaded_rt = _notes_test_runtime()
        reloaded_rt.data_dir = rt.data_dir
        resumed, _ = _notes_run(reloaded_rt, root, ["r", "", "s", "s"], "resume")
    finally:
        notes_mod._single_provider_completion = old_provider
    saved = json.loads(open(os.path.join(rt.data_dir, notes_mod._STATE_NAME), encoding="utf-8").read())
    assert saved["status"] == "stopped" and saved["provider_status"] == "succeeded"
    assert len(calls) == 1
    assert "The provider may already have received the selected notes. The result is unknown; no automatic retry will happen." in " ".join(resumed.split())
    assert "./notes/" in resumed and "one.md" in resumed and "content snapshot" in resumed
    active_cfg = notes_mod.active_cfg(reloaded_rt)
    assert saved["consent_provider_binding"] == notes_mod._provider_binding(
        active_cfg.get("provider") or "default", active_cfg)
    assert saved["consent_snapshot_sha256"] == state["content_snapshot_sha256"]
    assert not os.path.exists(os.path.join(root, "summary.md"))


def test_notes_ctrl_c_unknown_outcome_copy_is_precise():
    root = _notes_test_root({"one.md": "SYNTHETIC_NOTE"})
    rt = _notes_test_runtime()
    calls = []
    old_provider_call = notes_mod._provider_call

    def interrupt_after_dispatch(runtime, state, *args):
        calls.append(1)
        state["provider_status"] = "sent_outcome_unknown"
        state["stage"] = "summarizing"
        raise KeyboardInterrupt()

    notes_mod._provider_call = interrupt_after_dispatch
    try:
        output, _ = _notes_run(rt, root, ["1", "y", "", "s"])
    finally:
        notes_mod._provider_call = old_provider_call

    saved = json.loads(open(os.path.join(rt.data_dir, notes_mod._STATE_NAME), encoding="utf-8").read())
    normalized = " ".join(output.split())
    assert calls == [1]
    assert saved["status"] == "stopped" and saved["provider_status"] == "sent_outcome_unknown"
    assert "The provider may already have received the selected notes. The result is unknown; no automatic retry will happen." in normalized
    assert "Interrupted after dispatch. Any late provider result will be ignored." in normalized
    assert "the provider may still receive" not in output
    assert not os.path.exists(os.path.join(root, "summary.md"))


def test_notes_pre_dispatch_checkpoint_failure_is_not_sent():
    root = _notes_test_root({"one.md": "SYNTHETIC_NOTE"})
    rt = _notes_test_runtime()
    calls = []
    old_provider = notes_mod._single_provider_completion
    old_save = notes_mod._save_state
    old_poll = notes_mod._poll_inflight_control
    old_stdin = notes_mod.sys.stdin

    def fail_dispatch_checkpoint(runtime, state):
        if state.get("provider_status") == "sent_outcome_unknown":
            raise PermissionError("synthetic checkpoint write failure")
        return old_save(runtime, state)

    notes_mod._single_provider_completion = _notes_fake_provider("must not be sent", calls)
    notes_mod._save_state = fail_dispatch_checkpoint
    notes_mod._poll_inflight_control = lambda: None
    notes_mod.sys.stdin = _NotesFakeTTY()
    try:
        output, _ = _notes_run(rt, root, ["1", "y", "", "s"])
    finally:
        notes_mod._single_provider_completion = old_provider
        notes_mod._save_state = old_save
        notes_mod._poll_inflight_control = old_poll
        notes_mod.sys.stdin = old_stdin
    saved = json.loads(open(os.path.join(rt.data_dir, notes_mod._STATE_NAME), encoding="utf-8").read())
    assert calls == []
    assert saved["status"] in ("stopped", "paused") and saved["provider_status"] == "not_requested"
    assert saved["stage"] == "ready_to_summarize" and saved["last_provider_binding"] is None
    assert "No provider request was made" in " ".join(output.split())
    assert not any(t.name == "alva-notes-provider" and t.is_alive() for t in threading.enumerate())


def test_notes_closed_dispatch_gate_fault_is_not_sent_and_resumable():
    def start_server():
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(0.1)
        accepted = threading.Event()
        closed = threading.Event()
        stop = threading.Event()
        request_bytes = bytearray()
        connections = []

        def serve():
            connection = None
            try:
                while not stop.is_set():
                    try:
                        connection, _ = listener.accept()
                        break
                    except socket.timeout:
                        continue
                if connection is None:
                    return
                connections.append(connection)
                connection.settimeout(0.1)
                accepted.set()
                while not stop.is_set():
                    try:
                        chunk = connection.recv(4096)
                    except socket.timeout:
                        continue
                    except OSError:
                        closed.set()
                        return
                    if not chunk:
                        closed.set()
                        return
                    request_bytes.extend(chunk)
            except OSError:
                closed.set()
            finally:
                listener.close()

        server_thread = threading.Thread(target=serve, name="notes-gate-test-server", daemon=True)
        server_thread.start()
        port = listener.getsockname()[1]

        def cleanup():
            stop.set()
            try:
                listener.close()
            except OSError:
                pass
            for connection in connections:
                try:
                    connection.close()
                except OSError:
                    pass
            server_thread.join(timeout=2.0)

        return ("http://127.0.0.1:%d/v1" % port, accepted, closed,
                request_bytes, server_thread, cleanup)

    old_gate = notes_mod._open_dispatch_gate
    old_save = notes_mod._save_state
    old_poll = notes_mod._poll_inflight_control
    old_stdin = notes_mod.sys.stdin
    notes_mod._poll_inflight_control = lambda: None
    notes_mod.sys.stdin = _NotesFakeTTY()
    try:
        cases = (
            ("fresh", "not_requested", KeyboardInterrupt),
            ("prior-uncertain", "sent_outcome_unknown", RuntimeError),
        )
        for case_name, prior_status, fault_type in cases:
            root = _notes_test_root({"one.md": "SYNTHETIC_NOTE"})
            rt = _notes_test_runtime()
            provider_name = rt.cfg["active"]
            cfg = rt.cfg["profiles"][provider_name]
            endpoint, accepted, closed, request_bytes, server_thread, cleanup = start_server()
            cfg["base_url"] = endpoint
            prior_binding = (
                notes_mod._provider_binding(provider_name, cfg)
                if prior_status == "sent_outcome_unknown" else None)
            attempt_history = []

            def record_attempt_save(runtime, state):
                result = old_save(runtime, state)
                if state.get("stage") == "summarizing":
                    attempt_history.append((state.get("provider_status"),
                                            state.get("last_provider_binding")))
                return result

            notes_mod._save_state = record_attempt_save
            if prior_status == "sent_outcome_unknown":
                old_cwd = os.getcwd()
                try:
                    os.chdir(root)
                    _, hashes = notes_mod._read_selected(root, ["one.md"])
                finally:
                    os.chdir(old_cwd)
                state = notes_mod._new_state(root, ["one.md"], hashes)
                state.update({
                    "status": "stopped", "provider_status": prior_status,
                    "provider_name": provider_name, "model": cfg["model"],
                    "stage": "ready_to_summarize", "last_provider_binding": prior_binding,
                })
                notes_mod._save_state(rt, state)

            def fail_before_gate_open(gate, selected_fault=fault_type):
                assert not gate.is_set()
                assert attempt_history and attempt_history[-1][0] == "sent_outcome_unknown"
                if selected_fault is KeyboardInterrupt:
                    raise KeyboardInterrupt()
                raise RuntimeError("synthetic fault before dispatch gate open")

            notes_mod._open_dispatch_gate = fail_before_gate_open
            answers = ["1", "y", "", "s"] if prior_status == "not_requested" else ["r", "", "s"]
            action = "" if prior_status == "not_requested" else "resume"
            try:
                output, _ = _notes_run(rt, root, answers, action)
                assert accepted.wait(2.0), "%s never reached the closed gate" % case_name
                assert closed.wait(2.0), "%s left the loopback connection open" % case_name
            finally:
                notes_mod._open_dispatch_gate = old_gate
                cleanup()

            saved = json.loads(open(os.path.join(rt.data_dir, notes_mod._STATE_NAME), encoding="utf-8").read())
            assert request_bytes == b"", (case_name, request_bytes)
            assert not server_thread.is_alive(), case_name
            assert not any(t.name == "alva-notes-provider" and t.is_alive()
                           for t in threading.enumerate()), case_name
            assert saved["status"] == "paused" and saved["provider_status"] == prior_status, (case_name, saved)
            assert saved["stage"] == "ready_to_summarize" and saved["files"] == ["one.md"]
            assert saved["last_provider_binding"] == prior_binding
            assert saved["consent_provider_binding"] is None and saved["consent_snapshot_sha256"] is None
            assert attempt_history[-1] == (prior_status, prior_binding), (case_name, attempt_history)
            assert "Request not sent" in output
            if prior_status == "not_requested":
                assert "No provider request was made" in " ".join(output.split())
            else:
                normalized = " ".join(output.split())
                assert "This new request was not sent." in normalized
                assert "The provider may already have received the selected notes. The result is unknown; no automatic retry will happen." in normalized
                assert "the request ended without a usable result" not in output
                assert "ALVA / Result uncertain" not in output
    finally:
        notes_mod._open_dispatch_gate = old_gate
        notes_mod._save_state = old_save
        notes_mod._poll_inflight_control = old_poll
        notes_mod.sys.stdin = old_stdin


def test_notes_config_drift_requires_fresh_consent_and_never_falls_back():
    root = _notes_test_root({"one.md": "SYNTHETIC_NOTE"})
    rt = _notes_test_runtime()
    provider_name = rt.cfg["active"]
    old_cfg = notes_mod.active_cfg(rt)
    old_binding = notes_mod._provider_binding(provider_name, old_cfg)
    old_cwd = os.getcwd()
    try:
        os.chdir(root)
        _, hashes = notes_mod._read_selected(root, ["one.md"])
    finally:
        os.chdir(old_cwd)
    state = notes_mod._new_state(root, ["one.md"], hashes)
    state.update({
        "status": "stopped", "provider_status": "sent_outcome_unknown",
        "provider_name": provider_name, "model": old_cfg["model"],
        "stage": "ready_to_summarize", "last_provider_binding": old_binding,
    })
    notes_mod._save_state(rt, state)
    new_cfg = rt.cfg["profiles"][provider_name]
    new_cfg["base_url"] = "https://new-provider.invalid/v2"
    new_cfg["model"] = "synthetic-model-v2"
    calls = []
    old_provider = notes_mod._single_provider_completion
    notes_mod._single_provider_completion = _notes_fake_provider("Fresh synthetic summary.", calls)
    try:
        declined, _ = _notes_run(rt, root, ["r", "", "n"], "resume")
        saved = json.loads(open(os.path.join(rt.data_dir, notes_mod._STATE_NAME), encoding="utf-8").read())
        assert calls == []
        assert saved["status"] == "stopped" and saved["provider_status"] == "sent_outcome_unknown"
        assert saved["last_provider_binding"] == old_binding
        assert "provider configuration changed" in declined
        assert "synthetic-model-v2" in declined and "endpoint ID:" in declined
        assert "No new provider request was sent" in " ".join(declined.split())

        # A second explicit review and send authorizes only the newly displayed
        # provider/model/endpoint and exact unchanged note snapshot.
        sent, _ = _notes_run(rt, root, ["r", "", "s", "s"], "resume")
    finally:
        notes_mod._single_provider_completion = old_provider
    saved = json.loads(open(os.path.join(rt.data_dir, notes_mod._STATE_NAME), encoding="utf-8").read())
    assert len(calls) == 1
    assert calls[0][1]["base_url"] == "https://new-provider.invalid/v2"
    assert calls[0][1]["model"] == "synthetic-model-v2"
    current_binding = notes_mod._provider_binding(provider_name, notes_mod.active_cfg(rt))
    assert saved["last_provider_binding"] == current_binding
    assert "provider configuration changed" in sent
    assert not os.path.exists(os.path.join(root, "summary.md"))


def test_notes_credential_drift_requires_fresh_consent_without_key_storage():
    root = _notes_test_root({"one.md": "SYNTHETIC_NOTE"})
    rt = _notes_test_runtime()
    provider_name = rt.cfg["active"]
    cfg = notes_mod.active_cfg(rt)
    original_key = cfg["api_key"]
    old_binding = notes_mod._provider_binding(provider_name, cfg)
    old_cwd = os.getcwd()
    try:
        os.chdir(root)
        _, hashes = notes_mod._read_selected(root, ["one.md"])
    finally:
        os.chdir(old_cwd)
    state = notes_mod._new_state(root, ["one.md"], hashes)
    state.update({
        "status": "stopped", "provider_status": "sent_outcome_unknown",
        "provider_name": provider_name, "model": cfg["model"],
        "stage": "ready_to_summarize", "last_provider_binding": old_binding,
    })
    notes_mod._save_state(rt, state)
    replacement_key = "SYNTHETIC_ROTATED_CREDENTIAL_DO_NOT_LEAK"
    cfg["api_key"] = replacement_key
    calls = []
    old_provider = notes_mod._single_provider_completion
    notes_mod._single_provider_completion = _notes_fake_provider("must not be sent", calls)
    try:
        output, _ = _notes_run(rt, root, ["r", "", "n"], "resume")
    finally:
        notes_mod._single_provider_completion = old_provider
    state_path = os.path.join(rt.data_dir, notes_mod._STATE_NAME)
    saved_text = open(state_path, encoding="utf-8").read()
    saved = json.loads(saved_text)
    assert calls == []
    assert saved["status"] == "stopped" and saved["provider_status"] == "sent_outcome_unknown"
    assert saved["last_provider_binding"] == old_binding
    assert notes_mod._provider_binding(provider_name, cfg) != old_binding
    assert "provider configuration changed" in output
    assert original_key not in saved_text and replacement_key not in saved_text
    assert original_key not in output and replacement_key not in output


def test_notes_endpoint_fingerprint_preserves_explicit_zero_port():
    endpoint, host = notes_mod._canonical_provider_endpoint({
        "base_url": "https://provider.invalid:0/v1",
    })
    assert endpoint == "https://provider.invalid:0/v1"
    assert host == "provider.invalid:0"
    default_cfg = {"base_url": "https://provider.invalid/v1", "model": "synthetic-model", "api_key": "KEY_A"}
    explicit_cfg = {"base_url": endpoint, "model": "synthetic-model", "api_key": "KEY_A"}
    assert notes_mod._provider_binding("default", default_cfg) != notes_mod._provider_binding("default", explicit_cfg)


def test_notes_endpoint_display_is_canonical_redacted_and_40_columns():
    root = _notes_test_root({"one.md": "SYNTHETIC_NOTE"})
    calls = []
    old_provider = notes_mod._single_provider_completion
    old_width = notes_mod.shutil.get_terminal_size
    notes_mod._single_provider_completion = lambda *a, **k: calls.append((a, k))
    notes_mod.shutil.get_terminal_size = lambda fallback: os.terminal_size((40, 24))
    try:
        rt = _notes_test_runtime()
        provider_name = rt.cfg["active"]
        rt.cfg["profiles"][provider_name]["base_url"] = (
            "HTTPS://Provider.Example:8443/api/custom/base/")
        valid_output, _ = _notes_run(rt, root, ["1", "y", "", "n"])

        unsafe_rt = _notes_test_runtime()
        unsafe_name = unsafe_rt.cfg["active"]
        unsafe_rt.cfg["profiles"][unsafe_name]["base_url"] = (
            "https://SYNTHETIC_USER:SYNTHETIC_CREDENTIAL@Provider.Example:8443/"
            "api/custom/base?token=SYNTHETIC_QUERY#SYNTHETIC_FRAGMENT")
        unsafe_output, _ = _notes_run(unsafe_rt, root, ["1", "y", "", "n"])
    finally:
        notes_mod._single_provider_completion = old_provider
        notes_mod.shutil.get_terminal_size = old_width

    expected_wrap = (
        "  endpoint:\n"
        "    https://provider.example:8443\n"
        "    /api/custom/base")
    for output in (valid_output, unsafe_output):
        assert expected_wrap in output, output
        assert max(map(len, output.splitlines())) <= 40, output
        assert "SYNTHETIC_TEST_KEY" not in output
    for secret in ("SYNTHETIC_USER", "SYNTHETIC_CREDENTIAL", "SYNTHETIC_QUERY",
                   "SYNTHETIC_FRAGMENT"):
        assert secret not in unsafe_output, unsafe_output
    assert "provider is not configured for a request" in " ".join(unsafe_output.split())
    assert calls == []


def test_notes_post_dispatch_failure_stays_uncertain_and_is_not_retried():
    root = _notes_test_root({"one.md": "SYNTHETIC_NOTE"})
    rt = _notes_test_runtime()
    calls = []
    old_provider = notes_mod._single_provider_completion
    old_poll = notes_mod._poll_inflight_control
    old_stdin = notes_mod.sys.stdin

    def disconnect_after_dispatch(messages, cfg, before_dispatch=None, cancel_handle=None):
        if before_dispatch:
            before_dispatch()
        calls.append((messages, cfg))
        raise OSError("synthetic disconnect after send")

    notes_mod._single_provider_completion = disconnect_after_dispatch
    notes_mod._poll_inflight_control = lambda: None
    notes_mod.sys.stdin = _NotesFakeTTY()
    try:
        output, _ = _notes_run(rt, root, ["1", "y", "", "s", "l"])
        resumed, _ = _notes_run(rt, root, ["l"], "resume")
    finally:
        notes_mod._single_provider_completion = old_provider
        notes_mod._poll_inflight_control = old_poll
        notes_mod.sys.stdin = old_stdin
    saved = json.loads(open(os.path.join(rt.data_dir, notes_mod._STATE_NAME), encoding="utf-8").read())
    assert len(calls) == 1
    assert saved["status"] == "stopped" and saved["provider_status"] == "sent_outcome_unknown"
    assert "Result uncertain" in output and "no automatic retry" in output
    assert "No provider request was made" not in resumed
    assert not os.path.exists(os.path.join(root, "summary.md"))


def test_notes_stop_cancels_worker_on_connect_headers_body_and_dns_stalls():
    hint = "  p + Enter to pause · s + Enter to stop"
    assert len(hint) == 40
    for mode, control in (("tls", "s"), ("headers", "p"), ("body", "s")):
        base, entered, peer_closed, request_count, server_thread, cleanup = _notes_start_stall_server(mode)
        root = _notes_test_root({"one.md": "SYNTHETIC_NOTE"})
        rt = _notes_test_runtime()
        notes_mod.active_cfg(rt)["base_url"] = base
        answers = ["1", "y", "", "s"] + (["l"] if mode == "body" else [])
        try:
            output, _, elapsed = _notes_run_with_pty_control(rt, root, answers, entered, control)
            assert elapsed < 2.0, (mode, elapsed)
            assert hint in output, (mode, output)
            assert peer_closed.wait(1.5), "client socket remained open in %s stall" % mode
            saved = json.loads(open(os.path.join(rt.data_dir, notes_mod._STATE_NAME), encoding="utf-8").read())
            if mode == "tls":
                assert saved["status"] == "stopped" and saved["provider_status"] == "not_requested"
                assert request_count[0] == 0
                assert "No provider request was made" in " ".join(output.split())
            else:
                assert saved["provider_status"] == "sent_outcome_unknown"
                assert request_count[0] == 1
                assert "Result uncertain" in output
                if mode == "headers":
                    assert saved["status"] == "paused" and "Pause was received" in output
                else:
                    assert saved["status"] == "stopped" and "Stop was received" in output
            assert not any(t.name == "alva-notes-provider" and t.is_alive()
                           for t in threading.enumerate()), "provider worker leaked after %s input" % control
            assert not server_thread.is_alive(), "synthetic connection handler leaked after %s input" % control
        finally:
            cleanup()

    # Feed actual s+Enter during a resolver subprocess stall as well.
    root = _notes_test_root({"one.md": "SYNTHETIC_NOTE"})
    rt = _notes_test_runtime()
    notes_mod.active_cfg(rt)["base_url"] = "https://synthetic-dns-stall.invalid/v1"
    old_dns_code = notes_mod._DNS_LOOKUP_CODE
    old_popen = notes_mod.subprocess.Popen
    children = []
    resolver_started = threading.Event()
    notes_mod._DNS_LOOKUP_CODE = "import time; time.sleep(30)"

    def record_popen(*args, **kwargs):
        proc = old_popen(*args, **kwargs)
        children.append(proc)
        resolver_started.set()
        return proc

    notes_mod.subprocess.Popen = record_popen
    try:
        output, _, elapsed = _notes_run_with_pty_control(
            rt, root, ["1", "y", "", "s"], resolver_started, "s")
    finally:
        notes_mod._DNS_LOOKUP_CODE = old_dns_code
        notes_mod.subprocess.Popen = old_popen
    saved = json.loads(open(os.path.join(rt.data_dir, notes_mod._STATE_NAME), encoding="utf-8").read())
    assert elapsed < 2.0
    assert hint in output
    assert children and children[0].poll() is not None
    assert saved["status"] == "stopped" and saved["provider_status"] == "not_requested"
    assert "No provider request was made" in " ".join(output.split())
    assert not any(t.name == "alva-notes-provider" and t.is_alive() for t in threading.enumerate())

    # The same resolver child is also bounded without user Stop.
    old_dns_timeout = notes_mod._DNS_TIMEOUT
    old_dns_code = notes_mod._DNS_LOOKUP_CODE
    old_popen = notes_mod.subprocess.Popen
    children = []
    notes_mod._DNS_TIMEOUT = 0.1
    notes_mod._DNS_LOOKUP_CODE = "import time; time.sleep(30)"
    notes_mod.subprocess.Popen = record_popen
    try:
        try:
            notes_mod._resolve_addresses("synthetic-dns-timeout.invalid", 443,
                                         notes_mod._TransportCancellation())
            raise AssertionError("stalled DNS lookup was not bounded")
        except notes_mod.NotesSummaryError as exc:
            assert "DNS lookup timed out" in str(exc)
    finally:
        notes_mod._DNS_TIMEOUT = old_dns_timeout
        notes_mod._DNS_LOOKUP_CODE = old_dns_code
        notes_mod.subprocess.Popen = old_popen
    assert children and children[0].poll() is not None


def test_notes_symlinks_and_traversal_are_blocked():
    parent = tempfile.mkdtemp(prefix="alva_notes_escape_")
    _TMP_DIRS.append(parent)
    outside = os.path.join(parent, "outside.txt")
    with open(outside, "w", encoding="utf-8") as stream:
        stream.write("OUTSIDE_SENTINEL")
    root = _notes_test_root({"safe.md": "SYNTHETIC_SAFE_NOTE"})
    os.symlink(outside, os.path.join(root, "notes", "escape.md"))
    os.symlink(outside, os.path.join(root, "summary.md"))
    old_cwd = os.getcwd()
    try:
        os.chdir(root)
        assert notes_mod._list_note_names(root) == ["safe.md"]
        try:
            notes_mod._read_selected(root, ["../outside.txt"])
            raise AssertionError("parent traversal was accepted")
        except notes_mod.NotesSummaryError:
            pass
    finally:
        os.chdir(old_cwd)
    rt = _notes_test_runtime()
    calls = []
    old_provider = notes_mod._single_provider_completion
    notes_mod._single_provider_completion = lambda *a, **k: calls.append(1)
    try:
        output, _ = _notes_run(rt, root, [])
    finally:
        notes_mod._single_provider_completion = old_provider
    assert calls == []
    assert "not a regular file" in output
    assert open(outside, encoding="utf-8").read() == "OUTSIDE_SENTINEL"

    root2 = _notes_test_root({})
    os.rmdir(os.path.join(root2, "notes"))
    os.symlink(parent, os.path.join(root2, "notes"))
    old_cwd = os.getcwd()
    try:
        os.chdir(root2)
        try:
            notes_mod._list_note_names(root2)
            raise AssertionError("notes directory symlink was accepted")
        except notes_mod.NotesSummaryError:
            pass
    finally:
        os.chdir(old_cwd)


def test_notes_private_state_paths_modes_and_rejections():
    import stat

    parent = tempfile.mkdtemp(prefix="alva_notes_state_security_")
    _TMP_DIRS.append(parent)
    state_dir = os.path.join(parent, "permissive-state")
    os.mkdir(state_dir, 0o755)
    os.chmod(state_dir, 0o755)
    root = _notes_test_root({"one.md": "SYNTHETIC_NOTE"})
    rt = _notes_test_runtime()
    rt.data_dir = state_dir
    old_cwd = os.getcwd()
    try:
        os.chdir(root)
        _, hashes = notes_mod._read_selected(root, ["one.md"])
    finally:
        os.chdir(old_cwd)
    state = notes_mod._new_state(root, ["one.md"], hashes)
    notes_mod._save_state(rt, state)
    state_path = notes_mod._state_path(rt)
    assert stat.S_IMODE(os.stat(state_dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(state_path).st_mode) == 0o600
    state["draft_sha256"] = notes_mod._save_draft(rt, state, "SYNTHETIC_DRAFT")
    notes_mod._save_state(rt, state)
    draft_path = notes_mod._draft_path(rt, state["task_id"])
    assert stat.S_IMODE(os.stat(draft_path).st_mode) == 0o600
    assert notes_mod._load_draft(rt, state) == "SYNTHETIC_DRAFT\n"

    # A checkpoint symlink must not replace or read through to its target.
    external = os.path.join(parent, "external-state")
    with open(external, "w", encoding="utf-8") as stream:
        stream.write("OUTSIDE_STATE_SENTINEL")
    os.unlink(state_path)
    os.symlink(external, state_path)
    try:
        notes_mod._save_state(rt, state)
        raise AssertionError("symlink checkpoint target was accepted")
    except notes_mod.NotesSummaryError:
        pass
    assert open(external, encoding="utf-8").read() == "OUTSIDE_STATE_SENTINEL"

    real_dir = os.path.join(parent, "real-state")
    os.mkdir(real_dir, 0o700)
    link_dir = os.path.join(parent, "state-link")
    os.symlink(real_dir, link_dir)
    link_rt = _notes_test_runtime()
    link_rt.data_dir = link_dir
    try:
        notes_mod._state_path(link_rt)
        raise AssertionError("symlink state directory was accepted")
    except (notes_mod.NotesSummaryError, OSError):
        pass

    non_directory = os.path.join(parent, "not-a-directory")
    with open(non_directory, "w", encoding="utf-8") as stream:
        stream.write("SYNTHETIC_FILE")
    non_directory_rt = _notes_test_runtime()
    non_directory_rt.data_dir = non_directory
    try:
        notes_mod._state_path(non_directory_rt)
        raise AssertionError("non-directory state path was accepted")
    except (notes_mod.NotesSummaryError, OSError):
        pass

    wrong_owner_dir = os.path.join(parent, "wrong-owner")
    os.mkdir(wrong_owner_dir, 0o700)
    old_uid = notes_mod._current_uid
    notes_mod._current_uid = lambda: os.getuid() + 1
    try:
        try:
            notes_mod._ensure_private_dir(wrong_owner_dir)
            raise AssertionError("wrong-owner state directory was accepted")
        except notes_mod.NotesSummaryError as exc:
            assert "not owned by the current user" in str(exc)
    finally:
        notes_mod._current_uid = old_uid


def test_notes_storage_survives_alva_and_xdg_relaunch_and_root_move():
    parent = tempfile.mkdtemp(prefix="alva_notes_relaunch_")
    _TMP_DIRS.append(parent)
    env_names = ("ALVA_DATA_DIR", "POCKET_DATA_DIR", "XDG_STATE_HOME")
    saved_env = {name: os.environ.get(name) for name in env_names}
    old_data_dir = notes_mod.DATA_DIR
    try:
        for mode in ("ALVA_DATA_DIR", "XDG_STATE_HOME"):
            root_a = _notes_test_root({"one.md": "SYNTHETIC_SAME_NOTE"})
            root_b = _notes_test_root({"one.md": "SYNTHETIC_SAME_NOTE"})
            old_install = os.path.join(parent, mode, "install-a", ".alvaagent")
            new_install = os.path.join(parent, mode, "install-b", ".alvaagent")
            shared = os.path.join(parent, mode, "shared-data")
            xdg_home = os.path.join(parent, mode, "xdg")
            for name in env_names:
                os.environ.pop(name, None)

            if mode == "ALVA_DATA_DIR":
                os.environ["ALVA_DATA_DIR"] = shared
                first_data_dir = second_data_dir = shared
                expected_state_dir = shared
            else:
                os.environ["XDG_STATE_HOME"] = xdg_home
                first_data_dir, second_data_dir = old_install, new_install
                expected_state_dir = os.path.join(xdg_home, "alvaagent")
                notes_mod.DATA_DIR = first_data_dir

            first = _notes_test_runtime()
            first.data_dir = first_data_dir
            old_cwd = os.getcwd()
            try:
                os.chdir(root_a)
                _, hashes = notes_mod._read_selected(root_a, ["one.md"])
            finally:
                os.chdir(old_cwd)
            state = notes_mod._new_state(root_a, ["one.md"], hashes)
            state.update({"status": "stopped", "provider_status": "not_requested"})
            notes_mod._save_state(first, state)
            task_id = state["task_id"]
            assert notes_mod._notes_data_dir(first) == os.path.abspath(expected_state_dir)

            if mode == "XDG_STATE_HOME":
                notes_mod.DATA_DIR = second_data_dir
            second = _notes_test_runtime()
            second.data_dir = second_data_dir
            output, _ = _notes_run(second, root_b, ["s"], "resume")
            resumed = notes_mod._load_state(second)
            assert resumed["task_id"] == task_id
            assert resumed["root"] == os.path.realpath(root_b)
            assert resumed["status"] == "stopped"
            assert "resumed in a new checkout" in output
            assert notes_mod._notes_data_dir(second) == os.path.abspath(expected_state_dir)
    finally:
        notes_mod.DATA_DIR = old_data_dir
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_notes_corrupt_unreadable_and_unwritable_checkpoints_fail_truthfully():
    root = _notes_test_root({"one.md": "SYNTHETIC_NOTE"})
    rt = _notes_test_runtime()
    state_path = os.path.join(rt.data_dir, notes_mod._STATE_NAME)
    os.makedirs(rt.data_dir, exist_ok=True)
    with open(state_path, "wb") as stream:
        stream.write(b"{broken-json")
    os.chmod(state_path, 0o600)
    original_bytes = open(state_path, "rb").read()
    output, _ = _notes_run(rt, root, [], "resume")
    assert "checkpoint is unreadable" in output
    assert open(state_path, "rb").read() == original_bytes

    old_read = notes_mod._read_private
    notes_mod._read_private = lambda *a, **k: (_ for _ in ()).throw(PermissionError())
    try:
        output2, _ = _notes_run(rt, root, [], "resume")
    finally:
        notes_mod._read_private = old_read
    assert "checkpoint could not be read" in output2
    assert open(state_path, "rb").read() == original_bytes

    rt2 = _notes_test_runtime()
    calls = []
    old_save = notes_mod._save_state
    old_provider = notes_mod._single_provider_completion
    notes_mod._save_state = lambda *a, **k: (_ for _ in ()).throw(PermissionError())
    notes_mod._single_provider_completion = lambda *a, **k: calls.append(1)
    try:
        output3, _ = _notes_run(rt2, root, ["1", "y"])
    finally:
        notes_mod._save_state = old_save
        notes_mod._single_provider_completion = old_provider
    assert "could not save the task checkpoint" in output3
    assert "checkpoint saved" not in output3
    assert calls == []


def test_notes_unreadable_input_and_disk_write_failure_do_not_claim_success():
    unreadable_root = _notes_test_root({"bad.txt": b"\xff\xfe"})
    rt = _notes_test_runtime()
    calls = []
    old_provider = notes_mod._single_provider_completion
    notes_mod._single_provider_completion = lambda *a, **k: calls.append(1)
    try:
        output, _ = _notes_run(rt, unreadable_root, ["1", "y"])
    finally:
        notes_mod._single_provider_completion = old_provider
    assert "not valid UTF-8" in output and calls == []
    assert not os.path.exists(os.path.join(rt.data_dir, notes_mod._STATE_NAME))

    root = _notes_test_root({"one.md": "SYNTHETIC_NOTE"})
    with open(os.path.join(root, "summary.md"), "w", encoding="utf-8") as stream:
        stream.write("PRESERVE_ON_DISK_ERROR")
    rt2 = _notes_test_runtime()
    old_provider = notes_mod._single_provider_completion
    old_replace = notes_mod._atomic_replace
    notes_mod._single_provider_completion = _notes_fake_provider("Synthetic output.")
    notes_mod._atomic_replace = lambda *a, **k: (_ for _ in ()).throw(OSError("synthetic disk failure"))
    try:
        output2, _ = _notes_run(rt2, root, ["1", "y", "", "s", "", "y"])
    finally:
        notes_mod._single_provider_completion = old_provider
        notes_mod._atomic_replace = old_replace
    assert "file or storage operation failed (OSError)" in output2
    assert "complete" not in output2
    assert open(os.path.join(root, "summary.md"), encoding="utf-8").read() == "PRESERVE_ON_DISK_ERROR"


def test_notes_repl_40_column_offline_entry():
    root = _notes_test_root({"one.md": "SYNTHETIC_PTY_NOTE"})
    data_dir = tempfile.mkdtemp(prefix="alva_notes_pty_data_")
    _TMP_DIRS.append(data_dir)
    script = (
        "import os, sys\n"
        "sys.path.insert(0, %r)\n" % ROOT +
        "os.environ['ALVA_DATA_DIR'] = %r\n" % data_dir +
        "os.environ['NO_COLOR'] = '1'\n"
        "os.chdir(%r)\n" % root +
        "from alvaagent.repl import main\n"
        "main()\n"
    )
    pid, fd = _pty_run(script)
    status = None
    out = b""
    try:
        import fcntl
        import struct
        import termios
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 40, 0, 0))
        out += _pty_drain_until(fd, b"> ", 15.0)
        assert b"> " in out, out[-1000:]
        os.write(fd, b"/notes\n")
        out += _pty_drain_until(fd, b"select numbers", 10.0)
        assert b"select numbers" in out, out[-1000:]
        os.write(fd, b"1\n")
        out += _pty_drain_until(fd, b"proceed with this scope", 10.0)
        assert b"proceed with this scope" in out, out[-1000:]
        os.write(fd, b"y\n")
        out += _pty_drain_until(fd, b"Enter=continue", 10.0)
        assert b"Enter=continue" in out, out[-1000:]
        os.write(fd, b"\n")
        out += _pty_drain_until(fd, b"Type o + Enter to stay offline;", 10.0)
        assert b"Type o + Enter to stay offline;" in out, out[-1000:]
        assert b"n = cancel" in out, out[-1000:]
        os.write(fd, b"o\n")
        out += _pty_drain_until(fd, b"no local summarizer", 10.0)
        assert b"no local summarizer" in out, out[-1000:]
        assert b"provider: default" in out
        assert b"SYNTHETIC_PTY_NOTE" not in out
        if b"> " not in out.split(b"no local summarizer", 1)[-1]:
            out += _pty_drain_until(fd, b"> ", 10.0)
        os.write(fd, b"/exit\n")
        out += _pty_drain_until(fd, b"bye", 10.0)
        assert b"bye" in out, out[-1000:]
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    finally:
        if status is None:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
            try:
                os.waitpid(pid, 0)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass
    saved = json.loads(open(os.path.join(data_dir, notes_mod._STATE_NAME), encoding="utf-8").read())
    assert saved["status"] == "offline_unavailable"
    assert not os.path.exists(os.path.join(root, "summary.md"))


def _capture_home_text(rt, console_mode="runtime"):
    """Capture the home renderer through Rich, its fallback, or a test probe."""
    output = io.StringIO()
    previous = tui_mod._CON
    if console_mode == "runtime":
        if hasattr(tui_mod, "_ShimConsole"):
            tui_mod._CON = tui_mod._ShimConsole()
            try:
                with contextlib.redirect_stdout(output):
                    tui_mod.banner(rt)
            finally:
                tui_mod._CON = previous
            return output.getvalue()
        console_mode = "rich"
    try:
        if console_mode == "fallback":
            class FallbackProbe:
                def print(self, *values, **_kwargs):
                    for value in values:
                        output.write(str(value) + "\n")
            tui_mod._CON = FallbackProbe()
        else:
            try:
                from rich.console import Console as RichConsole
            except ImportError:
                class RichProbe:
                    def print(self, *values, **kwargs):
                        assert kwargs.get("markup") is False
                        for value in values:
                            output.write(str(value) + "\n")
                tui_mod._CON = RichProbe()
            else:
                tui_mod._CON = RichConsole(file=output, width=40, force_terminal=False)
        tui_mod.banner(rt)
    finally:
        tui_mod._CON = previous
    return output.getvalue()


def _seed_paused_notes_checkpoint(rt, root):
    names = sorted(os.listdir(os.path.join(root, "notes")))
    hashes = {}
    for name in names:
        with open(os.path.join(root, "notes", name), "rb") as stream:
            hashes[name] = hashlib.sha256(stream.read()).hexdigest()
    state = notes_mod._new_state(root, names, hashes)
    state["status"] = "paused"
    state["stage"] = "ready_to_summarize"
    notes_mod._save_state(rt, state)


def test_task_first_home_copy_and_rich_fallback_rendering():
    rt = mkrt()
    home = _capture_home_text(rt)
    expected = [
        "ALVAAGENT", "Ready · no task running", "Choose /notes or /chat",
        "/notes  Summarize selected notes", "/chat   Open a conversation",
        "/help   More commands",
    ]
    positions = [home.index(line) for line in expected]
    assert positions == sorted(positions)
    assert "Available Tools" not in home and "Available Skills" not in home
    assert "run_command" not in home and "skill_list" not in home
    assert "no API key" not in home and "provider endpoint" not in home

    original_lines = tui_mod._home_task_lines
    tui_mod._home_task_lines = lambda _rt: [
        "Notes [paused] · /notes resume", "Phase · ready_to_summarize"]
    try:
        for mode in ("fallback", "rich"):
            rendered = _capture_home_text(rt, mode)
            assert "Notes [paused] · /notes resume" in rendered
            assert "Phase · ready_to_summarize" in rendered
            assert "[dim" not in rendered and "[bold" not in rendered
            assert "[paused]" in rendered
            assert "Available Tools" not in rendered
    finally:
        tui_mod._home_task_lines = original_lines


def test_notes_status_summary_is_shared_read_only_home_content():
    rt = mkrt()
    root = _notes_test_root({"private-note.md": "SYNTHETIC_PRIVATE_NOTE_CONTENT"})
    _seed_paused_notes_checkpoint(rt, root)

    summary = notes_mod.notes_status_summary(rt)
    assert summary == {
        "status": "paused", "stage": "ready_to_summarize",
        "provider_outcome_unknown": False,
        "lines": ["saved notes task: paused",
                  "phase: ready_to_summarize | resume with /notes resume"],
    }
    notes_status = io.StringIO()
    with contextlib.redirect_stdout(notes_status):
        notes_mod.cmd_notes(rt, "status")
    assert "saved notes task: paused" in notes_status.getvalue()
    assert "phase: ready_to_summarize | resume with /notes resume" in notes_status.getvalue()

    home = _capture_home_text(rt)
    assert "Notes paused · /notes resume" in home
    assert "Phase · ready_to_summarize" in home
    assert "private-note.md" not in home and "SYNTHETIC_PRIVATE_NOTE_CONTENT" not in home
    assert "./notes" not in home and "select numbers" not in home
    assert notes_mod.notes_status_summary(rt)["status"] == "paused"

    state = notes_mod._load_state(rt)
    state["provider_status"] = "sent_outcome_unknown"
    notes_mod._save_state(rt, state)
    summary = notes_mod.notes_status_summary(rt)
    assert summary["status"] == "paused" and summary["provider_outcome_unknown"] is True
    assert any("no automatic retry" in line for line in summary["lines"])
    home = _capture_home_text(rt)
    assert "Notes result unknown · /notes resume" in home
    saved = notes_mod._load_state(rt)
    assert saved["status"] == "paused" and saved["provider_status"] == "sent_outcome_unknown"
    assert "private-note.md" not in home and "SYNTHETIC_PRIVATE_NOTE_CONTENT" not in home

    state["status"] = "stopped"
    notes_mod._save_state(rt, state)
    stopped_summary = notes_mod.notes_status_summary(rt)
    assert stopped_summary["status"] == "stopped" and stopped_summary["provider_outcome_unknown"] is True
    assert "Notes result unknown · /notes resume" in _capture_home_text(rt)


def test_chat_provider_setup_warning_is_contextual_and_truthful():
    rt = mkrt()
    rt.history = []
    rt.session = "default"
    home = _capture_home_text(rt)
    assert "provider" not in home.lower() and "api key" not in home.lower()
    assert repl_mod._chat_setup_warning(rt) == (
        "Chat needs a provider endpoint. Run /provider add or /config.")
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        repl_mod.send_message(rt, "SYNTHETIC_CHAT_WITHOUT_PROVIDER")
    assert "Chat needs a provider endpoint" in output.getvalue()
    assert rt.history == []

    rt.cfg = config_mod._normalize_state({
        "provider": "openai", "base_url": "https://api.openai.com/v1",
        "api_key": "", "model": "gpt-4o-mini"})
    warning = repl_mod._chat_setup_warning(rt)
    assert warning == "No API key is saved for OpenAI. Run /provider openai or /config."

    rt.cfg = config_mod._normalize_state({
        "provider": "default", "base_url": "https://api.openai.com/v1",
        "api_key": "", "model": "gpt-4o-mini"})
    assert repl_mod._chat_setup_warning(rt) == (
        "No API key is saved for OpenAI. Run /config.")

    rt.cfg = config_mod._normalize_state({
        "provider": "custom", "base_url": "https://custom.invalid/v1",
        "api_key": "", "model": "custom-model"})
    assert repl_mod._chat_setup_warning(rt) is None


def test_chat_home_commands_help_and_completion():
    assert repl_mod._slash_complete("/ch", 0) == "/chat"
    assert repl_mod._slash_complete("/ho", 0) == "/home"
    help_output = io.StringIO()
    with contextlib.redirect_stdout(help_output):
        commands_mod.cmd_help()
    assert "/chat" in help_output.getvalue()
    assert "/home" in help_output.getvalue()


def test_task_home_chat_home_chat_cycle_40_column_pty():
    _mock_server()
    root = _notes_test_root({"private-note.md": "SYNTHETIC_PRIVATE_NOTE_CONTENT"})
    data_dir = tempfile.mkdtemp(prefix="alva_task_home_pty_data_")
    _TMP_DIRS.append(data_dir)
    rt = mock_rt(data_dir)
    pa.save_state(rt)
    _seed_paused_notes_checkpoint(rt, root)
    call_log = os.path.join(data_dir, "chat-send-calls.txt")
    multi_log = os.path.join(data_dir, "multi-command-calls.txt")
    compress_log = os.path.join(data_dir, "compress-command-calls.txt")
    script = (
        "import os, sys\n"
        "sys.path.insert(0, %r)\n" % ROOT +
        "os.environ['ALVA_DATA_DIR'] = %r\n" % data_dir +
        "os.environ['NO_COLOR'] = '1'\n"
        "os.chdir(%r)\n" % root +
        "import importlib\n"
        "repl_mod = importlib.import_module('alvaagent.repl')\n"
        "main = repl_mod.main\n"
        "call_log = %r\n" % call_log +
        "multi_log = %r\n" % multi_log +
        "compress_log = %r\n" % compress_log +
        "_original_send = repl_mod.send_message\n"
        "def _counted_send(rt, text):\n"
        "    with open(call_log, 'a', encoding='utf-8') as stream:\n"
        "        stream.write(text + '\\n')\n"
        "    return _original_send(rt, text)\n"
        "repl_mod.send_message = _counted_send\n"
        "def _counted_multi():\n"
        "    with open(multi_log, 'a', encoding='utf-8') as stream:\n"
        "        stream.write('called\\n')\n"
        "    return '[plain] SYNTHETIC_MULTI_MESSAGE'\n"
        "repl_mod.cmd_multi = _counted_multi\n"
        "def _counted_compress(rt, history, session):\n"
        "    with open(compress_log, 'a', encoding='utf-8') as stream:\n"
        "        stream.write('called\\n')\n"
        "repl_mod.cmd_compress = _counted_compress\n"
        "main()\n"
    )
    pid, fd = _pty_run(script)
    status = None
    out = b""
    try:
        import fcntl
        import struct
        import termios
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 40, 0, 0))
        out += _pty_drain_until(fd, b"> ", 15.0)
        for marker in (b"ALVAAGENT", "Notes paused · /notes resume".encode("utf-8"),
                       "Phase · ready_to_summarize".encode("utf-8"),
                       b"Choose /notes or /chat", b"/notes  Summarize selected notes",
                       b"/chat   Open a conversation", b"/help   More commands"):
            assert marker in out, (marker, out[-1200:])
        assert b"provider endpoint" not in out and b"no API key" not in out
        assert b"Available Tools" not in out and b"Available Skills" not in out
        assert b"[dim" not in out and b"[bold" not in out
        assert b"private-note.md" not in out and b"SYNTHETIC_PRIVATE_NOTE_CONTENT" not in out

        os.write(fd, b"SYNTHETIC_HOME_TEXT_SHOULD_NOT_SEND\n")
        home_input = _pty_drain_until(fd, b"> ", 10.0)
        out += home_input
        assert b"Text is not sent from home" in home_input
        assert b"Choose /notes or /chat" in home_input
        assert b"Conversation" not in home_input
        assert not os.path.exists(call_log)

        os.write(fd, b"/multi\n")
        home_multi = _pty_drain_until(fd, b"> ", 10.0)
        out += home_multi
        assert b"Open /chat before /multi" in home_multi
        assert b"Choose /notes or /chat" in home_multi
        assert not os.path.exists(call_log) and not os.path.exists(multi_log)

        os.write(fd, b"/compress\n")
        home_compress = _pty_drain_until(fd, b"> ", 10.0)
        out += home_compress
        assert b"Open /chat before /compress" in home_compress
        assert b"Choose /notes or /chat" in home_compress
        assert not os.path.exists(compress_log)

        os.write(fd, b"/chat\n")
        out += _pty_drain_until(fd, b"chat> ", 10.0)
        assert b"Conversation" in out and b"/home returns to tasks" in out
        os.write(fd, b"[plain] SYNTHETIC_CHAT_MESSAGE\n")
        out += _pty_drain_until(fd, b"PLAIN_OK this is a direct answer", 15.0)
        assert b"PLAIN_OK this is a direct answer" in out
        assert open(call_log, encoding="utf-8").read().splitlines() == [
            "[plain] SYNTHETIC_CHAT_MESSAGE"]
        out += _pty_drain_until(fd, b"chat> ", 10.0)

        os.write(fd, b"/redo\n")
        redo_chat = _pty_drain_until(fd, b"PLAIN_OK this is a direct answer", 15.0)
        out += redo_chat
        out += _pty_drain_until(fd, b"chat> ", 10.0)
        assert open(call_log, encoding="utf-8").read().splitlines() == [
            "[plain] SYNTHETIC_CHAT_MESSAGE", "[plain] SYNTHETIC_CHAT_MESSAGE"]

        os.write(fd, b"/multi\n")
        multi_chat = _pty_drain_until(fd, b"PLAIN_OK this is a direct answer", 15.0)
        out += multi_chat
        out += _pty_drain_until(fd, b"chat> ", 10.0)
        assert open(call_log, encoding="utf-8").read().splitlines() == [
            "[plain] SYNTHETIC_CHAT_MESSAGE", "[plain] SYNTHETIC_CHAT_MESSAGE",
            "[plain] SYNTHETIC_MULTI_MESSAGE"]
        assert open(multi_log, encoding="utf-8").read().splitlines() == ["called"]

        os.write(fd, b"/compress\n")
        out += _pty_drain_until(fd, b"chat> ", 10.0)
        assert open(compress_log, encoding="utf-8").read().splitlines() == ["called"]

        checkpoint = notes_mod._load_state(rt)
        assert checkpoint["status"] == "paused"
        checkpoint["provider_status"] = "sent_outcome_unknown"
        notes_mod._save_state(rt, checkpoint)
        os.write(fd, b"/home\n")
        out += _pty_drain_until(fd, b"> ", 10.0)
        assert "Notes result unknown · /notes resume".encode("utf-8") in out
        os.write(fd, b"/redo\n")
        home_redo = _pty_drain_until(fd, b"> ", 10.0)
        out += home_redo
        assert b"Open /chat before /redo" in home_redo
        assert b"Choose /notes or /chat" in home_redo
        assert len(open(call_log, encoding="utf-8").read().splitlines()) == 3
        assert open(multi_log, encoding="utf-8").read().splitlines() == ["called"]
        assert open(compress_log, encoding="utf-8").read().splitlines() == ["called"]
        os.write(fd, b"/multi\n")
        home_multi = _pty_drain_until(fd, b"> ", 10.0)
        out += home_multi
        assert b"Open /chat before /multi" in home_multi
        assert len(open(call_log, encoding="utf-8").read().splitlines()) == 3
        assert open(multi_log, encoding="utf-8").read().splitlines() == ["called"]
        os.write(fd, b"/compress\n")
        home_compress = _pty_drain_until(fd, b"> ", 10.0)
        out += home_compress
        assert b"Open /chat before /compress" in home_compress
        assert open(compress_log, encoding="utf-8").read().splitlines() == ["called"]
        os.write(fd, b"/chat\n")
        out += _pty_drain_until(fd, b"chat> ", 10.0)
        assert b"/home returns to tasks" in out
        os.write(fd, b"/home\n")
        out += _pty_drain_until(fd, b"> ", 10.0)
        assert b"Choose /notes or /chat" in out
        os.write(fd, b"/exit\n")
        out += _pty_drain_until(fd, b"bye", 10.0)
        assert b"bye" in out
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    finally:
        if status is None:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
            try:
                os.waitpid(pid, 0)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass
    final_summary = notes_mod.notes_status_summary(rt)
    assert final_summary["status"] == "paused"
    assert final_summary["provider_outcome_unknown"] is True


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


atexit.register(_on_exit)


if __name__ == "__main__":
    try:
        _mock_server()
        print("[mock server ready]")
        code = _run_all()
    finally:
        _cleanup()
    sys.exit(code)
