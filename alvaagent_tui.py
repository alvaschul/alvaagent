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
#    /help /config /provider /models /test /tools /todos /todo /memory
#    /skin /sessions /session /new /clear /context /compress /multi /export
#    /stop /exit /quit
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
import ast
import codecs
import datetime
import fnmatch
import html
import json
import math
import os
import re
import readline
import secrets
import select
import shutil
import signal
import subprocess
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

# Rich backs the Hermes-style panels (pure-Python, pip-installs on Termux).
# The Hermes agent TUI renders with Rich `Panel(box=HORIZONTALS)`; we mirror
# that exactly so alvaagent reads as Hermes. `pip install --break-system-packages
# rich` is run by alva_fix.sh; if it's somehow absent we fall back to a tiny
# ANSI shim so the TUI still launches.
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.box import HORIZONTALS
    _CON = Console()
except Exception:  # pragma: no cover - only when rich is unavailable
    import sys as _sys
    class _ShimPanel:
        def __init__(self, *a, **k):
            self._render = (a[0] if a else "")
            self.title = k.get("title", "")
            self.border_style = k.get("border_style", "")
            self.box = k.get("box")
            self.padding = k.get("padding", (0, 0))
            self.width = k.get("width")
        def __str__(self):
            return str(self._render)
    class _ShimConsole:
        def print(self, *a, **k):
            for x in a:
                _sys.stdout.write(str(x) + "\n")
    class _ShimBox:
        HORIZONTALS = "HORIZONTALS"
    Console = _ShimConsole
    Panel = _ShimPanel
    HORIZONTALS = _ShimBox.HORIZONTALS
    _CON = Console()

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
HISTORY_PATH = os.path.join(DATA_DIR, "cmd_history.txt")

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

# known context windows (tokens) - used for the ctx meter + auto-compress
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
FEEDBACK_KEY = "alvaagent.feedback"
IMPROVEMENT_KEY = "alvaagent.improvements"
HISTORY_KEY = "alvaagent.history"
SESSION_KEY = "alvaagent.sessions"
ACTIVE_SESSION_KEY = "alvaagent.active_session"
MAX_SESSIONS = 30  # oldest sessions are pruned past this many (keeps store.json small)

# ---------------- autonomy: permissions ----------------
# The agent can run shell commands, edit files and manage skills. Everything
# outside the project folder (or risky) goes through ON_PERMISSION, which the
# REPL wires to an interactive y/N prompt. Headless (no hook) defaults to DENY,
# unless ALVA_AUTO_APPROVE=1 is set (attended/automated runs).

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.join(DATA_DIR, "skills")

ON_PERMISSION = None  # hook: ON_PERMISSION(description) -> bool

# commands that are safe to run without asking
# NOTE: `env` is intentionally NOT here - `env` executes its arguments
# (`env sh -c '...'`, `env -S '...'`) and would let a quoted destructive
# command bypass the risk scan.
_READONLY_PREFIXES = (
    "ls", "cat", "pwd", "whoami", "echo", "date", "which", "find",
    "head", "tail", "grep", "stat", "df", "du", "free", "uname",
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
    # command interpreters/executors: `sh -c '...'` runs arbitrary (quoted)
    # commands, so they can never be treated as read-only.
    "sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh", "fish", "pwsh",
    "eval", "exec", "source", "command", "xargs", "nohup", "env",
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
    # pass as read-only - they execute arbitrary commands.)
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
    # -exec/-execdir/-ok) turn it into a wipe - treat them as risky.
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
        _atomic_write(path, updated)
        return {"ok": True, "path": path, "replaced": 1}
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


def tool_file_search(pattern, path=None, max_depth=3):
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


# ---------------- skills: Hermes-style frontmatter + categorized storage ----------------
# Skills mirror Hermes: each skill file is a Markdown doc with an optional YAML
# frontmatter block between `---` fences. The frontmatter carries `name`,
# `description`, optional `version`/`author`/`tags`/`related_skills`, and the
# body is the procedure itself (trigger + numbered steps).
#
# Storage layout (Hermes-style):
#   ~/.alvaagent/skills/<category>/<name>.md
#     e.g. skills/productivity/product-price-monitor.md
#     e.g. skills/research/competitor-news-monitor.md
# `<category>` is a slash-free folder name; `<name>.md` is the skill filename.
# `tool_skill_save` writes into SKILLS_DIR/<category>/<name>.md when a category
# is supplied, otherwise falls back to the legacy flat layout so old skills keep
# working. `tool_skill_list` returns [{"name": ..., "category": ..., "file": ...,
# "description": ..., "tags": ..., "related_skills": ...}, ...] and strips the
# legacy flat names so callers that only want names still work.
#
# Backward compat: flat files (skills/<name>.md with no category folder) are
# still readable and listable. On save, if `category` is omitted or empty the
# skill lives flat; if supplied it goes into the categorized layout. `skill_read`
# accepts either "name" (flat) or "category/name" (categorized).

_SKILL_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

_SKILL_FM_DEFAULT = {
    "name": None,       # filled from the filename on save
    "description": None,
    "version": None,
    "author": None,
    "tags": [],
    "related_skills": [],
}

_VALID_FM_KEYS = frozenset(("name", "description", "version", "author", "tags", "related_skills"))


def _mini_scalar(v):
    """Coerce a bare frontmatter scalar to a Python value (strips quotes,
    parses inline '[a, b]' arrays so tags: [x, y] works without PyYAML)."""
    s = v.strip()
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_mini_scalar(x.strip()) for x in inner.split(",")]
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    if s.lower() in ("null", "none", "~"):
        return None
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def _finish_block(kind, lines, chomp):
    """Render collected block-scalar lines: '>' folds to a single space, '|'
    preserves newlines. Chomping: '-' strips the trailing newline, '+' keeps
    it, default (clip) yields a single trailing newline."""
    if kind == ">":
        text = " ".join(x for x in lines if x)
    else:
        text = "\n".join(lines)
    text = text.rstrip()
    if chomp == "+":
        text += "\n"
    return text


def _mini_yaml(text):
    """Tiny YAML-subset parser for the frontmatter format the harness writes:
    'key: scalar' lines, 'key:' followed by '  - item' list entries, and
    block scalars ('key: >' / 'key: |' with indented continuation lines).
    Used only when PyYAML isn't installed."""
    out = {}
    current_list = None
    block_kind = None
    block_key = None
    block_chomp = ""
    block_lines = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if (line.startswith(" ") or line.startswith("\t")) and block_kind is not None:
            block_lines.append(line.strip())
            continue
        if block_kind is not None:
            out[block_key] = _finish_block(block_kind, block_lines, block_chomp)
            block_kind = None
            block_lines = []
        if line.strip().startswith("- "):
            item = line.split("- ", 1)[1].strip()
            if current_list is not None:
                current_list.append(_mini_scalar(item))
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        if not key or key.startswith(" "):
            continue
        val = val.strip()
        if val in (">", "|", ">-", "|-", ">+", "|+"):
            block_kind = val[0]
            block_chomp = val[1:]
            block_key = key
            block_lines = []
            continue
        if val == "":
            out[key] = []
            current_list = out[key]
        else:
            out[key] = _mini_scalar(val)
            current_list = None
    if block_kind is not None:
        out[block_key] = _finish_block(block_kind, block_lines, block_chomp)
    return out


def _frontmatter_load(raw):
    """Parse skill frontmatter with PyYAML when available, else the mini parser."""
    if yaml is not None:
        try:
            loaded = yaml.safe_load(raw)
            if isinstance(loaded, dict):
                return loaded
        except Exception:
            pass
    try:
        loaded = _mini_yaml(raw)
    except Exception:
        loaded = {}
    return loaded if isinstance(loaded, dict) else {}


def _frontmatter_dump(fm):
    """Serialize frontmatter to the YAML block (PyYAML when available, else the
    mini writer, so skill_save works with zero extra installs)."""
    if yaml is not None:
        try:
            return yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip()
        except Exception:
            pass
    lines = []
    for k, v in fm.items():
        if v is None:
            continue
        if isinstance(v, list):
            if not v:
                lines.append("%s: []" % k)
            else:
                lines.append("%s:" % k)
                for item in v:
                    lines.append("  - %s" % str(item))
        elif isinstance(v, bool):
            lines.append("%s: %s" % (k, "true" if v else "false"))
        else:
            lines.append("%s: %s" % (k, v))
    return "\n".join(lines)


