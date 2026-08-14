import ast
import datetime
import fnmatch
import json
import math
import os
import re
import select
import subprocess
import sys
import time
import urllib.request

from alvaagent.client import _STREAM_POLL
from alvaagent.config import DATA_DIR, TOOL_MODES, save_state
from alvaagent.context import Runtime, default_rt
from alvaagent.permissions import (
    PROJECT_DIR, classify_command, classify_file_action, request_permission,
)
from alvaagent.skills import (
    tool_skill_install, tool_skill_list, tool_skill_read, tool_skill_remove,
    tool_skill_save, tool_skill_sync_repo,
)
from alvaagent.store import (
    FEEDBACK_KEY, IMPROVEMENT_KEY, MEM_PREFIX, TODO_KEY,
    get as store_get, set as store_set,
)
from alvaagent.trace import _trace
from alvaagent.util import _atomic_write

# ---------------- autonomy: shell + files + skills ----------------
def tool_run_command(rt, command):
    """Run a shell command on the device (Termux). Risky commands ask the user."""
    command = str(command).strip()
    if not command:
        return {"ok": False, "error": "empty command"}
    if classify_command(command) == "ask" and not request_permission(rt, "run command: %s" % command[:160]):
        return {"ok": False, "error": "permission denied by user"}
    try:
        proc = subprocess.run(command, shell=True, capture_output=True,
                              text=True, timeout=120)
        return {"ok": proc.returncode == 0, "exit": proc.returncode,
                "stdout": (proc.stdout or "")[-6000:],
                "stderr": (proc.stderr or "")[-3000:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "command timed out after 120s"}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def tool_file_read(rt, path):
    path = str(path).strip()
    if not path:
        return {"ok": False, "error": "empty path"}
    if classify_file_action(rt, path, "read") == "ask" and not request_permission(rt, "read file: %s" % path):
        return {"ok": False, "error": "permission denied by user"}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        truncated = len(content) > 20000
        if truncated:
            content = content[:20000] + "\n... [truncated]"
        return {"ok": True, "path": path, "chars": len(content),
                "truncated": truncated, "content": content}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def tool_file_write(rt, path, content):
    path = str(path).strip()
    if not path:
        return {"ok": False, "error": "empty path"}
    if classify_file_action(rt, path, "write") == "ask" and not request_permission(rt, "write file: %s" % path):
        return {"ok": False, "error": "permission denied by user"}
    try:
        text = str(content)
        _atomic_write(path, text)
        return {"ok": True, "path": path, "chars": len(text)}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def tool_file_edit(rt, path, old, new):
    path = str(path).strip()
    if not path:
        return {"ok": False, "error": "empty path"}
    if classify_file_action(rt, path, "write") == "ask" and not request_permission(rt, "edit file: %s" % path):
        return {"ok": False, "error": "permission denied by user"}
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if old not in content:
            return {"ok": False, "error": "old string not found in %s" % path}
        updated = content.replace(old, new, 1)
        _atomic_write(path, updated)
        return {"ok": True, "path": path, "replaced": 1}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def tool_file_list(rt, path="."):
    path = str(path).strip() or "."
    try:
        entries = sorted(os.listdir(path))
        info = []
        for e in entries[:200]:
            p = os.path.join(path, e)
            info.append({"name": e,
                         "type": "dir" if os.path.isdir(p) else "file",
                         "size": os.path.getsize(p) if os.path.isfile(p) else 0})
        return {"ok": True, "path": os.path.abspath(path),
                "count": len(entries), "entries": info}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def tool_file_search(rt, pattern, path=None, max_depth=3):
    """Find files by glob pattern (e.g. '*.py', 'test*') under a directory.

    Depth-limited, read-only walk (hidden dirs skipped, results capped) so it
    stays cheap even on big folders like /sdcard.
    """
    pattern = str(pattern or "").strip()
    if not pattern:
        return {"ok": False, "error": "empty pattern"}
    base = str(path or PROJECT_DIR).strip() or PROJECT_DIR
    base = os.path.abspath(os.path.expanduser(base))
    if not os.path.isdir(base):
        return {"ok": False, "error": "not a directory: %s" % base}
    try:
        max_depth = max(0, int(max_depth))
    except (TypeError, ValueError):
        max_depth = 3
    matches = []
    start_depth = base.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(base):
        depth = root.rstrip(os.sep).count(os.sep) - start_depth
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        if depth >= max_depth:
            dirs[:] = []
        for f in files:
            if fnmatch.fnmatch(f, pattern) or fnmatch.fnmatch(os.path.join(root, f), pattern):
                p = os.path.join(root, f)
                matches.append({"path": p,
                                "size": os.path.getsize(p) if os.path.isfile(p) else 0})
                if len(matches) >= 100:
                    return {"ok": True, "pattern": pattern, "base": base,
                            "count": len(matches), "matches": matches,
                            "truncated": True}
        if len(matches) >= 100:
            break
    return {"ok": True, "pattern": pattern, "base": base,
            "count": len(matches), "matches": matches}




# ---------------- tools ----------------
def tool_todo_list(rt):
    todos = store_get(rt, TODO_KEY, [])
    return {"count": len(todos), "todos": todos}


def tool_todo_add(rt, text):
    text = str(text).strip()
    if not text:
        return {"ok": False, "error": "empty todo text"}
    todos = store_get(rt, TODO_KEY, [])
    todos.append({"text": text, "done": False})
    store_set(rt, TODO_KEY, todos)
    return {"ok": True, "index": len(todos) - 1, "text": text, "count": len(todos)}


def tool_todo_toggle(rt, index):
    todos = store_get(rt, TODO_KEY, [])
    try:
        i = int(index)
        todos[i]["done"] = not todos[i]["done"]
        store_set(rt, TODO_KEY, todos)
        return {"ok": True, "index": i, "done": todos[i]["done"], "text": todos[i]["text"]}
    except Exception as e:
        return {"ok": False, "error": "invalid index %r: %s" % (index, e)}


def tool_todo_remove(rt, index):
    todos = store_get(rt, TODO_KEY, [])
    try:
        i = int(index)
        removed = todos.pop(i)
        store_set(rt, TODO_KEY, todos)
        return {"ok": True, "removed": removed}
    except Exception as e:
        return {"ok": False, "error": "invalid index %r: %s" % (index, e)}


def tool_memory_save(rt, key, value):
    key = str(key).strip()
    if not key:
        return {"ok": False, "error": "empty key"}
    store_set(rt, MEM_PREFIX + key, str(value))
    return {"ok": True, "key": key, "stored": str(value)}


def tool_memory_recall(rt, key):
    key = str(key).strip()
    v = store_get(rt, MEM_PREFIX + key)
    if v is None:
        return {"ok": False, "key": key, "found": False}
    return {"ok": True, "key": key, "found": True, "value": v}


def tool_memory_list(rt):
    """List every saved memory fact (key + value)."""
    facts = [{"key": k[len(MEM_PREFIX):], "value": v}
             for k, v in rt.store.items() if k.startswith(MEM_PREFIX)]
    return {"ok": True, "count": len(facts), "facts": facts}


def tool_memory_search(rt, query=""):
    """Search saved memory facts by key or value (case-insensitive substring).
    An empty query returns everything (same as memory_list)."""
    q = str(query or "").strip().lower()
    facts = []
    for k, v in rt.store.items():
        if not k.startswith(MEM_PREFIX):
            continue
        key = k[len(MEM_PREFIX):]
        val = v.get("value", v) if isinstance(v, dict) else v
        if not q or q in key.lower() or q in str(val).lower():
            facts.append({"key": key, "value": val})
    return {"ok": True, "query": q, "count": len(facts), "facts": facts}


def tool_get_time():
    now = datetime.datetime.now()
    return {
        "iso": now.isoformat(),
        "date": now.strftime("%A, %B %d, %Y"),
        "time": now.strftime("%H:%M:%S"),
    }



def tool_feedback(rt, rating, notes=None):
    """Record user feedback on the agent's last response.

    rating: "good", "bad", or "neutral". notes: optional free text.
    The agent calls this when the user expresses satisfaction or frustration.
    """
    rating = str(rating or "").strip().lower()
    if rating not in ("good", "bad", "neutral"):
        return {"ok": False, "error": "rating must be good/bad/neutral"}
    notes = str(notes or "").strip()
    entry = {
        "rating": rating,
        "notes": notes,
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    fb = store_get(rt, FEEDBACK_KEY, [])
    fb.append(entry)
    if len(fb) > 50:
        fb = fb[-50:]
    store_set(rt, FEEDBACK_KEY, fb)
    return {"ok": True, "rating": rating, "stored": True}


def tool_improvement_set(rt, area, action):
    """Record an area to improve and a concrete action to take.

    area: short label like "response brevity".
    action: what to do about it.
    Updates an existing area if present, else appends.
    """
    area = str(area or "").strip()
    action = str(action or "").strip()
    if not area or not action:
        return {"ok": False, "error": "both area and action are required"}
    items = store_get(rt, IMPROVEMENT_KEY, [])
    updated = False
    for it in items:
        if it["area"].lower() == area.lower():
            it["action"] = action
            it["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
            updated = True
            break
    if not updated:
        items.append({
            "area": area,
            "action": action,
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "updated": datetime.datetime.now().isoformat(timespec="seconds"),
            "done": False,
        })
    if len(items) > 30:
        items = items[-30:]
    store_set(rt, IMPROVEMENT_KEY, items)
    return {"ok": True, "area": area, "stored": True}


def tool_improvement_list(rt):
    """List all improvement areas the agent has recorded."""
    return {"ok": True, "improvements": store_get(rt, IMPROVEMENT_KEY, [])}


def tool_improvement_done(rt, area):
    """Mark an improvement area as resolved."""
    area = str(area or "").strip().lower()
    if not area:
        return {"ok": False, "error": "area is required"}
    items = store_get(rt, IMPROVEMENT_KEY, [])
    for it in items:
        if it["area"].lower() == area:
            it["done"] = True
            it["resolved"] = datetime.datetime.now().isoformat(timespec="seconds")
            store_set(rt, IMPROVEMENT_KEY, items)
            return {"ok": True, "area": it["area"], "done": True}
    return {"ok": False, "error": "no improvement area named: %s" % area}


def tool_reflect(rt):
    """Run a structured self-reflection pass.

    Reads the last 5 feedback entries and all pending improvements. Returns a
    summary the agent can use to decide what to change.
    """
    fb = store_get(rt, FEEDBACK_KEY, [])
    imps = store_get(rt, IMPROVEMENT_KEY, [])
    pending = [i for i in imps if not i.get("done")]
    recent_bad = [e for e in fb if e.get("rating") == "bad"][-5:]
    return {
        "ok": True,
        "feedback_count": len(fb),
        "bad_count": len([e for e in fb if e.get("rating") == "bad"]),
        "recent_bad": recent_bad,
        "improvement_count": len(imps),
        "pending_count": len(pending),
        "pending": pending,
    }


def tool_web_fetch(rt, url):
    url = str(url).strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"ok": False, "error": "only http/https URLs are allowed"}
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "alvaagent-tui/1.0", "Accept-Encoding": "identity"})
        with urllib.request.urlopen(req, timeout=20) as r:
            status = int(r.getcode())
            raw = r.read(300000).decode("utf-8", errors="replace")
        # crude HTML -> text
        text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
        text = re.sub(r"(?is)<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return {"ok": status < 400, "status": status, "chars": len(text), "snippet": text[:2500]}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def _safe_factorial(n):
    n = int(n)
    if n < 0 or n > 10000:
        raise ValueError("factorial argument must be between 0 and 10000")
    return math.factorial(n)


_CALC_ALLOWED = {
    "pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf,
    "abs": abs, "round": round, "min": min, "max": max,
    "sqrt": math.sqrt, "pow": math.pow, "log": math.log, "log10": math.log10,
    "log2": math.log2, "exp": math.exp, "sin": math.sin, "cos": math.cos,
    "tan": math.tan, "asin": math.asin, "acos": math.acos, "atan": math.atan,
    "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
    "floor": math.floor, "ceil": math.ceil, "trunc": math.trunc,
    "fabs": math.fabs, "degrees": math.degrees, "radians": math.radians,
    "gcd": math.gcd, "hypot": math.hypot,
    "atan2": math.atan2, "copysign": math.copysign, "remainder": math.remainder,
    "factorial": _safe_factorial,
}


def _calc_eval(node):
    if isinstance(node, ast.Expression):
        return _calc_eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise ValueError("only numeric constants allowed")
    if isinstance(node, ast.BinOp):
        l, r = _calc_eval(node.left), _calc_eval(node.right)
        op = type(node.op)
        if op is ast.Add: return l + r
        if op is ast.Sub: return l - r
        if op is ast.Mult: return l * r
        if op is ast.Div:
            if r == 0: raise ValueError("division by zero")
            return l / r
        if op is ast.FloorDiv:
            if r == 0: raise ValueError("division by zero")
            return l // r
        if op is ast.Mod:
            if r == 0: raise ValueError("modulo by zero")
            return l % r
        if op is ast.Pow:
            if isinstance(r, (int, float)) and not isinstance(r, bool) and abs(r) > 1000:
                raise ValueError("exponent too large")
            return l ** r
        raise ValueError("operator not allowed: %s" % op.__name__)
    if isinstance(node, ast.UnaryOp):
        v = _calc_eval(node.operand)
        if isinstance(node.op, ast.UAdd): return v
        if isinstance(node.op, ast.USub): return -v
        raise ValueError("unary operator not allowed")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("only simple function calls allowed")
        fn = _CALC_ALLOWED.get(node.func.id)
        if fn is None:
            raise ValueError("function not allowed: %s" % node.func.id)
        args = [_calc_eval(a) for a in node.args]
        kwargs = {k.arg: _calc_eval(k.value) for k in node.keywords}
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            raise ValueError("call failed: %s" % e)
    if isinstance(node, ast.Name):
        if node.id in _CALC_ALLOWED and isinstance(_CALC_ALLOWED[node.id], (int, float)):
            return _CALC_ALLOWED[node.id]
        raise ValueError("name not allowed: %s" % node.id)
    raise ValueError("syntax not allowed: %s" % type(node).__name__)


def _fmt_num(x):
    try:
        if isinstance(x, float) and math.isfinite(x) and x.is_integer():
            return int(x)
    except (OverflowError, ValueError):
        pass
    return x


def tool_calculator(expression):
    if not isinstance(expression, str):
        raise ValueError("expression must be a string")
    if len(expression) > 500:
        raise ValueError("expression too long")
    tree = ast.parse(expression, mode="eval")
    result = _fmt_num(_calc_eval(tree))
    # complex results (e.g. (-8)**0.5) aren't JSON-serializable and would
    # crash the request when the tool result is placed in the chat history.
    if isinstance(result, complex):
        raise ValueError("result is complex - not supported")
    if isinstance(result, (int, float)) and not isinstance(result, bool):
        try:
            if isinstance(result, float) and not math.isfinite(result):
                raise ValueError("result is infinite")
            if isinstance(result, int) and result.bit_length() > 512:
                raise ValueError("result too large to display")
            if isinstance(result, float) and abs(result) > 1e18:
                raise ValueError("result too large to display")
        except (OverflowError, ValueError) as e:
            raise ValueError(str(e))
    return {"ok": True, "expression": expression, "result": result}


_PY_RUN_TIMEOUT = 120     # wall-clock seconds for the python child (like run_command)
_PY_MAX_BYTES = 200_000   # hard cap on bytes read from the child before we kill it
_PY_MAX_CHARS = 5000      # chars of output returned to the agent


def classify_python(code):
    """allow / ask for Python code (best-effort risk scan).

    Code that only computes (math, strings, pure data) runs freely; code that
    can touch the device - imports of os/subprocess/shutil/sys, filesystem
    access, exec/eval/__import__, path strings outside /tmp - asks the user
    first, mirroring classify_command's deny-by-default stance.
    """
    import re as _re
    low = _re.sub(r"#[^\n]*", "", str(code))
    patterns = (
        r"\b(import|from)\s+(os|sys|subprocess|shutil|pathlib|builtins)\b",
        r"\b__import__\s*\(|\beval\s*\(|\bexec\s*\(|\bglobals\s*\(|\blocals\s*\(|\bvars\s*\(",
        r"\bopen\s*\(",
        r"\bos\.\s*(system|popen|remove|unlink|rmdir|removedirs|rename|replace|chmod|chown|"
        r"listdir|scandir|walk|makedirs|mkdir|symlink|kill)\b",
        r"\bshutil\.\s*(rmtree|move|copy|copy2|copyfile|copytree|chown)\b",
        r"\bsubprocess\.\s*[A-Za-z_]+|\bPopen\b|\bcheck_output\b|\bcheck_call\b|\bgetoutput\b",
        r"[\"']/(?:sdcard|data|etc|root|bin|system)|[\"']\.\./|[\"']/tmp/",
    )
    for p in patterns:
        if _re.search(p, low):
            return "ask"
    return "allow"


def tool_run_python(rt, code):
    """Execute Python code in a child process and return the output.

    The code runs under `python -c` in a separate process with a wall-clock
    timeout and a hard output cap, so runaway loops or huge prints can't hang
    the agent. Code that touches the device asks the user first (same as
    run_command). Pure computation runs freely.
    """
    code = str(code).strip()
    if not code:
        return {"ok": False, "error": "empty code"}
    if len(code) > 10000:
        return {"ok": False, "error": "code too long (>10000 chars)"}
    if classify_python(code) == "ask" and not request_permission(rt, "run python: %s" % code[:160]):
        return {"ok": False, "error": "permission denied by user"}
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    out = []
    total = 0
    deadline = time.monotonic() + _PY_RUN_TIMEOUT
    reason = None
    try:
        while proc.poll() is None:
            if rt.cancel.is_set():
                proc.kill()
                reason = "cancelled by user"
                break
            if time.monotonic() > deadline:
                proc.kill()
                reason = "timed out after %ds" % _PY_RUN_TIMEOUT
                break
            rlist, _, _ = select.select([proc.stdout], [], [], _STREAM_POLL)
            if rlist:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    break
                total += len(chunk)
                out.append(chunk.decode("utf-8", errors="replace"))
                if total > _PY_MAX_BYTES:
                    proc.kill()
                    reason = "output exceeded the %d-byte cap" % _PY_MAX_BYTES
                    break
        proc.wait(timeout=5)
        tail = proc.stdout.read()
        if tail:
            total += len(tail)
            out.append(tail.decode("utf-8", errors="replace"))
    except Exception as e:
        reason = "%s: %s" % (type(e).__name__, e)
    rc = proc.returncode
    output = "".join(out)
    if len(output) > _PY_MAX_CHARS:
        output = output[:_PY_MAX_CHARS] + "\n... (truncated)"
    if reason:
        return {"ok": False, "error": reason, "exit": rc, "output": output[:5000]}
    return {"ok": rc == 0, "exit": rc, "output": (output or "(no output)"), "chars": len(output)}



TOOLS = [
    {"type": "function", "function": {
        "name": "calculator",
        "description": "Evaluate a mathematical expression precisely. Supports + - * / // % **, parentheses, constants pi/e/tau, and math functions such as sqrt, sin, cos, tan, log, log10, exp, floor, ceil, abs, round, factorial, gcd.",
        "parameters": {"type": "object", "properties": {
            "expression": {"type": "string", "description": "Math expression to evaluate, e.g. 'sqrt(2**10) + 3*4'"}},
            "required": ["expression"]}}},
    {"type": "function", "function": {
        "name": "run_python",
        "description": "Execute Python code in a sandboxed child process and return stdout/stderr. Use for calculations, data processing, or any pure-Python task. Runs with a 120s timeout and output cap. Code that imports os/subprocess/shutil/sys, touches the filesystem, or uses exec/eval asks the user for permission first (like run_command).",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string", "description": "Python code to execute"}},
            "required": ["code"]}}},
    {"type": "function", "function": {
        "name": "web_fetch",
        "description": "Fetch and read the text content of a URL (http/https only). Returns status code and a text snippet.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "The URL to fetch"}},
            "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "get_time",
        "description": "Get the current date and time on the user's device. Use this whenever a task depends on 'now' (timestamps, file ages, scheduling, relative dates like 'tomorrow'). Do not guess the current date from memory.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "memory_save",
        "description": "Save a fact to the on-device memory store so it can be recalled later. Use for durable user preferences, recurring details, or anything worth remembering across sessions (e.g. 'user is on Android/Termux'). Prefer specific keys over vague ones.",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string", "description": "Short label for the fact (e.g. 'timezone', 'editor')"},
            "value": {"type": "string", "description": "The fact to remember"}},
            "required": ["key", "value"]}}},
    {"type": "function", "function": {
        "name": "memory_recall",
        "description": "Recall a previously saved fact from on-device memory by its exact key. Use memory_search instead when you know the topic but not the exact key.",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string", "description": "The label of the fact to recall"}},
            "required": ["key"]}}},
    {"type": "function", "function": {
        "name": "memory_search",
        "description": "Search on-device memory by key or value (case-insensitive substring). Use this when you need a fact but are unsure of its exact key.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Substring to match against keys or values (empty returns all facts)"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "memory_list",
        "description": "List every saved memory fact (key + value). Use when you need an overview of what the agent remembers, or before saving a new fact to avoid duplicates.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "todo_add",
        "description": "Add a new task to the user's to-do list. Use when a multi-step request is underway so progress stays visible.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "Task text"}},
            "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "todo_list",
        "description": "List all tasks in the user's to-do list with done/undone status. Use before working on or updating tasks.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "todo_toggle",
        "description": "Mark a task as done or undone. Use to close out a task once its work is finished and verified.",
        "parameters": {"type": "object", "properties": {
            "index": {"type": "integer", "description": "Zero-based index of the task (see todo_list)"}},
            "required": ["index"]}}},
    {"type": "function", "function": {
        "name": "todo_remove",
        "description": "Remove a task from the to-do list.",
        "parameters": {"type": "object", "properties": {
            "index": {"type": "integer", "description": "Zero-based index of the task"}},
            "required": ["index"]}}},
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Run a shell command on the device (Termux). Read-only commands run freely; mutating or unknown commands ask the user for permission first.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "The shell command to run"}},
            "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "file_read",
        "description": "Read a text file from the device. Returns the content (truncated past 20000 chars).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Absolute path of the file"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "file_write",
        "description": "Write text to a file (creates parent dirs). Writes inside the project folder are allowed; elsewhere asks the user.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Absolute path of the file"},
            "content": {"type": "string", "description": "Full text to write"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "file_edit",
        "description": "Replace the first occurrence of a string in a file. Like file_write, out-of-project paths ask the user.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Absolute path of the file"},
            "old": {"type": "string", "description": "Exact substring to find"},
            "new": {"type": "string", "description": "Replacement text"}},
            "required": ["path", "old", "new"]}}},
    {"type": "function", "function": {
        "name": "file_list",
        "description": "List the contents of a directory (name, type, size).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory path (default: current dir)"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "file_search",
        "description": "Find files by glob pattern (e.g. '*.py', 'test*') under a directory. Depth-limited and read-only - use this before file_read/file_edit when the exact path is unknown.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Glob pattern to match file names"},
            "path": {"type": "string", "description": "Directory to search (default: the project folder)"},
            "max_depth": {"type": "integer", "description": "How many subdirectory levels to descend (default 3)"}},
            "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "feedback",
        "description": "Record user feedback on the agent's last response (good/bad/neutral + optional notes). Call this when the user expresses satisfaction or frustration so the agent can learn what to repeat or avoid.",
        "parameters": {"type": "object", "properties": {
            "rating": {"type": "string", "description": "One of: good, bad, neutral"},
            "notes": {"type": "string", "description": "Optional free-text context"}}},
        "required": ["rating"]}},
    {"type": "function", "function": {
        "name": "improvement_set",
        "description": "Record an area the agent should improve and a concrete action to take. Call this when feedback or mistakes reveal a pattern to fix (e.g. are too verbose, keep making the same mistake).",
        "parameters": {"type": "object", "properties": {
            "area": {"type": "string", "description": "Short label for the area to improve"},
            "action": {"type": "string", "description": "Concrete step the agent plans to take"}}},
        "required": ["area", "action"]}},
    {"type": "function", "function": {
        "name": "improvement_list",
        "description": "List all improvement areas the agent has recorded for itself.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "improvement_done",
        "description": "Mark an improvement area as resolved after the agent has verified the fix works.",
        "parameters": {"type": "object", "properties": {
            "area": {"type": "string", "description": "The area to mark done"}}},
        "required": ["area"]}},
    {"type": "function", "function": {
        "name": "self_test",
        "description": "Run the harness self-test suite (test_tui.py) to validate the TUI after editing its own source code. Returns pass/fail + output.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "reflect",
        "description": "Run a structured self-reflection: read recent feedback and pending improvements. Call this when idle or after finishing a task to decide if anything needs fixing.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "skill_list",
        "description": "List available on-device skills (Hermes-style: YAML frontmatter + categorized storage). ALWAYS call this before starting a substantial task and read any skill whose name or tags match the task - skills encode the user's preferred way of doing that kind of work.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "skill_read",
        "description": "Read the full body of a named skill (name or category/name). Returns the skill's YAML frontmatter (name, description, tags, related_skills) plus its procedure body. Apply the skill's guidance faithfully when it matches the current task.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Skill name, or category/name for a categorized skill"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "skill_save",
        "description": "Save a reusable procedure as a skill so it can be applied on later tasks. Give it a descriptive name (lowercase-hyphenated) and a body that states the TRIGGER (when to use it) followed by numbered STEPS. Use the category parameter to place it in a category folder (Hermes-style). Only save genuinely reusable, non-obvious procedures.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Skill name, lowercase-hyphenated, without .md"},
            "content": {"type": "string", "description": "Skill body: a one-line trigger condition followed by concise numbered steps. May include a YAML frontmatter block between --- fences (name, description, version, tags, related_skills)."},
            "category": {"type": "string", "description": "Optional category folder (e.g. 'productivity'). When omitted the skill is saved flat."}},
            "required": ["name", "content"]}}},
    {"type": "function", "function": {
        "name": "skill_remove",
        "description": "Delete a skill from the device by name (or category/name). Use after confirming with the user that a skill should be removed.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Skill name, or category/name for a categorized skill"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "skill_install",
        "description": "Install a skill from a local .md file path or a URL (a raw.githubusercontent.com link, or any GitHub blob URL which is auto-rewritten to raw). Fetches the full markdown, parses its frontmatter, and saves it on-device. Use this whenever the user shares a skill as a link or file.",
        "parameters": {"type": "object", "properties": {
            "source": {"type": "string", "description": "Local .md path or an http(s) URL to the skill file"},
            "category": {"type": "string", "description": "Optional category folder to save the skill into"}},
            "required": ["source"]}}},
    {"type": "function", "function": {
        "name": "skill_sync_repo",
        "description": "Bulk-import a whole skills repository: clone a git repo (asks the user for permission, like run_command) and install every .md as a skill, using folder names as categories. Use when the user hands you a GitHub repo that contains skills.",
        "parameters": {"type": "object", "properties": {
            "repo": {"type": "string", "description": "Git clone URL, e.g. https://github.com/owner/skills-repo.git"},
            "subdir": {"type": "string", "description": "Optional: only import skills under this subfolder"}},
            "required": ["repo"]}}},
]

