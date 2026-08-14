"""JSON-lines agent trace (trace.log) — leaf module (imports config only)."""
import json
import os

from alvaagent.config import TRACE_PATH

_TRACE_MAX_LINES = 2000      # cap for trace.log
_TRACE_MAX_BYTES = 1_000_000  # cap before trace.log is trimmed back


def _trace(entry):
    """Append one JSON line to trace.log.

    Best-effort and never raises; the log is capped (line + byte limits) so it
    can't grow without bound on a phone.
    """
    try:
        import datetime as _dt
        rec = dict(entry)
        rec.setdefault("ts", _dt.datetime.now().isoformat(timespec="seconds"))
        line = json.dumps(rec, ensure_ascii=False)
        with open(TRACE_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if os.path.getsize(TRACE_PATH) > _TRACE_MAX_BYTES:
            with open(TRACE_PATH, encoding="utf-8") as f:
                lines = f.readlines()
            with open(TRACE_PATH, "w", encoding="utf-8") as f:
                f.writelines(lines[-_TRACE_MAX_LINES:])
    except Exception:
        pass


def _read_trace(limit=15):
    """Return the last `limit` non-empty trace.log lines, oldest first."""
    try:
        with open(TRACE_PATH, encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
    except Exception:
        return []
    return lines[-limit:]


def _trace_count():
    """Count total lines in trace.log."""
    try:
        with open(TRACE_PATH, encoding="utf-8") as f:
            return sum(1 for ln in f if ln.strip())
    except Exception:
        return 0
