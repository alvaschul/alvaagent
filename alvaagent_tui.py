#!/usr/bin/env python3
# ============================================================
#  alvaagent_tui.py — alvaagent terminal chat client
#
#  Same agent harness as the browser app (index.html), ported to
#  plain Python so it runs entirely inside Termux — no browser,
#  no web server, nothing to disconnect when you switch apps.
#
#  Uses only the Python standard library (no pip installs needed).
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
#    /help /config /provider /models /test /tools /todos /todo /memory
#    /skin /sessions /session /new /clear /context /compress /multi /export
#    /stop /exit /quit
#    Ctrl+C cancels a running request · Tab completes slash commands
#    (at the api key prompt, type 'none' to clear the key)
#
#  Sessions: conversations are saved to store.json and resumed on restart.
#    /sessions lists them · /session <name> switches/creates · /new starts fresh.
#  Context: the footer shows a live ctx meter (est. tokens / model window) and
#    auto-compresses older messages into a summary near the limit so long chats
#    don't drift out of context. /context shows the numbers · /compress forces it.
#  Skins: /skin lists & switches the color theme (midnight | ember | ocean |
#  daylight). Skins persist to config.json. The layout echoes the Hermes agent
#  TUI (banner + bordered message blocks + tool blocks + status chips) but with
#  its own palettes, a footer status line instead of a persistent bottom bar,
#  tab-completion instead of a dropdown, and the alvaagent ⚡ brand.
#
#  Note: single-line input — a multi-line paste submits only its first
#  line (the soft keyboard's Enter sends each line).
# ============================================================
import ast
import codecs
import datetime
import json
import math
import os
import re
import readline
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

# ---------------- paths / config ----------------
# Data lives next to this script (survives distro reinstalls on Termux
# proot, where ~ is inside the distro rootfs). ALVA_DATA_DIR overrides
# (POCKET_DATA_DIR is also accepted for backwards compatibility).


def _env(*names):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


DATA_DIR = (_env("ALVA_DATA_DIR", "POCKET_DATA_DIR")
            or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".alvaagent"))

# old default locations, checked once for a one-time data migration
_LEGACY_DIRS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".pocket_agent"),
    os.path.join(os.path.expanduser("~"), ".pocket_agent"),
]
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
STORE_PATH = os.path.join(DATA_DIR, "store.json")

