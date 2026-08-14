import datetime
import json
import os
import re
import shutil
import sys
import threading
import time

from alvaagent.config import ALVA_VERSION, DEFAULT_SKIN, active_cfg
from alvaagent.agent import _strip_xml_blocks, run_agent_stream
from alvaagent.sessions import compress_history, context_usage, context_window_for
from alvaagent.skills import skill_list
from alvaagent.tools import TOOLS, visible
from alvaagent.util import _fmt_k

# Rich backs the Hermes-style panels (pure-Python, pip-installs on Termux).
# The Hermes agent TUI renders with Rich `Panel(box=HORIZONTALS)`; we mirror
# that exactly so alvaagent reads as Hermes. `pip install rich` makes it
# available; if it's somehow absent we fall back to a tiny ANSI shim so the
# TUI still launches.
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.box import HORIZONTALS
    _CON = Console()
except Exception:  # pragma: no cover - only when rich is unavailable
    import sys as _sys
    class _ShimPanel:
        def __init__(self, *a, **k):
            self._render = (a[0] if a else "")
            self.title = k.get("title", "")
            self.border_style = k.get("border_style", "")
            self.box = k.get("box")
            self.padding = k.get("padding", (0, 0))
            self.width = k.get("width")
        def __str__(self):
            return str(self._render)
    class _ShimConsole:
        def print(self, *a, **k):
            for x in a:
                _sys.stdout.write(str(x) + "\n")
    class _ShimBox:
        HORIZONTALS = "HORIZONTALS"
    Console = _ShimConsole
    Panel = _ShimPanel
    HORIZONTALS = _ShimBox.HORIZONTALS
    _CON = Console()


# ============================================================
#  Terminal UI
# ============================================================
class C:
    RESET = "\x1b[0m"
    BOLD = "\x1b[1m"
    DIM = "\x1b[2m"
    ITALIC = "\x1b[3m"
    STRIKE = "\x1b[9m"
    CYAN = "\x1b[36m"
    GREEN = "\x1b[32m"
    YELLOW = "\x1b[33m"
    RED = "\x1b[31m"
    FG = "\x1b[38;5;%dm"   # 256-color foreground template
    BG = "\x1b[48;5;%dm"   # 256-color background template


COLOR = bool(sys.stdout.isatty()) and not os.environ.get("NO_COLOR")


# ---------------- skins (Hermes-style theming, own palettes) ----------------
# Each skin picks 256-color ANSI codes; the layout is always the same, only the
# palette changes. '/skin' lists & switches them (persisted in config.json).
SKINS = {
    "midnight": {  # default - deep-space blues
        "desc": "deep-space blues (default)",
        "accent": C.FG % 45, "user": C.FG % 220, "agent": C.FG % 81,
        "tool": C.FG % 141, "border": C.FG % 240, "chip": C.FG % 45,
        "code": C.FG % 187, "ok": C.FG % 114, "err": C.FG % 203,
        "dim": C.FG % 244,
    },
    "ember": {  # warm fire palette
        "desc": "warm embers & gold",
        "accent": C.FG % 208, "user": C.FG % 222, "agent": C.FG % 209,
        "tool": C.FG % 203, "border": C.FG % 240, "chip": C.FG % 208,
        "code": C.FG % 223, "ok": C.FG % 114, "err": C.FG % 196,
        "dim": C.FG % 244,
    },
    "ocean": {  # cool sea blues & teal
        "desc": "cool sea blues & teal",
        "accent": C.FG % 75, "user": C.FG % 51, "agent": C.FG % 117,
        "tool": C.FG % 110, "border": C.FG % 240, "chip": C.FG % 75,
        "code": C.FG % 158, "ok": C.FG % 114, "err": C.FG % 203,
        "dim": C.FG % 244,
    },
    "daylight": {  # for bright terminals - dark ink on light
        "desc": "bright terminals, dark ink",
        "accent": C.FG % 27, "user": C.FG % 130, "agent": C.FG % 27,
        "tool": C.FG % 90, "border": C.FG % 250, "chip": C.FG % 27,
        "code": C.FG % 22, "ok": C.FG % 28, "err": C.FG % 124,
        "dim": C.FG % 240,
    },
}

CUR_SKIN = SKINS[DEFAULT_SKIN]


