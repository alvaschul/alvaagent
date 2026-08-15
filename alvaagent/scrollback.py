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