# --- tiered tool selection -------------------------------------------------
# The model only sees a curated CORE set by default (~half of the registry).
# Advertising 28 tools at once makes the model mis-pick tools and slows every
# turn; the meta tools (skills, self-improvement, self-test, reflect) stay one
# `/tools` keystroke away. The mode lives on the Runtime (rt.tool_mode) and is
# persisted via config.json under "tool_mode".
_CORE_TOOL_NAMES = {
    "calculator", "run_python", "web_fetch", "get_time",
    "memory_save", "memory_recall", "memory_search",
    "todo_add", "todo_list", "todo_toggle",
    "run_command", "file_read", "file_write", "file_list", "file_edit",
}

_ADVANCED_TOOL_NAMES = {
    "memory_list", "todo_remove", "file_search",
    "feedback", "improvement_set", "improvement_list", "improvement_done",
    "self_test", "reflect",
    "skill_list", "skill_read", "skill_save", "skill_remove",
    "skill_install", "skill_sync_repo",
}


def visible(rt):
    """Tool schemas sent to the model: the CORE set, or everything in 'full' mode."""
    if rt.tool_mode == "full":
        return TOOLS
    return [t for t in TOOLS if t["function"]["name"] in _CORE_TOOL_NAMES]


def maybe_enable_full(rt, name):
    """Lazy-load advanced tools: the first time the model calls an advanced
    tool while in core mode, widen the advertised set to 'full' (one-way until
    the user runs /tools core). Returns True when the mode was just switched."""
    if rt.tool_mode != "full" and name in _ADVANCED_TOOL_NAMES:
        rt.tool_mode = "full"
        _trace({"event": "tool_mode", "mode": "full", "tool": name,
                "reason": "advanced tool requested by the model"})
        return True
    return False


