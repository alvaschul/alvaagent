# Scrollable Alt-Screen Chat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the alvaagent TUI chat scrollable by touch-swipe/wheel while keeping the alternate screen buffer.

**Architecture:** Capture every stdout line with a `StreamTee`, replace the main `input()` prompt with a raw-mode `LineReader` that parses SGR mouse events, and re-render a paged conversation view (`ScrollView`) inside the alt screen on swipe/wheel-up. The terminal's own buffer scroll is unusable in the alt screen, so the app does its own scrolling.

**Tech Stack:** Python 3.12+ stdlib only (`termios`, `tty`, `os.read`, `shutil`, `select`), `sys.stdout` proxy, raw ANSI/SGR escape sequences. No third-party dependencies.

**Spec:** `docs/superpowers/specs/2026-08-15-scrollable-alt-screen-chat-design.md`

## Global Constraints

- Stdlib only — no `curses`, `prompt_toolkit`, `blessed`, or any pip package (project constraint; tests run on `/usr/bin/python3` 3.12, app runs on Termux 3.14).
- Run tests from the repo root: `python3 test_tui.py` — success is `ALL TESTS PASSED ✓`; tests are plain functions named `test_*` in `test_tui.py`, run in sorted order by the bundled `_run_all()` runner.
- pyflakes clean on all changed files: `/data/data/com.termux/files/usr/bin/python3 -m pyflakes <files> 2>&1 | grep -v "imported but unused"`.
- Do not change behaviors of prompts other than the main chat prompt (`ask_permission`, `pick_model`, `/multi`, y/N confirms keep `input()`).
- The alternate screen (`\x1b[?1049h`) is intentionally restored; the `test_cli_smoke` assertion `"\x1b[?1049" not in r.stdout` MUST be reverted in Task 1.
- Keyboard semantics of the new reader must match `input()`: returns `str` on Enter, raises `EOFError` on Ctrl+D with an empty buffer, raises `KeyboardInterrupt` on Ctrl+C, so `repl.py`'s existing `except` blocks work unchanged.
- Commit after every task (each task is independently testable and committable). Commit messages follow the repo style (`feat:` / `fix:` / `refactor:` prefixes).

---

### Task 1: Revert the no-alt-screen change

**Files:**
- Modify: `alvaagent/repl.py` (main() + `_cleanup`)
- Test: `test_tui.py`

**Interfaces:**
- Consumes: nothing.
- Produces: the app re-enters the alternate screen (`?1049h` at launch, `?1049l` in `_cleanup`) — the baseline the rest of the plan builds on.

- [ ] **Step 1: Revert `_cleanup` to emit the alt-screen leave code**

In `alvaagent/repl.py`, inside `_cleanup`, restore the write of `\x1b[?1049l` before the `\n`:

```python
    def _cleanup(signum=None, frame=None):
        if _restored.is_set():
            return
        _restored.set()
        try:
            sys.stdout.write("\x1b[?1049l")
            sys.stdout.write("\n")
            sys.stdout.flush()
        except Exception:
            pass
        if signum is not None:
            sys.exit(signum)
```

- [ ] **Step 2: Restore the alt-screen enter code in main()**

In `alvaagent/repl.py` `main()`, replace the current comment + missing escape with:

```python
    # Alternate-screen buffer: take over the whole terminal like Hermes' TUI
    # (prior scrollback hidden on launch, restored on exit). Emit the enter
    # code, run, and always emit the leave code (even on Ctrl-C / error).
    sys.stdout.write("\x1b[?1049h")
    sys.stdout.flush()
    try:
        banner(rt)
        repl(rt)
    finally:
        _cleanup()
```

- [ ] **Step 3: Revert the `test_cli_smoke` alt-screen assertion**

In `test_tui.py` `test_cli_smoke`, delete the line `assert "\x1b[?1049" not in r.stdout, "must not enter the alternate-screen buffer (kills terminal scrollback)"`.