def set_active_skin(rt):
    """Pick the persisted skin (config.json) for the rest of the session."""
    global CUR_SKIN
    CUR_SKIN = SKINS.get(rt.skin, SKINS[DEFAULT_SKIN])


def col(code, s):
    return code + s + C.RESET if COLOR and code else s


def p_info(s):
    print(col(CUR_SKIN["dim"], s))


def p_err(s):
    print(col(C.BOLD + CUR_SKIN["err"], "error") + "  " + s)


def p_ok(s):
    print(col(CUR_SKIN["ok"], s))


def p_warn(s):
    print(col(C.YELLOW, "  [!]") + "  " + s)


# ---------------- Hermes-style display (clean minimal chat) ----------------
# User turns are a compact gold '●' bullet; tool calls are small indented dim
# lines ('  ▸ name (args)' / '  ✓ name → summary'); agent replies stream with a
# thin bronze left accent bar ('▍ ') and NO box. The only full-width Rich Panel
# left is the startup banner. Hermes' palette is fixed (gold bullet, bronze bar,
# cream text) so the chat reads the same regardless of the /skin palette.


def _term_width():
    try:
        return max(40, shutil.get_terminal_size((80, 24)).columns)
    except Exception:
        return 80


HERMES_ACCENT = "#FFD700"   # gold   - user bullet / banner title
HERMES_BORDER = "#CD7F32"   # bronze - agent reply border (Hermes response_border)
HERMES_TEXT   = "#FFF8DC"   # cream  - agent text
HERMES_DIM    = "#8B8682"   # session border / chips / tool dividers
HERMES_OK     = "#8FBC8F"
HERMES_ERR    = "#CD5C5C"


def _hrgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _fgh(h):
    """Foreground ANSI for a hex color (respects COLOR/NO_COLOR)."""
    return ("\x1b[38;2;%d;%d;%dm" % _hrgb(h)) if COLOR else ""


def _rsth():
    return C.RESET if COLOR else ""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _tool_line(label, color):
    """Compact indented tool line: '  ▸ name (args)' or '  ✓ name → summary'."""
    print(_fgh(color) + "  " + label + _rsth())


def print_user_turn(text, show_ts=False):
    """Compact user scrollback: gold '●' bullet + bold text, no rule."""
    print()
    ts = (" " + datetime.datetime.now().strftime("%H:%M")) if show_ts else ""
    for i, line in enumerate(text.split("\n")):
        if i == 0:
            print(_fgh(HERMES_ACCENT) + "●" + _rsth() + " " + C.BOLD + line + _rsth()
                  + _fgh(HERMES_DIM) + ts + _rsth())
        else:
            print("  " + C.BOLD + line + _rsth())


def render_agent_panel(text, skin=None):
    """Buffered agent reply rendered with the same thin left bar as streaming."""
    sk = skin or CUR_SKIN
    w = AgentWriter(sk, sk["agent"])
    w.feed(text)
    w.close()


_MD_STYLE = {"**": "b", "__": "b", "*": "i", "_": "i", "~~": "s"}


def _md_attr_sgr(stack):
    """Combined SGR attributes for an emphasis style stack (b=bold, i=italic,
    s=strike). Emitting one code like '\\x1b[1;3m' keeps nested styles intact
    instead of a mid-span RESET clobbering the outer style."""
    codes = []
    if "b" in stack:
        codes.append("1")
    if "i" in stack:
        codes.append("3")
    if "s" in stack:
        codes.append("9")
    return "\x1b[" + ";".join(codes) + "m" if codes else ""


def _has_ansi(parts):
    return any("\x1b[" in p for p in parts)