def sync_tool_mode(rt):
    """Restore the persisted tool mode onto the runtime (no cycle: config is a leaf)."""
    rt.tool_mode = rt.cfg.get("tool_mode", "core")


def set_mode(rt, mode):
    """Switch the advertised tool set and persist the choice in config.json."""
    if mode not in TOOL_MODES:
        mode = "core"
    rt.tool_mode = mode
    rt.cfg["tool_mode"] = mode
    try:
        save_state(rt)
    except Exception:
        pass
    _trace({"event": "tool_mode", "mode": mode, "reason": "user /tools"})


def active_tools():
    """Flat Phase-A bridge: the default rt's advertised tool set."""
    return visible(default_rt())


def _sync_tool_mode(state):
    """Flat Phase-A bridge: mirror the caller's config dict onto the default
    rt, then sync its tool_mode from the persisted value."""
    rt = default_rt()
    if isinstance(state, dict):
        rt.cfg = state
    sync_tool_mode(rt)


def _set_tool_mode(state, mode):
    """Flat Phase-A bridge: mirror the caller's config dict onto the default
    rt, then switch (and persist) the tool mode on it."""
    rt = default_rt()
    if isinstance(state, dict):
        rt.cfg = state
    set_mode(rt, mode)

class Tools:
    """Every tool as a method keyed by its TOOLS schema name.

    `dispatch_tool` builds one of these per call so each tool resolves its
    runtime (store, approvals, cancel flag, skills dir) through the rt it is
    dispatched against instead of module globals.
    """

    def __init__(self, rt):
        self.rt = rt

    def calculator(self, args):
        return tool_calculator(args.get("expression"))

    def run_python(self, args):
        return tool_run_python(self.rt, args.get("code"))

    def web_fetch(self, args):
        return tool_web_fetch(self.rt, args.get("url"))

    def get_time(self, args):
        return tool_get_time()

    def memory_save(self, args):
        return tool_memory_save(self.rt, args.get("key"), args.get("value"))

    def memory_recall(self, args):
        return tool_memory_recall(self.rt, args.get("key"))

    def memory_search(self, args):
        return tool_memory_search(self.rt, args.get("query"))

    def memory_list(self, args):
        return tool_memory_list(self.rt)

    def todo_add(self, args):
        return tool_todo_add(self.rt, args.get("text"))

    def todo_list(self, args):
        return tool_todo_list(self.rt)

    def todo_toggle(self, args):
        return tool_todo_toggle(self.rt, args.get("index"))

    def todo_remove(self, args):
        return tool_todo_remove(self.rt, args.get("index"))

    def run_command(self, args):
        return tool_run_command(self.rt, args.get("command"))

    def file_read(self, args):
        return tool_file_read(self.rt, args.get("path"))

    def file_write(self, args):
        return tool_file_write(self.rt, args.get("path"), args.get("content"))

    def file_edit(self, args):
        return tool_file_edit(self.rt, args.get("path"), args.get("old"), args.get("new"))

    def file_list(self, args):
        return tool_file_list(self.rt, args.get("path"))

    def file_search(self, args):
        return tool_file_search(
            self.rt, args.get("pattern"), args.get("path"), args.get("max_depth"))

    def feedback(self, args):
        return tool_feedback(self.rt, args.get("rating"), args.get("notes"))

    def improvement_set(self, args):
        return tool_improvement_set(self.rt, args.get("area"), args.get("action"))

    def improvement_list(self, args):
        return tool_improvement_list(self.rt)

    def improvement_done(self, args):
        return tool_improvement_done(self.rt, args.get("area"))

    def self_test(self, args):
        return tool_self_test()

    def reflect(self, args):
        return tool_reflect(self.rt)

    def skill_list(self, args):
        return tool_skill_list(self.rt)

    def skill_read(self, args):
        return tool_skill_read(self.rt, args.get("name"))

    def skill_save(self, args):
        return tool_skill_save(
            self.rt, args.get("name"), args.get("content"), category=args.get("category"))

    def skill_remove(self, args):
        return tool_skill_remove(self.rt, args.get("name"))

    def skill_install(self, args):
        return tool_skill_install(self.rt, args.get("source"), args.get("category"))

    def skill_sync_repo(self, args):
        return tool_skill_sync_repo(self.rt, args.get("repo"), args.get("subdir"))