- [ ] **Step 4: Run the full suite**

Run: `python3 test_tui.py`
Expected: `ALL TESTS PASSED ✓`

- [ ] **Step 5: Commit**

```bash
git add alvaagent/repl.py test_tui.py
git commit -m "feat: restore alternate-screen buffer as the scrollable-chat baseline"
```

---

### Task 2: StreamTee — stdout line capture

**Files:**
- Create: `alvaagent/scrollback.py`
- Test: `test_tui.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class StreamTee`:
    - `__init__(self, stream=None, max_lines=5000)` — `stream` defaults to `sys.stdout`.
    - `install(self)` — swaps `sys.stdout` to this tee, records the old stream as `self._orig`.
    - `uninstall(self)` — restores the original stdout.
    - `write(self, s)` — delegates to the original stream AND captures (bytes/str, ANSI included).
    - `flush(self)` — delegates to the original stream.
    - `captured_lines(self) -> list[str]` — completed lines (no trailing `\n`).
    - `partial_line(self) -> str` — text since the last `\n`.
    - `restore(self)` — emits `\x1b[2J\x1b[H` then replays `captured_lines()` in order (each followed by `\n`), then `flush()`.
  - Module-level constants `WHEEL_UP = 64`, `WHEEL_DOWN = 65` used by Task 3's parser.

Capture rules:
- Split incoming text on `\n`. Everything before the last `\n` becomes completed lines (bounded at `max_lines` — drop oldest). The remainder becomes the partial line.
- A completed line is SKIPPED (not captured) if it starts with `\x1b[?` (DEC private-mode: alt screen / mouse codes).
- Never capture an empty string; an empty completed line (just `\n`) IS captured as `""` if the preceding partial is empty.
- Lines are stored as `str`. When the tee receives bytes, `decode("utf-8", "replace")`.

- [ ] **Step 1: Write the failing tests**

In `test_tui.py`, add:

```python
def test_stream_tee_capture():
    from alvaagent.scrollback import StreamTee
    from io import StringIO
    sink = StringIO()
    tee = StreamTee(stream=sink)
    tee.write("hello\n")
    tee.write("\x1b[38;5;45mcolored\x1b[0m line\n")
    tee.write("partial")
    assert tee.captured_lines() == ["hello", "\x1b[38;5;45mcolored\x1b[0m line"]
    assert tee.partial_line() == "partial"
    assert sink.getvalue() == "hello\n\x1b[38;5;45mcolored\x1b[0m line\npartial"


def test_stream_tee_filters_private_modes():
    from alvaagent.scrollback import StreamTee
    from io import StringIO
    sink = StringIO()
    tee = StreamTee(stream=sink)
    tee.write("\x1b[?1049h")
    tee.write("\x1b[?1002h")
    tee.write("\n")
    tee.write("banner\n")
    assert tee.captured_lines() == ["banner"]


def test_stream_tee_restore_replays():
    from alvaagent.scrollback import StreamTee
    from io import StringIO
    sink = StringIO()
    tee = StreamTee(stream=sink)
    tee.write("line one\nline two\n")
    tee.restore()
    out = sink.getvalue()
    assert out.startswith("\x1b[2J\x1b[H")
    assert "line one\nline two\n" in out


def test_stream_tee_bounded():
    from alvaagent.scrollback import StreamTee
    from io import StringIO
    tee = StreamTee(stream=StringIO(), max_lines=3)
    for i in range(6):
        tee.write("line%d\n" % i)
    assert tee.captured_lines() == ["line3", "line4", "line5"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 test_tui.py 2>&1 | grep -E "stream_tee|FAIL|TESTS"`
Expected: 4 `FAIL` lines (`ModuleNotFoundError: No module named 'alvaagent.scrollback'`).

- [ ] **Step 3: Implement `StreamTee`**

Create `alvaagent/scrollback.py`:

