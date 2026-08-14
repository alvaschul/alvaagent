"""Atomic store.json persistence (todos/memory/sessions) + namespaced keys —
leaf module (imports config + util only). Extracted from alvaagent_tui.py
(Task 4)."""
import json
import os

from alvaagent.config import _LEGACY_DIRS
from alvaagent.util import _env

# ---------------- persistence (JSON file instead of localStorage) ----------------
# No module-global `_store` anymore: the store lives on the Runtime (`rt.store`).
# All reads/writes go through rt (the file path derives from `rt.data_dir`), so
# per-test runtimes are fully isolated.


def _migrate_legacy_dir(data_dir):
    """One-time copy of data from the old .pocket_agent folders (if any)."""
    if _env("ALVA_DATA_DIR", "POCKET_DATA_DIR"):
        return  # explicit override: don't second-guess
    if os.path.exists(os.path.join(data_dir, "store.json")) or \
            os.path.exists(os.path.join(data_dir, "config.json")):
        return  # new-brand data already present
    for old in _LEGACY_DIRS:
        if os.path.isdir(old) and any(
                os.path.exists(os.path.join(old, f)) for f in ("store.json", "config.json")):
            try:
                os.makedirs(data_dir, exist_ok=True)
                for name in os.listdir(old):
                    src, dst = os.path.join(old, name), os.path.join(data_dir, name)
                    if os.path.isfile(src) and not os.path.exists(dst):
                        with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
                            fdst.write(fsrc.read())
            except Exception:
                pass
            break


def load(rt):
    """Load store.json into rt.store (mutates rt.store in place)."""
    _migrate_legacy_dir(rt.data_dir)
    try:
        with open(os.path.join(rt.data_dir, "store.json")) as f:
            data = json.load(f)
    except Exception:
        data = {}
    rt.store.clear()
    rt.store.update(data)
    # rename keys saved under the old brand, once, in place
    if any(k.startswith("pocket_agent.") for k in rt.store):
        renamed = {("alvaagent." + k[len("pocket_agent."):]) if k.startswith("pocket_agent.") else k: v
                   for k, v in rt.store.items()}
        rt.store.clear()
        rt.store.update(renamed)
        save(rt)


def save(rt):
    """Atomically persist rt.store: write to a temp file, then rename into
    place. A kill/crash mid-write can never leave a truncated store.json."""
    try:
        import tempfile
        os.makedirs(rt.data_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=rt.data_dir, prefix=".store.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(rt.store, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, os.path.join(rt.data_dir, "store.json"))  # atomic on POSIX
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    except Exception:
        pass


def get(rt, key, default=None):
    return rt.store.get(key, default)


def set(rt, key, value):
    rt.store[key] = value
    save(rt)


TODO_KEY = "alvaagent.todos"
MEM_PREFIX = "alvaagent.mem."
FEEDBACK_KEY = "alvaagent.feedback"
IMPROVEMENT_KEY = "alvaagent.improvements"
HISTORY_KEY = "alvaagent.history"
SESSION_KEY = "alvaagent.sessions"
ACTIVE_SESSION_KEY = "alvaagent.active_session"
MAX_SESSIONS = 30  # oldest sessions are pruned past this many (keeps store.json small)