_TOOL_ERROR_HINTS = {
    "web_fetch": "hint: the URL is unreachable or the site blocks bots; try a different/mobile URL, or run_command('curl -sL <url>') as a fallback",
    "run_command": "hint: the command was blocked or failed; retry a read-only variant, or ask the user to approve/run it themselves",
    "file_read": "hint: check the absolute path exists and is readable (file_search finds the right path)",
    "file_write": "hint: the path may be outside the project or unwritable; try a path inside the project folder",
}

def dispatch_tool(*args, **kwargs):
    """Dual-dispatch (Phase A): rt-first `(rt, name, args)` from the facade
    and the Tools class; flat `(name, args)` from the agent's run loop (falls
    back to default_rt, which is the same runtime on every Phase A entry
    path). Dispatch is the single funnel for tool-mode auto-enable, error
    hints, and exception wrapping."""
    if len(args) == 3 and isinstance(args[0], Runtime):
        rt, name, tool_args = args
    elif len(args) == 2:
        name, tool_args = args
        rt = default_rt()
    else:
        raise TypeError("dispatch_tool expects (rt, name, args) or (name, args)")
    tool = getattr(Tools(rt), name, None)
    if tool is None:
        return {"error": "unknown tool: %s" % name}
    switched = maybe_enable_full(rt, name)
    try:
        result = tool(tool_args)
        if isinstance(result, dict) and not result.get("ok", True) and "hint" not in result:
            result["hint"] = _TOOL_ERROR_HINTS.get(name, "")
        if switched and isinstance(result, dict):
            result.setdefault("hint", "Advanced tool set enabled: all %d tools are now advertised to the model." % len(TOOLS))
        return result
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e),
                "hint": _TOOL_ERROR_HINTS.get(name, "check the tool arguments and try again")}