def _md_line(text, skin):
    """Style one inline line of markdown into ANSI (CommonMark emphasis).

    **x** / __x__ -> bold, *x* / _x_ -> italic, ~~x~~ -> strikethrough,
    `x` -> inline code in the skin's code color, \\* escapes a literal marker.
    Emphasis can nest (e.g. **bold *italic* bold**) and the combined SGR is
    emitted so the outer style survives inner resets.

    Returns (rendered, parked). parked is non-empty when the line ends inside
    an unclosed emphasis/code span or on a bare opener; the caller carries it
    into the next feed() so markers split across streamed chunks still render
    as one styled span. When COLOR is off the line is returned untouched, so
    piped output keeps its original markdown characters.
    """
    if not COLOR:
        return text, ""
    n = len(text)
    out = []
    stack = []
    buf = []
    last_open = -1
    i = 0

    def flush():
        if not buf:
            return
        s = "".join(buf)
        if stack:
            out.append(_md_prefix(stack) + s)
        elif _has_ansi(out):
            out.append(C.RESET + s)
        else:
            out.append(s)
        del buf[:]

    def can_open(pos, width):
        nxt = text[pos + width] if pos + width < n else None
        prv = text[pos - 1] if pos > 0 else None
        if nxt is None:
            return True   # end of line - park; the next chunk may continue it
        if nxt in (" ", "\t"):
            return False
        if prv is not None and prv.isalnum():
            return False  # intraword: 6*7, snake_case stay literal
        return True

    def can_close(pos, width):
        prv = text[pos - 1] if pos > 0 else None
        nxt = text[pos + width] if pos + width < n else None
        if prv is None or prv in (" ", "\t"):
            return False
        if nxt is not None and nxt.isalnum():
            return False
        return True

    def park_from(pos):
        tail = "".join(out)
        if _has_ansi(out):
            tail += C.RESET
        return tail, text[pos:]

    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n and text[i + 1] in "*_~`\\":
            buf.append(text[i + 1])
            i += 2
            continue
        if ch == "`":
            j = text.find("`", i + 1)
            if j == -1:
                flush()
                return park_from(i)
            flush()
            out.append(C.RESET + skin["code"] + _md_attr_sgr(stack) + text[i + 1:j])
            out.append(C.RESET)
            i = j + 1
            continue
        if text[i:i + 3] in ("***", "___"):
            if "b" in stack and "i" in stack:
                flush()
                stack[:] = [k for k in stack if k not in ("b", "i")]
            else:
                flush()
                stack.append("b")
                stack.append("i")
                last_open = i
            i += 3
            continue
        two = text[i:i + 2]
        if two in _MD_STYLE:
            kind = _MD_STYLE[two]
            if kind in stack and can_close(i, 2):
                flush()
                while stack and stack[-1] != kind:
                    stack.pop()
                if stack:
                    stack.pop()
                i += 2
                continue
            if can_open(i, 2):
                flush()
                stack.append(kind)
                last_open = i
                i += 2
                continue
            buf.append(two)
            i += 2
            continue
        if ch in "*_":
            if "i" in stack and can_close(i, 1):
                flush()
                while stack and stack[-1] != "i":
                    stack.pop()
                if stack:
                    stack.pop()
                i += 1
                continue
            if can_open(i, 1):
                flush()
                stack.append("i")
                last_open = i
                i += 1
                continue
            buf.append(ch)
            i += 1
            continue
        buf.append(ch)
        i += 1

    if stack:
        return park_from(last_open)
    flush()
    if _has_ansi(out):
        out.append(C.RESET)
    return "".join(out), ""


def _md_prefix(stack):
    """Full SGR prefix (RESET + combined attrs) for a style stack."""
    return C.RESET + _md_attr_sgr(stack)


def style_inline(text, skin):
    """Inline markdown -> ANSI: `code`, **bold**, *italic* / _italic_, ~~strike~~.

    Applies per line (multi-line input is split and re-joined). Returns text
    untouched when colors are off, so piped/NO_COLOR output keeps its original
    markdown characters. The streaming AgentWriter uses _md_line directly so
    markers split across chunks can be parked and merged.
    """
    if "\n" in text:
        return "\n".join(_md_line(ln, skin)[0] for ln in text.split("\n"))
    return _md_line(text, skin)[0]