def _parse_frontmatter(text):
    """Return (fm_dict, body) from a skill .md file. fm_dict shares the default
    template so callers always get the same keys; unknown frontmatter keys are
    dropped (like Hermes' strict frontmatter model)."""
    fm = dict(_SKILL_FM_DEFAULT)
    body = text
    m = _SKILL_FM_RE.match(text)
    if m:
        raw = m.group(1)
        body = text[m.end():]
        loaded = _frontmatter_load(raw)
        if isinstance(loaded, dict):
            for k in _VALID_FM_KEYS:
                v = loaded.get(k)
                if v is not None:
                    if k in ("tags", "related_skills") and isinstance(v, list):
                        fm[k] = [str(x) for x in v]
                    elif k == "description":
                        fm[k] = str(v)
                    elif k in ("version", "author"):
                        fm[k] = str(v) if v is not None else None
                    else:
                        fm[k] = v
    return fm, body


def _skill_body_for_tool(fm, body):
    """Render a skill's frontmatter as a one-line description the agent can
    scan, then the full body. This is what tool_skill_read returns as 'content'
    so the agent sees metadata + procedure in one call (Hermes injects the full
    SKILL.md including frontmatter into context)."""
    parts = []
    if fm.get("description"):
        parts.append("# " + fm["description"])
    if fm.get("tags"):
        parts.append("")
        parts.append("tags: " + ", ".join(str(t) for t in fm["tags"]))
    if fm.get("related_skills"):
        parts.append("related: " + ", ".join(str(r) for r in fm["related_skills"]))
    if body.strip():
        if parts:
            parts.append("")
        parts.append(body.rstrip())
    return "\n".join(parts).strip() or body.rstrip()


def _detect_category(name):
    """If `name` already contains a slash, treat the left of it as a category
    and the right as the skill name (so "category/name" works everywhere)."""
    if "/" in name:
        cat, _, nm = name.partition("/")
        return cat.strip(), nm.strip()
    return None, name.strip()


def _skill_filepath(category, name):
    if category:
        return os.path.join(SKILLS_DIR, category, name + ".md")
    return os.path.join(SKILLS_DIR, name + ".md")


def _inside_skills(path):
    """True when `path` (realpath) lives inside SKILLS_DIR. Guards every
    skill-path operation against `..` traversal writing/reading/deleting
    files elsewhere on the device."""
    real = os.path.realpath(path)
    base = os.path.realpath(SKILLS_DIR)
    return real == base or real.startswith(base + os.sep)


def _resolve_skill_path(name):
    """Map a skill name to a real .md file inside SKILLS_DIR.

    Accepts flat ("frontend-design"), category/name ("brainstorming/x"), and
    the frontmatter name of a categorized skill ("brainstorming", which lives
    at skills/brainstorming/SKILL.md). Returns the resolved absolute path, or
    None when nothing matches / the path escapes SKILLS_DIR.
    """
    category, skill_name = _detect_category(str(name))
    if skill_name:
        direct = os.path.realpath(_skill_filepath(category, skill_name))
        if os.path.isfile(direct) and _inside_skills(direct):
            return direct
    # Fallback: scan the index and match by frontmatter name (+ category when
    # the caller qualified it). This is how categorized files whose filename
    # differs from their `name:` (e.g. category/SKILL.md) stay reachable.
    want = str(name).strip().lower()
    for info in _skill_list_all():
        nm = str(info.get("name") or "").lower()
        if nm != want:
            continue
        cat = str(info.get("category") or "").lower()
        if category and cat != category.lower():
            continue
        p = os.path.realpath(os.path.join(SKILLS_DIR, info["file"]))
        if _inside_skills(p):
            return p
    return None