```python
"""In-app scrollback + input reader for the alt-screen TUI.

The alternate screen buffer has no terminal scrollback, so the app captures
what it printed and re-renders a conversation view on swipe/wheel.
"""

import sys

WHEEL_UP = 64
WHEEL_DOWN = 65


class StreamTee:
    """Wraps a stream, forwarding writes and recording completed lines.

    stdout in this app carries no '\\r' redraws or cursor-position escapes
    (the spinner writes those to stderr), so splitting on '\\n' is faithful.
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 test_tui.py 2>&1 | grep -E "stream_tee|FAIL|TESTS"`
Expected: 4 `ok` lines and `ALL TESTS PASSED ✓`

- [ ] **Step 5: Commit**

```bash
git add alvaagent/scrollback.py test_tui.py
git commit -m "feat: StreamTee stdout capture for alt-screen scrollback"
```

---

### Task 3: SGR mouse parser

**Files:**
- Modify: `alvaagent/scrollback.py`
- Test: `test_tui.py`

**Interfaces:**
- Consumes: `WHEEL_UP`, `WHEEL_DOWN` from Task 2.
- Produces:
  - `parse_mouse(esc: bytes) -> dict | None` — returns a dict `{"button": int, "col": int, "row": int, "kind": "press"|"release"}` for a valid SGR mouse sequence, else `None`.
  - `is_wheel_up(ev) -> bool`, `is_wheel_down(ev) -> bool` helpers (`button == WHEEL_UP` / `WHEEL_DOWN`).
  - Module constants `MOUSE_ENABLE = "\x1b[?1000h\x1b[?1002h\x1b[?1006h"`, `MOUSE_DISABLE = "\x1b[?1000l\x1b[?1002l\x1b[?1006l"` used by Task 6.

SGR mouse format: `\x1b[<b;c;rM` (press/drag) or `\x1b[<b;c;rm` (release), all ASCII digits/semicolons, 1-based col/row.

- [ ] **Step 1: Write the failing tests**

In `test_tui.py`, add:

```python
def test_mouse_parse():
    from alvaagent.scrollback import (parse_mouse, is_wheel_up, is_wheel_down)
    up = parse_mouse(b"\x1b[<64;20;5M")
    assert up == {"button": 64, "col": 20, "row": 5, "kind": "press"}
    assert is_wheel_up(up) and not is_wheel_down(up)
    down = parse_mouse(b"\x1b[<65;20;5M")
    assert is_wheel_down(down)
    rel = parse_mouse(b"\x1b[<0;1;1m")
    assert rel["kind"] == "release" and rel["button"] == 0
    assert parse_mouse(b"garbage") is None
    assert parse_mouse(b"\x1b[<64;x;5M") is None
    assert parse_mouse(b"\x1b[<64;20;5X") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 test_tui.py 2>&1 | grep -E "mouse_parse|FAIL|TESTS"`
Expected: `FAIL` (`ImportError: cannot import name 'parse_mouse'`).

- [ ] **Step 3: Implement the parser**

Append to `alvaagent/scrollback.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 test_tui.py 2>&1 | grep -E "mouse_parse|FAIL|TESTS"`
Expected: `ok - test_mouse_parse` and `ALL TESTS PASSED ✓`

- [ ] **Step 5: Commit**

```bash
git add alvaagent/scrollback.py test_tui.py
git commit -m "feat: parse SGR mouse events (wheel) for alt-screen scrolling"
```

---

### Task 4: ScrollView — paged conversation rendering

**Files:**
- Modify: `alvaagent/scrollback.py`
- Modify: `alvaagent/commands.py` (add a small helper to build display lines from `history`)
- Test: `test_tui.py`

