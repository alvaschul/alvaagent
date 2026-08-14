"""alvaagent — on-device AI agent harness for Termux (stdlib-only).

Package layout replaces the original single-file alvaagent_tui.py. This
facade re-exports the old flat API so `import alvaagent as pa` behaves like
the original module.
"""
from alvaagent_tui import *  # noqa: F401,F403
from alvaagent_tui import (  # noqa: F401
    _store, _TOOLS_MODE, _APPROVED_SET, _cancel_flag,
    ON_PERMISSION, ON_TOOL, _UI, CUR_SKIN, COLOR,
    _atomic_write, _find_session, _fmt_k, _looks_like_html, _md_line,
    _normalize_state, _parse_xml_tool_calls, _permission, _raw_fetch,
    _read_trace, _save_store, _sleep_retry, _store_get, _strip_xml,
    _trace_count, _unique_session_name, signal, subprocess, urllib, time, yaml,
)

# The single file's functions read module globals (ON_PERMISSION, _TOOLS_MODE,
# _raw_fetch, ...). The test suite monkeypatches them through `pa.<name> = ...`,
# so writes to the facade must land in alvaagent_tui's namespace too.
import sys as _sys, types as _types


class _Facade(_types.ModuleType):
    _tui = None

    def __getattribute__(self, name):
        if name.startswith("__") and name.endswith("__"):
            return super().__getattribute__(name)
        return getattr(_Facade._tui, name)

    def __setattr__(self, name, value):
        if name.startswith("__") and name.endswith("__"):
            super().__setattr__(name, value)
        else:
            setattr(_Facade._tui, name, value)
            super().__setattr__(name, value)


_Facade._tui = _sys.modules["alvaagent_tui"]
_sys.modules[__name__].__class__ = _Facade