def _skill_read(path):
    """Parse a skill .md file into its metadata dict plus the body the agent
    applies. Returns None when the file is missing or unreadable. This backs
    _skill_list_all() and tool_skill_read()."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return None
    fm, body = _parse_frontmatter(text)
    # Skills without a `name:` frontmatter key still deserve a usable name:
    # fall back to the filename so the banner and /skills can't crash on None.
    name = fm.get("name") or os.path.splitext(os.path.basename(path))[0]
    return {
        "name": name,
        "description": fm.get("description"),
        "version": fm.get("version"),
        "author": fm.get("author"),
        "tags": fm.get("tags"),
        "related_skills": fm.get("related_skills"),
        "content": _skill_body_for_tool(fm, body),
    }


def _scan_skill_files():
    """Walk SKILLS_DIR and yield (category_or_None, name, filepath) for every
    .md file, including legacy flat files. Categorized files take precedence:
    a file under skills/<cat>/<name>.md is NOT confused with a flat
    skills/<cat>.md (the latter is only produced by old saves)."""
    if not os.path.isdir(SKILLS_DIR):
        return
    for entry in os.listdir(SKILLS_DIR):
        full = os.path.join(SKILLS_DIR, entry)
        if os.path.isfile(full) and entry.endswith(".md"):
            yield None, entry[:-3], full
        elif os.path.isdir(full):
            cat = entry
            for sub in os.listdir(full):
                sub_full = os.path.join(full, sub)
                if os.path.isfile(sub_full) and sub.endswith(".md"):
                    yield cat, sub[:-3], sub_full


def _skill_list_all():
    """Scan SKILLS_DIR and return a list of skill dicts (Hermes-style).

    Each dict has: name, category, file, description, tags, related_skills.
    Flat files (no category folder) get category=None; categorized files get
    their folder name. Frontmatter is parsed from each .md file.
    """
    skills = []
    for cat, name, path in _scan_skill_files():
        info = _skill_read(path)
        if info is None:
            continue
        info["category"] = cat
        info["file"] = os.path.relpath(path, SKILLS_DIR)
        skills.append(info)
    return skills


def tool_skill_list():
    """List every skill on the device with metadata (Hermes-style).

    Returns {"ok": True, "skills": [dict, ...]} where each dict has:
      name, category, file, description, tags, related_skills.
    When the caller only wants names it can read d["name"].
    """
    try:
        return {"ok": True, "skills": _skill_list_all()}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def tool_skill_read(name):
    """Read a skill by name (flat) or category/name (categorized).

    Returns {"ok": True, "name": ..., "category": ..., "file": ...,
             "description": ..., "tags": ..., "content": ...}
    where 'content' is the frontmatter-annotated body the agent applies.
    """
    name = str(name).strip()
    if not name:
        return {"ok": False, "error": "empty name"}
    path = _resolve_skill_path(name)
    if path is None:
        return {"ok": False, "error": "no such skill: %s" % name}
    info = _skill_read(path)
    if info is None:
        return {"ok": False, "error": "no such skill: %s" % name}
    rel = os.path.relpath(path, SKILLS_DIR)
    info["category"] = os.path.dirname(rel) if "/" in rel else None
    info["file"] = rel
    return {"ok": True, **info}


def tool_skill_remove(name):
    """Delete a skill by name (flat) or category/name (categorized).

    Returns {"ok": True} on success, {"ok": False, "error": ...} otherwise.
    """
    name = str(name).strip()
    if not name:
        return {"ok": False, "error": "empty name"}
    path = _resolve_skill_path(name)
    if path is None or not _inside_skills(path):
        return {"ok": False, "error": "no such skill: %s" % name}
    try:
        os.remove(path)
        return {"ok": True, "name": os.path.splitext(os.path.basename(path))[0],
                "category": os.path.dirname(os.path.relpath(path, SKILLS_DIR))
                if "/" in os.path.relpath(path, SKILLS_DIR) else None}
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


def tool_skill_save(name, content, category=None):
    """Save a skill with optional YAML frontmatter.

    `name` may be "skill-name" (flat) or "category/skill-name" (categorized);
    the explicit `category` kwarg overrides any slash in `name`.
    `content` is the full .md source including an optional leading `---` block.
    If no frontmatter is present, one is synthesized from the name so every
    skill carries at least a name (Hermes-style self-describing skills).
    """
    name = str(name).strip()
    if not name or "/" in name:
        return {"ok": False, "error": "invalid skill name: %r (use category kwarg for folders)" % name}
    if category:
        cat = str(category).strip()
        # `..` and absolute paths must not escape SKILLS_DIR
        if "/" in cat or "\\" in cat or not cat or ".." in cat.split(os.sep) or cat.startswith("/"):
            return {"ok": False, "error": "invalid category: %r" % category}
    else:
        cat = None
    if not content:
        content = ""
    fm, body = _parse_frontmatter(content)
    # ensure name is always present
    if not fm.get("name"):
        fm["name"] = name
    if not fm.get("description"):
        fm["description"] = name.replace("-", " ").replace("_", " ").title()
    path = _skill_filepath(cat, name)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # rebuild the .md source with a clean frontmatter block
        fm_block = "---\n" + _frontmatter_dump(
            {k: fm[k] for k in ("name", "description", "version", "author", "tags", "related_skills")
             if fm.get(k) is not None}) + "\n---\n\n"
        full = fm_block + body
        _atomic_write(path, full)
        return {"ok": True, "name": name, "category": cat, "path": path,
                "chars": len(full)}
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


def tool_memory_list():
    """List every saved memory fact (key + value)."""
    facts = [{"key": k[len(MEM_PREFIX):], "value": v}
             for k, v in _store.items() if k.startswith(MEM_PREFIX)]
    return {"ok": True, "count": len(facts), "facts": facts}


def tool_memory_search(query=""):
    """Search saved memory facts by key or value (case-insensitive substring).
    An empty query returns everything (same as memory_list)."""
    q = str(query or "").strip().lower()
    facts = []
    for k, v in _store.items():
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



def tool_feedback(rating, notes=None):
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
    fb = _store_get(FEEDBACK_KEY, [])
    fb.append(entry)
    if len(fb) > 50:
        fb = fb[-50:]
    _store_set(FEEDBACK_KEY, fb)
    return {"ok": True, "rating": rating, "stored": True}


def tool_improvement_set(area, action):
    """Record an area to improve and a concrete action to take.

    area: short label like "response brevity".
    action: what to do about it.
    Updates an existing area if present, else appends.
    """
    area = str(area or "").strip()
    action = str(action or "").strip()
    if not area or not action:
        return {"ok": False, "error": "both area and action are required"}
    items = _store_get(IMPROVEMENT_KEY, [])
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
    _store_set(IMPROVEMENT_KEY, items)
    return {"ok": True, "area": area, "stored": True}


def tool_improvement_list():
    """List all improvement areas the agent has recorded."""
    return {"ok": True, "improvements": _store_get(IMPROVEMENT_KEY, [])}


def tool_improvement_done(area):
    """Mark an improvement area as resolved."""
    area = str(area or "").strip().lower()
    if not area:
        return {"ok": False, "error": "area is required"}
    items = _store_get(IMPROVEMENT_KEY, [])
    for it in items:
        if it["area"].lower() == area:
            it["done"] = True
            it["resolved"] = datetime.datetime.now().isoformat(timespec="seconds")
            _store_set(IMPROVEMENT_KEY, items)
            return {"ok": True, "area": it["area"], "done": True}
    return {"ok": False, "error": "no improvement area named: %s" % area}


def tool_reflect():
    """Run a structured self-reflection pass.

    Reads the last 5 feedback entries and all pending improvements. Returns a
    summary the agent can use to decide what to change.
    """
    fb = _store_get(FEEDBACK_KEY, [])
    imps = _store_get(IMPROVEMENT_KEY, [])
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
        "name": "memory_search",
        "description": "Search on-device memory by key or value (case-insensitive substring). Use this when you need a fact but are unsure of its exact key.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Substring to match against keys or values (empty returns all facts)"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "memory_list",
        "description": "List every saved memory fact (key + value).",
        "parameters": {"type": "object", "properties": {}}}},
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
]

TOOL_IMPL = {
    "calculator": lambda a: tool_calculator(a.get("expression")),
    "web_fetch": lambda a: tool_web_fetch(a.get("url")),
    "get_time": lambda a: tool_get_time(),
    "memory_save": lambda a: tool_memory_save(a.get("key"), a.get("value")),
    "memory_recall": lambda a: tool_memory_recall(a.get("key")),
    "memory_search": lambda a: tool_memory_search(a.get("query")),
    "memory_list": lambda a: tool_memory_list(),
    "todo_add": lambda a: tool_todo_add(a.get("text")),
    "todo_list": lambda a: tool_todo_list(),
    "todo_toggle": lambda a: tool_todo_toggle(a.get("index")),
    "todo_remove": lambda a: tool_todo_remove(a.get("index")),
    "run_command": lambda a: tool_run_command(a.get("command")),
    "file_read": lambda a: tool_file_read(a.get("path")),
    "file_write": lambda a: tool_file_write(a.get("path"), a.get("content")),
    "file_edit": lambda a: tool_file_edit(a.get("path"), a.get("old"), a.get("new")),
    "file_list": lambda a: tool_file_list(a.get("path")),
    "file_search": lambda a: tool_file_search(a.get("pattern"), a.get("path"), a.get("max_depth")),
    "feedback": lambda a: tool_feedback(a.get("rating"), a.get("notes")),
    "improvement_set": lambda a: tool_improvement_set(a.get("area"), a.get("action")),
    "improvement_list": lambda a: tool_improvement_list(),
    "improvement_done": lambda a: tool_improvement_done(a.get("area")),
    "self_test": lambda a: tool_self_test(),
    "reflect": lambda a: tool_reflect(),
    "skill_list": lambda a: tool_skill_list(),
    "skill_read": lambda a: tool_skill_read(a.get("name")),
    "skill_save": lambda a: tool_skill_save(
        a.get("name"), a.get("content"),
        category=a.get("category")),
    "skill_remove": lambda a: tool_skill_remove(a.get("name")),
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
1. Use the calculator tool for ANY arithmetic - never guess math.
2. Use web_fetch to read a webpage when the user asks about online content.
3. Use memory_save / memory_recall to remember facts the user asks you to
   remember; memory_search / memory_list find facts when the exact key is unknown.
4. Use todo_add / todo_list / todo_toggle / todo_remove to manage the user's to-do list.
5. Use get_time when the user needs the current date or time.
6. You have real device access: run_command runs shell commands (Termux), and
   file_read / file_write / file_edit / file_list / file_search work on the
   device's files.
   Read-only commands and in-project file edits run freely; mutating/unknown
   commands or out-of-project writes ask the user first - if denied, do not
   retry, and explain what was blocked and why.
7. Skills: skill_list / skill_read / skill_save manage reusable procedures
   stored on the device. BEFORE starting any substantial task, call skill_list
   and read any skill whose name matches the task. Apply the skill's guidance
   faithfully - a skill is the user's preferred way of doing that kind of work.
   When you discover a reusable, non-obvious procedure during a task, save it
   as a skill with a descriptive name and a concise body (trigger + steps).
   Keep skills small and self-contained so they stay easy to apply and test.
8. Self-improvement: you can read your OWN source (alvaagent_tui.py,
   start.sh, test_tui.py) and improve it with file_edit / file_write, then
   validate with run_command("python3 -m py_compile alvaagent_tui.py") and
   run_command("python3 test_tui.py"). After any edit to your own source,
   ALWAYS run self_test to confirm nothing is broken before you tell the user
   the change is done. Changes take effect the next time the user restarts the
   TUI - always say so, and keep edits small, targeted, and tested.
9. Feedback loop: the user can rate your responses with /feedback good|bad.
   When you notice the user expressing satisfaction or frustration, call
   feedback(rating="good"|"bad"|"neutral", notes=...) so the harness records
   it. Periodically call reflect() to review recent feedback and pending
   improvements, and improvement_set(area, action) when a pattern emerges.
   Mark improvements done with improvement_done(area) after you verify the fix.
   Treat repeated "bad" feedback on the same thing as a real bug to fix.
10. Format your answers with light markdown for readability: **bold** for key
    terms, *italic* for emphasis, `code` for commands and file paths, ## headings
    for structure, and - bullets for lists. Keep it light - never wrap tool
    outputs or file contents in emphasis; real code belongs in ``` fences.
Only call a tool when it genuinely helps. If no tool is needed, answer directly.
Respond in the same language the user writes in. Be concise, friendly, and precise."""