**Interfaces:**
- Consumes: `render_conversation(history)` already exists in `alvaagent/commands.py` (returns plain text, `""` for empty history).
- Produces:
  - `class ScrollView`:
    - `__init__(self, history, columns=None, rows=None)` — `columns`/`rows` default via `shutil.get_terminal_size()`.
    - `total_lines(self) -> int`
    - `page_count(self) -> int` — `max(1, ceil(total_lines / window))`.
    - `page_text(self, page) -> str` — the wrapped lines for `page` (0-based), plus a footer line.
    - `window(self) -> int` — `rows - 2`.
  - Wrapping helper `wrap_to(text, width) -> list[str]` (word-wrap at `width`, split long words).
  - `lines_for_history(history, columns) -> list[str]` — wraps `render_conversation(history)`.

Behavior:
- Footer format (last line of `page_text`): `"  ◀ older · page %d/%d · ▼ newer · ⏎ return  " % (page + 1, page_count)`.
- `page_text` returns exactly `window` content lines + 1 footer line; a page shorter than `window` is padded with blank lines.
- Empty `history` → `page_count() == 1`, `page_text(0)` returns just the footer.

- [ ] **Step 1: Write the failing tests**

In `test_tui.py`, add:

```python
def test_scroll_view_wrap():
    from alvaagent.scrollback import wrap_to
    assert wrap_to("one two three four", 10) == ["one two", "three four"]
    long_word = "x" * 30
    assert wrap_to("a " + long_word, 10) == ["a", long_word[:10], long_word[10:20], long_word[20:30]]


def test_scroll_view_pages():
    from alvaagent.scrollback import ScrollView
    history = [{"role": "user", "content": "hello"},
               {"role": "assistant", "content": "a" * 60}]
    sv = ScrollView(history, columns=20, rows=12)
    assert sv.window() == 10
    assert sv.total_lines() > 0
    assert sv.page_count() >= 1
    page0 = sv.page_text(0)
    assert "## you" in page0
    assert "hello" in page0
    assert "page 1/%d" % sv.page_count() in page0
    assert "◀ older" in page0 and "▼ newer" in page0 and "⏎ return" in page0
    lines = page0.split("\n")
    assert len(lines) == sv.window() + 1


def test_scroll_view_empty():
    from alvaagent.scrollback import ScrollView
    sv = ScrollView([], columns=20, rows=12)
    assert sv.page_count() == 1
    assert "⏎ return" in sv.page_text(0)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 test_tui.py 2>&1 | grep -E "scroll_view|FAIL|TESTS"`
Expected: 3 `FAIL` lines (`ImportError: cannot import name 'wrap_to'`).

- [ ] **Step 3: Implement the helpers + ScrollView**

Append to `alvaagent/scrollback.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 test_tui.py 2>&1 | grep -E "scroll_view|FAIL|TESTS"`
Expected: 3 `ok` lines and `ALL TESTS PASSED ✓`

- [ ] **Step 5: Commit**

```bash
git add alvaagent/scrollback.py test_tui.py
git commit -m "feat: ScrollView paged conversation renderer for the alt screen"
```

---

### Task 5: LineReader — raw-mode prompt input with mouse support

**Files:**
- Modify: `alvaagent/scrollback.py`
- Modify: `alvaagent/repl.py`
- Test: `test_tui.py`

**Interfaces:**
- Consumes:
  - `parse_mouse`, `is_wheel_up`, `is_wheel_down`, `ScrollView`, `StreamTee` from earlier tasks.
  - `_slash_complete(text, state)` and `_SLASH_COMMANDS` from `alvaagent/repl.py` (already exist).
  - `readline.add_history` / `readline.write_history_file` / `readline.read_history_file` via the existing `setup_completion()` / `save_completion_history()` (already exist).
- Produces:
  - `class LineReader`:
    - `__init__(self, tee: StreamTee, history_list: list, prompt="> ")`
    - `read_line(self) -> str` — one committed line.
    - `on_scroll(self, handler)` — set a callback `handler(direction: str)` with `direction in ("older", "newer")`; the reader calls it on wheel-up/wheel-down and taps on footer zones.
    - `run_scroll_loop(self, scroll_view, offset_page)` — interactive scroll mode (details below), returns the page to return to or `None` to stay in scroll mode.
  - Module-level enable/disable of raw mode: `enter_raw(tty_fd)` / `leave_raw(tty_fd)` helpers (termios). The reader uses fd 0.