PROVIDERS = {
    "openai":     {"label": "OpenAI",                   "base": "https://api.openai.com/v1",                      "model": "gpt-4o-mini"},
    "groq":       {"label": "Groq (free tier)",         "base": "https://api.groq.com/openai/v1",                "model": "llama-3.3-70b-versatile"},
    "openrouter": {"label": "OpenRouter (free models)",  "base": "https://openrouter.ai/api/v1",                 "model": "meta-llama/llama-3.3-70b-instruct:free"},
    "gemini":     {"label": "Google Gemini",            "base": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-2.0-flash"},
    "custom":     {"label": "Custom endpoint",          "base": "",                                               "model": ""},
}

DEFAULT_CFG = {
    "base_url": PROVIDERS["openai"]["base"],
    "api_key": "",
    "model": PROVIDERS["openai"]["model"],
    "temperature": 0.7,
    "context_window": 0,      # 0 = auto-detect from the model name
    "auto_compress": True,     # summarize older messages near the context limit
}

# neutral profile for the very first run (no provider assumed)
FIRST_RUN_CFG = {"base_url": "", "api_key": "", "model": "", "temperature": 0.7,
                  "context_window": 0, "auto_compress": True}

# UI skin the user last picked (persisted in config.json) + version string
DEFAULT_SKIN = "midnight"
SKIN_NAMES = ("midnight", "ember", "ocean", "daylight")
ALVA_VERSION = "1.2.0"

# fallback context window when the model can't be identified
DEFAULT_CONTEXT_WINDOW = 128000

# known context windows (tokens) — used for the ctx meter + auto-compress
MODEL_CONTEXT = {
    "gpt-4o-mini": 128000, "gpt-4o": 128000, "gpt-4-turbo": 128000,
    "gpt-3.5-turbo": 16385, "o1-mini": 128000, "o1": 200000,
    "o3-mini": 200000, "o3": 200000,
    "gemini-2.0-flash": 1048576, "gemini-1.5-flash": 1048576,
    "gemini-1.5-pro": 2097152,
    "claude-3-5-sonnet": 200000, "claude-3-7-sonnet": 200000,
    "claude-sonnet-4": 200000, "claude-opus-4": 200000,
    "llama-3.3-70b-versatile": 131072, "llama-3.1-8b-instant": 131072,
    "llama-3.2-3b-preview": 131072,
    "meta-llama/llama-3.3-70b-instruct:free": 131072,
    "deepseek": 65536,
}


def _skin_of(raw):
    """Persisted skin name from raw state (validated), else the default."""
    if isinstance(raw, dict):
        s = raw.get("skin")
        if s in SKIN_NAMES:
            return s
    return DEFAULT_SKIN


def _normalize_state(raw):
    """Migrate a legacy flat config (or empty) into the profiles format:
    {"active": name, "profiles": {name: {base_url, api_key, model, temperature}}, "skin": name}."""
    if isinstance(raw, dict):
        profiles = raw.get("profiles")
        if isinstance(profiles, dict):
            profiles = {k: v for k, v in profiles.items() if isinstance(v, dict)}
            if profiles:
                active = raw.get("active")
                if active not in profiles:
                    active = next(iter(profiles))
                return {"active": active, "profiles": profiles, "skin": _skin_of(raw)}
        # legacy flat config: {"provider": ..., "base_url": ..., ...}
        if raw.get("provider") or raw.get("base_url"):
            name = raw.get("provider") or "default"
            if name not in PROVIDERS:
                name = "default"
            prof = {k: raw.get(k, DEFAULT_CFG[k]) for k in
                    ("base_url", "api_key", "model", "temperature",
                     "context_window", "auto_compress")}
            return {"active": name, "profiles": {name: prof}, "skin": _skin_of(raw)}
    # first run: a neutral, keyless profile
    return {"active": "default", "profiles": {"default": dict(FIRST_RUN_CFG)}, "skin": DEFAULT_SKIN}


def load_state():
    """Load provider profiles; env vars override the active profile."""
    try:
        with open(CONFIG_PATH) as f:
            raw = json.load(f)
    except Exception:
        raw = {}
    state = _normalize_state(raw)
    prof = state["profiles"].setdefault(state["active"], dict(DEFAULT_CFG))
    for names, key in ((("ALVA_BASE_URL", "POCKET_BASE_URL"), "base_url"),
                       (("ALVA_API_KEY", "POCKET_API_KEY"), "api_key"),
                       (("ALVA_MODEL", "POCKET_MODEL"), "model")):
        v = _env(*names)
        if v:
            prof[key] = v.strip()
    if _env("ALVA_TEMPERATURE", "POCKET_TEMPERATURE"):
        try:
            prof["temperature"] = float(_env("ALVA_TEMPERATURE", "POCKET_TEMPERATURE"))
        except ValueError:
            pass
    if _env("ALVA_CONTEXT_WINDOW", "POCKET_CONTEXT_WINDOW"):
        try:
            prof["context_window"] = int(float(_env("ALVA_CONTEXT_WINDOW", "POCKET_CONTEXT_WINDOW")))
        except ValueError:
            pass
    return state


def save_state(state):
    """Atomically persist config: temp file + fsync + rename (see _save_store)."""
    try:
        import tempfile
        os.makedirs(DATA_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".config.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, CONFIG_PATH)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    except Exception:
        pass


def active_cfg(state):
    return state["profiles"][state["active"]]


# ---------------- persistence (JSON file instead of localStorage) ----------------
_store = {}


def _migrate_legacy_dir():
    """One-time copy of data from the old .pocket_agent folders (if any)."""
    if _env("ALVA_DATA_DIR", "POCKET_DATA_DIR"):
        return  # explicit override: don't second-guess
    if os.path.exists(STORE_PATH) or os.path.exists(CONFIG_PATH):
        return  # new-brand data already present
    for old in _LEGACY_DIRS:
        if os.path.isdir(old) and any(
                os.path.exists(os.path.join(old, f)) for f in ("store.json", "config.json")):
            try:
                os.makedirs(DATA_DIR, exist_ok=True)
                for name in os.listdir(old):
                    src, dst = os.path.join(old, name), os.path.join(DATA_DIR, name)
                    if os.path.isfile(src) and not os.path.exists(dst):
                        with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
                            fdst.write(fsrc.read())
            except Exception:
                pass
            break


def _load_store():
    global _store
    _migrate_legacy_dir()
    try:
        with open(STORE_PATH) as f:
            _store = json.load(f)
    except Exception:
        _store = {}
    # rename keys saved under the old brand, once, in place
    if any(k.startswith("pocket_agent.") for k in _store):
        _store = {("alvaagent." + k[len("pocket_agent."):]) if k.startswith("pocket_agent.") else k: v
                  for k, v in _store.items()}
        _save_store()


def _save_store():
    """Atomically persist the store: write to a temp file, then rename into
    place. A kill/crash mid-write can never leave a truncated store.json."""
    try:
        import tempfile
        os.makedirs(DATA_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".store.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(_store, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, STORE_PATH)  # atomic on POSIX
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    except Exception:
        pass


def _store_get(key, default=None):
    return _store.get(key, default)


def _store_set(key, value):
    _store[key] = value
    _save_store()


_load_store()  # load persisted todos/memory at import time

TODO_KEY = "alvaagent.todos"
MEM_PREFIX = "alvaagent.mem."
HISTORY_KEY = "alvaagent.history"
SESSION_KEY = "alvaagent.sessions"
ACTIVE_SESSION_KEY = "alvaagent.active_session"

# ---------------- autonomy: permissions ----------------
# The agent can run shell commands, edit files and manage skills. Everything
# outside the project folder (or risky) goes through ON_PERMISSION, which the
# REPL wires to an interactive y/N prompt. Headless (no hook) defaults to DENY,
# unless ALVA_AUTO_APPROVE=1 is set (attended/automated runs).

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.join(DATA_DIR, "skills")

ON_PERMISSION = None  # hook: ON_PERMISSION(description) -> bool

# commands that are safe to run without asking
_READONLY_PREFIXES = (
    "ls", "cat", "pwd", "whoami", "echo", "date", "which", "find",
    "head", "tail", "grep", "stat", "df", "du", "free", "uname", "env",
    "wc", "readlink", "basename", "dirname", "python3 --version",
    "python3 -V", "python3 -m py_compile", "git status", "git diff",
    "git log", "git --version", "git branch", "git remote -v",
)

# anything containing these is treated as mutating/risky -> ask the user
_RISKY_TOKENS = frozenset({
    "rm", "mv", "cp", "mkdir", "touch", "chmod", "chown",
    "chattr", "sudo", "su", "apt", "apt-get", "pkg", "pip", "npm",
    "kill", "pkill", "killall", "reboot", "shutdown", "poweroff", "mkfs",
    "dd", "wget", "curl", "git push", "git commit",
    "git reset", "git clean", "git checkout", "git branch",
    "systemctl", "service", "mount", "umount", "fdisk", "tee",
})
_RISKY_OPERATORS = frozenset({">", ">>", "|", "&&", ";"})


def _tokenize_shell(cmd):
    """Simple shell-aware tokenizer. Splits on whitespace and quoted strings."""
    tokens = []
    i = 0
    while i < len(cmd):
        if cmd[i] in ('"', "'"):
            q = cmd[i]
            i += 1
            while i < len(cmd) and cmd[i] != q:
                i += 1
            i += 1
            tokens.append("QUOTED")
        elif cmd[i] in ('>', '|', ';', '&'):
            op = cmd[i]
            if i + 1 < len(cmd) and cmd[i:i+2] in ('>>', '&&', '||', '2>'):
                op = cmd[i:i+2]
                i += 1
            tokens.append(op)
            i += 1
        elif not cmd[i].isspace():
            start = i
            while i < len(cmd) and not cmd[i].isspace() and cmd[i] not in ('>', '|', ';', '&', '"', "'"):
                i += 1
            tokens.append(cmd[start:i])
        else:
            i += 1
    return tokens


def classify_command(cmd):
    """allow / ask / deny for a shell command (token-aware, safe default)."""
    c = cmd.strip()
    if not c:
        return "deny"
    # Shell metacharacters that enable command substitution / sub-shells are
    # never needed by the allowlisted read-only commands -> always ask.
    # (e.g. ``echo $(touch /tmp/x)`` and ``echo `touch /tmp/x` `` must NOT
    # pass as read-only — they execute arbitrary commands.)
    if any(ch in c for ch in "$`(){}"):
        return "ask"
    tokens = _tokenize_shell(c)
    # Risky operators anywhere -> ask
    if any(t in _RISKY_OPERATORS for t in tokens):
        return "ask"
    # Check EVERY token against the risky-command set, not just the first word
    # (e.g. ``env X=1 rm -rf /`` previously slipped through as "allow").
    words = [t for t in tokens if t != "QUOTED"]
    if any(w in _RISKY_TOKENS for w in words):
        return "ask"
    # find is allowlisted for searches but its destructive flags (-delete,
    # -exec/-execdir/-ok) turn it into a wipe — treat them as risky.
    if words and words[0] == "find":
        for w in words:
            if w.startswith("-delete") or w.startswith("-exec") \
               or w.startswith("-execdir") or w.startswith("-ok") \
               or w.startswith("-fprint"):
                return "ask"
    # Read-only commands: exact word-boundary match against the allowlist
    # (``catastrophe --version`` must NOT match the ``cat`` entry).
    if any(c == p or c.startswith(p + " ") for p in _READONLY_PREFIXES):
        return "allow"
    return "ask"


def _in_project(path):
    real = os.path.realpath(os.path.expanduser(str(path)))
    for base in (os.path.realpath(PROJECT_DIR), os.path.realpath(DATA_DIR)):
        if real == base or real.startswith(base + os.sep):
            return True
    return False


def classify_file_action(path, kind):
    """allow / ask for a file action (reads and writes prompt outside project)."""
    return "allow" if _in_project(path) else "ask"


def _permission(desc):
    """Resolve a permission request: hook -> env override -> default deny."""
    if os.environ.get("ALVA_AUTO_APPROVE") == "1":
        return True
    if ON_PERMISSION is not None:
        return ON_PERMISSION(desc)
    return False  # headless default: deny


# ---------------- autonomy: shell + files + skills ----------------
def tool_run_command(command):
    """Run a shell command on the device (Termux). Risky commands ask the user."""
    command = str(command).strip()
    if not command:
        return {"ok": False, "error": "empty command"}
    if classify_command(command) == "ask" and not _permission("run command: %s" % command[:160]):
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


def tool_file_read(path):
    path = str(path).strip()
    if not path:
        return {"ok": False, "error": "empty path"}
    if classify_file_action(path, "read") == "ask" and not _permission("read file: %s" % path):
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


def tool_file_write(path, content):
    path = str(path).strip()
    if not path:
        return {"ok": False, "error": "empty path"}
    if classify_file_action(path, "write") == "ask" and not _permission("write file: %s" % path):
        return {"ok": False, "error": "permission denied by user"}
    try:
        text = str(content)
        _atomic_write(path, text)
        return {"ok": True, "path": path, "chars": len(text)}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def tool_file_edit(path, old, new):
    path = str(path).strip()
    if not path:
        return {"ok": False, "error": "empty path"}
    if classify_file_action(path, "write") == "ask" and not _permission("edit file: %s" % path):
        return {"ok": False, "error": "permission denied by user"}
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if old not in content:
            return {"ok": False, "error": "old string not found in %s" % path}
        updated = content.replace(old, new, 1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(updated)
        return {"ok": True, "path": path, "replaced": content.count(old)}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def tool_file_list(path="."):
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


def tool_skill_list():
    try:
        os.makedirs(SKILLS_DIR, exist_ok=True)
        names = sorted(n[:-3] for n in os.listdir(SKILLS_DIR) if n.endswith(".md"))
        return {"ok": True, "skills": names}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def tool_skill_read(name):
    name = str(name).strip()
    path = os.path.join(SKILLS_DIR, name if name.endswith(".md") else name + ".md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return {"ok": True, "name": name, "content": content}
    except FileNotFoundError:
        return {"ok": False, "error": "no such skill: %s" % name}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def _atomic_write(path, text, mode="w"):
    """Write text to `path` atomically: temp file + fsync + rename into place.
    Creates parent dirs. Raises on failure so callers can report the error."""
    import tempfile
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)) or ".",
                               prefix=".tmp.", suffix=".write")
    try:
        with os.fdopen(fd, mode, encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def tool_skill_save(name, content):
    name = str(name).strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return {"ok": False, "error": "invalid skill name: %r" % name}
    try:
        os.makedirs(SKILLS_DIR, exist_ok=True)
        path = os.path.join(SKILLS_DIR, name + ".md")
        _atomic_write(path, str(content))
        return {"ok": True, "name": name, "path": path, "chars": len(str(content))}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


# ---------------- tools ----------------
def tool_todo_list():
    todos = _store_get(TODO_KEY, [])
    return {"count": len(todos), "todos": todos}


def tool_todo_add(text):
    text = str(text).strip()
    if not text:
        return {"ok": False, "error": "empty todo text"}
    todos = _store_get(TODO_KEY, [])
    todos.append({"text": text, "done": False})
    _store_set(TODO_KEY, todos)
    return {"ok": True, "index": len(todos) - 1, "text": text, "count": len(todos)}


def tool_todo_toggle(index):
    todos = _store_get(TODO_KEY, [])
    try:
        i = int(index)
        todos[i]["done"] = not todos[i]["done"]
        _store_set(TODO_KEY, todos)
        return {"ok": True, "index": i, "done": todos[i]["done"], "text": todos[i]["text"]}
    except Exception as e:
        return {"ok": False, "error": "invalid index %r: %s" % (index, e)}


def tool_todo_remove(index):
    todos = _store_get(TODO_KEY, [])
    try:
        i = int(index)
        removed = todos.pop(i)
        _store_set(TODO_KEY, todos)
        return {"ok": True, "removed": removed}
    except Exception as e:
        return {"ok": False, "error": "invalid index %r: %s" % (index, e)}


def tool_memory_save(key, value):
    key = str(key).strip()
    if not key:
        return {"ok": False, "error": "empty key"}
    _store_set(MEM_PREFIX + key, str(value))
    return {"ok": True, "key": key, "stored": str(value)}


def tool_memory_recall(key):
    key = str(key).strip()
    v = _store_get(MEM_PREFIX + key)
    if v is None:
        return {"ok": False, "key": key, "found": False}
    return {"ok": True, "key": key, "found": True, "value": v}


def tool_get_time():
    now = datetime.datetime.now()
    return {
        "iso": now.isoformat(),
        "date": now.strftime("%A, %B %d, %Y"),
        "time": now.strftime("%H:%M:%S"),
    }


def tool_web_fetch(url):
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
    return {"expression": expression, "result": result}


TOOLS = [
    {"type": "function", "function": {
        "name": "calculator",
        "description": "Evaluate a mathematical expression precisely. Supports + - * / // % **, parentheses, constants pi/e/tau, and math functions such as sqrt, sin, cos, tan, log, log10, exp, floor, ceil, abs, round, factorial, gcd.",
        "parameters": {"type": "object", "properties": {
            "expression": {"type": "string", "description": "Math expression to evaluate, e.g. 'sqrt(2**10) + 3*4'"}},
            "required": ["expression"]}}},
    {"type": "function", "function": {
        "name": "web_fetch",
        "description": "Fetch and read the text content of a URL (http/https only). Returns status code and a text snippet.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "The URL to fetch"}},
            "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "get_time",
        "description": "Get the current date and time on the user's device.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "memory_save",
        "description": "Save a fact to the on-device memory store so it can be recalled later.",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string", "description": "Short label for the fact"},
            "value": {"type": "string", "description": "The fact to remember"}},
            "required": ["key", "value"]}}},
    {"type": "function", "function": {
        "name": "memory_recall",
        "description": "Recall a previously saved fact from on-device memory.",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string", "description": "The label of the fact to recall"}},
            "required": ["key"]}}},
    {"type": "function", "function": {
        "name": "todo_add",
        "description": "Add a new task to the user's to-do list.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "Task text"}},
            "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "todo_list",
        "description": "List all tasks in the user's to-do list with done/undone status.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "todo_toggle",
        "description": "Mark a task as done or undone.",
        "parameters": {"type": "object", "properties": {
            "index": {"type": "integer", "description": "Zero-based index of the task"}},
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
        "name": "skill_list",
        "description": "List available on-device skill files. ALWAYS call this before starting a substantial task and read any skill whose name matches the task — skills encode the user's preferred way of doing that kind of work.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "skill_read",
        "description": "Read the full body of a named skill. Use the exact name from skill_list (without .md). Apply the skill's guidance faithfully when it matches the current task.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Skill name (without .md)"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "skill_save",
        "description": "Save a reusable procedure as a skill so it can be applied on later tasks. Give it a descriptive name (lowercase-hyphenated) and a concise body that states the TRIGGER (when to use it) followed by numbered STEPS. Only save genuinely reusable, non-obvious procedures.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Skill name, lowercase-hyphenated, without .md"},
            "content": {"type": "string", "description": "Skill body: a one-line trigger condition followed by concise numbered steps."}},
            "required": ["name", "content"]}}},
]