class AgentWriter:
    """Streams an agent response with a thin left accent bar ('▍ '), no box.

    Text flows immediately, line by line. Code fences (```) switch to dim,
    indented code with a small '─ lang' marker; the language name after the
    opening fence is captured and shown in the marker.
    """

    def __init__(self, skin, color):
        self.skin = skin
        self.color = color  # kept for API compat
        self.in_code = False
        self._code_label = None   # not None while waiting for the code-fence language
        self._pending = ""        # cross-chunk buffer for <think>/<tool_call> blocks
        self._md_pending = ""     # cross-chunk buffer for an unclosed inline marker
        self.started = False
        self.closed = False
        self.at_line_start = True

    def _filter_xml(self, chunk):
        """Hide <think>/<reasoning>/<tool_call> blocks (they're executed by
        run_agent_stream and reported as tool lines, not shown as raw text)."""
        self._pending += chunk
        clean, self._pending = _strip_xml_blocks(self._pending)
        return clean

    # ---- low-level output ----
    def _write(self, s):
        if self.at_line_start:
            self.at_line_start = False
            sys.stdout.write(_fgh(HERMES_BORDER) + "▍ " + _rsth())   # left accent bar
        sys.stdout.write(s)

    def _nl(self):
        sys.stdout.write("\n")
        self.at_line_start = True

    def _code_marker(self, lang):
        """Small dim marker opening a code block: '▍ ─ python'."""
        if not self.at_line_start:
            self._nl()
        self._write(col(self.skin["dim"], "─ " + (lang or "code").strip()))
        self._nl()

    # ---- public API ----
    def feed(self, chunk):
        if not self.started:
            self.started = True
        chunk = self._filter_xml(chunk)
        if not chunk:
            return
        parts = chunk.split("```")
        for i, part in enumerate(parts):
            if i > 0:
                self.in_code = not self.in_code
                if self.in_code:
                    if self._md_pending:
                        # emphasis can't span a code fence - flush as plain text
                        self._write(self._md_pending)
                        self._md_pending = ""
                    self._code_label = ""   # collect the language until the newline
                else:
                    self._flush_code_label()   # closing fence: no literal marker
            if part:
                if self.in_code:
                    self._write_code(part)
                else:
                    self._write_inline(part)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._pending = ""   # drop a trailing unclosed XML block
        if not self.started:
            return
        if self._md_pending:
            self._write(self._md_pending)   # flush an unclosed inline marker as text
            self._md_pending = ""
        if self.in_code:
            self.in_code = False
            self._flush_code_label()
        if not self.at_line_start:
            self._nl()

    def _flush_code_label(self):
        """Code buffered while waiting for a language newline is real code -
        never drop it (single-line blocks have no newline at all). Emits a
        clean dim '─ lang ─' marker instead of a literal ``` fence."""
        if self._code_label is None or not self._code_label.strip():
            self._code_label = None
            return
        lang = self._code_label.strip()
        self._code_label = None
        self._code_marker(lang)
        # content follows in the next feed() part

    # ---- content writers ----
    def _write_code(self, part):
        if self._code_label is not None:
            self._code_label += part
            if "\n" not in self._code_label:
                return
            label, rest = self._code_label.split("\n", 1)
            self._code_label = None
            self._code_marker(label.strip())
            part = rest
        raw = part.split("\n")
        if raw and raw[0] == "" and len(raw) > 1:
            # leading newline: break from previous line if mid-line
            if not self.at_line_start:
                self._nl()
            raw = raw[1:]
        end_nl = part.endswith("\n")
        if end_nl:
            raw = raw[:-1]   # drop trailing empty (real line break handled below)
        wrote = False
        for idx, piece in enumerate(raw):
            if idx > 0:
                self._nl()
            if piece:
                self._write(col(self.skin["code"], "  " + piece))   # indent code
                wrote = True
        if end_nl and wrote:
            self._nl()

    def _decorate(self, piece):
        """Line-level markdown: headings, horizontal rules, task checkboxes,
        bullets and blockquotes.

        Returns (styled_prefix_or_None, remainder_to_style). With colors off
        the line is returned untouched so raw markdown survives piped output.
        """
        if not COLOR:
            return None, piece
        s = self.skin
        hm = re.match(r"^#{1,6}\s+(.*)$", piece)
        if hm:  # '## Heading' -> bold accent heading
            return col(C.BOLD + s["agent"], hm.group(1)), ""
        if re.match(r"^\s*[-*_][-*_\s]{2,}$", piece):  # '---' / '***' / '___'
            return col(s["dim"], "─" * 20), ""
        cm = re.match(r"^\s*[-*]\s+\[([ xX])\]\s+(.*)$", piece)
        if cm:  # '- [x] done' / '- [ ] todo'
            mark = col(s["ok"], "✓ ") if cm.group(1) in ("x", "X") else col(s["dim"], "☐ ")
            return mark, cm.group(2)
        bm = re.match(r"^\s*[-*]\s+(.*)$", piece)
        if bm:  # '- item' -> accent bullet
            return col(s["accent"], "• "), bm.group(1)
        qm = re.match(r"^\s*>\s?(.*)$", piece)
        if qm:  # '> quote' -> bordered bar
            return col(s["border"], "│ "), qm.group(1)
        return None, piece

    def _write_inline(self, part):
        raw = part.split("\n")
        if raw and raw[0] == "" and len(raw) > 1:
            # leading newline: break from previous line if mid-line
            if not self.at_line_start:
                self._nl()
            raw = raw[1:]
        end_nl = part.endswith("\n")
        if end_nl:
            raw = raw[:-1]   # drop trailing empty (real line break handled below)
        wrote = False
        for idx, piece in enumerate(raw):
            if idx > 0:
                self._nl()
            if self._md_pending:
                piece = self._md_pending + piece   # finish the parked inline span
                self._md_pending = ""
            if piece:
                prefix, rest = self._decorate(piece)
                if prefix is not None:
                    self._write(prefix)
                rendered, parked = _md_line(rest, self.skin)
                if parked:
                    self._md_pending = parked
                if rendered:
                    self._write(rendered)
                if prefix is not None or rendered:
                    wrote = True
        if end_nl and wrote:
            self._nl()