`read_line()` behavior:
- Enter raw mode (save + set cbreak, echo off, min=1, time=0) for the duration of the read; always leave raw mode before returning/raising (use `try/finally`).
- Mouse enable/disable lifetime is Task 6's job (`MOUSE_ENABLE` in `main`, `MOUSE_DISABLE` in `_cleanup`); `read_line` must still work when no mouse bytes ever arrive.
- Echo every printable char (append to buffer, `sys.stdout.write(char)`), backspace removes last char and writes `\b \b`, Enter writes `\n` and returns the buffer.
- Up/Down (`\x1b[A`/`\x1b[B`): walk a local history copy (init `self._hist = list(reversed(history_list))`), store the current draft, swap in the recalled line by clearing the line with `\r\x1b[K` + reprinting prompt + line.
- Tab: complete using `_slash_complete(buf, i)` over states `0..15`; on a match, replace the trailing word and re-echo (clear + prompt + new line).
- SGR mouse sequence: buffer escape bytes until a complete `M`/`m` terminator (max 32 bytes), call `parse_mouse`; if wheel-up → callback `on_scroll("older")`, wheel-down → `on_scroll("newer")`. Also report `press` events whose `col` is within the footer zones when a callback is registered: `col <= 8` → older, `col >= columns - 8` → newer, and rows equal to the last row → return ("live") via callback `on_scroll("return")`.
- Ctrl+C: raise `KeyboardInterrupt`. Ctrl+D with empty buffer: raise `EOFError`; with text: return the buffer.
- Any other escape sequence: consume and ignore.
- After Enter, push the committed line into history via `readline.add_history(line)` (so the existing `save_completion_history()` persists it) and append to `self._hist`.

`run_scroll_loop(scroll_view, offset_page)`:
- Called by the scroll-mode wrapper (Task 6) — it clears the screen, then loops:
  - Print `scroll_view.page_text(page)`, then read bytes (raw mode active).
  - Wheel-down / swipe-down at the NEWEST page, or a `"return"` event, or Enter/`q`: return `page` (exit scroll mode).
  - Wheel-up: `page = min(page + 1, scroll_view.page_count() - 1)`.
  - Wheel-down: `page = max(page - 1, 0)`.
  - Redraw between pages with `\x1b[H\x1b[2J` + page text.
