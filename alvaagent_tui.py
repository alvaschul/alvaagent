#!/usr/bin/env python3
"""Compatibility shim — the real app lives in the alvaagent package.

Keeps the historical entry points working unchanged:
    python3 alvaagent_tui.py   (start.sh and the `alvaagent` launcher)
    import alvaagent_tui       (old docs / external scripts)
"""
from alvaagent.repl import (  # noqa: E402,F401
    _slash_complete, main, repl, save_completion_history, send_message,
    setup_completion,
)

if __name__ == "__main__":
    main()