def fmt_args(args):
    return ", ".join("%s=%r" % (k, v) for k, v in (args or {}).items())


def tool_summary(result):
    if not isinstance(result, dict):
        return str(result)[:80]
    if "result" in result:
        return str(result["result"])[:80]
    if "status" in result:
        return "HTTP %s | %s chars" % (result.get("status"), result.get("chars", "?"))
    if "exit" in result:
        snippet = (result.get("stdout") or result.get("stderr") or "").strip()
        return "exit %s%s" % (result.get("exit"), " | " + snippet[:60] if snippet else "")
    if "entries" in result:
        return "%d entries" % result.get("count", len(result.get("entries", [])))
    if "skills" in result:
        return "%d skills" % len(result.get("skills", []))
    if "path" in result and "content" in result:
        return "%s (%s chars)" % (result.get("path"), result.get("chars", 0))
    if "path" in result and result.get("ok") is True:
        return "%s [OK]" % result.get("path")
    if result.get("ok") is False:
        return str(result.get("error", "failed"))[:80]
    if "found" in result:
        return "found" if result.get("found") else "not found"
    if "matches" in result:
        return "%d matches" % result.get("count", len(result.get("matches", [])))
    if "count" in result:
        return "%d todos" % result.get("count", 0)
    if result.get("ok") is True:
        return "ok"
    return json.dumps(result)[:80]


class Spinner:
    """Tiny animated indicator; safe to start()/stop() repeatedly.

    The verb (message) can change live - 'thinking', 'streaming', 'running
    tools' - like the Hermes TUI's customizable busy verbs. stop() wakes the
    thread immediately (Event) so it can be called between every streamed
    chunk without stuttering, and disable() permanently silences it once real
    output starts streaming (otherwise the \r frames collide with the text).
    """

    def __init__(self, msg="thinking"):
        self.msg = msg
        self._stop = True
        self._dead = False
        self._wake = threading.Event()
        self._t = None
        self._lock = threading.Lock()

    def set_msg(self, msg):
        with self._lock:
            self.msg = msg

    def _run(self):
        frames = "|/-\\"
        i = 0
        while True:
            with self._lock:
                if self._dead or self._stop:
                    return
                msg = self.msg
            sys.stderr.write("\r" + msg + " " + frames[i % 4])
            sys.stderr.flush()  # \r frames must land now, not sit in the buffer
            i += 1
            self._wake.wait(0.12)
            self._wake.clear()

    def start(self):
        with self._lock:
            if self._dead or not self._stop:
                return
            self._stop = False
        self._wake.set()
        if self._t is None or not self._t.is_alive():
            self._t = threading.Thread(target=self._run, daemon=True)
            self._t.start()

    def stop(self):
        with self._lock:
            self._stop = True
        self._wake.set()
        if self._t is not None:
            self._t.join(timeout=0.5)
        if not self._dead:
            sys.stderr.write("\r" + " " * 30 + "\r")
            sys.stderr.flush()

    def disable(self):
        """Permanently stop drawing frames and clear the line (called once
        real output starts streaming, so \r frames can't collide with text)."""
        self.stop()          # clears the line while still "alive"
        with self._lock:
            self._dead = True


