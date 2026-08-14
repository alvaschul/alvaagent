"""JSON-lines agent trace (trace.log) — leaf module (imports config only)."""
import json
import os

_TRACE_MAX_LINES = 2000      # cap for trace.log
_TRACE_MAX_BYTES = 1_000_000  # cap before trace.log is trimmed back


def _log_path(rt):
    """trace.log lives in the runtime's data dir (per-test isolation)."""
    return os.path.join(rt.data_dir, "trace.log")


def trace(rt, entry):
    """Append one JSON line to trace.log.

    Best-effort and never raises; the log is capped (line + byte limits) so it
    can't grow without bound on a phone.
    """
    try:
        import datetime as _dt
        rec = dict(entry)
        rec.setdefault("ts", _dt.datetime.now().isoformat(timespec="seconds"))
        line = json.dumps(rec, ensure_ascii=False)
        path = _log_path(rt)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if os.path.getsize(path) > _TRACE_MAX_BYTES:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines[-_TRACE_MAX_LINES:])
    except Exception:
        pass


def read_trace(rt, limit=15):
    """Return the last `limit` non-empty trace.log lines, oldest first."""
    try:
        with open(_log_path(rt), encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
    except Exception:
        return []
    return lines[-limit:]


def trace_count(rt):
    """Count total lines in trace.log."""
    try:
        with open(_log_path(rt), encoding="utf-8") as f:
            return sum(1 for ln in f if ln.strip())
    except Exception:
        return 0