# ---------------- harness self-test ----------------
def tool_count():
    return len(TOOLS)


def self_test(rt):
    checks = []
    checks.append(("calculator", tool_calculator("2+3*4")["result"] == 14))
    try:
        tool_calculator("__import__('os').listdir('.')")
        checks.append(("sandbox", False))
    except Exception:
        checks.append(("sandbox", True))
    r = tool_todo_add(rt, "self-test")
    checks.append(("todos", r["ok"] is True))
    if r.get("ok"):
        tool_todo_remove(rt, r["index"])
    checks.append(("memory", tool_memory_recall(rt, "__no_such_key__")["found"] is False))
    checks.append(("clock", isinstance(tool_get_time(), dict) and "iso" in tool_get_time()))

    # run_python: dispatched, sandboxed, permission-gated
    try:
        checks.append(("run_python_dispatch", tool_run_python(rt, "print(2+2)").get("output") == "4\n"))
    except Exception:
        checks.append(("run_python_dispatch", False))
    checks.append(("run_python_gate", classify_python("import os") == "ask"))

    # tiered tool selection: core mode advertises a subset, full mode all
    _saved_mode = rt.tool_mode
    try:
        rt.tool_mode = "core"
        checks.append(("tools_core_subset",
                       len(visible(rt)) < len(TOOLS)
                       and "skill_list" not in _CORE_TOOL_NAMES
                       and "run_command" in _CORE_TOOL_NAMES))
        rt.tool_mode = "full"
        checks.append(("tools_full_all", len(visible(rt)) == len(TOOLS)))
    finally:
        rt.tool_mode = _saved_mode

    # skills: list should work
    try:
        skills = tool_skill_list(rt)
        checks.append(("skills_list", skills.get("ok") is True))
    except Exception:
        checks.append(("skills_list", False))

    # command classification: allowlist and risky
    checks.append(("classify_allow_ls", classify_command("ls -la") == "allow"))
    checks.append(("classify_ask_rm", classify_command("rm -rf /") == "ask"))
    checks.append(("classify_ask_subshell", classify_command("cat $(whoami)") == "ask"))

    # file tools: read this file
    try:
        r = tool_file_read(rt, __file__)
        checks.append(("file_read", r.get("ok") is True))
    except Exception:
        checks.append(("file_read", False))

    # file tools: write to temp dir (inside DATA_DIR so the headless default
    # deny-on-outside-write never blocks the check)
    try:
        tmp = os.path.join(DATA_DIR, ".alva_self_test_tmp.txt")
        r = tool_file_write(rt, tmp, "test content")
        if r.get("ok"):
            content = tool_file_read(rt, tmp).get("content", "")
            checks.append(("file_write", content == "test content"))
            os.remove(tmp)
        else:
            checks.append(("file_write", False))
    except Exception:
        checks.append(("file_write", False))

    # feedback + improvement + reflect tools
    try:
        r = tool_feedback(rt, "good", "self-test check")
        checks.append(("feedback", r.get("ok") is True))
    except Exception:
        checks.append(("feedback", False))

    try:
        r = tool_improvement_set(rt, "test-area", "test action")
        checks.append(("improvement_set", r.get("ok") is True))
    except Exception:
        checks.append(("improvement_set", False))

    try:
        imps = tool_improvement_list(rt)
        checks.append(("improvement_list", imps.get("ok") is True))
    except Exception:
        checks.append(("improvement_list", False))

    try:
        r = tool_reflect(rt)
        checks.append(("reflect", r.get("ok") is True))
    except Exception:
        checks.append(("reflect", False))

    return json.dumps({k: v for k, v in checks})


