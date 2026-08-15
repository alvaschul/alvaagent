# Scrollable chat inside the alternate screen

Date: 2026-08-15

## Problem

The TUI runs in the terminal's alternate screen buffer (clean full-screen takeover, like
Hermes). The alternate buffer has no scrollback, so chat lines that scroll off the top are
unreachable: touch-swipe / mouse-wheel / PageUp do nothing on the live chat. A `/scroll`
pager command exists but the user wants to scroll the screen directly, not type a command.

On Termux/Android, touch-swipe is translated into wheel events (`SGR` button 64/65) only
when mouse tracking is enabled in the app; the terminal's own buffer scroll cannot move an
alt-screen view. Therefore the app must do its own scrolling and re-rendering.

## Approach

Keep the alternate screen. Give the app a captured copy of what it printed and a custom
input reader so it can detect swipes/wheel while at the prompt, then re-render a
conversation view inside the alt screen. This mirrors how Hermes-style TUIs work
(alternate-screen rendering + wheel mouse mode `1000+1006`).

## Components

### 1. `StreamTee` (new, `alvaagent/scrollback.py`)

Wraps `sys.stdout` with a write proxy. Records each completed line (text including ANSI
SGR color codes) into a bounded deque plus one partial "current" line.

- Split on `\n`. stdout carries no `\r` redraws or cursor-position sequences (verified: the
  spinner writes `\r` frames to `stderr` only), so line splitting is byte-faithful.
- Drop captured lines whose text starts with `\x1b[?` (DEC private-mode codes for the
  alternate screen and mouse modes) so they are never replayed.
- Exposes:
  - `captured_lines()` -> list[str]
  - `restore()`: emits `\x1b[2J\x1b[H`, replays captured lines in order, then the caller
    reprints the prompt.
  - Install before the chat loop starts (after the `?1049h` enter-code is emitted) and
    uninstall on exit.

### 2. `LineReader` (new, `alvaagent/scrollback.py`)

Replaces `input(prompt)` at the main chat prompt only. Puts stdin in cbreak raw mode
(`termios`, echo off), reads bytes via `os.read`, echoes with the styled skin prompt.
Restores terminal modes before returning. Semantics match `input()`: returns `str` on
Enter, raises `EOFError` on Ctrl+D (empty line) and `KeyboardInterrupt` on Ctrl+C, so the
REPL's existing `except` blocks work unchanged.

Key handling:

- Printable chars: append + echo (prompt re-rendered on one line).
- Enter/Return (`\r`, `\n`): commit the line.
- Backspace (`\x7f`/`\x08`): delete last char, re-echo.
- Ctrl+C (`\x03`): raise `KeyboardInterrupt`.
- Ctrl+D (`\x04`): empty line -> raise `EOFError`; else commit the line.
- Ctrl+L (`\x0c`): re-draw the current line.
- Up/Down (`\x1b[A`/`\x1b[B`): navigate persisted history (loaded from `HISTORY_PATH` via
  the existing `setup_completion()` readline bridge; committed lines are added with
  `readline.add_history()` so `save_completion_history()` persists them as today).
- Tab (`\t`): complete slash commands using the existing `_slash_complete(text, state)`
  function (cycle matches on repeated Tab).
- SGR mouse sequences (`\x1b[<b;c;rM` / `...m`): parse; wheel-up (b=64) / wheel-down
  (b=65) trigger scroll; taps on the scroll-view footer zones scroll/return.
- All other escape sequences: ignored.

### 3. Scroll mode (`ScrollView`, new, `alvaagent/scrollback.py`)

Triggered by wheel-up (swipe up) while idle at the prompt.

- Source of truth: the current session `history` list (role user/assistant/tool messages).
  Rendered as display lines (wrapped to `shutil.get_terminal_size().columns`) with the
  role markers used by `/export` (`## you`, `## agent`, `## summary (compressed)`,
  `## tool (id)`), plain text, no external pager, no nested alternate screen.
- On enter: `\x1b[2J\x1b[H`, render pages of the display lines; the visible window is
  `rows - 2` (footer + spacer). Footer (dim): `◀ older · page p/n · ▼ newer · ⏎ return`.
- Wheel-up / swipe-up / tap on left footer zone / PgUp: next (older) page.
- Wheel-down / swipe-down / tap on right footer zone / PgDn: previous (newer) page.
- At the newest page, wheel-down / swipe-down / Enter / tap the return zone: exit scroll
  mode, `StreamTee.restore()` re-plays the captured live lines, and the prompt is
  re-printed at the bottom.
- Enter scroll mode is a no-op when the conversation is empty.

### 4. Wiring (`alvaagent/repl.py`)

- Re-add `\x1b[?1049h` on launch and `\x1b[?1049l` in `_cleanup` (revert the
  no-alternate-screen change from this session).
- After the banner, enable mouse tracking: `\x1b[?1000h\x1b[?1002h\x1b[?1006h`
  (`1002` cell-motion makes Termux deliver touch-swipes as wheel events; `1006` SGR
  coordinates). Disable all three in `_cleanup`.
- Install `StreamTee` immediately after the `?1049h` enter-code so startup control codes
  are never captured.
- Replace the main `input(prompt)` call in the REPL loop with the `LineReader`.
- All other prompts (`ask_permission`, `pick_model`, `/multi`, y/N confirms) keep plain
  `input()`; they run mid-turn where scrolling is not required.

## Testing

TDD, stdlib only (no pytest):

- `StreamTee`: captures completed lines; partial last line exposed; `\x1b[?` lines
  filtered; `restore()` output equals the captured content.
- Mouse parser: wheel-up / wheel-down / press / release / tap coordinates.
- Scroll view: page math (window size, page count), wrapping to width, empty history
  no-op, footer text, "return from newest page" condition.
- `LineReader` via PTY: typing + Enter returns the line; Ctrl+C raises
  `KeyboardInterrupt`; Ctrl+D raises `EOFError`; Up-arrow recalls history; Tab completes
  `/he` -> `/help`.
- End-to-end via PTY: seeded session store -> boot app -> feed wheel-up bytes
  (`\x1b[<64;1;1M`) -> older conversation rendered with footer -> feed Enter -> live
  prompt restored -> `/exit` exits cleanly (returncode 0).
- Revert the `test_cli_smoke` "no `?1049` escape" assertion added earlier (the alt screen
  is intentionally back).
- Full suite must print `ALL TESTS PASSED ✓`; pyflakes clean on changed files.

## Files

- New: `alvaagent/scrollback.py`
- Edit: `alvaagent/repl.py`, `alvaagent/tui.py` (only if a render helper needs to live
  there), `test_tui.py`
- Edit: `README.md` (document swipe/wheel scrolling)
