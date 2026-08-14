"""Paths, profiles, provider defaults and tool-mode validation — leaf module
(imports util only). Extracted from alvaagent_tui.py (Task 3)."""
import json
import os

from alvaagent.context import Runtime
from alvaagent.util import _env


def data_dir():
    """Data folder: ALVA_DATA_DIR overrides (POCKET_DATA_DIR is also accepted
    for backwards compatibility); otherwise <repo root>/.alvaagent."""
    return (_env("ALVA_DATA_DIR", "POCKET_DATA_DIR")
            or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".alvaagent"))


# Data lives next to the repo (survives distro reinstalls on Termux proot).
# Module constant: remaining alvaagent_tui.py code reads DATA_DIR at 10+ sites.
DATA_DIR = data_dir()

# old default locations, checked once for a one-time data migration
_LEGACY_DIRS = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".pocket_agent"),
    os.path.join(os.path.expanduser("~"), ".pocket_agent"),
]
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
STORE_PATH = os.path.join(DATA_DIR, "store.json")
HISTORY_PATH = os.path.join(DATA_DIR, "cmd_history.txt")
TRACE_PATH = os.path.join(DATA_DIR, "trace.log")

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
                return {"active": active, "profiles": profiles, "skin": _skin_of(raw),
                        "tool_mode": _tool_mode_of(raw)}
        # legacy flat config: {"provider": ..., "base_url": ..., ...}
        if raw.get("provider") or raw.get("base_url"):
            name = raw.get("provider") or "default"
            if name not in PROVIDERS:
                name = "default"
            prof = {k: raw.get(k, DEFAULT_CFG[k]) for k in
                    ("base_url", "api_key", "model", "temperature",
                     "context_window", "auto_compress")}
            return {"active": name, "profiles": {name: prof}, "skin": _skin_of(raw),
                    "tool_mode": _tool_mode_of(raw)}
    # first run: a neutral, keyless profile
    return {"active": "default", "profiles": {"default": dict(FIRST_RUN_CFG)},
            "skin": DEFAULT_SKIN, "tool_mode": "core"}


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
    """Atomically persist config: temp file + fsync + rename (see store.save).

    Accepts a Runtime (persists rt.cfg) or a plain state dict. Strict rt-first
    lands in Task 15 once every consumer threads rt."""
    cfg = state.cfg if isinstance(state, Runtime) else state
    try:
        import tempfile
        os.makedirs(DATA_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".config.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
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
    """Active profile: rt-first (rt.active_cfg) or a plain state dict (Phase A
    bridge for the still-flat tui/commands/repl callers)."""
    if isinstance(state, Runtime):
        return state.active_cfg
    return state["profiles"][state["active"]]


TOOL_MODES = ("core", "full")


def _tool_mode_of(raw):
    """Persisted tool mode from raw state (validated), else 'core'."""
    if isinstance(raw, dict) and raw.get("tool_mode") in TOOL_MODES:
        return raw["tool_mode"]
    return "core"