def tool_self_test():
    """Run the full self-test suite via `test_tui.py` and return results.

    This is the tool the agent calls to validate itself after editing its own
    source code. It runs the external `test_tui.py` harness (which tests the
    full agent loop with a mock LLM) AND the built-in self_test() checks.
    Always call this after any file_edit or file_write to your own source.
    """
    my_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root (package sits one level down)
    tpath = os.path.join(my_dir, "test_tui.py")

    result = {"tests": [], "all_passed": True}

    # Run the external test harness
    if os.path.isfile(tpath):
        try:
            proc = subprocess.run([sys.executable, tpath],
                                  capture_output=True, text=True, timeout=30)
            test_tui_passed = proc.returncode == 0
            result["tests"].append({
                "name": "test_tui.py (external harness)",
                "passed": test_tui_passed,
                "exit_code": proc.returncode,
                "stdout": (proc.stdout or "")[-2000:],
                "stderr": (proc.stderr or "")[-800:],
            })
            if not test_tui_passed:
                result["all_passed"] = False
        except Exception as e:
            result["tests"].append({
                "name": "test_tui.py (external harness)",
                "passed": False,
                "error": repr(e),
            })
            result["all_passed"] = False
    else:
        result["tests"].append({
            "name": "test_tui.py (external harness)",
            "passed": False,
            "error": "test_tui.py not found",
        })
        result["all_passed"] = False

    # Run built-in self_test checks
    try:
        builtin_json = self_test(default_rt())
        builtin_checks = json.loads(builtin_json) if isinstance(builtin_json, str) else builtin_json
        builtin_ok = all(v for v in builtin_checks.values())
        result["tests"].append({
            "name": "builtin self_test checks",
            "passed": builtin_ok,
            "details": builtin_checks,
        })
        if not builtin_ok:
            result["all_passed"] = False
    except Exception as e:
        result["tests"].append({
            "name": "builtin self_test checks",
            "passed": False,
            "error": repr(e),
        })
        result["all_passed"] = False

    return result