def tool_open(rt, name, args):
    """Compact tool line: '  ▸ name (args)' - dim, no full-width rule."""
    a = fmt_args(args)
    label = "▸ " + name + ((" (" + a + ")") if a else "")
    _tool_line(label, HERMES_DIM)


def tool_close(rt, name, status, result):
    """Close line of a tool block: '  ✓ name → summary'."""
    mark = ("✓ " if status == "done" else "✗ ")
    label = mark + name
    if result is not None:
        s = tool_summary(result).replace("\n", " ").replace("\r", " ").strip()
        s = " ".join(s.split())   # collapse runs of whitespace onto one line
        if s:
            label += " → " + s
    _tool_line(label, HERMES_OK if status == "done" else HERMES_ERR)


def on_tool(rt, tool_id, name, args, result, status):
    sp = rt.spinner
    if sp:
        sp.stop()
    if status == "running":
        tool_open(rt, name, args)
    else:
        tool_close(rt, name, status, result)
    if sp:
        sp.start()


def run_agent_tui(rt, history):
    """Run the agent loop with streaming output, spinner, and live tool blocks.

    Returns the 'done' payload augmented with:
      tools    - number of tool calls this turn
      elapsed  - wall-clock seconds for the turn
      streamed - whether any text was printed (so the caller can avoid
                 re-printing the answer, fixing the old double-print)
    """
    sp = Spinner("thinking")
    rt.spinner = sp
    sp.start()
    content_parts = []
    tool_count = 0
    writer = AgentWriter(CUR_SKIN, CUR_SKIN["agent"])
    t0 = time.monotonic()
    try:
        for evt_type, evt_data in run_agent_stream(rt, history):
            if evt_type == "text":
                sp.disable()   # kill the spinner BEFORE text lands (no \r collision)
                content_parts.append(evt_data)
                writer.feed(evt_data)
            elif evt_type == "tool_start":
                sp.disable()
                tool_count += 1
                tool_open(rt, evt_data["name"], evt_data["args"])
            elif evt_type == "tool_end":
                tool_close(rt, evt_data["name"], evt_data["status"], evt_data["result"])
            elif evt_type == "done":
                writer.close()
                res = dict(evt_data)
                res["tools"] = tool_count
                res["elapsed"] = time.monotonic() - t0
                res["streamed"] = bool(content_parts)
                return res
    finally:
        writer.close()
        sp.stop()
        rt.spinner = None
    return {"content": "".join(content_parts), "history": history, "cancelled": False,
            "tools": tool_count, "elapsed": time.monotonic() - t0,
            "streamed": bool(content_parts)}


# Block wordmark generated by `pyfiglet -f block "ALVAAGENT"` (tool output,
# not hand-authored) so it reads like the Hermes HERMES_AGENT_LOGO banner.
ALVA_WORDMARK = (
    "  _|_|    _|      _|      _|    _|_|      _|_|      _|_|_|  _|_|_|_|  \n"
    "_|    _|  _|      _|      _|  _|    _|  _|    _|  _|        _|        \n"
    "_|_|_|_|  _|      _|      _|  _|_|_|_|  _|_|_|_|  _|  _|_|  _|_|_|    \n"
    "_|    _|  _|        _|  _|    _|    _|  _|    _|  _|    _|  _|        \n"
    "_|    _|  _|_|_|_|    _|      _|    _|  _|    _|    _|_|_|  _|_|_|_|  \n"
    "                        \n"
    "_|      _|  _|_|_|_|_|  \n"
    "_|_|    _|      _|      \n"
    "_|  _|  _|      _|      \n"
    "_|    _|_|      _|      \n"
    "_|      _|      _|      \n"
)

# Toolset grouping for the banner grid (mirrors Hermes' per-category tool panel).
TOOLSETS = {
    "shell":   ["run_command", "run_python"],
    "files":   ["file_read", "file_write", "file_edit", "file_list", "file_search"],
    "skills":  ["skill_list", "skill_read", "skill_save", "skill_install", "skill_sync_repo"],
    "memory":  ["memory_save", "memory_recall", "memory_search", "memory_list"],
    "todos":   ["todo_add", "todo_list", "todo_toggle", "todo_remove"],
    "web":     ["web_fetch"],
    "system":  ["calculator", "get_time"],
}


