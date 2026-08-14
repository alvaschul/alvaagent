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
# _raw_fetch, ...). The test suite monkeypatches them through `pa.<name> = ...`.
# As the mechanical split moves readers into alvaagent.* submodules, a write to
# the facade must land in every loaded module that exposes the name (the
# def-owner plus any module that imported it by name). Reads forward to
# alvaagent_tui, which re-imports the full surface until Task 13.
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
            return
        super().__setattr__(name, value)
        setattr(_Facade._tui, name, value)
        for _mname, _mod in _sys.modules.items():
            if _mname.startswith("alvaagent.") and hasattr(_mod, name):
                setattr(_mod, name, value)


_Facade._tui = _sys.modules["alvaagent_tui"]
_sys.modules[__name__].__class__ = _Facade