TOOL_IMPL = {
    "calculator": lambda a: tool_calculator(a.get("expression")),
    "web_fetch": lambda a: tool_web_fetch(a.get("url")),
    "get_time": lambda a: tool_get_time(),
    "memory_save": lambda a: tool_memory_save(a.get("key"), a.get("value")),
    "memory_recall": lambda a: tool_memory_recall(a.get("key")),
    "todo_add": lambda a: tool_todo_add(a.get("text")),
    "todo_list": lambda a: tool_todo_list(),
    "todo_toggle": lambda a: tool_todo_toggle(a.get("index")),
    "todo_remove": lambda a: tool_todo_remove(a.get("index")),
    "run_command": lambda a: tool_run_command(a.get("command")),
    "file_read": lambda a: tool_file_read(a.get("path")),
    "file_write": lambda a: tool_file_write(a.get("path"), a.get("content")),
    "file_edit": lambda a: tool_file_edit(a.get("path"), a.get("old"), a.get("new")),
    "file_list": lambda a: tool_file_list(a.get("path")),
    "skill_list": lambda a: tool_skill_list(),
    "skill_read": lambda a: tool_skill_read(a.get("name")),
    "skill_save": lambda a: tool_skill_save(a.get("name"), a.get("content")),
}


def dispatch_tool(name, args):
    fn = TOOL_IMPL.get(name)
    if fn is None:
        return {"error": "unknown tool: %s" % name}
    try:
        return fn(args)
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}


# ---------------- LLM client (OpenAI-compatible) ----------------
SYSTEM_PROMPT = """You are alvaagent, a helpful AI agent running on the user's Android device (Termux / proot).
You can call tools to do real work. Guidelines:
1. Use the calculator tool for ANY arithmetic — never guess math.
2. Use web_fetch to read a webpage when the user asks about online content.
3. Use memory_save / memory_recall to remember facts the user asks you to remember.
4. Use todo_add / todo_list / todo_toggle / todo_remove to manage the user's to-do list.
5. Use get_time when the user needs the current date or time.
6. You have real device access: run_command runs shell commands (Termux), and
   file_read / file_write / file_edit / file_list work on the device's files.
   Read-only commands and in-project file edits run freely; mutating/unknown
   commands or out-of-project writes ask the user first — if denied, do not
   retry, and explain what was blocked and why.
7. Skills: skill_list / skill_read / skill_save manage reusable procedures
   stored on the device. BEFORE starting any substantial task, call skill_list
   and read any skill whose name matches the task. Apply the skill's guidance
   faithfully — a skill is the user's preferred way of doing that kind of work.
   When you discover a reusable, non-obvious procedure during a task, save it
   as a skill with a descriptive name and a concise body (trigger + steps).
   Keep skills small and self-contained so they stay easy to apply and test.
8. Self-improvement: you may read your OWN source (alvaagent_tui.py,
   index.html, start.sh, test_tui.py) and improve it with file_edit/
   file_write, then validate with run_command("python3 -m py_compile
   alvaagent_tui.py") and run_command("python3 test_tui.py"). Changes take
   effect the next time the user restarts the TUI — always say so, and keep
   edits small, targeted, and tested.
Only call a tool when it genuinely helps. If no tool is needed, answer directly.
Respond in the same language the user writes in. Be concise, friendly, and precise."""


def _readable_error(status, text):
    """Best-effort readable message from an API error body (JSON or HTML).

    Gateways/WAFs often return HTML error pages whose <title> says exactly
    what's blocked (Cloudflare, nginx, …); proxies sometimes wrap upstream
    failures as JSON like {"error": {"message": "[403]: <html>…"}}.
    """
    msg = ""
    try:
        data = json.loads(text)
        err = data.get("error", {})
        msg = err.get("message", str(data)[:200]) if isinstance(err, dict) else str(err)
    except Exception:
        msg = text[:300]
    if not msg:
        return "HTTP %s" % status
    # drop redundant "[403]: ..." style prefixes (gateway-wrapped upstream errors)
    m = re.match(r"^\s*\[\s*\d+\s*\]\s*:\s*", msg)
    if m:
        msg = msg[m.end():]
    # HTML pages: prefer the <title>, else the stripped text
    if re.search(r"(?is)<(title|h1)", msg):
        for tag in ("title", "h1"):
            tm = re.search(r"(?is)<%s[^>]*>(.*?)</%s>" % (tag, tag), msg)
            if tm:
                t = re.sub(r"(?is)<[^>]+>", " ", tm.group(1)).strip()
                if t:
                    return "blocked by error page: %s" % t[:160]
        plain = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", msg)
        plain = re.sub(r"(?is)<[^>]+>", " ", plain)
        plain = re.sub(r"\s+", " ", plain).strip()
        if plain:
            return plain[:200]
    return msg.strip()[:300]