def _markup_safe(s):
    """Strip Rich markup tag characters from user-controlled strings before
    they're interpolated into banner cells (a `[` in a provider/model/skill
    name would otherwise crash Rich's markup parser)."""
    return str(s).replace("[", "").replace("]", "")


def _banner_tools_lines(rt):
    """Hermes-style 'Available Tools' grid: active tools grouped by toolset,
    with a footer noting how many advanced tools are hidden in core mode.

    Returns Rich-markup strings (Hermes' own convention inside Panels).
    """
    active = set(t["function"]["name"] for t in visible(rt))
    lines = ["[bold %s]Available Tools[/]" % HERMES_ACCENT]
    for ts, names in TOOLSETS.items():
        shown = [n for n in names if n in active]
        if shown:
            lines.append("[dim %s]%s:[/] [bold %s]%s[/]"
                         % (HERMES_DIM, ts, HERMES_TEXT, ", ".join(shown)))
    hidden = len(TOOLS) - len(active)
    if hidden:
        lines.append("[dim %s]%d advanced tools hidden - /tools full shows them[/]" % (HERMES_DIM, hidden))
    return lines


def _banner_skills_lines(rt):
    """Hermes-style 'Available Skills' grid: skills grouped by category."""
    lines = ["", "[bold %s]Available Skills[/]" % HERMES_ACCENT]
    try:
        skills = skill_list(rt).get("skills") or []
    except Exception:
        skills = []
    if not skills:
        lines.append("[dim %s]saved: (none yet - ask the agent to save one)[/]" % HERMES_DIM)
        return lines
    # group by category
    by_cat = {}
    for s in skills:
        cat = s.get("category") or "(flat)"
        by_cat.setdefault(cat, []).append(s)
    disp_name = lambda c: "flat" if c == "(flat)" else c
    for cat in sorted(by_cat):
        names = [_markup_safe(s["name"]) for s in by_cat[cat]]
        lines.append("[dim %s]%s:[/] [bold %s]%s[/]"
                     % (HERMES_DIM, disp_name(cat), HERMES_TEXT, ", ".join(names)))
    return lines


def banner(rt):
    """Hermes-banner style: block wordmark + bronze panel with tools/skills grid.

    Mirrors hermes_cli/banner.build_welcome_banner: a large block wordmark on
    top, then a bordered panel whose LEFT is model/provider/skin meta and RIGHT
    is an 'Available Tools' / 'Available Skills' grid, laid out with a real Rich
    two-column grid (exactly how Hermes aligns its banner). Colors use the fixed
    Hermes palette (gold accent, bronze border) regardless of /skin. The wordmark
    is pyfiglet output; grid cells use Rich markup tags (Hermes' own convention).
    NOTE: Table is imported lazily (inside try/except) so the banner still
    renders when rich is absent — the module-level fallback shim covers
    Console/Panel only.
    """
    cfg = active_cfg(rt)
    _CON.print()  # top spacer like Hermes
    # Hermes only prints its big wordmark when the terminal is wide enough; on
    # narrow terminals (phones) it would wrap ugly, so we mirror that gate.
    # NOTE: use plain print() + raw ANSI here (not _CON.print) — the rest of the
    # TUI renders color via print()+_fgh(), and Rich's Console leaks bare escapes.
    term_w = _term_width()
    if COLOR and term_w >= 72:
        for ln in ALVA_WORDMARK.split("\n"):
            if ln.strip():
                print(_fgh(HERMES_ACCENT) + ln + _rsth())
    elif COLOR:
        print(_fgh(HERMES_ACCENT) + C.BOLD + "ALVAAGENT" + _rsth())
    else:
        print("ALVAAGENT")
    print()

    model_short = _markup_safe(cfg.get("model") or "?")
    if "/" in model_short:
        model_short = model_short.split("/")[-1]
    ctx = _fmt_k(context_window_for(cfg))
    # Left meta column (Hermes banner left side): model · context · provider.
    left_lines = [
        "",
        "[bold %s]%s[/]  [dim %s]·[/] [dim %s]%s context[/]"
        % (HERMES_ACCENT, model_short, HERMES_DIM, HERMES_DIM, ctx),
        "[dim %s]skin[/] %s" % (HERMES_DIM, rt.skin or DEFAULT_SKIN),
        "[dim %s]provider[/] %s" % (HERMES_DIM, _markup_safe(rt.cfg.get("active", "local"))),
        "[dim %s]config/store:[/] %s" % (HERMES_DIM, _markup_safe(rt.data_dir)),
    ]
    right_lines = _banner_tools_lines(rt) + _banner_skills_lines(rt)
    right_lines.append("")
    right_lines.append("[dim %s]%d/%d tools (%s) · v%s · you are here · /help for commands[/]"
                       % (HERMES_DIM, len(visible(rt)), len(TOOLS), rt.tool_mode, ALVA_VERSION))

    try:
        from rich.table import Table
        grid = Table.grid(padding=(0, 2))
        grid.add_column("left", justify="left")
        grid.add_column("right", justify="left")
        for i in range(max(len(left_lines), len(right_lines))):
            l = left_lines[i] if i < len(left_lines) else ""
            r = right_lines[i] if i < len(right_lines) else ""
            grid.add_row(l, r)
        panel_body = grid
    except Exception:
        # Rich Table unavailable — fall back to a single stacked block.
        panel_body = "\n".join(left_lines + [""] + right_lines)

    _CON.print(Panel(
        panel_body,
        title="[bold %s]%s v%s[/]" % (HERMES_ACCENT, "⚕ alvaagent", ALVA_VERSION),
        border_style=HERMES_BORDER,
        padding=(0, 2),
    ))
    print("  " + col(C.DIM, "type a message | /help lists commands | Tab completes /commands"))
    print()
    if not cfg.get("api_key"):
        p_info("no API key set for '%s' - run /provider %s or /config"
               % (rt.cfg.get("active", "local"), rt.cfg.get("active", "local")))