def _readable_error(status, text):
    """Best-effort readable message from an API error body (JSON or HTML).

    Gateways/WAFs often return HTML error pages whose <title> says exactly
    what's blocked (Cloudflare, nginx, ...); proxies sometimes wrap upstream
    failures as JSON like {"error": {"message": "[403]: <html>..."}}.
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


_MAX_RETRIES = 3
_RETRY_BACKOFF = (0.5, 1.5, 4.0)  # seconds between retries
_STREAM_IDLE_LIMIT = 90.0         # seconds with no bytes before a stream is treated as stalled
_STREAM_POLL = 0.25               # cancel-check interval while waiting on a stream socket


def _retryable_status(code):
    """Status codes worth retrying (rate limits + transient server errors)."""
    return code in (408, 409, 429) or code >= 500


def _sleep_retry(attempt):
    time.sleep(_RETRY_BACKOFF[min(attempt - 1, len(_RETRY_BACKOFF) - 1)])


class _Cancelled(Exception):
    """Raised when the user cancels mid-stream (propagates to the caller as a stop)."""


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
    last_err = None
    for attempt in range(_MAX_RETRIES + 1):
        if attempt:
            _sleep_retry(attempt)
        if _cancel_flag[0]:
            raise RuntimeError("LLM request cancelled by user")
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                status = int(r.getcode())
                text = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            status = int(e.code)
            text = e.read().decode("utf-8", errors="replace")
            if _retryable_status(status):
                last_err = "HTTP %s" % status
                continue
        except urllib.error.URLError as e:
            last_err = "LLM API unreachable: %s" % e.reason
            continue
        except Exception as e:
            last_err = "LLM request failed: %s" % e
            continue
        try:
            data = json.loads(text)
        except Exception:
            if status >= 500:
                last_err = "API returned non-JSON (HTTP %s)" % status
                continue
            raise RuntimeError("API returned non-JSON (HTTP %s): %s" % (status, _readable_error(status, text)))
        if status >= 400 or "error" in data:
            if _retryable_status(status):
                last_err = "LLM API error %s" % status
                continue
            raise RuntimeError("LLM API error %s: %s" % (status, _readable_error(status, text)))
        if not data.get("choices"):
            raise RuntimeError("LLM API returned no choices")
        return data
    raise RuntimeError("LLM request failed after %d attempts: %s" % (_MAX_RETRIES + 1, last_err))


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
    resp = None
    for attempt in range(_MAX_RETRIES + 1):
        if attempt:
            _sleep_retry(attempt)
        if _cancel_flag[0]:
            raise _Cancelled()
        try:
            resp = urllib.request.urlopen(req, timeout=60)
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if _retryable_status(e.code) and attempt < _MAX_RETRIES:
                continue
            raise RuntimeError("LLM API error %s: %s" % (e.code, _readable_error(e.code, body)))
        except urllib.error.URLError as e:
            if attempt < _MAX_RETRIES:
                continue
            raise RuntimeError("LLM API unreachable: %s" % e.reason)
        except Exception as e:
            if attempt < _MAX_RETRIES:
                continue
            raise RuntimeError("LLM request failed: %s" % e)
    try:
        sock = resp.fileno()
    except Exception:
        sock = None  # non-socket response (tests/fakes): fall back to blocking reads
    buffer = ""
    tool_calls_acc = {}
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    last_byte_at = time.monotonic()
    while True:
        # Poll the socket so Ctrl+C (cancel) is honored within ~_STREAM_POLL
        # even when the server stalls, and fail a dead link after
        # _STREAM_IDLE_LIMIT seconds of silence instead of hanging for the
        # full socket timeout.
        if sock is not None:
            while True:
                if _cancel_flag[0]:
                    resp.close()
                    raise _Cancelled()
                rlist, _, _ = select.select([sock], [], [], _STREAM_POLL)
                if rlist:
                    break
                if time.monotonic() - last_byte_at > _STREAM_IDLE_LIMIT:
                    resp.close()
                    raise RuntimeError("LLM stream stalled (no data for %ds)" % _STREAM_IDLE_LIMIT)
        elif _cancel_flag[0]:
            resp.close()
            raise _Cancelled()
        chunk = resp.read(1024)
        if not chunk:
            break
        last_byte_at = time.monotonic()
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
                    if tcc.get("id") and not acc["id"]:
                        acc["id"] = tcc["id"]
                    # Some OpenAI-compatible gateways omit the tool_call id in the
                    # streamed deltas. Fall back to a stable synthetic id so the
                    # assistant tool_calls and the resulting tool message stay
                    # paired (the API rejects empty/missing tool_call_id).
                    if not acc["id"]:
                        acc["id"] = "call_%d" % idx
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
    # parse the raw body directly so responses still render. Flush the
    # incremental decoder so a trailing partial multi-byte sequence is decoded
    # (errors="replace" turns it into U+FFFD rather than dropping it).
    buffer += decoder.decode("", final=True)
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
    last_err = None
    for attempt in range(_MAX_RETRIES + 1):
        if attempt:
            _sleep_retry(attempt)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace"))
            return [str(m["id"]) for m in (data.get("data") or [])
                    if isinstance(m, dict) and m.get("id")]
        except urllib.error.HTTPError as e:
            if _retryable_status(e.code) and attempt < _MAX_RETRIES:
                last_err = "HTTP %s" % e.code
                continue
            raise RuntimeError("models endpoint: HTTP %s" % e.code)
        except urllib.error.URLError as e:
            last_err = "unreachable: %s" % e.reason
            if attempt >= _MAX_RETRIES:
                raise RuntimeError("models endpoint %s" % last_err)
        except Exception as e:
            last_err = str(e)
            if attempt >= _MAX_RETRIES:
                raise RuntimeError("models endpoint failed: %s" % e)
    raise RuntimeError("models endpoint failed after %d attempts: %s"
                       % (_MAX_RETRIES + 1, last_err))


# ---------------- agent loop ----------------
MAX_STEPS = 25
_cancel_flag = [False]
ON_TOOL = None  # optional hook: ON_TOOL(tool_id, name, args, result, status)


def cancel_agent():
    _cancel_flag[0] = True


def _repair_tool_pairs(history):
    """Heal persisted history so every role:"tool" message has a tool_call_id.

    Older sessions saved by a buggy build dropped tool_call_id from tool
    messages, which makes the OpenAI-compatible API reject the request
    (400: missing field toolcallid). Walk the history and, for any tool
    message missing tool_call_id, attach the id of the preceding assistant
    tool_call (or a synthetic id as a last resort). Returns a new list.
    """
    if not isinstance(history, list):
        return history
    out = []
    pending_ids = []
    for m in history:
        if not isinstance(m, dict):
            out.append(m)
            continue
        m = dict(m)  # don't mutate the caller's dict
        role = m.get("role")
        if role == "assistant":
            tcs = m.get("tool_calls") or []
            # Heal assistant tool_calls that have an empty/missing id (old
            # buggy streaming builds emitted id=""), so the following tool
            # message can be paired to a valid id.
            for i, tc in enumerate(tcs):
                if isinstance(tc, dict) and not tc.get("id"):
                    tc["id"] = "repaired_a%d" % (len(out) * 10 + i)
            ids = [tc.get("id") for tc in tcs if isinstance(tc, dict) and tc.get("id")]
            pending_ids = ids
            out.append(m)
        elif role == "tool":
            if not m.get("tool_call_id"):
                if pending_ids:
                    m["tool_call_id"] = pending_ids.pop(0)
                else:
                    # orphan tool result with no preceding call - synthesize
                    m["tool_call_id"] = "repaired_%d" % len(out)
            elif m["tool_call_id"] == "":
                # previously emitted empty id - keep a stable synthetic one
                m["tool_call_id"] = "repaired_%d" % len(out)
            out.append(m)
        else:
            out.append(m)
    return out


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
    history = _repair_tool_pairs(history)
    for m in history:
        if m.get("role") == "system":
            continue
        # Copy the full message dict so tool messages keep their tool_call_id
        # and assistant messages keep their tool_calls (required by the API).
        if not isinstance(m, dict):
            continue
        messages.append(dict(m))

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


# Hermes-style XML function calling: some models can't use native OpenAI
# tool_calls and instead emit <think>...</think> reasoning plus
#   <tool_call>
#     <function=name>
#       <parameter=key>value</parameter>
#     </function>
#   </tool_call>
# blocks inside the content stream. We hide those blocks from the live
# display (AgentWriter) and, when a turn contains them, execute them like
# real tools and feed the results back (run_agent_stream).
_XML_THINK_RE = re.compile(r"<(?:think|reasoning)\b.*?</(?:think|reasoning)\s*>", re.DOTALL)
_XML_CALL_RE = re.compile(r"<tool_call\b.*?</tool_call\s*>", re.DOTALL)
_XML_BLOCK_RE = re.compile(r"<(?:think|reasoning|tool_call)\b.*?</(?:think|reasoning|tool_call)\s*>", re.DOTALL)
_XML_FUNC_RE = re.compile(r"<function\s*=\s*([^\s>]+)>")
_XML_PARAM_RE = re.compile(r"<parameter\s*=\s*([^\s>]+)>(.*?)</parameter\s*>", re.DOTALL)
# Some reasoning models emit a bare closing </think> with no opening tag in the
# stream (the opener is consumed by the provider's reasoning pipeline). Strip
# those strays too, with their trailing newline, so no raw tags ever render.
_XML_STRAY_CLOSE_RE = re.compile(r"</(?:think|reasoning|tool_call)\s*>[\r\n]*")
_XML_OPEN_TAGS = ("<think", "<reasoning", "<tool_call")


def _clean_segment(s):
    """Remove stray closing tags (</think> etc.) from a plain-text segment."""
    return _XML_STRAY_CLOSE_RE.sub("", s)


def _strip_xml_blocks(text):
    """Remove complete think/reasoning/tool_call blocks from text, returning
    (clean_text, pending_tail). pending_tail parks a block whose opening tag
    may be truncated at the end of the buffer; the next feed() completes it.
    Stray closing tags with no opener are removed too.
    """
    out = []
    pos = 0
    while True:
        m = _XML_BLOCK_RE.search(text, pos)
        if m:
            out.append(_clean_segment(text[pos:m.start()]))
            pos = m.end()
            continue
        sm = _XML_STRAY_CLOSE_RE.search(text, pos)
        if sm:
            out.append(text[pos:sm.start()])
            pos = sm.end()
            continue
        tail = text[pos:]
        # park from the last unclosed opener in the tail (may be truncated)
        best = -1
        for tag in _XML_OPEN_TAGS:
            i = tail.rfind(tag)
            if i > best:
                best = i
        if best >= 0:
            out.append(_clean_segment(tail[:best]))
            return "".join(out), tail[best:]
        lt = tail.rfind("<")
        if lt >= 0:
            frag = tail[lt:]
            if any(tag.startswith(frag) for tag in _XML_OPEN_TAGS):
                out.append(_clean_segment(tail[:lt]))
                return "".join(out), tail[lt:]
        out.append(_clean_segment(tail))
        return "".join(out), ""


def _parse_xml_tool_calls(text):
    """Extract (name, args) pairs from <tool_call> blocks in text."""
    calls = []
    for block in _XML_CALL_RE.findall(text):
        m = _XML_FUNC_RE.search(block)
        if not m:
            continue
        args = {}
        for pm in _XML_PARAM_RE.finditer(block):
            args[pm.group(1).strip()] = html.unescape(pm.group(2)).strip()
        calls.append((m.group(1).strip(), args))
    return calls


def _strip_xml(text):
    """Full-content version: drop all think/tool_call blocks and stray closing
    tags, then tidy spacing."""
    t = _XML_THINK_RE.sub("", text)
    t = _XML_CALL_RE.sub("", t)
    t = _XML_STRAY_CLOSE_RE.sub("", t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def run_agent_stream(history, config):
    """Generator that yields ('text', chunk) or ('tool', tool_info) or ('done', final_dict)."""
    _cancel_flag[0] = False
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    history = _repair_tool_pairs(history)
    for m in history:
        if m.get("role") == "system":
            continue
        # Copy the full message dict so tool messages keep their tool_call_id
        # and assistant messages keep their tool_calls (required by the API).
        if not isinstance(m, dict):
            continue
        messages.append(dict(m))

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
        except _Cancelled:
            yield "done", {"content": "(stopped by user)", "history": messages, "cancelled": True}
            return
        except RuntimeError as e:
            yield "done", {"content": "error: %s" % e, "history": messages, "cancelled": False}
            return

        full_content = "".join(content_parts)
        has_xml = bool(_XML_CALL_RE.search(full_content) or _XML_THINK_RE.search(full_content))
        msg = {"role": "assistant",
               "content": _strip_xml(full_content) if has_xml else full_content}

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
        elif has_xml:
            xml_calls = _parse_xml_tool_calls(full_content)
            messages.append(msg)
            for i, (name, args) in enumerate(xml_calls):
                if _cancel_flag[0]:
                    yield "done", {"content": "(stopped by user)", "history": messages, "cancelled": True}
                    return
                yield "tool_start", {"name": name, "args": args}
                result = dispatch_tool(name, args)
                status = "done" if (isinstance(result, dict) and "error" not in result) else "error"
                yield "tool_end", {"name": name, "args": args, "result": result, "status": status}
                messages.append({"role": "tool", "tool_call_id": "xml_%d" % i, "content": json.dumps(result)})
        else:
            messages.append(msg)
            yield "done", {"content": msg["content"], "history": messages, "cancelled": False}
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

    # skills: list should work
    try:
        skills = tool_skill_list()
        checks.append(("skills_list", skills.get("ok") is True))
    except Exception:
        checks.append(("skills_list", False))

    # command classification: allowlist and risky
    checks.append(("classify_allow_ls", classify_command("ls -la") == "allow"))
    checks.append(("classify_ask_rm", classify_command("rm -rf /") == "ask"))
    checks.append(("classify_ask_subshell", classify_command("cat $(whoami)") == "ask"))

    # file tools: read this file
    try:
        r = tool_file_read(__file__)
        checks.append(("file_read", r.get("ok") is True))
    except Exception:
        checks.append(("file_read", False))

    # file tools: write to temp dir (inside DATA_DIR so the headless default
    # deny-on-outside-write never blocks the check)
    try:
        tmp = os.path.join(DATA_DIR, ".alva_self_test_tmp.txt")
        r = tool_file_write(tmp, "test content")
        if r.get("ok"):
            content = tool_file_read(tmp).get("content", "")
            checks.append(("file_write", content == "test content"))
            os.remove(tmp)
        else:
            checks.append(("file_write", False))
    except Exception:
        checks.append(("file_write", False))

    # feedback + improvement + reflect tools
    try:
        r = tool_feedback("good", "self-test check")
        checks.append(("feedback", r.get("ok") is True))
    except Exception:
        checks.append(("feedback", False))

    try:
        r = tool_improvement_set("test-area", "test action")
        checks.append(("improvement_set", r.get("ok") is True))
    except Exception:
        checks.append(("improvement_set", False))

    try:
        imps = tool_improvement_list()
        checks.append(("improvement_list", imps.get("ok") is True))
    except Exception:
        checks.append(("improvement_list", False))

    try:
        r = tool_reflect()
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
    my_dir = os.path.dirname(os.path.abspath(__file__))
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
        builtin_json = self_test()
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
    """Persist a session's messages and mark it active. Prunes the oldest
    sessions past MAX_SESSIONS so store.json can't grow without bound."""
    sess = sessions_map()
    rec = sess.get(name) or {"name": name, "created": now_iso(), "messages": []}
    rec["messages"] = list(messages)
    rec["updated"] = now_iso()
    sess[name] = rec
    if len(sess) > MAX_SESSIONS:
        others = sorted(((n, sess[n].get("updated") or "") for n in sess if n != name),
                        key=lambda x: x[1])
        for old_name, _ in others[:len(sess) - MAX_SESSIONS]:
            sess.pop(old_name, None)
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
        "Be dense and factual - no preamble, under %d words total. "
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