def chat_completion(messages, config, tools=None):
    base = (config.get("base_url") or "").rstrip("/")
    url = base + "/chat/completions"
    payload = {
        "model": config.get("model") or "gpt-4o-mini",
        "messages": messages,
        "temperature": float(config.get("temperature") if config.get("temperature") is not None else 0.7),
        "stream": False,  # some gateways default to SSE; we want a plain JSON reply
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept-Encoding": "identity",
            "Authorization": "Bearer " + (config.get("api_key") or ""),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            status = int(r.getcode())
            text = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        status = int(e.code)
        text = e.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        raise RuntimeError("LLM API unreachable: %s" % e.reason)
    except Exception as e:
        raise RuntimeError("LLM request failed: %s" % e)
    try:
        data = json.loads(text)
    except Exception:
        raise RuntimeError("API returned non-JSON (HTTP %s): %s" % (status, _readable_error(status, text)))
    if status >= 400 or "error" in data:
        raise RuntimeError("LLM API error %s: %s" % (status, _readable_error(status, text)))
    if not data.get("choices"):
        raise RuntimeError("LLM API returned no choices")
    return data


def chat_completion_stream(messages, config, tools=None):
    """Streaming version of chat_completion. Yields (content_chunk, tool_calls_json_or_None)."""
    base = (config.get("base_url") or "").rstrip("/")
    url = base + "/chat/completions"
    payload = {
        "model": config.get("model") or "gpt-4o-mini",
        "messages": messages,
        "temperature": float(config.get("temperature") if config.get("temperature") is not None else 0.7),
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept-Encoding": "identity",
            "Authorization": "Bearer " + (config.get("api_key") or ""),
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=180)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError("LLM API error %s: %s" % (e.code, _readable_error(e.code, body)))
    except urllib.error.URLError as e:
        raise RuntimeError("LLM API unreachable: %s" % e.reason)
    except Exception as e:
        raise RuntimeError("LLM request failed: %s" % e)
    buffer = ""
    tool_calls_acc = {}
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        # Read in chunks (1-byte reads = one syscall per byte, painfully slow
        # on flaky mobile links). 1024 is plenty for SSE deltas.
        chunk = resp.read(1024)
        if not chunk:
            break
        buffer += decoder.decode(chunk)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("data: "):
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    return
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choices = data.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                content = delta.get("content") or ""
                if content:
                    yield content, None
                tc = delta.get("tool_calls") or []
                for tcc in tc:
                    idx = tcc.get("index", 0)
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {"id": "", "function": {"name": "", "arguments": ""}}
                    acc = tool_calls_acc[idx]
                    if tcc.get("id"):
                        acc["id"] += tcc["id"]
                    fn = tcc.get("function") or {}
                    if fn.get("name"):
                        acc["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        acc["function"]["arguments"] += fn["arguments"]
                finish = choices[0].get("finish_reason")
                if finish == "tool_calls":
                    tc_list = []
                    for idx in sorted(tool_calls_acc):
                        tc_list.append({"id": tool_calls_acc[idx]["id"],
                                        "type": "function",
                                        "function": tool_calls_acc[idx]["function"]})
                    yield "", tc_list
                    return
                if finish == "stop":
                    return
                if finish == "length":
                    return
    # Fallback: some gateways/proxies ignore "stream": true and answer with a
    # plain JSON completion instead of SSE lines. If nothing SSE-ish arrived,
    # parse the raw body directly so responses still render.
    body = buffer.strip()
    if body and not tool_calls_acc:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return
        choices = data.get("choices") or []
        if not choices:
            return
        msg = choices[0].get("message", {})
        content = msg.get("content") or ""
        if content:
            yield content, None
        tc = msg.get("tool_calls") or []
        if tc:
            yield "", tc
        return


def fetch_models(base_url, api_key, timeout=20):
    """GET {base}/models and return the list of model ids (raises on failure)."""
    base = (base_url or "").rstrip("/")
    if not base:
        raise RuntimeError("no base url configured")
    req = urllib.request.Request(
        base + "/models",
        headers={"Authorization": "Bearer " + (api_key or ""), "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", errors="replace"))
    return [str(m["id"]) for m in (data.get("data") or [])
            if isinstance(m, dict) and m.get("id")]


# ---------------- agent loop ----------------
MAX_STEPS = 25
_cancel_flag = [False]
ON_TOOL = None  # optional hook: ON_TOOL(tool_id, name, args, result, status)


def cancel_agent():
    _cancel_flag[0] = True


def _report_tool(tool_id, name, args, result, status):
    if ON_TOOL is not None:
        try:
            ON_TOOL(tool_id, name, args, result, status)
        except Exception:
            pass


def run_agent(history_json, config_json):
    history = json.loads(str(history_json))
    config = json.loads(str(config_json))
    _cancel_flag[0] = False
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in history:
        if m.get("role") == "system":
            continue  # avoid duplicate system prompts
        messages.append({"role": m["role"], "content": m.get("content")})

    for step in range(MAX_STEPS):
        if _cancel_flag[0]:
            return json.dumps({"content": "(stopped by user)", "history": messages, "cancelled": True})
        data = chat_completion(messages, config, tools=TOOLS)
        msg = data["choices"][0]["message"]
        if msg.get("content") is None:
            msg["content"] = ""
        messages.append(msg)

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            return json.dumps({"content": msg.get("content") or "", "history": messages, "cancelled": False})

        for tc in tool_calls:
            if _cancel_flag[0]:
                return json.dumps({"content": "(stopped by user)", "history": messages, "cancelled": True})
            fn = tc.get("function", {})
            name = fn.get("name", "?")
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    args = {}
            except Exception:
                args = {}
            tool_id = tc.get("id", "?")
            _report_tool(tool_id, name, args, None, "running")
            result = dispatch_tool(name, args)
            status = "done" if (isinstance(result, dict) and "error" not in result) else "error"
            _report_tool(tool_id, name, args, result, status)
            messages.append({"role": "tool", "tool_call_id": tool_id, "content": json.dumps(result)})

    return json.dumps({"content": "(reached the maximum number of tool steps)", "history": messages, "cancelled": False})


def run_agent_stream(history, config):
    """Generator that yields ('text', chunk) or ('tool', tool_info) or ('done', final_dict)."""
    _cancel_flag[0] = False
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in history:
        if m.get("role") == "system":
            continue
        messages.append({"role": m["role"], "content": m.get("content")})

    for step in range(MAX_STEPS):
        if _cancel_flag[0]:
            yield "done", {"content": "(stopped by user)", "history": messages, "cancelled": True}
            return

        # Use streaming to detect tool calls and collect text
        content_parts = []
        tool_calls_result = None
        try:
            for chunk, tcs in chat_completion_stream(messages, config, tools=TOOLS):
                if _cancel_flag[0]:
                    yield "done", {"content": "(stopped by user)", "history": messages, "cancelled": True}
                    return
                if chunk:
                    content_parts.append(chunk)
                    yield "text", chunk
                if tcs:
                    tool_calls_result = tcs
        except RuntimeError as e:
            yield "done", {"content": "error: %s" % e, "history": messages, "cancelled": False}
            return

        full_content = "".join(content_parts)
        msg = {"role": "assistant", "content": full_content}

        if tool_calls_result:
            msg["tool_calls"] = tool_calls_result
            messages.append(msg)
            for tc in tool_calls_result:
                if _cancel_flag[0]:
                    yield "done", {"content": "(stopped by user)", "history": messages, "cancelled": True}
                    return
                fn = tc.get("function", {})
                name = fn.get("name", "?")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except Exception:
                    args = {}
                tool_id = tc.get("id", "?")
                yield "tool_start", {"name": name, "args": args}
                result = dispatch_tool(name, args)
                status = "done" if (isinstance(result, dict) and "error" not in result) else "error"
                yield "tool_end", {"name": name, "args": args, "result": result, "status": status}
                messages.append({"role": "tool", "tool_call_id": tool_id, "content": json.dumps(result)})
        else:
            messages.append(msg)
            yield "done", {"content": full_content, "history": messages, "cancelled": False}
            return

    yield "done", {"content": "(reached the maximum number of tool steps)", "history": messages, "cancelled": False}


# ---------------- harness self-test ----------------
def tool_count():
    return len(TOOLS)


def self_test():
    checks = []
    checks.append(("calculator", tool_calculator("2+3*4")["result"] == 14))
    try:
        tool_calculator("__import__('os').listdir('.')")
        checks.append(("sandbox", False))
    except Exception:
        checks.append(("sandbox", True))
    r = tool_todo_add("self-test")
    checks.append(("todos", r["ok"] is True))
    if r.get("ok"):
        tool_todo_remove(r["index"])
    checks.append(("memory", tool_memory_recall("__no_such_key__")["found"] is False))
    checks.append(("clock", isinstance(tool_get_time(), dict) and "iso" in tool_get_time()))
    return json.dumps({k: v for k, v in checks})


# ---------------- context tracking & sessions ----------------
# Rough token estimates power the ctx meter, the auto-compress trigger and the
# /context command. Sessions persist to store.json so conversations can be
# saved, listed and resumed across runs (Hermes-style /sessions).

def context_window_for(cfg):
    """Context window (tokens) for a provider config: explicit override wins,
    else best-effort lookup by model name, else the default."""
    w = cfg.get("context_window") or 0
    if w and int(w) > 0:
        return int(w)
    model = (cfg.get("model") or "").lower()
    if model in MODEL_CONTEXT:
        return MODEL_CONTEXT[model]
    for key, size in MODEL_CONTEXT.items():
        if key in model:
            return size
    return DEFAULT_CONTEXT_WINDOW


def estimate_tokens(text):
    """Rough token estimate: ~4 chars per token; wide scripts count ~2x."""
    s = str(text)
    wide = sum(1 for ch in s if ord(ch) >= 128)
    return max(1, int((len(s) + wide) / 4))


def estimate_message_tokens(m):
    c = m.get("content") or ""
    if isinstance(c, list):
        n = sum(estimate_tokens(part.get("text", "") if isinstance(part, dict) else part)
                for part in c)
    else:
        n = estimate_tokens(c)
    return n + 8  # role + metadata overhead


def context_usage(history, cfg):
    """Estimated (tokens, window) for the whole conversation + system prompt."""
    total = estimate_tokens(SYSTEM_PROMPT)
    for m in history:
        total += estimate_message_tokens(m)
    return total, context_window_for(cfg)


def _fmt_k(n):
    n = int(n)
    return "%.1fk" % (n / 1000.0) if n >= 1000 else str(n)


# ---------------- sessions ----------------
def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def sessions_map():
    return _store_get(SESSION_KEY, {})


def load_session(name):
    msgs = sessions_map().get(name, {}).get("messages")
    return list(msgs) if isinstance(msgs, list) else []


def save_session(name, messages):
    """Persist a session's messages and mark it active."""
    sess = sessions_map()
    rec = sess.get(name) or {"name": name, "created": now_iso(), "messages": []}
    rec["messages"] = list(messages)
    rec["updated"] = now_iso()
    sess[name] = rec
    _store_set(SESSION_KEY, sess)
    _store_set(ACTIVE_SESSION_KEY, name)


def delete_session(name):
    sess = sessions_map()
    sess.pop(name, None)
    _store_set(SESSION_KEY, sess)


def _find_session(target):
    """Case-insensitive session-name lookup; returns the canonical name or None."""
    t = target.strip().lower()
    for name in sessions_map():
        if name.lower() == t:
            return name
    return None


def _rename_session_in_store(old, new):
    sess = sessions_map()
    if old in sess:
        rec = sess.pop(old)
        rec["name"] = new
        sess[new] = rec
        _store_set(SESSION_KEY, sess)
    _store_set(ACTIVE_SESSION_KEY, new)


def auto_title(text):
    """A short human-readable session name derived from the first message."""
    t = re.sub(r"\s+", " ", str(text)).strip().strip(".:-")
    return t[:28] or "conversation"


def _unique_session_name(title):
    base = auto_title(title)
    name = base
    i = 2
    while name in sessions_map():
        name = "%s %d" % (base, i)
        i += 1
    return name


# ---------------- auto-compression ----------------
def summarize_with_llm(messages, cfg, max_words=350):
    """Condense `messages` into a structured summary for a fresh context window.

    Returns a concise multi-section summary string, or None on any failure.
    Uses a lean system note (not the full harness prompt) so we don't waste
    tokens re-sending instructions or risk bleeding them into the summary.
    """
    prompt = (
        "Condense the conversation below into a structured summary for a fresh "
        "context window. Use these sections only where applicable:\n"
        "- GOALS: what the user wanted to achieve\n"
        "- DECISIONS: choices made and their rationale\n"
        "- FACTS: durable facts, names, values, config learned\n"
        "- ACTIONS: concrete work done (commands run, files changed, code edits)\n"
        "- OPEN: unresolved questions or next steps\n"
        "Be dense and factual — no preamble, under %d words total. "
        "Output only the summary sections." % max_words)
    sys_note = "You are a compression assistant. Output only the requested summary, no preamble."
    msgs = ([{"role": "system", "content": sys_note}]
            + list(messages) + [{"role": "user", "content": prompt}])
    try:
        data = chat_completion(msgs, cfg)
        text = (data["choices"][0]["message"].get("content") or "").strip()
        if not text:
            return None
        # guard against a chatty model prefixing a preamble ("Here is...", "Sure:")
        low = text.lower()
        if low.startswith("here") or low.startswith("sure") or low.startswith("ok"):
            text = re.split(r"\n", text, 1)[-1].strip()
        return text[:4000] or None
    except Exception:
        return None


def _fallback_summary(head):
    first = next((m.get("content", "") for m in head if m.get("role") == "user"), "")
    first = re.sub(r"\s+", " ", str(first)).strip()
    return ("Earlier conversation was compacted to save context (%d messages dropped).\n"
            "First user message: %s" % (len(head), first[:200] or "(none)"))


def compress_history(messages, cfg, summarizer=None, keep_frac=0.4, min_keep=8):
    """Summarize the older messages into one summary message, keeping a recent tail.

    Returns (new_history, stats) with stats None when there's nothing to compress.
    `summarizer` is injectable for tests: callable(messages, cfg) -> str.
    """
    window = context_window_for(cfg)
    if window <= 0 or len(messages) <= min_keep:
        return messages, None
    keep_tokens = max(400, int(window * keep_frac))  # small windows can still compress
    acc = 0
    tail_start = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        acc += estimate_message_tokens(messages[i])
        if acc > keep_tokens and len(messages) - i >= min_keep:
            tail_start = i
            break
    if tail_start >= len(messages) or tail_start <= 1:
        return messages, None
    head, tail = messages[:tail_start], messages[tail_start:]
    # never leave the tail starting mid-tool-sequence: tool results must follow
    # their assistant tool_call, so push leading tool messages into the summary part
    while tail and tail[0].get("role") == "tool":
        head.append(tail.pop(0))
    if not tail:
        return messages, None
    if summarizer is None:
        summarizer = summarize_with_llm
    summary = summarizer(head, cfg)
    mode = "llm"
    if not summary:
        summary = _fallback_summary(head)
        mode = "fallback"
    new = [{"role": "user", "content": "[summary of earlier conversation]\n" + summary}] + tail
    return new, {"dropped": len(head), "kept": len(tail), "mode": mode}


def compress_now(history, cfg, threshold=0.75, force=False):
    """If usage exceeds the threshold (or force=True), summarize older messages
    in place. Returns True when a compression happened; never raises on failure."""
    tokens, window = context_usage(history, cfg)
    if window <= 0:
        p_info("(no context window configured)")
        return False
    if not force and tokens <= int(window * threshold):
        return False
    p_info("context %d%% of %s — compressing older messages…"
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
            p_info("(nothing to compress — a single message dominates the window; consider /new)")
        else:
            p_info("(nothing to compress)")
        return False
    history[:] = new
    p_ok("✓ context compressed · %d earlier message%s → summary"
         % (stats["dropped"], "" if stats["dropped"] == 1 else "s"))
    if stats.get("mode") == "fallback":
        p_info("  (offline summary — the model call failed, kept a basic note)")
    return True


# ============================================================
#  Terminal UI
# ============================================================
class C:
    RESET = "\x1b[0m"
    BOLD = "\x1b[1m"
    DIM = "\x1b[2m"
    CYAN = "\x1b[36m"
    GREEN = "\x1b[32m"
    YELLOW = "\x1b[33m"
    RED = "\x1b[31m"
    FG = "\x1b[38;5;%dm"   # 256-color foreground template
    BG = "\x1b[48;5;%dm"   # 256-color background template


COLOR = bool(sys.stdout.isatty()) and not os.environ.get("NO_COLOR")


# ---------------- skins (Hermes-style theming, own palettes) ----------------
# Each skin picks 256-color ANSI codes; the layout is always the same, only the
# palette changes. '/skin' lists & switches them (persisted in config.json).
SKINS = {
    "midnight": {  # default — deep-space blues
        "desc": "deep-space blues (default)",
        "accent": C.FG % 45, "user": C.FG % 220, "agent": C.FG % 81,
        "tool": C.FG % 141, "border": C.FG % 240, "chip": C.FG % 45,
        "code": C.FG % 187, "ok": C.FG % 114, "err": C.FG % 203,
        "dim": C.FG % 244,
    },
    "ember": {  # warm fire palette
        "desc": "warm embers & gold",
        "accent": C.FG % 208, "user": C.FG % 222, "agent": C.FG % 209,
        "tool": C.FG % 203, "border": C.FG % 240, "chip": C.FG % 208,
        "code": C.FG % 223, "ok": C.FG % 114, "err": C.FG % 196,
        "dim": C.FG % 244,
    },
    "ocean": {  # cool sea blues & teal
        "desc": "cool sea blues & teal",
        "accent": C.FG % 75, "user": C.FG % 51, "agent": C.FG % 117,
        "tool": C.FG % 110, "border": C.FG % 240, "chip": C.FG % 75,
        "code": C.FG % 158, "ok": C.FG % 114, "err": C.FG % 203,
        "dim": C.FG % 244,
    },
    "daylight": {  # for bright terminals — dark ink on light
        "desc": "bright terminals, dark ink",
        "accent": C.FG % 27, "user": C.FG % 130, "agent": C.FG % 27,
        "tool": C.FG % 90, "border": C.FG % 250, "chip": C.FG % 27,
        "code": C.FG % 22, "ok": C.FG % 28, "err": C.FG % 124,
        "dim": C.FG % 240,
    },
}

CUR_SKIN = SKINS[DEFAULT_SKIN]


def set_active_skin(state):
    """Pick the persisted skin (config.json) for the rest of the session."""
    global CUR_SKIN
    name = (state or {}).get("skin")
    CUR_SKIN = SKINS.get(name, SKINS[DEFAULT_SKIN])


def col(code, s):
    return code + s + C.RESET if COLOR and code else s


def p_info(s):
    print(col(CUR_SKIN["dim"], s))


def p_agent(s):
    print(col(C.BOLD + CUR_SKIN["agent"], "agent") + "  " + s)


def p_err(s):
    print(col(C.BOLD + CUR_SKIN["err"], "error") + "  " + s)


def p_ok(s):
    print(col(CUR_SKIN["ok"], s))


def p_warn(s):
    print(col(C.YELLOW, "  ⚠") + "  " + s)


# ---------------- blocks & width-aware layout ----------------
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _vlen(s):
    """Visible length of a string (ANSI escapes stripped)."""
    return len(_ANSI_RE.sub("", s))


def _term_width():
    try:
        return max(40, shutil.get_terminal_size((80, 24)).columns)
    except Exception:
        return 80


def _content_w():
    return max(20, _term_width() - 6)


def _wrap_text(text, w):
    """Wrap plain text to width w; returns a list of lines."""
    out = []
    for para in text.split("\n"):
        if not para.strip():
            out.append("")
            continue
        if _vlen(para) <= w:
            out.append(para)
            continue
        cur = ""
        for word in para.split(" "):
            while _vlen(word) > w:  # break over-long words
                if cur:
                    out.append(cur)
                    cur = ""
                out.append(word[:w])
                word = word[w:]
            cand = (cur + " " + word).strip() if cur else word
            if _vlen(cand) <= w:
                cur = cand
            else:
                if cur:
                    out.append(cur)
                cur = word
        if cur:
            out.append(cur)
    return out


def box_open(title, color):
    """Top border of a rounded-corner block, e.g. '  ╭─ agent ──╮'."""
    w = _term_width()
    t = title
    head = "  ╭─ " + col(color, t) + " "
    pad = w - _vlen(head) - 1
    while pad < 2 and t:  # shrink an over-long title instead of overflowing
        t = t[:-1]
        head = "  ╭─ " + col(color, t) + " "
        pad = w - _vlen(head) - 1
    return head + "─" * max(pad, 1) + "╮"


def box_close():
    return "  ╰" + "─" * max(_term_width() - 4, 1) + "╯"


def box_line(content, color=None, right_pad=True):
    """A content row: '  │ text │'. right_pad=False streams without the right border."""
    w = _term_width()
    c = col(color, content) if color else content
    if right_pad:
        pad = w - 6 - _vlen(c)
        if pad > 0:
            c += " " * pad
        return "  │ " + c + " │"
    return "  │ " + c


def box(title, lines, color):
    """A complete static block (used for the 'you' bubble and the banner)."""
    out = [box_open(title, color)]
    for ln in lines:
        for piece in _wrap_text(ln, _content_w()):
            out.append(box_line(piece))
    out.append(box_close())
    return "\n".join(out)


def style_inline(text, skin):
    """Inline markdown → ANSI: `code`, **bold**, *italic*.

    Returns text untouched when colors are off, so piped/NO_COLOR output keeps
    its original markdown characters.
    """
    if not COLOR:
        return text
    t = re.sub(r'`([^`]+)`', lambda m: col(skin["code"], m.group(1)), text)
    t = re.sub(r'\*\*(.+?)\*\*', lambda m: col(C.BOLD, m.group(1)), t)
    t = re.sub(r'__(.+?)__', lambda m: col(C.BOLD, m.group(1)), t)
    t = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', lambda m: col(C.DIM, m.group(1)), t)
    t = re.sub(r'(?<!_)_(?!_)(.+?)(?<!_)_(?!_)', lambda m: col(C.DIM, m.group(1)), t)
    return t


class AgentWriter:
    """Streams an agent response inside a bordered box with live markdown.

    Text flows immediately, line by line, with a '  │ ' gutter. Code fences
    (```) switch to a dim code gutter with a '─ code ─' marker; the language
    name after the opening fence is captured and shown in the marker.
    """

    def __init__(self, skin, color):
        self.skin = skin
        self.color = color
        self.in_code = False
        self._code_label = None   # not None while waiting for the code-fence language
        self.started = False
        self.closed = False
        self.at_line_start = True

    # ---- low-level output ----
    def _write(self, s):
        if self.at_line_start:
            self.at_line_start = False
            sys.stdout.write("  │ ")
        sys.stdout.write(s)

    def _nl(self):
        sys.stdout.write("\n")
        self.at_line_start = True

    def _emit_plain(self, styled):
        if not self.at_line_start:
            self._nl()
        self._write(styled)
        self._nl()

    # ---- public API ----
    def feed(self, chunk):
        if not self.started:
            self.started = True
            print(box_open("agent", self.color), flush=True)
        parts = chunk.split("```")
        for i, part in enumerate(parts):
            if i > 0:
                self.in_code = not self.in_code
                if self.in_code:
                    self._code_label = ""   # collect the language until the newline
                else:
                    self._flush_code_label()
                    self._emit_plain(col(self.skin["dim"], "─ end"))
            if part:
                if self.in_code:
                    self._write_code(part)
                else:
                    self._write_inline(part)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if not self.started:
            return
        if self.in_code:
            self.in_code = False
            self._flush_code_label()
            self._emit_plain(col(self.skin["dim"], "─ end"))
        if not self.at_line_start:
            self._nl()
        print(box_close(), flush=True)

    def _flush_code_label(self):
        """Code buffered while waiting for a language newline is real code —
        never drop it (single-line blocks have no newline at all)."""
        if self._code_label is None or not self._code_label.strip():
            self._code_label = None
            return
        text = self._code_label
        self._code_label = None
        self._emit_plain(col(self.skin["dim"], "─ code ─"))
        lines = text.split("\n")
        for idx, piece in enumerate(lines):
            if piece:
                self._write(col(self.skin["code"], piece))
            if idx < len(lines) - 1:
                self._nl()

    # ---- content writers ----
    def _write_code(self, part):
        if self._code_label is not None:
            self._code_label += part
            if "\n" not in self._code_label:
                return
            label, rest = self._code_label.split("\n", 1)
            self._code_label = None
            self._emit_plain(col(self.skin["dim"], "─ " + (label.strip() or "code") + " ─"))
            part = rest
        lines = part.split("\n")
        for idx, piece in enumerate(lines):
            if piece:
                self._write(col(self.skin["code"], piece))
            if idx < len(lines) - 1:
                self._nl()

    def _write_inline(self, part):
        lines = style_inline(part, self.skin).split("\n")
        for idx, piece in enumerate(lines):
            if piece:
                if self.at_line_start:
                    hm = re.match(r"^#{1,6}\s+(.*)$", piece)
                    if hm:  # '## Heading' -> bold accent heading
                        piece = col(C.BOLD + self.skin["agent"], hm.group(1))
                self._write(piece)
            if idx < len(lines) - 1:
                self._nl()


def render_markdown(text):
    """Render a full text with inline markdown + fenced code blocks (no streaming)."""
    out = []
    in_code = False
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        out.append(col(CUR_SKIN["code"], line) if in_code else style_inline(line, CUR_SKIN))
    return "\n".join(out)


def fmt_args(args):
    return ", ".join("%s=%r" % (k, v) for k, v in (args or {}).items())


def tool_summary(result):
    if not isinstance(result, dict):
        return str(result)[:80]
    if "result" in result:
        return str(result["result"])[:80]
    if "status" in result:
        return "HTTP %s · %s chars" % (result.get("status"), result.get("chars", "?"))
    if "exit" in result:
        snippet = (result.get("stdout") or result.get("stderr") or "").strip()
        return "exit %s%s" % (result.get("exit"), " · " + snippet[:60] if snippet else "")
    if "entries" in result:
        return "%d entries" % result.get("count", len(result.get("entries", [])))
    if "skills" in result:
        return "%d skills" % len(result.get("skills", []))
    if "path" in result and "content" in result:
        return "%s (%s chars)" % (result.get("path"), result.get("chars", 0))
    if "path" in result and result.get("ok") is True:
        return "%s ✓" % result.get("path")
    if result.get("ok") is False:
        return str(result.get("error", "failed"))[:80]
    if "found" in result:
        return "found" if result.get("found") else "not found"
    if "count" in result:
        return "%d todos" % result.get("count", 0)
    if result.get("ok") is True:
        return "ok"
    return json.dumps(result)[:80]


class Spinner:
    """Tiny animated indicator; safe to start()/stop() repeatedly.

    The verb (message) can change live — 'thinking', 'streaming', 'running
    tools' — like the Hermes TUI's customizable busy verbs.
    """

    def __init__(self, msg="thinking"):
        self.msg = msg
        self._stop = True
        self._t = None
        self._lock = threading.Lock()

    def set_msg(self, msg):
        with self._lock:
            self.msg = msg

    def _run(self):
        frames = "|/-\\"
        i = 0
        while True:
            with self._lock:
                if self._stop:
                    return
                msg = self.msg
            sys.stderr.write("\r" + msg + " " + frames[i % 4])
            i += 1
            time.sleep(0.12)

    def start(self):
        with self._lock:
            if not self._stop:
                return
            self._stop = False
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def stop(self):
        with self._lock:
            self._stop = True
        if self._t is not None:
            self._t.join(timeout=0.5)
        sys.stderr.write("\r" + " " * 30 + "\r")
        sys.stderr.flush()


_UI = {"spinner": None}


def tool_open(name, args):
    """Open line of a compact tool block: '  ╭─ ⚙ name (args)'."""
    a = fmt_args(args)
    print("  " + col(CUR_SKIN["tool"], "╭─ ⚙ " + name + ((" (" + a + ")") if a else "")), flush=True)


def tool_close(name, status, result):
    """Close line of a tool block: '  ╰─ ✓ name → summary'."""
    mark = col(CUR_SKIN["ok"], "✓") if status == "done" else col(CUR_SKIN["err"], "✗")
    line = "  " + col(CUR_SKIN["tool"], "╰─") + " " + mark + " " + name
    if result is not None:
        line += " " + col(CUR_SKIN["dim"], "→ " + tool_summary(result))
    print(line, flush=True)


def on_tool(tool_id, name, args, result, status):
    sp = _UI["spinner"]
    if sp:
        sp.stop()
    if status == "running":
        tool_open(name, args)
    else:
        tool_close(name, status, result)
    if sp:
        sp.start()


def run_agent_tui(history, cfg):
    """Run the agent loop with streaming output, spinner, and live tool blocks.

    Returns the 'done' payload augmented with:
      tools    — number of tool calls this turn
      elapsed  — wall-clock seconds for the turn
      streamed — whether any text was printed (so the caller can avoid
                 re-printing the answer, fixing the old double-print)
    """
    sp = Spinner("thinking")
    _UI["spinner"] = sp
    sp.start()
    content_parts = []
    tool_count = 0
    writer = AgentWriter(CUR_SKIN, CUR_SKIN["agent"])
    t0 = time.monotonic()
    try:
        for evt_type, evt_data in run_agent_stream(history, cfg):
            if evt_type == "text":
                sp.set_msg("streaming")
                content_parts.append(evt_data)
                writer.feed(evt_data)
            elif evt_type == "tool_start":
                sp.set_msg("running tools")
                tool_count += 1
                tool_open(evt_data["name"], evt_data["args"])
            elif evt_type == "tool_end":
                tool_close(evt_data["name"], evt_data["status"], evt_data["result"])
            elif evt_type == "done":
                writer.close()
                res = dict(evt_data)
                res["tools"] = tool_count
                res["elapsed"] = time.monotonic() - t0
                res["streamed"] = bool(content_parts)
                return res
    finally:
        writer.close()
        sp.stop()
        _UI["spinner"] = None
    return {"content": "".join(content_parts), "history": history, "cancelled": False,
            "tools": tool_count, "elapsed": time.monotonic() - t0,
            "streamed": bool(content_parts)}


def trim_history(history):
    # a pure safety net now — the context meter + auto-compress manage the real
    # per-model limit, so the hard cap is generous
    # never trim away a leading compression summary (that would silently lose
    # all of the summarized context)
    head = []
    if history and str(history[0].get("content", "")).startswith("[summary of earlier conversation]"):
        head = [history[0]]
        history[:] = history[1:]
    if len(history) > 120:
        history[:] = history[-120:]
    chars = 0
    for i in range(len(history) - 1, -1, -1):
        chars += len(history[i].get("content") or "")
        if chars > 500000 and i > 0:
            del history[:i]
            break
    history[:] = head + history


def mask_key(key):
    return "****" if key else "(none)"


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
    """Interactive y/N prompt used as ON_PERMISSION in the REPL."""
    sp = _UI.get("spinner")
    if sp:
        sp.stop()
    print()
    print(col(CUR_SKIN["err"], "  ⚠ permission needed") + "  " + desc)
    try:
        v = input("    allow? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    finally:
        if sp:
            sp.start()
    return v in ("y", "yes", "a", "allow")


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
        p_info("  invalid choice — keeping %s" % keep)
        return current
    return ask("model", current)


def cmd_models(state):
    cfg = active_cfg(state)
    if not (cfg.get("base_url") or "").rstrip("/"):
        p_err("no base url configured for '%s' — run /provider %s" % (state["active"], state["active"]))
        return
    cfg["model"] = pick_model(cfg.get("base_url", ""), cfg.get("api_key", ""), cfg.get("model", ""))
    save_state(state)
    p_ok("saved ✓")


_SLASH_COMMANDS = [
    "/help", "/config", "/provider", "/models", "/test", "/skin",
    "/sessions", "/session", "/new", "/clear", "/context", "/compress",
    "/tools", "/todos", "/todo", "/memory", "/skills", "/install_skill",
    "/multi", "/export", "/stop", "/exit", "/quit",
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
        p_err("unknown skin '%s' — see /skin" % arg)
        return
    state["skin"] = arg
    save_state(state)
    set_active_skin(state)
    p_ok("skin set to '%s' ✓" % arg)


def cmd_sessions():
    """List saved sessions (name, message count, last updated)."""
    sess = sessions_map()
    if not sess:
        print("  (no sessions yet — /session <name> starts one)")
        return
    active = _store_get(ACTIVE_SESSION_KEY, "default")
    print("  sessions (%d):" % len(sess))
    for name in sorted(sess, key=lambda n: (sess[n].get("updated") or ""), reverse=True):
        rec = sess[name]
        n = len(rec.get("messages") or [])
        upd = (rec.get("updated") or "")[11:16] or "?"
        mark = "▶" if name == active else " "
        print("   %s %-22s %3d msgs · %s" % (mark, name[:22], n, upd))
    print("  usage: /session <name> · /session rm <name> · /session rename <old> <new>")


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
        except ValueError:
            p_err("usage: /context window <tokens>  (0 = auto-detect from the model)")
            return
        cfg["context_window"] = w
        save_state(state)
        p_ok("context window set to %s ✓" % (_fmt_k(w) if w else "auto"))
        return
    if sub in ("auto", "autocompress", "auto-compress"):
        cfg["auto_compress"] = not cfg.get("auto_compress", True)
        save_state(state)
        p_ok("auto-compress %s ✓" % ("on" if cfg["auto_compress"] else "off"))
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
        p_warn("context is %d%% full — /new starts a fresh session · /compress summarizes now" % pct)


def cmd_compress(history, state, session):
    """Manually summarize older messages to free context (persists immediately)."""
    if len(history) < 8:
        p_info("(conversation is short — nothing to compress)")
        return
    if compress_now(history, active_cfg(state), force=True):
        save_session(session, history)


def cmd_help():
    print("  commands:")
    print("    /help /?               this help")
    print("    /sessions              list saved sessions (name · messages · updated)")
    print("    /session <name>        switch to (or create) a session")
    print("    /session rm <name>     delete a session      /session rename <old> <new>")
    print("    /new                   start a fresh session (the current one is saved)")
    print("    /clear                 wipe the current conversation")
    print("    /context               context meter + settings (window, auto-compress)")
    print("    /compress              summarize older messages to free context now")
    print("    /multi                 multi-line input ('.' on its own line submits)")
    print("    /export                save the conversation as a text file")
    print("    /provider [name]       list / add / switch provider profiles")
    print("    /provider rm <name>    delete a provider")
    print("    /config                edit the ACTIVE provider (base url, key, model, temp)")
    print("    /models                fetch + pick a model for the active provider")
    print("    /test                  test the active provider's connection")
    print("    /skin [name]           list / switch the UI skin (midnight, ember, ocean, daylight)")
    print("    (at the api key prompt, type 'none' to clear the key)")
    print("    /tools                 list the agent's tools")
    print("    /todos                 show the to-do list")
    print("    /todo <text>           add a task")
    print("    /todo done <i>         toggle task i      /todo rm <i>   remove task i")
    print("    /todo clear            empty the list")
    print("    /memory                show saved memory facts")
    print("    /skills", "/install_skill                list saved skills")
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
    except ValueError:
        pass
    ac = ask("auto-compress near the context limit (y/n)",
             "y" if cfg.get("auto_compress", True) else "n").strip().lower()
    cfg["auto_compress"] = ac in ("y", "yes", "on", "1", "true")
    save_state(state)
    p_ok("saved ✓")


def _list_providers(state):
    profiles = state["profiles"]
    if not profiles:
        print("  (no providers configured)")
        return
    print("  providers:")
    for name, p in profiles.items():
        mark = "   <- active" if name == state["active"] else ""
        print("    %-12s %s · model %s · key %s%s"
              % (name, p.get("base_url") or "(no base)",
                 p.get("model") or "-", mask_key(p.get("api_key", "")), mark))
    print("  usage: /provider <name> (add or switch) · /provider rm <name>")
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
        p_ok("removed '%s' ✓" % sub)
        return
    if not arg:
        _list_providers(state)
        return

    # switch to an existing provider
    if arg in profiles:
        state["active"] = arg
        save_state(state)
        p_ok("switched to '%s' ✓" % arg)
        return

    # add a new provider — a fresh profile, never inherits another provider's key
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
    p_ok("added '%s' ✓" % arg)


def cmd_test(state):
    cfg = active_cfg(state)
    base = (cfg.get("base_url") or "").rstrip("/")
    if not base:
        p_err("no base url configured for '%s' — run /provider %s" % (state["active"], state["active"]))
        return
    req = urllib.request.Request(
        base + "/models",
        headers={"Authorization": "Bearer " + (cfg.get("api_key") or ""),
                 "Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        n = len(data.get("data") or [])
        p_ok("Connected ✓ · %d model%s available" % (n, "" if n == 1 else "s"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        p_err("HTTP %d — %s" % (e.code, _readable_error(e.code, body)))
    except Exception as e:
        p_err("cannot reach API: %s" % e)


def cmd_tools():
    for t in TOOLS:
        fn = t["function"]
        print("  %-12s %s" % (fn["name"], fn.get("description", "")))


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


def cmd_skills():
    r = tool_skill_list()
    names = r.get("skills") or []
    if not names:
        print("  (no skills yet — ask the agent to save one)")
        return
    print("  skills (%d):" % len(names))
    for n in names:
        print("    - " + n)



def cmd_install_skill(rest):
    # Parse and install a skill from a markdown file.
    path = rest.strip()
    if not path or not os.path.exists(path):
        p_err("usage: /install_skill <path_to_skill.md>")
        return
    try:
        with open(path, "r") as f:
            content = f.read()
        name = os.path.basename(path).replace(".md", "")
        r = tool_skill_save(name, content)
        if r.get("ok"):
            p_ok("installed skill '%s' ✓" % name)
        else:
            p_err("failed to save skill: %s" % r.get("error", "unknown error"))
    except Exception as e:
        p_err("failed to install skill: %s" % e)
def cmd_clear(history):
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
    print(col(C.DIM, "  (multi-line mode — type '.' alone to submit, Ctrl+C to cancel)"))
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
def new_session_name():
    """Name for a fresh, not-yet-titled session (auto-titled from the first message)."""
    return "sess-" + secrets.token_hex(2)


def setup_completion():
    """Tab-complete slash commands via readline (Hermes-style autocomplete,
    adapted to a line-oriented Termux prompt)."""
    try:
        readline.set_completer(_slash_complete)
        readline.parse_and_bind("tab: complete")
    except Exception:
        pass


def _slash_complete(text, state):
    if text.startswith("/"):
        opts = [c for c in _SLASH_COMMANDS if c.startswith(text.lower())]
        return opts[state] if state < len(opts) else None
    return None


def banner(state):
    """Hero header, Hermes-banner style: branding, version, skin, chips."""
    cfg = active_cfg(state)
    skin = CUR_SKIN
    lines = [
        "autonomous terminal agent  " + col(C.DIM, "v" + ALVA_VERSION),
        "shell · files · skills · self-improvement",
        "",
        col(skin["chip"], "●") + " skin " + (state.get("skin") or DEFAULT_SKIN)
        + "   " + col(skin["chip"], "●") + " provider " + state["active"]
        + "   " + col(skin["chip"], "●") + " model " + (cfg.get("model") or "?"),
        col(skin["chip"], "●") + " ctx " + _fmt_k(context_window_for(cfg))
        + "   " + col(skin["chip"], "●") + " auto-compress "
        + ("on" if cfg.get("auto_compress", True) else "off"),
        col(C.DIM, "config/store: " + DATA_DIR),
    ]
    print()
    print(box("⚡ alvaagent", lines, skin["accent"]))
    print("  " + col(C.DIM, "type a message · /help lists commands · Tab completes /commands"))
    print()
    if not cfg.get("api_key"):
        p_info("no API key set for '%s' — run /provider %s or /config" % (state["active"], state["active"]))


def render_status_bar(state, session, elapsed, tools, history):
    # Render a persistent status bar at the bottom of the terminal.
    cfg = active_cfg(state)
    skin = CUR_SKIN
    tokens, window = context_usage(history, cfg)
    pct = tokens * 100 // window if window else 0
    ctx_col = skin["ok"] if pct < 60 else (C.YELLOW if pct < 85 else skin["err"])
    
    parts = [
        col(skin["chip"], "●") + " " + col(skin["dim"], session[:16]),
        col(skin["chip"], "●") + " " + col(skin["dim"], state["active"] + "/" + (cfg.get("model") or "?")),
        col(skin["chip"], "●") + " " + col(ctx_col, "ctx %d%%" % pct),
        col(skin["chip"], "●") + " " + col(skin["dim"], "%.1fs" % elapsed)
    ]
    status_line = "  " + "   ".join(parts)
    # ANSI: Save cursor, move to bottom, clear line, write status, restore cursor
    print(f"\033[s\033[999;1H\033[K{status_line}\033[u", end="", flush=True)

def status_footer(state, session, elapsed, tools, history):
    # This now just acts as an alias or trigger to render the persistent bar
    render_status_bar(state, session, elapsed, tools, history)

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
    print(box("you", [text], CUR_SKIN["user"]))
    history.append({"role": "user", "content": text})
    trim_history(history)
    # pre-turn safety: only act if the window is nearly full (0.9) — the post-turn
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
    if not res.get("streamed"):
        p_agent(res.get("content") or "")
    # post-turn: auto-compress if the response pushed us past the threshold
    compressed = False
    if cfg.get("auto_compress", True):
        compressed = compress_now(history, cfg)
    tokens, window = context_usage(history, cfg)
    status_footer(state, session, res.get("elapsed", 0.0), res.get("tools", 0), history)
    pct = tokens * 100 // window if window else 0
    if not compressed and window and pct >= 85:
        p_warn("context at %d%% of %s — /new starts a fresh session · /compress summarizes older messages"
               % (pct, _fmt_k(window)))
    save_session(session, history)
    return session


def repl():
    state = load_state()
    set_active_skin(state)
    # resume the last active session (conversations persist across restarts)
    session = _store_get(ACTIVE_SESSION_KEY) or "default"
    history = load_session(session)
    while True:
        try:
            prompt = col(CUR_SKIN["accent"], "❯ ") if COLOR else "❯ "
            line = input(prompt)
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
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
                        p_err("that's the active session — switch first (/session <name>)")
                    else:
                        delete_session(target)
                        p_ok("deleted session '%s' ✓" % target)
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
                        p_ok("renamed '%s' → '%s' ✓" % (target, new))
                else:
                    save_session(session, history)  # persist the outgoing session
                    target = _find_session(arg)
                    if target is None:
                        target = arg
                        p_info("(new session '%s')" % target)
                    history[:] = load_session(target)
                    session = target
                    save_session(session, history)  # mark active + refresh timestamp
                    p_ok("switched to session '%s' · %d messages" % (session, len(history)))
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
            elif c == "/skills":
                cmd_skills()
            elif c == "/memory":
                cmd_memory()
            elif c == "/export":
                cmd_export(history)
            elif c == "/stop":
                cancel_agent()
                p_info("stopping…")
            elif c in ("/exit", "/quit", "/q"):
                break
            else:
                p_err("unknown command: " + c + "   (/help for the list)")
            continue

        session = send_message(line, history, state, session)
    save_session(session, history)
    print(col(C.DIM, "bye 👋"))


def main():
    global ON_TOOL, ON_PERMISSION
    _load_store()
    setup_completion()
    ON_TOOL = on_tool        # live tool-progress blocks
    ON_PERMISSION = ask_permission  # interactive y/N for risky actions
    state = load_state()
    set_active_skin(state)
    banner(state)
    repl()


if __name__ == "__main__":
    main()
