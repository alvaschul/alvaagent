"""In-app scrollback + input reader for the alt-screen TUI.

The alternate screen buffer has no terminal scrollback, so the app captures
what it printed and re-renders a conversation view on swipe/wheel.
"""

import sys

WHEEL_UP = 64
WHEEL_DOWN = 65


class StreamTee:
    """Wraps a stream, forwarding writes and recording completed lines.

    stdout in this app carries no '\r' redraws or cursor-position escapes
    (the spinner writes those to stderr), so splitting on '\n' is faithful.
    """

    def __init__(self, stream=None, max_lines=5000):
        self._orig = stream if stream is not None else sys.stdout
        self._max_lines = max_lines
        self._lines = []
        self._partial = ""

    def install(self):
        sys.stdout = self

    def uninstall(self):
        sys.stdout = self._orig

    def write(self, s):
        if isinstance(s, bytes):
            s = s.decode("utf-8", "replace")
        self._orig.write(s)
        self._capture(s)

    def _capture(self, s):
        parts = s.split("\n")
        for part in parts[:-1]:
            line = self._partial + part
            self._partial = ""
            if not line.startswith("\x1b[?"):
                self._lines.append(line)
                if len(self._lines) > self._max_lines:
                    del self._lines[:len(self._lines) - self._max_lines]
        self._partial = self._partial + parts[-1]

    def flush(self):
        self._orig.flush()

    def captured_lines(self):
        return list(self._lines)

    def partial_line(self):
        return self._partial

    def restore(self):
        """Clear and replay the captured content (byte-faithful)."""
        self._orig.write("\x1b[2J\x1b[H")
        for line in self._lines:
            self._orig.write(line + "\n")
        self._orig.flush()


MOUSE_ENABLE = "\x1b[?1000h\x1b[?1002h\x1b[?1006h"
MOUSE_DISABLE = "\x1b[?1000l\x1b[?1002l\x1b[?1006l"


def parse_mouse(esc):
    """Parse an SGR mouse sequence into a dict, or return None."""
    if isinstance(esc, str):
        esc = esc.encode("utf-8")
    if not esc.startswith(b"\x1b[<") or esc[-1:] not in (b"M", b"m"):
        return None
    body = esc[3:-1]
    if not body or not all(c in b"0123456789;" for c in body):
        return None
    parts = [int(p) for p in body.split(b";")]
    if len(parts) != 3:
        return None
    button, col, row = parts
    return {"button": button, "col": col, "row": row,
            "kind": "press" if esc[-1:] == b"M" else "release"}


def is_wheel_up(ev):
    return bool(ev) and ev["button"] == WHEEL_UP


def is_wheel_down(ev):
    return bool(ev) and ev["button"] == WHEEL_DOWN


import math
import shutil

from alvaagent.commands import render_conversation


def wrap_to(text, width):
    """Word-wrap text to width; long words are hard-split."""
    if width < 1:
        width = 1
    words = text.split()
    if not words:
        return []
    out = []
    cur = ""
    for w in words:
        while len(w) > width:
            if cur:
                out.append(cur)
                cur = ""
            out.append(w[:width])
            w = w[width:]
        cand = (cur + " " + w).strip() if cur else w
        if len(cand) > width:
            out.append(cur)
            cur = w
        else:
            cur = cand
    if cur:
        out.append(cur)
    return out


def lines_for_history(history, columns):
    return wrap_to(render_conversation(history), columns)


class ScrollView:
    """Paged view of the conversation rendered inside the alt screen."""

    def __init__(self, history, columns=None, rows=None):
        size = shutil.get_terminal_size()
        self.columns = columns if columns is not None else size.columns
        self.rows = rows if rows is not None else size.lines
        self.lines = lines_for_history(history, self.columns)

    def window(self):
        return max(1, self.rows - 2)

    def total_lines(self):
        return len(self.lines)

    def page_count(self):
        return max(1, int(math.ceil(len(self.lines) / self.window())))

    def page_text(self, page):
        w = self.window()
        start = page * w
        chunk = self.lines[start:start + w]
        chunk += [""] * (w - len(chunk))
        footer = "  ◀ older · page %d/%d · ▼ newer · ⏎ return  " % (page + 1, self.page_count())
        return "\n".join(chunk + [footer])