def render_status_bar(rt, session, elapsed, tools, history):
    """Render a one-line status footer after each agent turn (Hermes-style).

    Uses normal print flow - no raw ANSI cursor jumps, which misalign on
    Termux (no reliable terminal height). Prints a dim, boxed-style line.
    """
    cfg = active_cfg(rt)
    skin = CUR_SKIN
    tokens, window = context_usage(rt, history)
    pct = tokens * 100 // window if window else 0
    ctx_col = skin["ok"] if pct < 60 else (C.YELLOW if pct < 85 else skin["err"])
    parts = [
        col(skin["dim"], session[:16]),
        col(skin["dim"], rt.cfg.get("active", "local") + "/" + (cfg.get("model") or "?")),
        col(ctx_col, "ctx %d%%" % pct),
        col(skin["dim"], "%.1fs" % elapsed),
        col(skin["dim"], "%d tool calls" % (tools or 0)),
    ]
    # Hermes-style footer: dim '│' prefix + space-separated chips.
    print(col(C.DIM, "  " + "│".join([""] + parts)))


def compress_now(rt, history, threshold=0.75, force=False):
    """If usage exceeds the threshold (or force=True), summarize older messages
    in place. Returns True when a compression happened; never raises on failure."""
    cfg = rt.active_cfg
    tokens, window = context_usage(rt, history)
    if window <= 0:
        p_info("(no context window configured)")
        return False
    if not force and tokens <= int(window * threshold):
        return False
    p_info("context %d%% of %s - compressing older messages..."
           % (tokens * 100 // window, _fmt_k(window)))
    sp = Spinner("compressing")
    rt.spinner = sp
    sp.start()
    try:
        new, stats = compress_history(rt, history, cfg)
    except KeyboardInterrupt:
        p_info("compression cancelled")
        return False
    except Exception as e:
        p_info("compression failed: %s" % e)
        return False
    finally:
        sp.stop()
        rt.spinner = None
    if not stats:
        if tokens > int(window * 0.6):
            p_info("(nothing to compress - a single message dominates the window; consider /new)")
        else:
            p_info("(nothing to compress)")
        return False
    history[:] = new
    p_ok("[OK] context compressed | %d earlier message%s -> summary"
         % (stats["dropped"], "" if stats["dropped"] == 1 else "s"))
    if stats.get("mode") == "fallback":
        p_info("  (offline summary - the model call failed, kept a basic note)")
    return True