# ============================================================
#  Terminal UI
# ============================================================
class C:
    RESET = "\x1b[0m"
    BOLD = "\x1b[1m"
    DIM = "\x1b[2m"
    ITALIC = "\x1b[3m"
    STRIKE = "\x1b[9m"
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
    "midnight": {  # default - deep-space blues
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
    "daylight": {  # for bright terminals - dark ink on light
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


def p_err(s):
    print(col(C.BOLD + CUR_SKIN["err"], "error") + "  " + s)


def p_ok(s):
    print(col(CUR_SKIN["ok"], s))


def p_warn(s):
    print(col(C.YELLOW, "  [!]") + "  " + s)


# ---------------- Hermes-style display (clean minimal chat) ----------------
# User turns are a compact gold '●' bullet; tool calls are small indented dim
# lines ('  ▸ name (args)' / '  ✓ name → summary'); agent replies stream with a
# thin bronze left accent bar ('▍ ') and NO box. The only full-width Rich Panel
# left is the startup banner. Hermes' palette is fixed (gold bullet, bronze bar,
# cream text) so the chat reads the same regardless of the /skin palette.


def _term_width():
    try:
        return max(40, shutil.get_terminal_size((80, 24)).columns)
    except Exception:
        return 80


HERMES_ACCENT = "#FFD700"   # gold   - user bullet / banner title
HERMES_BORDER = "#CD7F32"   # bronze - agent reply border (Hermes response_border)
HERMES_TEXT   = "#FFF8DC"   # cream  - agent text
HERMES_DIM    = "#8B8682"   # session border / chips / tool dividers
HERMES_OK     = "#8FBC8F"
HERMES_ERR    = "#CD5C5C"


def _hrgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _fgh(h):
    """Foreground ANSI for a hex color (respects COLOR/NO_COLOR)."""
    return ("\x1b[38;2;%d;%d;%dm" % _hrgb(h)) if COLOR else ""


def _rsth():
    return C.RESET if COLOR else ""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _tool_line(label, color):
    """Compact indented tool line: '  ▸ name (args)' or '  ✓ name → summary'."""
    print(_fgh(color) + "  " + label + _rsth())


def print_user_turn(text, show_ts=False):
    """Compact user scrollback: gold '●' bullet + bold text, no rule."""
    print()
    ts = (" " + datetime.datetime.now().strftime("%H:%M")) if show_ts else ""
    for i, line in enumerate(text.split("\n")):
        if i == 0:
            print(_fgh(HERMES_ACCENT) + "●" + _rsth() + " " + C.BOLD + line + _rsth()
                  + _fgh(HERMES_DIM) + ts + _rsth())
        else:
            print("  " + C.BOLD + line + _rsth())


def render_agent_panel(text, skin=None):
    """Buffered agent reply rendered with the same thin left bar as streaming."""
    sk = skin or CUR_SKIN
    w = AgentWriter(sk, sk["agent"])
    w.feed(text)
    w.close()


_MD_STYLE = {"**": "b", "__": "b", "*": "i", "_": "i", "~~": "s"}


def _md_attr_sgr(stack):
    """Combined SGR attributes for an emphasis style stack (b=bold, i=italic,
    s=strike). Emitting one code like '\\x1b[1;3m' keeps nested styles intact
    instead of a mid-span RESET clobbering the outer style."""
    codes = []
    if "b" in stack:
        codes.append("1")
    if "i" in stack:
        codes.append("3")
    if "s" in stack:
        codes.append("9")
    return "\x1b[" + ";".join(codes) + "m" if codes else ""


def _has_ansi(parts):
    return any("\x1b[" in p for p in parts)


def _md_line(text, skin):
    """Style one inline line of markdown into ANSI (CommonMark emphasis).

    **x** / __x__ -> bold, *x* / _x_ -> italic, ~~x~~ -> strikethrough,
    `x` -> inline code in the skin's code color, \\* escapes a literal marker.
    Emphasis can nest (e.g. **bold *italic* bold**) and the combined SGR is
    emitted so the outer style survives inner resets.

    Returns (rendered, parked). parked is non-empty when the line ends inside
    an unclosed emphasis/code span or on a bare opener; the caller carries it
    into the next feed() so markers split across streamed chunks still render
    as one styled span. When COLOR is off the line is returned untouched, so
    piped output keeps its original markdown characters.
    """
    if not COLOR:
        return text, ""
    n = len(text)
    out = []
    stack = []
    buf = []
    last_open = -1
    i = 0

    def flush():
        if not buf:
            return
        s = "".join(buf)
        if stack:
            out.append(_md_prefix(stack) + s)
        elif _has_ansi(out):
            out.append(C.RESET + s)
        else:
            out.append(s)
        del buf[:]

    def can_open(pos, width):
        nxt = text[pos + width] if pos + width < n else None
        prv = text[pos - 1] if pos > 0 else None
        if nxt is None:
            return True   # end of line - park; the next chunk may continue it
        if nxt in (" ", "\t"):
            return False
        if prv is not None and prv.isalnum():
            return False  # intraword: 6*7, snake_case stay literal
        return True

    def can_close(pos, width):
        prv = text[pos - 1] if pos > 0 else None
        nxt = text[pos + width] if pos + width < n else None
        if prv is None or prv in (" ", "\t"):
            return False
        if nxt is not None and nxt.isalnum():
            return False
        return True

    def park_from(pos):
        tail = "".join(out)
        if _has_ansi(out):
            tail += C.RESET
        return tail, text[pos:]

    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n and text[i + 1] in "*_~`\\":
            buf.append(text[i + 1])
            i += 2
            continue
        if ch == "`":
            j = text.find("`", i + 1)
            if j == -1:
                flush()
                return park_from(i)
            flush()
            out.append(C.RESET + skin["code"] + _md_attr_sgr(stack) + text[i + 1:j])
            out.append(C.RESET)
            i = j + 1
            continue
        if text[i:i + 3] in ("***", "___"):
            if "b" in stack and "i" in stack:
                flush()
                stack[:] = [k for k in stack if k not in ("b", "i")]
            else:
                flush()
                stack.append("b")
                stack.append("i")
                last_open = i
            i += 3
            continue
        two = text[i:i + 2]
        if two in _MD_STYLE:
            kind = _MD_STYLE[two]
            if kind in stack and can_close(i, 2):
                flush()
                while stack and stack[-1] != kind:
                    stack.pop()
                if stack:
                    stack.pop()
                i += 2
                continue
            if can_open(i, 2):
                flush()
                stack.append(kind)
                last_open = i
                i += 2
                continue
            buf.append(two)
            i += 2
            continue
        if ch in "*_":
            if "i" in stack and can_close(i, 1):
                flush()
                while stack and stack[-1] != "i":
                    stack.pop()
                if stack:
                    stack.pop()
                i += 1
                continue
            if can_open(i, 1):
                flush()
                stack.append("i")
                last_open = i
                i += 1
                continue
            buf.append(ch)
            i += 1
            continue
        buf.append(ch)
        i += 1

    if stack:
        return park_from(last_open)
    flush()
    if _has_ansi(out):
        out.append(C.RESET)
    return "".join(out), ""


def _md_prefix(stack):
    """Full SGR prefix (RESET + combined attrs) for a style stack."""
    return C.RESET + _md_attr_sgr(stack)


def style_inline(text, skin):
    """Inline markdown -> ANSI: `code`, **bold**, *italic* / _italic_, ~~strike~~.

    Applies per line (multi-line input is split and re-joined). Returns text
    untouched when colors are off, so piped/NO_COLOR output keeps its original
    markdown characters. The streaming AgentWriter uses _md_line directly so
    markers split across chunks can be parked and merged.
    """
    if "\n" in text:
        return "\n".join(_md_line(ln, skin)[0] for ln in text.split("\n"))
    return _md_line(text, skin)[0]


class AgentWriter:
    """Streams an agent response with a thin left accent bar ('▍ '), no box.

    Text flows immediately, line by line. Code fences (```) switch to dim,
    indented code with a small '─ lang' marker; the language name after the
    opening fence is captured and shown in the marker.
    """

    def __init__(self, skin, color):
        self.skin = skin
        self.color = color  # kept for API compat
        self.in_code = False
        self._code_label = None   # not None while waiting for the code-fence language
        self._pending = ""        # cross-chunk buffer for <think>/<tool_call> blocks
        self._md_pending = ""     # cross-chunk buffer for an unclosed inline marker
        self.started = False
        self.closed = False
        self.at_line_start = True

    def _filter_xml(self, chunk):
        """Hide <think>/<reasoning>/<tool_call> blocks (they're executed by
        run_agent_stream and reported as tool lines, not shown as raw text)."""
        self._pending += chunk
        clean, self._pending = _strip_xml_blocks(self._pending)
        return clean

    # ---- low-level output ----
    def _write(self, s):
        if self.at_line_start:
            self.at_line_start = False
            sys.stdout.write(_fgh(HERMES_BORDER) + "▍ " + _rsth())   # left accent bar
        sys.stdout.write(s)

    def _nl(self):
        sys.stdout.write("\n")
        self.at_line_start = True

    def _code_marker(self, lang):
        """Small dim marker opening a code block: '▍ ─ python'."""
        if not self.at_line_start:
            self._nl()
        self._write(col(self.skin["dim"], "─ " + (lang or "code").strip()))
        self._nl()

    # ---- public API ----
    def feed(self, chunk):
        if not self.started:
            self.started = True
        chunk = self._filter_xml(chunk)
        if not chunk:
            return
        parts = chunk.split("```")
        for i, part in enumerate(parts):
            if i > 0:
                self.in_code = not self.in_code
                if self.in_code:
                    if self._md_pending:
                        # emphasis can't span a code fence - flush as plain text
                        self._write(self._md_pending)
                        self._md_pending = ""
                    self._code_label = ""   # collect the language until the newline
                else:
                    self._flush_code_label()   # closing fence: no literal marker
            if part:
                if self.in_code:
                    self._write_code(part)
                else:
                    self._write_inline(part)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._pending = ""   # drop a trailing unclosed XML block
        if not self.started:
            return
        if self._md_pending:
            self._write(self._md_pending)   # flush an unclosed inline marker as text
            self._md_pending = ""
        if self.in_code:
            self.in_code = False
            self._flush_code_label()
        if not self.at_line_start:
            self._nl()

    def _flush_code_label(self):
        """Code buffered while waiting for a language newline is real code -
        never drop it (single-line blocks have no newline at all). Emits a
        clean dim '─ lang ─' marker instead of a literal ``` fence."""
        if self._code_label is None or not self._code_label.strip():
            self._code_label = None
            return
        lang = self._code_label.strip()
        self._code_label = None
        self._code_marker(lang)
        # content follows in the next feed() part

    # ---- content writers ----
    def _write_code(self, part):
        if self._code_label is not None:
            self._code_label += part
            if "\n" not in self._code_label:
                return
            label, rest = self._code_label.split("\n", 1)
            self._code_label = None
            self._code_marker(label.strip())
            part = rest
        raw = part.split("\n")
        if raw and raw[0] == "" and len(raw) > 1:
            # leading newline: break from previous line if mid-line
            if not self.at_line_start:
                self._nl()
            raw = raw[1:]
        end_nl = part.endswith("\n")
        if end_nl:
            raw = raw[:-1]   # drop trailing empty (real line break handled below)
        wrote = False
        for idx, piece in enumerate(raw):
            if idx > 0:
                self._nl()
            if piece:
                self._write(col(self.skin["code"], "  " + piece))   # indent code
                wrote = True
        if end_nl and wrote:
            self._nl()

    def _decorate(self, piece):
        """Line-level markdown: headings, horizontal rules, task checkboxes,
        bullets and blockquotes.

        Returns (styled_prefix_or_None, remainder_to_style). With colors off
        the line is returned untouched so raw markdown survives piped output.
        """
        if not COLOR:
            return None, piece
        s = self.skin
        hm = re.match(r"^#{1,6}\s+(.*)$", piece)
        if hm:  # '## Heading' -> bold accent heading
            return col(C.BOLD + s["agent"], hm.group(1)), ""
        if re.match(r"^\s*[-*_][-*_\s]{2,}$", piece):  # '---' / '***' / '___'
            return col(s["dim"], "─" * 20), ""
        cm = re.match(r"^\s*[-*]\s+\[([ xX])\]\s+(.*)$", piece)
        if cm:  # '- [x] done' / '- [ ] todo'
            mark = col(s["ok"], "✓ ") if cm.group(1) in ("x", "X") else col(s["dim"], "☐ ")
            return mark, cm.group(2)
        bm = re.match(r"^\s*[-*]\s+(.*)$", piece)
        if bm:  # '- item' -> accent bullet
            return col(s["accent"], "• "), bm.group(1)
        qm = re.match(r"^\s*>\s?(.*)$", piece)
        if qm:  # '> quote' -> bordered bar
            return col(s["border"], "│ "), qm.group(1)
        return None, piece

    def _write_inline(self, part):
        raw = part.split("\n")
        if raw and raw[0] == "" and len(raw) > 1:
            # leading newline: break from previous line if mid-line
            if not self.at_line_start:
                self._nl()
            raw = raw[1:]
        end_nl = part.endswith("\n")
        if end_nl:
            raw = raw[:-1]   # drop trailing empty (real line break handled below)
        wrote = False
        for idx, piece in enumerate(raw):
            if idx > 0:
                self._nl()
            if self._md_pending:
                piece = self._md_pending + piece   # finish the parked inline span
                self._md_pending = ""
            if piece:
                prefix, rest = self._decorate(piece)
                if prefix is not None:
                    self._write(prefix)
                rendered, parked = _md_line(rest, self.skin)
                if parked:
                    self._md_pending = parked
                if rendered:
                    self._write(rendered)
                if prefix is not None or rendered:
                    wrote = True
        if end_nl and wrote:
            self._nl()


def fmt_args(args):
    return ", ".join("%s=%r" % (k, v) for k, v in (args or {}).items())


def tool_summary(result):
    if not isinstance(result, dict):
        return str(result)[:80]
    if "result" in result:
        return str(result["result"])[:80]
    if "status" in result:
        return "HTTP %s | %s chars" % (result.get("status"), result.get("chars", "?"))
    if "exit" in result:
        snippet = (result.get("stdout") or result.get("stderr") or "").strip()
        return "exit %s%s" % (result.get("exit"), " | " + snippet[:60] if snippet else "")
    if "entries" in result:
        return "%d entries" % result.get("count", len(result.get("entries", [])))
    if "skills" in result:
        return "%d skills" % len(result.get("skills", []))
    if "path" in result and "content" in result:
        return "%s (%s chars)" % (result.get("path"), result.get("chars", 0))
    if "path" in result and result.get("ok") is True:
        return "%s [OK]" % result.get("path")
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

    The verb (message) can change live - 'thinking', 'streaming', 'running
    tools' - like the Hermes TUI's customizable busy verbs. stop() wakes the
    thread immediately (Event) so it can be called between every streamed
    chunk without stuttering, and disable() permanently silences it once real
    output starts streaming (otherwise the \r frames collide with the text).
    """

    def __init__(self, msg="thinking"):
        self.msg = msg
        self._stop = True
        self._dead = False
        self._wake = threading.Event()
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
                if self._dead or self._stop:
                    return
                msg = self.msg
            sys.stderr.write("\r" + msg + " " + frames[i % 4])
            sys.stderr.flush()  # \r frames must land now, not sit in the buffer
            i += 1
            self._wake.wait(0.12)
            self._wake.clear()

    def start(self):
        with self._lock:
            if self._dead or not self._stop:
                return
            self._stop = False
        self._wake.set()
        if self._t is None or not self._t.is_alive():
            self._t = threading.Thread(target=self._run, daemon=True)
            self._t.start()

    def stop(self):
        with self._lock:
            self._stop = True
        self._wake.set()
        if self._t is not None:
            self._t.join(timeout=0.5)
        if not self._dead:
            sys.stderr.write("\r" + " " * 30 + "\r")
            sys.stderr.flush()

    def disable(self):
        """Permanently stop drawing frames and clear the line (called once
        real output starts streaming, so \r frames can't collide with text)."""
        self.stop()          # clears the line while still "alive"
        with self._lock:
            self._dead = True


_UI = {"spinner": None}


def tool_open(name, args):
    """Compact tool line: '  ▸ name (args)' - dim, no full-width rule."""
    a = fmt_args(args)
    label = "▸ " + name + ((" (" + a + ")") if a else "")
    _tool_line(label, HERMES_DIM)


def tool_close(name, status, result):
    """Close line of a tool block: '  ✓ name → summary'."""
    mark = ("✓ " if status == "done" else "✗ ")
    label = mark + name
    if result is not None:
        s = tool_summary(result).replace("\n", " ").replace("\r", " ").strip()
        s = " ".join(s.split())   # collapse runs of whitespace onto one line
        if s:
            label += " → " + s
    _tool_line(label, HERMES_OK if status == "done" else HERMES_ERR)


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
      tools    - number of tool calls this turn
      elapsed  - wall-clock seconds for the turn
      streamed - whether any text was printed (so the caller can avoid
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
                sp.disable()   # kill the spinner BEFORE text lands (no \r collision)
                content_parts.append(evt_data)
                writer.feed(evt_data)
            elif evt_type == "tool_start":
                sp.disable()
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
    # a pure safety net now - the context meter + auto-compress manage the real
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
    was_running = sp is not None
    if sp:
        sp.stop()
    print()
    print(col(CUR_SKIN["err"], "  [!] permission needed") + "  " + desc)
    try:
        v = input("    allow? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        if was_running and sp:
            sp.start()
        return False
    if was_running and sp:
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
    "/tools", "/todos", "/todo", "/memory", "/skills", "/skill",
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
    print("    /tools                 list the agent's tools")
    print("    /todos                 show the to-do list")
    print("    /todo <text>           add a task")
    print("    /todo done <i>         toggle task i      /todo rm <i>   remove task i")
    print("    /todo clear            empty the list")
    print("    /memory                show saved memory facts")
    print("    /skills                list saved skills (grouped by category)")
    print("    /skill rm <name>       delete a skill (name or category/name)")
    print("    /skill category [n]    list categories / show skills in category n")
    print("    /install_skill <path>  install a skill from a .md file")
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


def cmd_skills():
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
            p_ok("installed skill '%s' [OK]" % name)
        else:
            p_err("failed to save skill: %s" % r.get("error", "unknown error"))
    except Exception as e:
        p_err("failed to install skill: %s" % e)
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
def new_session_name():
    """Name for a fresh, not-yet-titled session (auto-titled from the first message)."""
    return "sess-" + secrets.token_hex(2)


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


# Block wordmark generated by `pyfiglet -f block "ALVAAGENT"` (tool output,
# not hand-authored) so it reads like the Hermes HERMES_AGENT_LOGO banner.
ALVA_WORDMARK = (
    "  _|_|    _|      _|      _|    _|_|      _|_|      _|_|_|  _|_|_|_|  \n"
    "_|    _|  _|      _|      _|  _|    _|  _|    _|  _|        _|        \n"
    "_|_|_|_|  _|      _|      _|  _|_|_|_|  _|_|_|_|  _|  _|_|  _|_|_|    \n"
    "_|    _|  _|        _|  _|    _|    _|  _|    _|  _|    _|  _|        \n"
    "_|    _|  _|_|_|_|    _|      _|    _|  _|    _|    _|_|_|  _|_|_|_|  \n"
    "                        \n"
    "_|      _|  _|_|_|_|_|  \n"
    "_|_|    _|      _|      \n"
    "_|  _|  _|      _|      \n"
    "_|    _|_|      _|      \n"
    "_|      _|      _|      \n"
)

# Toolset grouping for the banner grid (mirrors Hermes' per-category tool panel).
TOOLSETS = {
    "shell":   ["run_command"],
    "files":   ["file_read", "file_write", "file_edit", "file_list", "file_search"],
    "skills":  ["skill_list", "skill_read", "skill_save"],
    "memory":  ["memory_save", "memory_recall", "memory_search", "memory_list"],
    "todos":   ["todo_add", "todo_list", "todo_toggle", "todo_remove"],
    "web":     ["web_fetch"],
    "system":  ["calculator", "get_time"],
}


def _markup_safe(s):
    """Strip Rich markup tag characters from user-controlled strings before
    they're interpolated into banner cells (a `[` in a provider/model/skill
    name would otherwise crash Rich's markup parser)."""
    return str(s).replace("[", "").replace("]", "")


def _banner_tools_lines():
    """Hermes-style 'Available Tools' grid: 'toolset: tool, tool, ...' rows.

    Returns Rich-markup strings (Hermes' own convention inside Panels).
    """
    lines = ["[bold %s]Available Tools[/]" % HERMES_ACCENT]
    for ts, names in TOOLSETS.items():
        lines.append("[dim %s]%s:[/] [bold %s]%s[/]"
                     % (HERMES_DIM, ts, HERMES_TEXT, ", ".join(names)))
    return lines


def _banner_skills_lines():
    """Hermes-style 'Available Skills' grid: skills grouped by category."""
    lines = ["", "[bold %s]Available Skills[/]" % HERMES_ACCENT]
    try:
        skills = tool_skill_list().get("skills") or []
    except Exception:
        skills = []
    if not skills:
        lines.append("[dim %s]saved: (none yet - ask the agent to save one)[/]" % HERMES_DIM)
        return lines
    # group by category
    by_cat = {}
    for s in skills:
        cat = s.get("category") or "(flat)"
        by_cat.setdefault(cat, []).append(s)
    disp_name = lambda c: "flat" if c == "(flat)" else c
    for cat in sorted(by_cat):
        names = [_markup_safe(s["name"]) for s in by_cat[cat]]
        lines.append("[dim %s]%s:[/] [bold %s]%s[/]"
                     % (HERMES_DIM, disp_name(cat), HERMES_TEXT, ", ".join(names)))
    return lines


def banner(state):
    """Hermes-banner style: block wordmark + bronze panel with tools/skills grid.

    Mirrors hermes_cli/banner.build_welcome_banner: a large block wordmark on
    top, then a bordered panel whose LEFT is model/provider/skin meta and RIGHT
    is an 'Available Tools' / 'Available Skills' grid, laid out with a real Rich
    two-column grid (exactly how Hermes aligns its banner). Colors use the fixed
    Hermes palette (gold accent, bronze border) regardless of /skin. The wordmark
    is pyfiglet output; grid cells use Rich markup tags (Hermes' own convention).
    NOTE: Table is imported lazily (inside try/except) so the banner still
    renders when rich is absent — the module-level fallback shim covers
    Console/Panel only.
    """
    cfg = active_cfg(state)
    _CON.print()  # top spacer like Hermes
    # Hermes only prints its big wordmark when the terminal is wide enough; on
    # narrow terminals (phones) it would wrap ugly, so we mirror that gate.
    # NOTE: use plain print() + raw ANSI here (not _CON.print) — the rest of the
    # TUI renders color via print()+_fgh(), and Rich's Console leaks bare escapes.
    term_w = _term_width()
    if COLOR and term_w >= 72:
        for ln in ALVA_WORDMARK.split("\n"):
            if ln.strip():
                print(_fgh(HERMES_ACCENT) + ln + _rsth())
    elif COLOR:
        print(_fgh(HERMES_ACCENT) + C.BOLD + "ALVAAGENT" + _rsth())
    else:
        print("ALVAAGENT")
    print()

    model_short = _markup_safe(cfg.get("model") or "?")
    if "/" in model_short:
        model_short = model_short.split("/")[-1]
    ctx = _fmt_k(context_window_for(cfg))
    # Left meta column (Hermes banner left side): model · context · provider.
    left_lines = [
        "",
        "[bold %s]%s[/]  [dim %s]·[/] [dim %s]%s context[/]"
        % (HERMES_ACCENT, model_short, HERMES_DIM, HERMES_DIM, ctx),
        "[dim %s]skin[/] %s" % (HERMES_DIM, state.get("skin") or DEFAULT_SKIN),
        "[dim %s]provider[/] %s" % (HERMES_DIM, _markup_safe(state["active"])),
        "[dim %s]config/store:[/] %s" % (HERMES_DIM, _markup_safe(DATA_DIR)),
    ]
    right_lines = _banner_tools_lines() + _banner_skills_lines()
    right_lines.append("")
    right_lines.append("[dim %s]%d tools · v%s · you are here · /help for commands[/]"
                       % (HERMES_DIM, len(TOOLS), ALVA_VERSION))

    try:
        from rich.table import Table
        grid = Table.grid(padding=(0, 2))
        grid.add_column("left", justify="left")
        grid.add_column("right", justify="left")
        for i in range(max(len(left_lines), len(right_lines))):
            l = left_lines[i] if i < len(left_lines) else ""
            r = right_lines[i] if i < len(right_lines) else ""
            grid.add_row(l, r)
        panel_body = grid
    except Exception:
        # Rich Table unavailable — fall back to a single stacked block.
        panel_body = "\n".join(left_lines + [""] + right_lines)

    _CON.print(Panel(
        panel_body,
        title="[bold %s]%s v%s[/]" % (HERMES_ACCENT, "⚕ alvaagent", ALVA_VERSION),
        border_style=HERMES_BORDER,
        padding=(0, 2),
    ))
    print("  " + col(C.DIM, "type a message | /help lists commands | Tab completes /commands"))
    print()
    if not cfg.get("api_key"):
        p_info("no API key set for '%s' - run /provider %s or /config" % (state["active"], state["active"]))


def render_status_bar(state, session, elapsed, tools, history):
    """Render a one-line status footer after each agent turn (Hermes-style).

    Uses normal print flow - no raw ANSI cursor jumps, which misalign on
    Termux (no reliable terminal height). Prints a dim, boxed-style line.
    """
    cfg = active_cfg(state)
    skin = CUR_SKIN
    tokens, window = context_usage(history, cfg)
    pct = tokens * 100 // window if window else 0
    ctx_col = skin["ok"] if pct < 60 else (C.YELLOW if pct < 85 else skin["err"])
    parts = [
        col(skin["dim"], session[:16]),
        col(skin["dim"], state["active"] + "/" + (cfg.get("model") or "?")),
        col(ctx_col, "ctx %d%%" % pct),
        col(skin["dim"], "%.1fs" % elapsed),
        col(skin["dim"], "%d tool calls" % (tools or 0)),
    ]
    # Hermes-style footer: dim '│' prefix + space-separated chips.
    print(col(C.DIM, "  " + "│".join([""] + parts)))


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
    if not res.get("streamed"):
        content = (res.get("content") or "").strip()
        if content:
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
    set_active_skin(state)
    # resume the last active session (conversations persist across restarts)
    session = _store_get(ACTIVE_SESSION_KEY) or "default"
    history = load_session(session)
    # last completed turn, for /redo (session-scoped so it can't leak across
    # a /session switch)
    _last_turn = {"session": None, "text": None, "pre": None}
    while True:
        try:
            prompt = col(CUR_SKIN["accent"], "> ") if COLOR else "> "
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
                cmd_skills()
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
    global ON_TOOL, ON_PERMISSION
    _load_store()
    setup_completion()
    ON_TOOL = on_tool        # live tool-progress blocks
    ON_PERMISSION = ask_permission  # interactive y/N for risky actions
    state = load_state()
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