- Keeps `KeyboardInterrupt`/`EOFError` propagation (so a stuck scroll loop can be Ctrl+C'd).

- [ ] **Step 1: Write the failing tests**

In `test_tui.py`, add (PTY tests — pattern: fork a child running a small script that uses the reader against the pty; parent writes bytes and reads output). First add `import pty` to the test file's top imports (next to the existing `import select`):

```python
import pty
```

Then add the helpers and tests:

```python
def _pty_run(script):
    """Run `script` in a pty child; return (pid, fd, reader) set up for the parent."""
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir("/data/data/com.termux/files/home/alvaagent")
        os.execv(sys.executable, [sys.executable, "-c", script])
        os._exit(127)
    os.set_blocking(fd, False)
    return pid, fd


def _pty_drain(fd, seconds):
    import select
    out = b""
    end = time.time() + seconds
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        else:
            time.sleep(0.05)
    return out


def test_line_reader_basic():
    script = (
        "import sys\n"
        "from io import StringIO\n"
        "from alvaagent.scrollback import StreamTee, LineReader\n"
        "tee = StreamTee(stream=StringIO())\n"
        "sys.stdout = tee\n"
        "r = LineReader(tee, [], prompt='> ')\n"
        "print('RESULT=' + repr(r.read_line()))\n"
        "sys.stdout.flush()\n"
    )
    pid, fd = _pty_run(script)
    _pty_drain(fd, 2.0)
    os.write(fd, b"hi there\n")
    out = _pty_drain(fd, 2.0)
    assert b"RESULT='hi there'" in out, out[-500:]
    try:
        os.kill(pid, 15)
    except OSError:
        pass
    try:
        os.waitpid(pid, os.WNOHANG)
    except OSError:
        pass


def test_line_reader_eof_and_interrupt():
    script = (
        "import sys\n"
        "from io import StringIO\n"
        "from alvaagent.scrollback import StreamTee, LineReader\n"
        "tee = StreamTee(stream=StringIO())\n"
        "sys.stdout = tee\n"
        "r = LineReader(tee, [], prompt='> ')\n"
        "try:\n"
        "    r.read_line()\n"
        "    print('NO-RAISE')\n"
        "except EOFError:\n"
        "    print('GOT-EOF')\n"
        "sys.stdout.flush()\n"
    )
    pid, fd = _pty_run(script)
    _pty_drain(fd, 2.0)
    os.write(fd, b"\x04")
    out = _pty_drain(fd, 2.0)
    assert b"GOT-EOF" in out, out[-500:]
    try:
        os.kill(pid, 15)
    except OSError:
        pass
    try:
        os.waitpid(pid, os.WNOHANG)
    except OSError:
        pass
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 test_tui.py 2>&1 | grep -E "line_reader|FAIL|TESTS"`
Expected: 2 `FAIL` lines (`ImportError: cannot import name 'LineReader'`).

- [ ] **Step 3: Implement `LineReader`**

Append to `alvaagent/scrollback.py`:

```python
import os
import sys
import termios
import tty
import readline


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
        """Gather a CSI/mouse sequence; return bytes or None."""
        seq = b"\x1b"
        for _ in range(32):
            b = self._read_byte()
            if not b:
                return None
            seq += b
            if b in (b"M", b"m", b"A", b"B", b"C", b"D", b"~", b"H", b"F", b"Z"):
                break
        return seq

    def read_line(self):
        buf = ""
        raw = enter_raw(0)
        try:
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
            leave_raw(0, raw)

    def _handle_mouse(self, ev):
        h = self._scroll_handler
        if is_wheel_up(ev):
            h("older")
        elif is_wheel_down(ev):
            h("newer")
        elif ev["kind"] == "press" and ev["row"] >= self._rows_for_scroll():
            if ev["col"] <= 8:
                h("older")
            elif ev["col"] >= self._columns - 8:
                h("newer")
            else:
                h("return")

    def _rows_for_scroll(self):
        try:
            return shutil.get_terminal_size().lines
        except Exception:
            return 24

    def run_scroll_loop(self, scroll_view, page):
        raw = enter_raw(0)
        try:
            while True:
                self._emit("\x1b[H\x1b[2J" + scroll_view.page_text(page))
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
                    ev = parse_mouse(seq)
                    if ev is not None:
                        if is_wheel_up(ev) or (ev["kind"] == "press" and ev["col"] <= 8):
                            page = min(page + 1, scroll_view.page_count() - 1)
                        elif is_wheel_down(ev) or (ev["kind"] == "press" and ev["col"] >= self._columns - 8):
                            page = max(page - 1, 0)
                        elif ev["kind"] == "press":
                            return page
                elif b == b"\x1b[A":
                    page = min(page + 1, scroll_view.page_count() - 1)
                elif b == b"\x1b[B":
                    page = max(page - 1, 0)
        finally:
            leave_raw(0, raw)
```

Also append to `alvaagent/scrollback.py` a completion helper that defers to the REPL's completer (import inside the function to avoid a circular import at module load):

```python
def _complete(text, state):
    from alvaagent.repl import _slash_complete, _SLASH_COMMANDS
    return _slash_complete(text, state)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 test_tui.py 2>&1 | grep -E "line_reader|FAIL|TESTS"`
Expected: 2 `ok` lines and `ALL TESTS PASSED ✓`

- [ ] **Step 5: Commit**

```bash
git add alvaagent/scrollback.py test_tui.py
git commit -m "feat: LineReader raw-mode prompt with SGR mouse scrolling"
```

---

### Task 6: Wire into the REPL + main()

**Files:**
- Modify: `alvaagent/repl.py`
- Modify: `test_tui.py`
- Modify: `README.md`

**Interfaces:**
- Consumes:
  - `StreamTee`, `LineReader`, `ScrollView`, `MOUSE_ENABLE`, `MOUSE_DISABLE` from `alvaagent/scrollback.py`.
  - `rt.history` — the in-memory conversation list in the REPL.
- Produces:
  - `repl(rt)` uses the `LineReader` for the main prompt and drives scroll mode via a callback; the module-level `_TEE` and `_READER` are accessible to `main()` for cleanup.
  - `main()` emits `MOUSE_ENABLE` after the banner and `MOUSE_DISABLE` + `\x1b[?1049l` in `_cleanup`.

Implementation notes (exact edits):

In `alvaagent/repl.py`:

1. Add imports at the top (with the other `from alvaagent...` imports):

```python
from alvaagent.scrollback import (StreamTee, LineReader, ScrollView,
                                  MOUSE_ENABLE, MOUSE_DISABLE)
```

2. Add module-level state and two helpers after `setup_completion()`:

```python
_TEE = None


def _history_file_lines():
    """Load persisted input history as plain lines (for the LineReader)."""
    lines = []
    try:
        if os.path.exists(HISTORY_PATH):
            with open(HISTORY_PATH, encoding="utf-8") as f:
                lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    except Exception:
        pass
    return lines


def _prompt(rt):
    return col(_tui.CUR_SKIN["accent"], "> ") if COLOR else "> "
```

3. In `repl()`, right before the `while True:` input loop, replace the line that sets `prompt`:

```python
            prompt = col(_tui.CUR_SKIN["accent"], "> ") if COLOR else "> "
```

with:

```python
            prompt = _prompt(rt)
            global _TEE
            _TEE = StreamTee()
            _TEE.install()
            _reader = LineReader(_TEE, _history_file_lines(), prompt=prompt)
```

(The `prompt` variable stays in scope; the reader uses it. `_reader` is used by `_handle_scroll` below.)

4. In the same loop, replace the input call:

```python
            line = input(prompt)
```

with:

```python
            _reader.on_scroll(lambda d: _handle_scroll(d, rt, _reader))
            line = _reader.read_line()
```

5. Add `_handle_scroll` at module level (after `repl`):

```python
def _handle_scroll(direction, rt, reader):
    """Enter the scroll view; restore the live screen when it exits."""
    sv = ScrollView(list(rt.history))
    if not sv.total_lines():
        return
    reader.run_scroll_loop(sv, sv.page_count() - 1)
    if _TEE is not None:
        _TEE.restore()
```

Note: `run_scroll_loop` is synchronous and runs inside the mouse handler while the reader's raw mode is active; it saves/restores its own termios attrs via `enter_raw`/`leave_raw`, so nesting is safe.

6. In `main()`, after the banner is printed (the line `banner(rt)` inside the try), emit mouse enable. The current code is:

```python
    try:
        banner(rt)
        repl(rt)
    finally:
        _cleanup()
```

Change to:

```python
    try:
        banner(rt)
        sys.stdout.write(MOUSE_ENABLE)
        sys.stdout.flush()
        repl(rt)
    finally:
        _cleanup()
```

7. In `_cleanup`, disable mouse before leaving the alt screen. Current:

```python
        try:
            sys.stdout.write("\x1b[?1049l")
            sys.stdout.write("\n")
            sys.stdout.flush()
        except Exception:
            pass
```

Change to:

```python
        try:
            sys.stdout.write(MOUSE_DISABLE)
            sys.stdout.write("\x1b[?1049l")
            sys.stdout.write("\n")
            sys.stdout.flush()
        except Exception:
            pass
```

- [ ] **Step 1: Write the failing test (end-to-end scroll via PTY)**

In `test_tui.py`, add:

```python
def test_scroll_e2e():
    # Boot the real app with a seeded session, feed a wheel-up (swipe up)
    # event, expect the scroll view with older content, then Enter to
    # return to live, then /exit.
    data_dir = tempfile.mkdtemp(prefix="alva_scrolle2e_")
    _TMP_DIRS.append(data_dir)
    store = {
        "alvaagent.sessions": {
            "test": {"name": "test", "created": "2026-08-15T00:00:00",
                     "updated": "2026-08-15T00:00:00",
                     "messages": [
                         {"role": "user", "content": "old question"},
                         {"role": "assistant", "content": "old answer"},
                         {"role": "user", "content": "new question"},
                         {"role": "assistant", "content": "new answer"},
                     ]}
        },
        "alvaagent.active_session": "test",
    }
    with open(os.path.join(data_dir, "store.json"), "w") as f:
        json.dump(store, f)
    env = dict(os.environ)
    env["ALVA_DATA_DIR"] = data_dir
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir("/data/data/com.termux/files/home/alvaagent")
        os.execv(sys.executable, [sys.executable, "-m", "alvaagent"])
        os._exit(127)
    os.set_blocking(fd, False)
    out = b""
    try:
        out += _pty_drain(fd, 5.0)
        os.write(fd, b"\x1b[<64;1;1M")  # wheel up == swipe up on Termux
        out += _pty_drain(fd, 3.0)
        assert b"old question" in out, out[-800:]
        assert b"old answer" in out, out[-800:]
        os.write(fd, b"\r")             # Enter: return to live
        out += _pty_drain(fd, 2.0)
        assert b"> " in out[-400:], out[-400:]  # live prompt restored
        os.write(fd, b"/exit\n")
        out += _pty_drain(fd, 3.0)
        assert b"bye" in out, out[-400:]
    finally:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except OSError:
            pass
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 test_tui.py 2>&1 | grep -E "scroll_e2e|FAIL|TESTS"`
Expected: `FAIL` — wheel-up bytes do nothing yet, so `b"old question" not in out`.

- [ ] **Step 3: Apply the wiring edits**

Make the edits described in the Interfaces/implementation notes above. The `_handle_scroll` flow:

```python
def _handle_scroll(direction, rt, reader):
    sv = ScrollView(list(rt.history))
    if not sv.total_lines():
        return
    page = reader.run_scroll_loop(sv, sv.page_count() - 1)
    if _TEE is not None:
        _TEE.restore()
```

Note: `_reader.on_scroll(lambda d: _handle_scroll(d, rt, _reader))` must be set BEFORE the `read_line()` call each iteration.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 test_tui.py 2>&1 | grep -E "scroll_e2e|FAIL|TESTS"`
Expected: `ok - test_scroll_e2e` and `ALL TESTS PASSED ✓`

- [ ] **Step 5: Update README**

In `README.md`, in the section that lists `/scroll`, add a sentence:

```
Touch-swipe (or mouse-wheel) up while at the prompt opens a scroll view of the
conversation inside the same screen; swipe down or press Enter to return to the
live chat.
```

- [ ] **Step 6: Full suite + lint**

Run: `python3 test_tui.py`
Expected: `ALL TESTS PASSED ✓`

Run: `/data/data/com.termux/files/usr/bin/python3 -m pyflakes alvaagent/scrollback.py alvaagent/repl.py test_tui.py 2>&1 | grep -v "imported but unused"`
Expected: no output.

- [ ] **Step 7: Commit**

```bash
git add alvaagent/repl.py alvaagent/scrollback.py test_tui.py README.md
git commit -m "feat: swipe/wheel scrollable conversation inside the alt screen"
```
