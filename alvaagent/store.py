"""Atomic store.json persistence (todos/memory/sessions) + namespaced keys —
leaf module (imports config + util only). Extracted from alvaagent_tui.py
(Task 4)."""
import json
import os

from alvaagent.config import STORE_PATH, CONFIG_PATH, DATA_DIR, _LEGACY_DIRS
from alvaagent.util import _env

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
