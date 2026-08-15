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

    def write_direct(self, s):
        """Write to the underlying stream without capturing (scroll views)."""
        if isinstance(s, bytes):
            s = s.decode("utf-8", "replace")
        self._orig.write(s)
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
    """Parse an SGR or X10 mouse sequence into a dict, or return None."""
    if isinstance(esc, str):
        esc = esc.encode("utf-8")
    if esc.startswith(b"\x1b[M") and len(esc) == 6:
        button, col, row = esc[3] - 32, esc[4] - 32, esc[5] - 32
        return {"button": button, "col": col, "row": row,
                "kind": "release" if button == 3 else "press"}
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


import os
import readline
import termios
import tty


def enter_raw(fd):
    attrs = termios.tcgetattr(fd)
    tty.setraw(fd)
    return attrs


def leave_raw(fd, attrs):
    if attrs is not None:
        termios.tcsetattr(fd, termios.TCSADRAIN, attrs)


class LineReader:
    """Raw-mode line editor with SGR mouse scroll support.

    Replaces input() at the main chat prompt. Raises EOFError on Ctrl+D
    (empty buffer) and KeyboardInterrupt on Ctrl+C, like input().
    """

    def __init__(self, tee, history_list, prompt="> "):
        self._tee = tee
        self._prompt = prompt
        self._hist = list(reversed(history_list))
        self._hist_i = -1
        self._draft = ""
        self._scroll_handler = None
        self._columns = 0
        try:
            self._columns = shutil.get_terminal_size().columns
        except Exception:
            self._columns = 80

    def on_scroll(self, handler):
        self._scroll_handler = handler

    def _emit(self, s):
        self._tee.write(s)
        try:
            self._tee.flush()
        except Exception:
            pass

    def _redraw(self, buf):
        self._emit("\r\x1b[K" + self._prompt + buf)

    def _push_history(self, line):
        self._hist.insert(0, line)
        self._hist_i = -1
        try:
            readline.add_history(line)
        except Exception:
            pass

    def _read_byte(self):
        b = os.read(0, 1)
        return b

    def _collect_escape(self):
        """Gather a complete escape sequence; return bytes or None.

        Handles CSI (\\x1b[...final), X10 mouse (\\x1b[M + 3 bytes), SS3
        (\\x1bO + 1 byte) and bare \\x1b+char so no trailing bytes can leak
        back into the printable-echo path.
        """
        seq = b"\x1b"
        b = self._read_byte()
        if not b:
            return None
        seq += b
        if b == b"O":                       # SS3 (application keypad)
            nb = self._read_byte()
            if nb:
                seq += nb
            return seq
        if b != b"[":
            return seq                      # Alt+key or bare escape
        nxt = self._read_byte()             # first byte after CSI '['
        if not nxt:
            return None
        seq += nxt
        if nxt == b"M":                     # X10 mouse: 3 more bytes
            for _ in range(3):
                nb = self._read_byte()
                if not nb:
                    break
                seq += nb
            return seq
        for _ in range(30):                 # CSI until the final byte
            nb = self._read_byte()
            if not nb:
                return None
            seq += nb
            if 0x40 <= nb[0] <= 0x7e:
                return seq
        return seq

    def read_line(self):
        buf = ""
        try:
            raw = enter_raw(0)
        except (OSError, termios.error):
            # Not a tty (piped/redirected stdin): fall back to input() so the
            # REPL still works headlessly.
            return input(self._prompt)
        self._emit(MOUSE_ENABLE)
        try:
            self._emit(self._prompt)
            while True:
                b = self._read_byte()
                if not b:
                    continue
                if b == b"\r" or b == b"\n":
                    self._emit("\r\n")
                    if buf:
                        self._push_history(buf)
                    return buf
                if b == b"\x03":
                    raise KeyboardInterrupt
                if b == b"\x04":
                    if not buf:
                        raise EOFError
                    self._emit("\r\n")
                    if buf:
                        self._push_history(buf)
                    return buf
                if b == b"\x7f" or b == b"\x08":
                    if buf:
                        buf = buf[:-1]
                        self._redraw(buf)
                    continue
                if b == b"\x0c":
                    self._redraw(buf)
                    continue
                if b == b"\t":
                    if buf.startswith("/"):
                        m = _complete(buf, 0)
                        if m and m != buf:
                            buf = m
                            self._redraw(buf)
                    continue
                if b == b"\x1b":
                    seq = self._collect_escape()
                    if seq is None:
                        continue
                    if seq == b"\x1b[A":
                        if self._hist_i < len(self._hist) - 1:
                            if self._hist_i == -1:
                                self._draft = buf
                            self._hist_i += 1
                            buf = self._hist[self._hist_i]
                            self._redraw(buf)
                    elif seq == b"\x1b[B":
                        if self._hist_i >= 0:
                            self._hist_i -= 1
                            buf = self._draft if self._hist_i == -1 else self._hist[self._hist_i]
                            self._redraw(buf)
                    else:
                        ev = parse_mouse(seq)
                        if ev is not None and self._scroll_handler is not None:
                            self._handle_mouse(ev)
                            # restore() replays only completed lines; the prompt
                            # is a partial line, so redraw it here.
                            self._redraw(buf)
                    continue
                try:
                    s = b.decode("utf-8")
                except UnicodeDecodeError:
                    s = b.decode("utf-8", "replace")
                if s.isprintable():
                    buf += s
                    self._emit(s)
        finally:
            self._emit(MOUSE_DISABLE)
            leave_raw(0, raw)

    def _handle_mouse(self, ev):
        h = self._scroll_handler
        if ev["kind"] != "press" or (ev["button"] & 32):
            return
        if is_wheel_up(ev):
            h("older")

    def run_scroll_loop(self, scroll_view, page):
        raw = enter_raw(0)
        last = scroll_view.page_count() - 1
        try:
            while True:
                self._tee.write_direct("\x1b[H\x1b[2J" + scroll_view.page_text(page))
                b = self._read_byte()
                if not b:
                    continue
                if b in (b"\r", b"\n", b"q"):
                    return page
                if b == b"\x03":
                    raise KeyboardInterrupt
                if b == b"\x04":
                    return page
                if b == b"\x1b":
                    seq = self._collect_escape()
                    if seq == b"\x1b[A":
                        page = max(page - 1, 0)
                        continue
                    if seq == b"\x1b[B":
                        page = min(page + 1, last)
                        continue
                    ev = parse_mouse(seq)
                    if ev is not None:
                        btn = ev["button"]
                        motion = bool(btn & 32)
                        if ev["kind"] != "press" or motion:
                            continue
                        if btn == WHEEL_UP:
                            page = max(page - 1, 0)
                        elif btn == WHEEL_DOWN:
                            if page >= last:
                                return page
                            page = min(page + 1, last)
                        elif btn == 0:
                            if ev["row"] >= scroll_view.rows - 1 and ev["col"] <= 8:
                                page = max(page - 1, 0)
                            elif ev["row"] >= scroll_view.rows - 1 and ev["col"] >= scroll_view.columns - 8:
                                if page >= last:
                                    return page
                                page = min(page + 1, last)
                            else:
                                return page
        finally:
            leave_raw(0, raw)


def _complete(text, state):
    from alvaagent.repl import _slash_complete
    return _slash_complete(text, state)
