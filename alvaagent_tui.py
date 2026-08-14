#!/usr/bin/env python3
# ============================================================
#  alvaagent_tui.py - alvaagent terminal chat client
#
#  Same agent harness ported to plain Python so it runs entirely inside
#  Termux - no browser, no web server, nothing to disconnect when you
#  switch apps.
#
#  Uses only the Python standard library plus `rich` (pure-Python, pip-installs
#  on Termux - see alva_fix.sh). `rich` backs the Hermes-style panels.
#
#  Run:
#    python3 alvaagent_tui.py     (or:  bash start.sh tui)
#
#  Providers: each setup (/provider <name>) is saved as its own named profile
#  in ./.alvaagent/config.json next to this script (e.g. on Android shared
#  storage); /provider <name> adds a new profile or switches to an existing
#  one. Env vars ALVA_BASE_URL, ALVA_API_KEY, ALVA_MODEL, ALVA_TEMPERATURE
#  (POCKET_* names also still accepted) override the ACTIVE profile at start.
#
#  Todos & memory facts persist to ./.alvaagent/store.json next to this
#  script (override the folder with ALVA_DATA_DIR).
#
#  Commands:
#    /help /config /provider /models /test /tools /trace /todos /todo /memory
#    /skin /sessions /session /new /clear /context /compress /multi /export
#    /stop /exit /quit
#
#  Tool selection: by default the model sees a curated CORE tool set (~15);
#  /tools full advertises all tools, and any advanced tool call auto-enables
#  full mode. The choice persists in config.json ("tool_mode").
#    Ctrl+C cancels a running request | Tab completes slash commands
#    (at the api key prompt, type 'none' to clear the key)
#
#  Sessions: conversations are saved to store.json and resumed on restart.
#    /sessions lists them | /session <name> switches/creates | /new starts fresh.
#  Context: the footer shows a live ctx meter (est. tokens / model window) and
#    auto-compresses older messages into a summary near the limit so long chats
#    don't drift out of context. /context shows the numbers | /compress forces it.
#  Skins: /skin lists & switches the color theme (midnight | ember | ocean |
#  daylight). Skins persist to config.json. The layout echoes the Hermes agent
#  TUI (banner + bordered message blocks + tool blocks + status chips) but with
#  its own palettes, a footer status line instead of a persistent bottom bar,
#  tab-completion instead of a dropdown, and the alvaagent ⚡ brand.
#
#  Note: single-line input - a multi-line paste submits only its first
#  line (the soft keyboard's Enter sends each line).
# ============================================================
import codecs
import datetime
import html
import json
import os
import re
import readline
import secrets
import select
import shutil
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

# PyYAML is OPTIONAL: it powers full YAML parsing of skill frontmatter, but the
# harness ships a tiny fallback parser/serializer for the simple key:value +
# list format it writes, so the TUI stays runnable with zero pip installs
# (stdlib only, as the README promises).
try:
    import yaml
except Exception:
    yaml = None

# Rich backs the Hermes-style panels (pure-Python, pip-installs on Termux).
# The Hermes agent TUI renders with Rich `Panel(box=HORIZONTALS)`; we mirror
# that exactly so alvaagent reads as Hermes. `pip install --break-system-packages
# rich` is run by alva_fix.sh; if it's somehow absent we fall back to a tiny
# ANSI shim so the TUI still launches.
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

from alvaagent.util import (  # noqa: E402,F401
    _env, now_iso, _fmt_k, _atomic_write, _looks_like_html, _raw_fetch,
    mask_key, _parse_frontmatter, _frontmatter_load, _frontmatter_dump,
    _mini_yaml, _mini_scalar, _finish_block, _cancel_flag,
)

# paths / config moved to alvaagent/config.py (Task 3)
from alvaagent.config import (  # noqa: E402,F401
    data_dir, DATA_DIR, _LEGACY_DIRS, CONFIG_PATH, STORE_PATH, HISTORY_PATH,
    TRACE_PATH, PROVIDERS, DEFAULT_CFG, FIRST_RUN_CFG, DEFAULT_SKIN,
    SKIN_NAMES, ALVA_VERSION, DEFAULT_CONTEXT_WINDOW, MODEL_CONTEXT,
    TOOL_MODES, _tool_mode_of, _skin_of, _normalize_state, load_state,
    save_state, active_cfg,
)

# store moved to alvaagent/store.py (Task 4)
from alvaagent.store import (  # noqa: E402,F401
    _store, _migrate_legacy_dir, _load_store, _save_store,
    _store_get, _store_set, TODO_KEY, MEM_PREFIX, FEEDBACK_KEY,
    IMPROVEMENT_KEY, HISTORY_KEY, SESSION_KEY, ACTIVE_SESSION_KEY, MAX_SESSIONS,
)

# ---------------- autonomy: permissions ----------------
# (classifiers moved to alvaagent/permissions.py (Task 5))
from alvaagent.permissions import (  # noqa: E402,F401
    _READONLY_PREFIXES, _RISKY_TOKENS, _RISKY_OPERATORS, _tokenize_shell,
    classify_command, PROJECT_DIR, _in_project, classify_file_action,
    _APPROVED_SET, _permission, ON_PERMISSION,
)

# tools moved to alvaagent/tools.py (Task 7)
import alvaagent.tools as _tools  # noqa: E402
from alvaagent.tools import (  # noqa: E402,F401
    _PY_RUN_TIMEOUT, _PY_MAX_BYTES, _PY_MAX_CHARS, _CALC_ALLOWED,
    _CORE_TOOL_NAMES, _ADVANCED_TOOL_NAMES,
    active_tools, _maybe_enable_full, _set_tool_mode, _sync_tool_mode,
    tool_run_command, tool_file_read, tool_file_write, tool_file_edit,
    tool_file_list, tool_file_search, tool_todo_list, tool_todo_add,
    tool_todo_toggle, tool_todo_remove, tool_memory_save, tool_memory_recall,
    tool_memory_list, tool_memory_search, tool_get_time, tool_feedback,
    tool_improvement_set, tool_improvement_list, tool_improvement_done,
    tool_reflect, tool_web_fetch, _safe_factorial, _calc_eval, _fmt_num,
    tool_calculator, classify_python, tool_run_python,
    TOOLS, TOOL_IMPL, _TOOL_ERROR_HINTS, dispatch_tool, self_test, tool_self_test,
)

# skills moved to alvaagent/skills.py (Task 6)
from alvaagent.skills import (  # noqa: E402,F401
    _SKILL_FM_RE, _SKILL_FM_DEFAULT, _VALID_FM_KEYS, _SKILL_RAW_MAX,
    _skill_body_for_tool, _detect_category, _skill_filepath, _inside_skills,
    _resolve_skill_path, _skill_read, _scan_skill_files, _skill_list_all,
    tool_skill_list, tool_skill_read, tool_skill_remove, tool_skill_save,
    tool_skill_install, tool_skill_sync_repo,
)
# ---------------- LLM client (OpenAI-compatible) ----------------
SYSTEM_PROMPT = """You are alvaagent, a helpful AI agent running on the user's Android device (Termux / proot).
You can call tools to do real work. Guidelines:
1. Use the calculator tool for ANY arithmetic - never guess math.
2. Use web_fetch to read a webpage when the user asks about online content.
3. Use memory_save / memory_recall to remember facts the user asks you to
   remember; memory_search / memory_list find facts when the exact key is unknown.
4. Use todo_add / todo_list / todo_toggle / todo_remove to manage the user's to-do list.
5. Use get_time when the user needs the current date or time.
6. You have real device access: run_command runs shell commands (Termux), and
   file_read / file_write / file_edit / file_list / file_search work on the
   device's files.
   Read-only commands and in-project file edits run freely; mutating/unknown
   commands or out-of-project writes ask the user first - if denied, do not
   retry, and explain what was blocked and why.
7. Skills: skill_list / skill_read / skill_save manage reusable procedures
   stored on the device. BEFORE starting any substantial task, call skill_list
   and read any skill whose name matches the task. Apply the skill's guidance
   faithfully - a skill is the user's preferred way of doing that kind of work.
   When you discover a reusable, non-obvious procedure during a task, save it
   as a skill with a descriptive name and a concise body (trigger + steps).
   Keep skills small and self-contained so they stay easy to apply and test.
   When the user shares a skill as a link or file, install it with
   skill_install (single .md from a URL or path); when they hand you a whole
   skills repo, use skill_sync_repo (clones it, permission-gated, imports every
   .md with folder names as categories).
 8. Self-improvement: you can read your OWN source (alvaagent_tui.py,
    start.sh, test_tui.py) and improve it with file_edit / file_write, then
    validate with run_command("python3 -m py_compile alvaagent_tui.py") and
    run_command("python3 test_tui.py"). After any edit to your own source,
    ALWAYS run self_test to confirm nothing is broken before you tell the user
    the change is done. Changes take effect the next time the user restarts the
    TUI - always say so, and keep edits small, targeted, and tested.
    LEAVE NOTHING BEHIND: clean up every file you create while working - put
    scratch/exploratory files in /tmp and delete them after, remove any test
    skill/todo/file you made, and finish by running git status to confirm only
    your intended changes remain. Do not commit session junk (.opencode/,
    opencode.json) or runtime state (.alvaagent/config.json, store.json,
    cmd_history.txt). Ask the user before committing any skill or script you
    created only to explore or test.
9. Feedback loop: the user can rate your responses with /feedback good|bad.
   When you notice the user expressing satisfaction or frustration, call
   feedback(rating="good"|"bad"|"neutral", notes=...) so the harness records
   it. Periodically call reflect() to review recent feedback and pending
   improvements, and improvement_set(area, action) when a pattern emerges.
   Mark improvements done with improvement_done(area) after you verify the fix.
   Treat repeated "bad" feedback on the same thing as a real bug to fix.
10. Format your answers with light markdown for readability: **bold** for key
    terms, *italic* for emphasis, `code` for commands and file paths, ## headings
    for structure, and - bullets for lists. Keep it light - never wrap tool
    outputs or file contents in emphasis; real code belongs in ``` fences.
Only call a tool when it genuinely helps. If no tool is needed, answer directly.
Respond in the same language the user writes in. Be concise, friendly, and precise."""


def _readable_error(status, text):
    """Best-effort readable message from an API error body (JSON or HTML).

    Gateways/WAFs often return HTML error pages whose <title> says exactly
    what's blocked (Cloudflare, nginx, ...); proxies sometimes wrap upstream
    failures as JSON like {"error": {"message": "[403]: <html>..."}}.
    """
    msg = ""
    try:
        data = json.loads(text)
        err = data.get("error", {})
        msg = err.get("message", str(data)[:200]) if isinstance(err, dict) else str(err)
    except Exception:
        msg = text[:300]
    if not msg:
        return "HTTP %s" % status
    # drop redundant "[403]: ..." style prefixes (gateway-wrapped upstream errors)
    m = re.match(r"^\s*\[\s*\d+\s*\]\s*:\s*", msg)
    if m:
        msg = msg[m.end():]
    # HTML pages: prefer the <title>, else the stripped text
    if re.search(r"(?is)<(title|h1)", msg):
        for tag in ("title", "h1"):
            tm = re.search(r"(?is)<%s[^>]*>(.*?)</%s>" % (tag, tag), msg)
            if tm:
                t = re.sub(r"(?is)<[^>]+>", " ", tm.group(1)).strip()
                if t:
                    return "blocked by error page: %s" % t[:160]
        plain = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", msg)
        plain = re.sub(r"(?is)<[^>]+>", " ", plain)
        plain = re.sub(r"\s+", " ", plain).strip()
        if plain:
            return plain[:200]
    return msg.strip()[:300]


_MAX_RETRIES = 3
_RETRY_BACKOFF = (0.5, 1.5, 4.0)  # seconds between retries
_STREAM_IDLE_LIMIT = 90.0         # seconds with no bytes before a stream is treated as stalled
_STREAM_POLL = 0.25               # cancel-check interval while waiting on a stream socket


def _retryable_status(code):
    """Status codes worth retrying (rate limits + transient server errors)."""
    return code in (408, 409, 429) or code >= 500


def _sleep_retry(attempt):
    time.sleep(_RETRY_BACKOFF[min(attempt - 1, len(_RETRY_BACKOFF) - 1)])


class _Cancelled(Exception):
    """Raised when the user cancels mid-stream (propagates to the caller as a stop)."""


def chat_completion(messages, config, tools=None):
    base = (config.get("base_url") or "").rstrip("/")
    url = base + "/chat/completions"
    payload = {
        "model": config.get("model") or "gpt-4o-mini",
        "messages": messages,
        "temperature": float(config.get("temperature") if config.get("temperature") is not None else 0.7),
        "stream": False,  # some gateways default to SSE; we want a plain JSON reply
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept-Encoding": "identity",
            "Authorization": "Bearer " + (config.get("api_key") or ""),
        },
    )
    last_err = None
    for attempt in range(_MAX_RETRIES + 1):
        if attempt:
            _sleep_retry(attempt)
        if _cancel_flag[0]:
            raise RuntimeError("LLM request cancelled by user")
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                status = int(r.getcode())
                text = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            status = int(e.code)
            text = e.read().decode("utf-8", errors="replace")
            if _retryable_status(status):
                last_err = "HTTP %s" % status
                continue
        except urllib.error.URLError as e:
            last_err = "LLM API unreachable: %s" % e.reason
            continue
        except Exception as e:
            last_err = "LLM request failed: %s" % e
            continue
        try:
            data = json.loads(text)
        except Exception:
            if status >= 500:
                last_err = "API returned non-JSON (HTTP %s)" % status
                continue
            raise RuntimeError("API returned non-JSON (HTTP %s): %s" % (status, _readable_error(status, text)))
        if status >= 400 or "error" in data:
            if _retryable_status(status):
                last_err = "LLM API error %s" % status
                continue
            raise RuntimeError("LLM API error %s: %s" % (status, _readable_error(status, text)))
        if not data.get("choices"):
            raise RuntimeError("LLM API returned no choices")
        return data
    raise RuntimeError("LLM request failed after %d attempts: %s" % (_MAX_RETRIES + 1, last_err))


def chat_completion_stream(messages, config, tools=None):
    """Streaming version of chat_completion. Yields (content_chunk, tool_calls_json_or_None)."""
    base = (config.get("base_url") or "").rstrip("/")
    url = base + "/chat/completions"
    payload = {
        "model": config.get("model") or "gpt-4o-mini",
        "messages": messages,
        "temperature": float(config.get("temperature") if config.get("temperature") is not None else 0.7),
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept-Encoding": "identity",
            "Authorization": "Bearer " + (config.get("api_key") or ""),
        },
    )
    resp = None
    for attempt in range(_MAX_RETRIES + 1):
        if attempt:
            _sleep_retry(attempt)
        if _cancel_flag[0]:
            raise _Cancelled()
        try:
            resp = urllib.request.urlopen(req, timeout=60)
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if _retryable_status(e.code) and attempt < _MAX_RETRIES:
                continue
            raise RuntimeError("LLM API error %s: %s" % (e.code, _readable_error(e.code, body)))
        except urllib.error.URLError as e:
            if attempt < _MAX_RETRIES:
                continue
            raise RuntimeError("LLM API unreachable: %s" % e.reason)
        except Exception as e:
            if attempt < _MAX_RETRIES:
                continue
            raise RuntimeError("LLM request failed: %s" % e)
    try:
        sock = resp.fileno()
    except Exception:
        sock = None  # non-socket response (tests/fakes): fall back to blocking reads
    buffer = ""
    raw_parts = []          # full raw response body, for the plain-JSON fallback
    saw_sse = False         # True once a real "data:" SSE line is processed
    tool_calls_acc = {}
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    last_byte_at = time.monotonic()
    while True:
        # Poll the socket so Ctrl+C (cancel) is honored within ~_STREAM_POLL
        # even when the server stalls, and fail a dead link after
        # _STREAM_IDLE_LIMIT seconds of silence instead of hanging for the
        # full socket timeout.
        if sock is not None:
            while True:
                if _cancel_flag[0]:
                    resp.close()
                    raise _Cancelled()
                rlist, _, _ = select.select([sock], [], [], _STREAM_POLL)
                if rlist:
                    break
                if time.monotonic() - last_byte_at > _STREAM_IDLE_LIMIT:
                    resp.close()
                    raise RuntimeError("LLM stream stalled (no data for %ds)" % _STREAM_IDLE_LIMIT)
        elif _cancel_flag[0]:
            resp.close()
            raise _Cancelled()
        chunk = resp.read(1024)
        if not chunk:
            break
        last_byte_at = time.monotonic()
        raw_parts.append(chunk)
        buffer += decoder.decode(chunk)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("data: "):
                saw_sse = True
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    return
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choices = data.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                content = delta.get("content") or ""
                if content:
                    yield content, None
                tc = delta.get("tool_calls") or []
                for tcc in tc:
                    idx = tcc.get("index", 0)
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {"id": "", "function": {"name": "", "arguments": ""}}
                    acc = tool_calls_acc[idx]
                    if tcc.get("id") and not acc["id"]:
                        acc["id"] = tcc["id"]
                    # Some OpenAI-compatible gateways omit the tool_call id in the
                    # streamed deltas. Fall back to a stable synthetic id so the
                    # assistant tool_calls and the resulting tool message stay
                    # paired (the API rejects empty/missing tool_call_id).
                    if not acc["id"]:
                        acc["id"] = "call_%d" % idx
                    fn = tcc.get("function") or {}
                    if fn.get("name"):
                        acc["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        acc["function"]["arguments"] += fn["arguments"]
                finish = choices[0].get("finish_reason")
                if finish == "tool_calls":
                    tc_list = []
                    for idx in sorted(tool_calls_acc):
                        tc_list.append({"id": tool_calls_acc[idx]["id"],
                                        "type": "function",
                                        "function": tool_calls_acc[idx]["function"]})
                    yield "", tc_list
                    return
                if finish == "stop":
                    return
                if finish == "length":
                    return
    # Fallback: some gateways/proxies ignore "stream": true and answer with a
    # plain JSON completion instead of SSE lines (minified or pretty-printed).
    # If no real SSE "data:" line arrived, parse the whole raw body directly so
    # responses still render. (This must use the raw bytes: the incremental
    # decoder can't be flushed with a str, and the line loop above drains
    # pretty-printed JSON, so `buffer` alone would be empty and the reply would
    # be silently lost.)
    if not saw_sse and not tool_calls_acc:
        body = b"".join(raw_parts).decode("utf-8", errors="replace").strip()
        if body:
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return
            choices = data.get("choices") or []
            if not choices:
                return
            msg = choices[0].get("message", {})
            content = msg.get("content") or ""
            if content:
                yield content, None
            tc = msg.get("tool_calls") or []
            if tc:
                yield "", tc
    return


def fetch_models(base_url, api_key, timeout=20):
    """GET {base}/models and return the list of model ids (raises on failure)."""
    base = (base_url or "").rstrip("/")
    if not base:
        raise RuntimeError("no base url configured")
    req = urllib.request.Request(
        base + "/models",
        headers={"Authorization": "Bearer " + (api_key or ""), "Accept-Encoding": "identity"})
    last_err = None
    for attempt in range(_MAX_RETRIES + 1):
        if attempt:
            _sleep_retry(attempt)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace"))
            return [str(m["id"]) for m in (data.get("data") or [])
                    if isinstance(m, dict) and m.get("id")]
        except urllib.error.HTTPError as e:
            if _retryable_status(e.code) and attempt < _MAX_RETRIES:
                last_err = "HTTP %s" % e.code
                continue
            raise RuntimeError("models endpoint: HTTP %s" % e.code)
        except urllib.error.URLError as e:
            last_err = "unreachable: %s" % e.reason
            if attempt >= _MAX_RETRIES:
                raise RuntimeError("models endpoint %s" % last_err)
        except Exception as e:
            last_err = str(e)
            if attempt >= _MAX_RETRIES:
                raise RuntimeError("models endpoint failed: %s" % e)
    raise RuntimeError("models endpoint failed after %d attempts: %s"
                       % (_MAX_RETRIES + 1, last_err))


# ---------------- agent loop ----------------
MAX_STEPS = 25
_TURN_TIMEOUT = 180
_MAX_CONSEC_TOOL_FAILURES = 4
ON_TOOL = None  # optional hook: ON_TOOL(tool_id, name, args, result, status)


def cancel_agent():
    _cancel_flag[0] = True


def _repair_tool_pairs(history):
    """Heal persisted history so every role:"tool" message has a tool_call_id.

    Older sessions saved by a buggy build dropped tool_call_id from tool
    messages, which makes the OpenAI-compatible API reject the request
    (400: missing field toolcallid). Walk the history and, for any tool
    message missing tool_call_id, attach the id of the preceding assistant
    tool_call (or a synthetic id as a last resort). Returns a new list.
    """
    if not isinstance(history, list):
        return history
    out = []
    pending_ids = []
    for m in history:
        if not isinstance(m, dict):
            out.append(m)
            continue
        m = dict(m)  # don't mutate the caller's dict
        role = m.get("role")
        if role == "assistant":
            tcs = m.get("tool_calls") or []
            # Heal assistant tool_calls that have an empty/missing id (old
            # buggy streaming builds emitted id=""), so the following tool
            # message can be paired to a valid id.
            for i, tc in enumerate(tcs):
                if isinstance(tc, dict) and not tc.get("id"):
                    tc["id"] = "repaired_a%d" % (len(out) * 10 + i)
            ids = [tc.get("id") for tc in tcs if isinstance(tc, dict) and tc.get("id")]
            pending_ids = ids
            out.append(m)
        elif role == "tool":
            if not m.get("tool_call_id"):
                if pending_ids:
                    m["tool_call_id"] = pending_ids.pop(0)
                else:
                    # orphan tool result with no preceding call - synthesize
                    m["tool_call_id"] = "repaired_%d" % len(out)
            elif m["tool_call_id"] == "":
                # previously emitted empty id - keep a stable synthetic one
                m["tool_call_id"] = "repaired_%d" % len(out)
            out.append(m)
        else:
            out.append(m)
    return out

# trace moved to alvaagent/trace.py (Task 3)
from alvaagent.trace import (  # noqa: E402,F401
    _trace, _read_trace, _trace_count, _TRACE_MAX_LINES, _TRACE_MAX_BYTES,
)


def _report_tool(tool_id, name, args, result, status):
    if ON_TOOL is not None:
        try:
            ON_TOOL(tool_id, name, args, result, status)
        except Exception:
            pass


def run_agent(history_json, config_json):
    history = json.loads(str(history_json))
    config = json.loads(str(config_json))
    _cancel_flag[0] = False
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    history = _repair_tool_pairs(history)
    for m in history:
        if m.get("role") == "system":
            continue
        # Copy the full message dict so tool messages keep their tool_call_id
        # and assistant messages keep their tool_calls (required by the API).
        if not isinstance(m, dict):
            continue
        messages.append(dict(m))

    consec_failures = 0
    _t0 = time.monotonic()
    _trace({"event": "turn_start", "steps": 0})
    for step in range(MAX_STEPS):
        if _cancel_flag[0]:
            _trace({"event": "turn_end", "reason": "cancelled", "steps": step})
            return json.dumps({"content": "(stopped by user)", "history": messages, "cancelled": True})
        if _TURN_TIMEOUT <= 0 or time.monotonic() - _t0 > _TURN_TIMEOUT:
            note = "(stopped: the turn exceeded the %d-second time budget)" % int(_TURN_TIMEOUT)
            messages.append({"role": "assistant", "content": note})
            _trace({"event": "turn_end", "reason": "timeout", "steps": step})
            return json.dumps({"content": note, "history": messages, "cancelled": False})
        data = chat_completion(messages, config, tools=active_tools())
        msg = data["choices"][0]["message"]
        if msg.get("content") is None:
            msg["content"] = ""
        messages.append(msg)

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            _trace({"event": "turn_end", "reason": "answer", "steps": step + 1})
            return json.dumps({"content": msg.get("content") or "", "history": messages, "cancelled": False})

        for tc in tool_calls:
            if _cancel_flag[0]:
                _trace({"event": "turn_end", "reason": "cancelled", "steps": step + 1})
                return json.dumps({"content": "(stopped by user)", "history": messages, "cancelled": True})
            fn = tc.get("function", {})
            name = fn.get("name", "?")
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    args = {}
            except Exception:
                args = {}
            tool_id = tc.get("id", "?")
            _report_tool(tool_id, name, args, None, "running")
            _trace({"event": "tool", "name": name, "args": args})
            result = dispatch_tool(name, args)
            status = "done" if (isinstance(result, dict) and "error" not in result) else "error"
            if status == "error":
                consec_failures += 1
            else:
                consec_failures = 0
            _trace({"event": "tool", "name": name, "status": status})
            _report_tool(tool_id, name, args, result, status)
            messages.append({"role": "tool", "tool_call_id": tool_id, "content": json.dumps(result)})

        if consec_failures >= _MAX_CONSEC_TOOL_FAILURES:
            _trace({"event": "turn_end", "reason": "circuit_breaker", "steps": step + 1,
                    "consec_failures": consec_failures})
            note = "(stopped early: %d tools in a row failed - the current approach is not working)" % consec_failures
            messages.append({"role": "assistant", "content": note})
            return json.dumps({"content": note, "history": messages, "cancelled": False})

    _trace({"event": "turn_end", "reason": "max_steps", "steps": MAX_STEPS})
    return json.dumps({"content": "(reached the maximum number of tool steps)", "history": messages, "cancelled": False})


# Hermes-style XML function calling: some models can't use native OpenAI
# tool_calls and instead emit <think>...</think> reasoning plus
#   <tool_call>
#     <function=name>
#       <parameter=key>value</parameter>
#     </function>
#   </tool_call>
# blocks inside the content stream. We hide those blocks from the live
# display (AgentWriter) and, when a turn contains them, execute them like
# real tools and feed the results back (run_agent_stream).
_XML_THINK_RE = re.compile(r"<(?:think|reasoning)\b.*?</(?:think|reasoning)\s*>", re.DOTALL)
_XML_CALL_RE = re.compile(r"<tool_call\b.*?</tool_call\s*>", re.DOTALL)
_XML_BLOCK_RE = re.compile(r"<(?:think|reasoning|tool_call)\b.*?</(?:think|reasoning|tool_call)\s*>", re.DOTALL)
_XML_FUNC_RE = re.compile(r"<function\s*=\s*([^\s>]+)>")
_XML_PARAM_RE = re.compile(r"<parameter\s*=\s*([^\s>]+)>(.*?)</parameter\s*>", re.DOTALL)
# Some reasoning models emit a bare closing </think> with no opening tag in the
# stream (the opener is consumed by the provider's reasoning pipeline). Strip
# those strays too, with their trailing newline, so no raw tags ever render.
_XML_STRAY_CLOSE_RE = re.compile(r"</(?:think|reasoning|tool_call)\s*>[\r\n]*")
_XML_OPEN_TAGS = ("<think", "<reasoning", "<tool_call")


def _clean_segment(s):
    """Remove stray closing tags (</think> etc.) from a plain-text segment."""
    return _XML_STRAY_CLOSE_RE.sub("", s)


def _strip_xml_blocks(text):
    """Remove complete think/reasoning/tool_call blocks from text, returning
    (clean_text, pending_tail). pending_tail parks a block whose opening tag
    may be truncated at the end of the buffer; the next feed() completes it.
    Stray closing tags with no opener are removed too.
    """
    out = []
    pos = 0
    while True:
        m = _XML_BLOCK_RE.search(text, pos)
        if m:
            out.append(_clean_segment(text[pos:m.start()]))
            pos = m.end()
            continue
        sm = _XML_STRAY_CLOSE_RE.search(text, pos)
        if sm:
            out.append(text[pos:sm.start()])
            pos = sm.end()
            continue
        tail = text[pos:]
        # park from the last unclosed opener in the tail (may be truncated)
        best = -1
        for tag in _XML_OPEN_TAGS:
            i = tail.rfind(tag)
            if i > best:
                best = i
        if best >= 0:
            out.append(_clean_segment(tail[:best]))
            return "".join(out), tail[best:]
        lt = tail.rfind("<")
        if lt >= 0:
            frag = tail[lt:]
            if any(tag.startswith(frag) for tag in _XML_OPEN_TAGS):
                out.append(_clean_segment(tail[:lt]))
                return "".join(out), tail[lt:]
        out.append(_clean_segment(tail))
        return "".join(out), ""


def _parse_xml_tool_calls(text):
    """Extract (name, args) pairs from <tool_call> blocks in text."""
    calls = []
    for block in _XML_CALL_RE.findall(text):
        m = _XML_FUNC_RE.search(block)
        if not m:
            continue
        args = {}
        for pm in _XML_PARAM_RE.finditer(block):
            args[pm.group(1).strip()] = html.unescape(pm.group(2)).strip()
        calls.append((m.group(1).strip(), args))
    return calls


def _strip_xml(text):
    """Full-content version: drop all think/tool_call blocks and stray closing
    tags, then tidy spacing."""
    t = _XML_THINK_RE.sub("", text)
    t = _XML_CALL_RE.sub("", t)
    t = _XML_STRAY_CLOSE_RE.sub("", t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def run_agent_stream(history, config):
    """Generator that yields ('text', chunk) or ('tool', tool_info) or ('done', final_dict)."""
    _cancel_flag[0] = False
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    history = _repair_tool_pairs(history)
    for m in history:
        if m.get("role") == "system":
            continue
        # Copy the full message dict so tool messages keep their tool_call_id
        # and assistant messages keep their tool_calls (required by the API).
        if not isinstance(m, dict):
            continue
        messages.append(dict(m))

    consec_failures = 0
    _t0 = time.monotonic()
    _trace({"event": "turn_start", "steps": 0})
    for step in range(MAX_STEPS):
        if _cancel_flag[0]:
            _trace({"event": "turn_end", "reason": "cancelled", "steps": step})
            yield "done", {"content": "(stopped by user)", "history": messages, "cancelled": True}
            return
        if _TURN_TIMEOUT <= 0 or time.monotonic() - _t0 > _TURN_TIMEOUT:
            note = "(stopped: the turn exceeded the %d-second time budget)" % int(_TURN_TIMEOUT)
            messages.append({"role": "assistant", "content": note})
            _trace({"event": "turn_end", "reason": "timeout", "steps": step})
            yield "done", {"content": note, "history": messages, "cancelled": False}
            return

        # Use streaming to detect tool calls and collect text
        content_parts = []
        tool_calls_result = None
        try:
            for chunk, tcs in chat_completion_stream(messages, config, tools=active_tools()):
                if _cancel_flag[0]:
                    yield "done", {"content": "(stopped by user)", "history": messages, "cancelled": True}
                    return
                if chunk:
                    content_parts.append(chunk)
                    yield "text", chunk
                if tcs:
                    tool_calls_result = tcs
        except _Cancelled:
            yield "done", {"content": "(stopped by user)", "history": messages, "cancelled": True}
            return
        except RuntimeError as e:
            yield "done", {"content": "error: %s" % e, "history": messages, "cancelled": False}
            return

        full_content = "".join(content_parts)
        has_xml = bool(_XML_CALL_RE.search(full_content) or _XML_THINK_RE.search(full_content))
        msg = {"role": "assistant",
               "content": _strip_xml(full_content) if has_xml else full_content}

        if tool_calls_result:
            msg["tool_calls"] = tool_calls_result
            messages.append(msg)
            for tc in tool_calls_result:
                if _cancel_flag[0]:
                    yield "done", {"content": "(stopped by user)", "history": messages, "cancelled": True}
                    return
                fn = tc.get("function", {})
                name = fn.get("name", "?")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except Exception:
                    args = {}
                tool_id = tc.get("id", "?")
                yield "tool_start", {"name": name, "args": args}
                _trace({"event": "tool", "name": name, "args": args})
                result = dispatch_tool(name, args)
                status = "done" if (isinstance(result, dict) and "error" not in result) else "error"
                if status == "error":
                    consec_failures += 1
                else:
                    consec_failures = 0
                _trace({"event": "tool", "name": name, "status": status})
                yield "tool_end", {"name": name, "args": args, "result": result, "status": status}
                messages.append({"role": "tool", "tool_call_id": tool_id, "content": json.dumps(result)})
        elif has_xml:
            xml_calls = _parse_xml_tool_calls(full_content)
            messages.append(msg)
            for i, (name, args) in enumerate(xml_calls):
                if _cancel_flag[0]:
                    yield "done", {"content": "(stopped by user)", "history": messages, "cancelled": True}
                    return
                yield "tool_start", {"name": name, "args": args}
                _trace({"event": "tool", "name": name, "args": args})
                result = dispatch_tool(name, args)
                status = "done" if (isinstance(result, dict) and "error" not in result) else "error"
                if status == "error":
                    consec_failures += 1
                else:
                    consec_failures = 0
                _trace({"event": "tool", "name": name, "status": status})
                yield "tool_end", {"name": name, "args": args, "result": result, "status": status}
                messages.append({"role": "tool", "tool_call_id": "xml_%d" % i, "content": json.dumps(result)})
        else:
            messages.append(msg)
            _trace({"event": "turn_end", "reason": "answer", "steps": step + 1})
            yield "done", {"content": msg["content"], "history": messages, "cancelled": False}
            return

        if consec_failures >= _MAX_CONSEC_TOOL_FAILURES:
            note = "(stopped early: %d tools in a row failed - the current approach is not working)" % consec_failures
            messages.append({"role": "assistant", "content": note})
            _trace({"event": "turn_end", "reason": "circuit_breaker", "steps": step + 1, "consec_failures": consec_failures})
            yield "done", {"content": note, "history": messages, "cancelled": False}
            return

    _trace({"event": "turn_end", "reason": "max_steps", "steps": MAX_STEPS})
    yield "done", {"content": "(reached the maximum number of tool steps)", "history": messages, "cancelled": False}



# ---------------- context tracking & sessions ----------------
# Rough token estimates power the ctx meter, the auto-compress trigger and the
# /context command. Sessions persist to store.json so conversations can be
# saved, listed and resumed across runs (Hermes-style /sessions).

def context_window_for(cfg):
    """Context window (tokens) for a provider config: explicit override wins,
    else best-effort lookup by model name, else the default."""
    w = cfg.get("context_window") or 0
    if w and int(w) > 0:
        return int(w)
    model = (cfg.get("model") or "").lower()
    if model in MODEL_CONTEXT:
        return MODEL_CONTEXT[model]
    for key, size in MODEL_CONTEXT.items():
        if key in model:
            return size
    return DEFAULT_CONTEXT_WINDOW


def estimate_tokens(text):
    """Rough token estimate: ~4 chars per token; wide scripts count ~2x."""
    s = str(text)
    wide = sum(1 for ch in s if ord(ch) >= 128)
    return max(1, int((len(s) + wide) / 4))


def estimate_message_tokens(m):
    c = m.get("content") or ""
    if isinstance(c, list):
        n = sum(estimate_tokens(part.get("text", "") if isinstance(part, dict) else part)
                for part in c)
    else:
        n = estimate_tokens(c)
    return n + 8  # role + metadata overhead


def context_usage(history, cfg):
    """Estimated (tokens, window) for the whole conversation + system prompt."""
    total = estimate_tokens(SYSTEM_PROMPT)
    for m in history:
        total += estimate_message_tokens(m)
    return total, context_window_for(cfg)


# ---------------- sessions ----------------
def sessions_map():
    return _store_get(SESSION_KEY, {})


def load_session(name):
    msgs = sessions_map().get(name, {}).get("messages")
    return list(msgs) if isinstance(msgs, list) else []


def save_session(name, messages):
    """Persist a session's messages and mark it active. Prunes the oldest
    sessions past MAX_SESSIONS so store.json can't grow without bound."""
    sess = sessions_map()
    rec = sess.get(name) or {"name": name, "created": now_iso(), "messages": []}
    rec["messages"] = list(messages)
    rec["updated"] = now_iso()
    sess[name] = rec
    if len(sess) > MAX_SESSIONS:
        others = sorted(((n, sess[n].get("updated") or "") for n in sess if n != name),
                        key=lambda x: x[1])
        for old_name, _ in others[:len(sess) - MAX_SESSIONS]:
            sess.pop(old_name, None)
    _store_set(SESSION_KEY, sess)
    _store_set(ACTIVE_SESSION_KEY, name)


def delete_session(name):
    sess = sessions_map()
    sess.pop(name, None)
    _store_set(SESSION_KEY, sess)


def _find_session(target):
    """Case-insensitive session-name lookup; returns the canonical name or None."""
    t = target.strip().lower()
    for name in sessions_map():
        if name.lower() == t:
            return name
    return None


def _rename_session_in_store(old, new):
    sess = sessions_map()
    if old in sess:
        rec = sess.pop(old)
        rec["name"] = new
        sess[new] = rec
        _store_set(SESSION_KEY, sess)
    _store_set(ACTIVE_SESSION_KEY, new)


def auto_title(text):
    """A short human-readable session name derived from the first message."""
    t = re.sub(r"\s+", " ", str(text)).strip().strip(".:-")
    return t[:28] or "conversation"


def _unique_session_name(title):
    base = auto_title(title)
    name = base
    i = 2
    while name in sessions_map():
        name = "%s %d" % (base, i)
        i += 1
    return name


# ---------------- auto-compression ----------------
def summarize_with_llm(messages, cfg, max_words=350):
    """Condense `messages` into a structured summary for a fresh context window.

    Returns a concise multi-section summary string, or None on any failure.
    Uses a lean system note (not the full harness prompt) so we don't waste
    tokens re-sending instructions or risk bleeding them into the summary.
    """
    prompt = (
        "Condense the conversation below into a structured summary for a fresh "
        "context window. Use these sections only where applicable:\n"
        "- GOALS: what the user wanted to achieve\n"
        "- DECISIONS: choices made and their rationale\n"
        "- FACTS: durable facts, names, values, config learned\n"
        "- ACTIONS: concrete work done (commands run, files changed, code edits)\n"
        "- OPEN: unresolved questions or next steps\n"
        "Be dense and factual - no preamble, under %d words total. "
        "Output only the summary sections." % max_words)
    sys_note = "You are a compression assistant. Output only the requested summary, no preamble."
    msgs = ([{"role": "system", "content": sys_note}]
            + list(messages) + [{"role": "user", "content": prompt}])
    try:
        data = chat_completion(msgs, cfg)
        text = (data["choices"][0]["message"].get("content") or "").strip()
        if not text:
            return None
        # guard against a chatty model prefixing a preamble ("Here is...", "Sure:")
        low = text.lower()
        if low.startswith("here") or low.startswith("sure") or low.startswith("ok"):
            text = re.split(r"\n", text, 1)[-1].strip()
        return text[:4000] or None
    except Exception:
        return None


def _fallback_summary(head):
    first = next((m.get("content", "") for m in head if m.get("role") == "user"), "")
    first = re.sub(r"\s+", " ", str(first)).strip()
    return ("Earlier conversation was compacted to save context (%d messages dropped).\n"
            "First user message: %s" % (len(head), first[:200] or "(none)"))


def compress_history(messages, cfg, summarizer=None, keep_frac=0.4, min_keep=8):
    """Summarize the older messages into one summary message, keeping a recent tail.

    Returns (new_history, stats) with stats None when there's nothing to compress.
    `summarizer` is injectable for tests: callable(messages, cfg) -> str.
    """
    window = context_window_for(cfg)
    if window <= 0 or len(messages) <= min_keep:
        return messages, None
    keep_tokens = max(400, int(window * keep_frac))  # small windows can still compress
    acc = 0
    tail_start = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        acc += estimate_message_tokens(messages[i])
        if acc > keep_tokens and len(messages) - i >= min_keep:
            tail_start = i
            break
    if tail_start >= len(messages) or tail_start <= 1:
        return messages, None
    head, tail = messages[:tail_start], messages[tail_start:]
    # never leave the tail starting mid-tool-sequence: tool results must follow
    # their assistant tool_call, so push leading tool messages into the summary part
    while tail and tail[0].get("role") == "tool":
        head.append(tail.pop(0))
    if not tail:
        return messages, None
    if summarizer is None:
        summarizer = summarize_with_llm
    summary = summarizer(head, cfg)
    mode = "llm"
    if not summary:
        summary = _fallback_summary(head)
        mode = "fallback"
    new = [{"role": "user", "content": "[summary of earlier conversation]\n" + summary}] + tail
    return new, {"dropped": len(head), "kept": len(tail), "mode": mode}


def compress_now(history, cfg, threshold=0.75, force=False):
    """If usage exceeds the threshold (or force=True), summarize older messages
    in place. Returns True when a compression happened; never raises on failure."""
    tokens, window = context_usage(history, cfg)
    if window <= 0:
        p_info("(no context window configured)")
        return False
    if not force and tokens <= int(window * threshold):
        return False
    p_info("context %d%% of %s - compressing older messages..."
           % (tokens * 100 // window, _fmt_k(window)))
    sp = Spinner("compressing")
    _UI["spinner"] = sp
    sp.start()
    try:
        new, stats = compress_history(history, cfg)
    except KeyboardInterrupt:
        p_info("compression cancelled")
        return False
    except Exception as e:
        p_info("compression failed: %s" % e)
        return False
    finally:
        sp.stop()
        _UI["spinner"] = None
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


def set_active_skin(state):
    """Pick the persisted skin (config.json) for the rest of the session."""
    global CUR_SKIN
    name = (state or {}).get("skin")
    CUR_SKIN = SKINS.get(name, SKINS[DEFAULT_SKIN])


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


_UI = {"spinner": None}


def tool_open(name, args):
    """Compact tool line: '  ▸ name (args)' - dim, no full-width rule."""
    a = fmt_args(args)
    label = "▸ " + name + ((" (" + a + ")") if a else "")
    _tool_line(label, HERMES_DIM)


def tool_close(name, status, result):
    """Close line of a tool block: '  ✓ name → summary'."""
    mark = ("✓ " if status == "done" else "✗ ")
    label = mark + name
    if result is not None:
        s = tool_summary(result).replace("\n", " ").replace("\r", " ").strip()
        s = " ".join(s.split())   # collapse runs of whitespace onto one line
        if s:
            label += " → " + s
    _tool_line(label, HERMES_OK if status == "done" else HERMES_ERR)


def on_tool(tool_id, name, args, result, status):
    sp = _UI["spinner"]
    if sp:
        sp.stop()
    if status == "running":
        tool_open(name, args)
    else:
        tool_close(name, status, result)
    if sp:
        sp.start()


def run_agent_tui(history, cfg):
    """Run the agent loop with streaming output, spinner, and live tool blocks.

    Returns the 'done' payload augmented with:
      tools    - number of tool calls this turn
      elapsed  - wall-clock seconds for the turn
      streamed - whether any text was printed (so the caller can avoid
                 re-printing the answer, fixing the old double-print)
    """
    sp = Spinner("thinking")
    _UI["spinner"] = sp
    sp.start()
    content_parts = []
    tool_count = 0
    writer = AgentWriter(CUR_SKIN, CUR_SKIN["agent"])
    t0 = time.monotonic()
    try:
        for evt_type, evt_data in run_agent_stream(history, cfg):
            if evt_type == "text":
                sp.disable()   # kill the spinner BEFORE text lands (no \r collision)
                content_parts.append(evt_data)
                writer.feed(evt_data)
            elif evt_type == "tool_start":
                sp.disable()
                tool_count += 1
                tool_open(evt_data["name"], evt_data["args"])
            elif evt_type == "tool_end":
                tool_close(evt_data["name"], evt_data["status"], evt_data["result"])
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
        _UI["spinner"] = None
    return {"content": "".join(content_parts), "history": history, "cancelled": False,
            "tools": tool_count, "elapsed": time.monotonic() - t0,
            "streamed": bool(content_parts)}


def trim_history(history):
    # a pure safety net now - the context meter + auto-compress manage the real
    # per-model limit, so the hard cap is generous
    # never trim away a leading compression summary (that would silently lose
    # all of the summarized context)
    head = []
    if history and str(history[0].get("content", "")).startswith("[summary of earlier conversation]"):
        head = [history[0]]
        history[:] = history[1:]
    if len(history) > 120:
        history[:] = history[-120:]
    chars = 0
    for i in range(len(history) - 1, -1, -1):
        chars += len(history[i].get("content") or "")
        if chars > 500000 and i > 0:
            del history[:i]
            break
    history[:] = head + history


def ask(label, current):
    try:
        v = input("    %s [%s]: " % (label, current)).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return current
    return v if v else current


def parse_key(v, current):
    """Resolve a key-prompt answer: '' keeps, 'none'/'clear'/'-' clears, else replaces."""
    if v == "":
        return current
    if v.lower() in ("none", "clear", "-"):
        return ""
    return v


def ask_key(current):
    """Ask for an API key; 'none' / 'clear' / '-' empties it, Enter keeps."""
    print("    api key [%s]   ('none' clears it)" % mask_key(current))
    try:
        v = input("    > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return current
    return parse_key(v, current)


def ask_permission(desc):
    """Interactive y/N/a prompt used as ON_PERMISSION in the REPL.

    y or a approve (and are remembered for this session, so the same action
    won't prompt again); anything else denies.
    """
    sp = _UI.get("spinner")
    was_running = sp is not None
    if sp:
        sp.stop()
    print()
    print(col(CUR_SKIN["err"], "  [!] permission needed") + "  " + desc)
    try:
        v = input("    allow? [y/N/a]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        if was_running and sp:
            sp.start()
        return False
    if was_running and sp:
        sp.start()
    if v in ("y", "yes", "a", "allow", "always"):
        print("    (remembered for this session - restart to reset)")
        return True
    return False


# ---------------- slash commands ----------------
def pick_model(base_url, api_key, current, fetch=True):
    """Fetch the models for an endpoint+key and let the user pick one.

    With fetch=False (endpoint unchanged), just ask for the model id directly.
    """
    if fetch:
        try:
            models = fetch_models(base_url, api_key)
        except Exception as e:
            p_info("  (couldn't fetch models: %s)" % e)
            return ask("model", current)
        if not models:
            p_info("  (endpoint reported no models)")
            return ask("model", current)
        print("  available models:")
        for i, m in enumerate(models):
            mark = "   <- current" if m == current else ""
            print("    %2d) %s%s" % (i, m, mark))
        keep = current or "(none)"
        try:
            choice = input("    pick a model [0-%d, Enter=keep %s]: " % (len(models) - 1, keep)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return current
        if choice == "":
            return current
        if choice in models:
            return choice
        try:
            idx = int(choice)
            if 0 <= idx < len(models):
                return models[idx]
        except ValueError:
            pass
        p_info("  invalid choice - keeping %s" % keep)
        return current
    return ask("model", current)


def cmd_models(state):
    cfg = active_cfg(state)
    if not (cfg.get("base_url") or "").rstrip("/"):
        p_err("no base url configured for '%s' - run /provider %s" % (state["active"], state["active"]))
        return
    cfg["model"] = pick_model(cfg.get("base_url", ""), cfg.get("api_key", ""), cfg.get("model", ""))
    save_state(state)
    p_ok("saved [OK]")


_SLASH_COMMANDS = [
    "/help", "/config", "/provider", "/models", "/test", "/skin",
    "/sessions", "/session", "/new", "/clear", "/context", "/compress",
    "/tools", "/trace", "/todos", "/todo", "/memory", "/skills", "/skill",
    "/install_skill", "/feedback", "/reflect", "/self-test", "/improve",
    "/multi", "/export", "/redo", "/stop", "/exit", "/quit",
]


def cmd_skin(state, rest):
    """List or switch the UI skin (persisted in config.json)."""
    arg = rest.strip().lower()
    if not arg or arg in ("ls", "list"):
        print("  skins:")
        for name, sk in SKINS.items():
            mark = "   <- active" if (state.get("skin") or DEFAULT_SKIN) == name else ""
            print("    %-10s %s%s" % (name, sk["desc"], mark))
        print("  usage: /skin <name>")
        return
    if arg not in SKINS:
        p_err("unknown skin '%s' - see /skin" % arg)
        return
    state["skin"] = arg
    save_state(state)
    set_active_skin(state)
    p_ok("skin set to '%s' [OK]" % arg)


def cmd_sessions():
    """List saved sessions (name, message count, last updated)."""
    sess = sessions_map()
    if not sess:
        print("  (no sessions yet - /session <name> starts one)")
        return
    active = _store_get(ACTIVE_SESSION_KEY, "default")
    print("  sessions (%d):" % len(sess))
    for name in sorted(sess, key=lambda n: (sess[n].get("updated") or ""), reverse=True):
        rec = sess[name]
        n = len(rec.get("messages") or [])
        upd = (rec.get("updated") or "")[11:16] or "?"
        mark = ">" if name == active else " "
        print("   %s %-22s %3d msgs | %s" % (mark, name[:22], n, upd))
    print("  usage: /session <name> | /session rm <name> | /session rename <old> <new>")


def cmd_context(state, rest, history):
    """Context usage for the active provider + its settings."""
    cfg = active_cfg(state)
    parts = rest.strip().split(None, 1)
    sub = parts[0].lower() if parts else ""
    val = parts[1].strip() if len(parts) > 1 else ""
    if sub == "window":
        if not val:
            p_err("usage: /context window <tokens>  (0 = auto-detect from the model)")
            return
        try:
            w = int(float(val))
        except (ValueError, OverflowError):
            p_err("usage: /context window <tokens>  (0 = auto-detect from the model)")
            return
        cfg["context_window"] = w
        save_state(state)
        p_ok("context window set to %s [OK]" % (_fmt_k(w) if w else "auto"))
        return
    if sub in ("auto", "autocompress", "auto-compress"):
        cfg["auto_compress"] = not cfg.get("auto_compress", True)
        save_state(state)
        p_ok("auto-compress %s [OK]" % ("on" if cfg["auto_compress"] else "off"))
        return
    tokens, window = context_usage(history, cfg)
    pct = tokens * 100 // window if window else 0
    print("  context usage:")
    print("    system prompt : ~%s tokens" % _fmt_k(estimate_tokens(SYSTEM_PROMPT)))
    print("    conversation  : %s tokens (%d messages)"
          % (_fmt_k(tokens - estimate_tokens(SYSTEM_PROMPT)), len(history)))
    print("    total         : %s / %s  (%d%%)" % (_fmt_k(tokens), _fmt_k(window), pct))
    print("  settings ('%s'):" % state["active"])
    print("    context window: %s   (/context window <n> to override)" % _fmt_k(window))
    print("    auto-compress : %s  at 75%% of the window   (/context autocompress toggles)"
          % ("on" if cfg.get("auto_compress", True) else "off"))
    if pct >= 85:
        p_warn("context is %d%% full - /new starts a fresh session | /compress summarizes now" % pct)


def cmd_compress(history, state, session):
    """Manually summarize older messages to free context (persists immediately)."""
    if len(history) < 8:
        p_info("(conversation is short - nothing to compress)")
        return
    if compress_now(history, active_cfg(state), force=True):
        save_session(session, history)



def cmd_self_test():
    """Run the harness self-test suite and show results.

    Tests calculator, sandbox, todo, memory, skills, command classification,
    file tools, and the feedback/improvement/reflect tools.
    """
    tests = [
        ("calculator basic", lambda: _check(tool_calculator("2+2")["ok"])),
        ("calculator sqrt", lambda: _check(tool_calculator("sqrt(144)")["ok"])),
        ("sandbox rejects div0", lambda: _check(_raises(lambda: tool_calculator("1/0")))),
        ("sandbox rejects complex", lambda: _check(_raises(lambda: tool_calculator("(-8)**0.5")))),
        ("todo add+list+remove", lambda: _check(_todo_check())),
        ("memory save+recall", lambda: _check(_mem_check())),
        ("skills list+read", lambda: _check(_skill_check())),
        ("classify allow: ls", lambda: _check(classify_command("ls -la") == "allow")),
        ("classify ask: rm -rf", lambda: _check(classify_command("rm -rf /") == "ask")),
        ("classify ask: subshell", lambda: _check(classify_command("echo $(whoami)") == "ask")),
        ("file_read in project", lambda: _check(tool_file_read(__file__)["ok"])),
        ("file_write temp", lambda: _check(_file_write_check())),
        ("file_edit temp", lambda: _check(_file_edit_check())),
        ("feedback+improvement+reflect", lambda: _check(_feedback_check())),
    ]
    total = len(tests)
    passed = 0
    for name, fn in tests:
        try:
            ok = fn()
        except Exception as e:
            ok = False
            print("  [FAIL] %-35s -> %s" % (name, e))
            continue
        if ok:
            passed += 1
            print("  [PASS] %-35s" % name)
        else:
            print("  [FAIL] %-35s" % name)
    print("\n  %d/%d tests passed" % (passed, total))
    if passed == total:
        print("  self-test: ALL PASSED")
    else:
        print("  self-test: %d FAILED" % (total - passed))
    return {"passed": passed, "total": total}


def _check(cond):
    return bool(cond)


def _raises(fn):
    """True when calling fn() raises an exception (used by self-test entries
    that assert a tool rejects bad input)."""
    try:
        fn()
    except Exception:
        return True
    return False


def _todo_check():
    r = tool_todo_add("self-test-todo")
    if not r.get("ok"):
        return False
    items = tool_todo_list().get("todos", [])
    found = any(t.get("text") == "self-test-todo" and not t.get("done") for t in items)
    tool_todo_remove(len(items) - 1)
    return found


def _mem_check():
    r = tool_memory_save("self-test-mem", "hello")
    if not r.get("ok"):
        return False
    v = tool_memory_recall("self-test-mem").get("value")
    tool_memory_save("self-test-mem", "")
    return v == "hello"


def _skill_check():
    skills = tool_skill_list().get("skills", [])
    if not skills:
        return True
    name = skills[0]["name"]
    r = tool_skill_read(name)
    return r.get("ok") and r.get("content")


def _file_write_check():
    # stay inside PROJECT_DIR: /tmp is out-of-project and would trigger a
    # permission prompt (or a headless deny), which a self-test shouldn't do
    tmp = os.path.join(PROJECT_DIR, ".alva_sst_write.txt")
    r = tool_file_write(tmp, "test")
    if not r.get("ok"):
        return False
    content = tool_file_read(tmp).get("content", "")
    try:
        os.remove(tmp)
    except OSError:
        pass
    return content == "test"


def _file_edit_check():
    tmp = os.path.join(PROJECT_DIR, ".alva_sst_edit.txt")
    tool_file_write(tmp, "hello world")
    r = tool_file_edit(tmp, "hello", "goodbye")
    if not r.get("ok"):
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    content = tool_file_read(tmp).get("content", "")
    try:
        os.remove(tmp)
    except OSError:
        pass
    return content == "goodbye world"


def _feedback_check():
    r1 = tool_feedback("good", "self-test check")
    if not r1.get("ok"):
        return False
    fb = _store_get(FEEDBACK_KEY, [])
    if not fb or fb[-1].get("rating") != "good":
        return False
    r2 = tool_improvement_set("self-test-area", "fix something")
    if not r2.get("ok"):
        return False
    imps = _store_get(IMPROVEMENT_KEY, [])
    if not imps or imps[-1].get("area") != "self-test-area":
        return False
    r3 = tool_reflect()
    if not r3.get("ok"):
        return False
    tool_improvement_done("self-test-area")
    return True


def cmd_help():
    print("  commands:")
    print("    /help /?               this help")
    print("    /sessions              list saved sessions (name | messages | updated)")
    print("    /session <name>        switch to (or create) a session")
    print("    /session rm <name>     delete a session      /session rename <old> <new>")
    print("    /new                   start a fresh session (the current one is saved)")
    print("    /clear                 wipe the current conversation")
    print("    /context               context meter + settings (window, auto-compress)")
    print("    /compress              summarize older messages to free context now")
    print("    /multi                 multi-line input ('.' on its own line submits)")
    print("    /export                save the conversation as a text file")
    print("    /redo                  re-run the last request (regenerates the answer)")
    print("    /provider [name]       list / add / switch provider profiles")
    print("    /provider rm <name>    delete a provider")
    print("    /config                edit the ACTIVE provider (base url, key, model, temp)")
    print("    /models                fetch + pick a model for the active provider")
    print("    /test                  test the active provider's connection")
    print("    /skin [name]           list / switch the UI skin (midnight, ember, ocean, daylight)")
    print("    (at the api key prompt, type 'none' to clear the key)")
    print("    /tools                 list the agent's tools (and switch mode: /tools full|core)")
    print("    /trace [n]             show the last n agent-trace lines (default 15)")
    print("    /todos                 show the to-do list")
    print("    /todo <text>           add a task")
    print("    /todo done <i>         toggle task i      /todo rm <i>   remove task i")
    print("    /todo clear            empty the list")
    print("    /memory                show saved memory facts")
    print("    /skills                list saved skills (grouped by category)")
    print("    /skills install <u>    install a skill from a URL or local .md [category]")
    print("    /skills sync <repo>    bulk-import a whole skills git repo [subdir]")
    print("    /skill rm <name>       delete a skill (name or category/name)")
    print("    /skill category [n]    list categories / show skills in category n")
    print("    /install_skill <u>     install a skill from a URL or local .md file")
    print("    /feedback <good|bad>   record feedback on the last response")
    print("    /reflect               review feedback + improvements, propose actions")
    print("    /improve               manage self-improvement areas (list/add/done)")
    print("    /self-test             run harness self-test suite (validate after edits)")
    print("    /stop                  cancel the running request")
    print("    /exit  /quit           leave the agent")
    print("    Tab                    completes slash commands")
    print("    Ctrl+C                 while a request runs: cancel it (same as /stop)")


def cmd_config(state):
    cfg = active_cfg(state)
    print("  provider '%s' settings:" % state["active"])
    print("    base url    : " + cfg.get("base_url", ""))
    print("    api key     : " + mask_key(cfg.get("api_key", "")))
    print("    model       : " + cfg.get("model", ""))
    print("    temperature : " + str(cfg.get("temperature", 0.7)))
    print("    context     : " + (_fmt_k(cfg.get("context_window", 0)) if cfg.get("context_window") else "auto"))
    print("    auto-compress: " + ("on" if cfg.get("auto_compress", True) else "off"))
    print("  (Enter keeps the current value; at the api key prompt type 'none' to clear it)")
    base = ask("base url", cfg.get("base_url", ""))
    key = ask_key(cfg.get("api_key", ""))
    unchanged = (base == (cfg.get("base_url") or "") and key == (cfg.get("api_key") or "") and bool(cfg.get("model")))
    cfg["base_url"], cfg["api_key"] = base, key
    cfg["model"] = pick_model(base, key, cfg.get("model", ""), fetch=not unchanged)
    try:
        cfg["temperature"] = float(ask("temperature", str(cfg.get("temperature", 0.7))))
    except ValueError:
        pass
    try:
        cfg["context_window"] = int(float(ask("context window (tokens, 0=auto)", str(cfg.get("context_window") or 0))))
    except (ValueError, OverflowError):
        pass
    ac = ask("auto-compress near the context limit (y/n)",
             "y" if cfg.get("auto_compress", True) else "n").strip().lower()
    cfg["auto_compress"] = ac in ("y", "yes", "on", "1", "true")
    save_state(state)
    p_ok("saved [OK]")


def _list_providers(state):
    profiles = state["profiles"]
    if not profiles:
        print("  (no providers configured)")
        return
    print("  providers:")
    for name, p in profiles.items():
        mark = "   <- active" if name == state["active"] else ""
        print("    %-12s %s | model %s | key %s%s"
              % (name, p.get("base_url") or "(no base)",
                 p.get("model") or "-", mask_key(p.get("api_key", "")), mark))
    print("  usage: /provider <name> (add or switch) | /provider rm <name>")
    print("  presets: openai | groq | openrouter | gemini | custom   (any other name = custom endpoint)")


def cmd_provider(state, rest):
    profiles = state["profiles"]
    arg, _, sub = rest.strip().partition(" ")
    arg = arg.strip().lower()
    sub = sub.strip()

    if arg in ("ls", "list"):
        _list_providers(state)
        return
    if arg in ("rm", "remove", "del", "delete"):
        sub = sub.lower()
        if not sub:
            p_err("usage: /provider rm <name>")
            return
        if sub not in profiles:
            p_err("no provider named '%s'" % sub)
            return
        del profiles[sub]
        if state["active"] == sub:
            state["active"] = next(iter(profiles)) if profiles else "openai"
            if state["active"] not in profiles:
                profiles[state["active"]] = dict(DEFAULT_CFG)
        save_state(state)
        p_ok("removed '%s' [OK]" % sub)
        return
    if not arg:
        _list_providers(state)
        return

    # switch to an existing provider
    if arg in profiles:
        state["active"] = arg
        save_state(state)
        p_ok("switched to '%s' [OK]" % arg)
        return

    # add a new provider - a fresh profile, never inherits another provider's key
    p = PROVIDERS.get(arg)
    prof = dict(FIRST_RUN_CFG)
    if p:
        print("  " + p["label"])
        prof["base_url"] = p["base"]
        if p["model"]:
            prof["model"] = p["model"]
    if arg == "custom" or p is None:
        prof["base_url"] = ask("base url", prof.get("base_url", ""))
    prof["api_key"] = ask_key("")
    prof["model"] = pick_model(prof.get("base_url", ""), prof.get("api_key", ""), prof.get("model", ""))
    profiles[arg] = prof
    state["active"] = arg
    save_state(state)
    p_ok("added '%s' [OK]" % arg)


def cmd_test(state):
    cfg = active_cfg(state)
    base = (cfg.get("base_url") or "").rstrip("/")
    if not base:
        p_err("no base url configured for '%s' - run /provider %s" % (state["active"], state["active"]))
        return
    req = urllib.request.Request(
        base + "/models",
        headers={"Authorization": "Bearer " + (cfg.get("api_key") or ""),
                 "Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        n = len(data.get("data") or [])
        p_ok("Connected [OK] | %d model%s available" % (n, "" if n == 1 else "s"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        p_err("HTTP %d - %s" % (e.code, _readable_error(e.code, body)))
    except Exception as e:
        p_err("cannot reach API: %s" % e)


def cmd_tools():
    """List the active tool set (only what the model can currently see)."""
    active = active_tools()
    print("  tool mode: %s - %d/%d tools advertised to the model"
          % (_tools._TOOLS_MODE, len(active), len(TOOLS)))
    for t in active:
        fn = t["function"]
        print("  %-14s %s" % (fn["name"], fn.get("description", "")))
    hidden = len(TOOLS) - len(active)
    if hidden:
        print("  hidden (%d advanced; /tools full to advertise, /tools core to revert): %s"
              % (hidden, ", ".join(sorted(_ADVANCED_TOOL_NAMES))))


def cmd_trace(rest):
    """Print the last n lines of trace.log, oldest first (default 15)."""
    try:
        n = max(1, min(200, int(str(rest or "15").strip())))
    except ValueError:
        n = 15
    lines = _read_trace(n)
    if not lines:
        print("  (trace.log is empty - run some turns first)")
        return
    for ln in lines:
        try:
            rec = json.loads(ln)
        except Exception:
            print("  " + ln[:200])
            continue
        ev = rec.get("event", "?")
        rest_rec = {k: v for k, v in rec.items() if k not in ("event", "ts")}
        print("  %s %s" % (ev, json.dumps(rest_rec, ensure_ascii=False)[:220]))


def cmd_todos():
    lst = tool_todo_list().get("todos", [])
    if not lst:
        print("  (empty)")
        return
    for i, t in enumerate(lst):
        mark = "[x]" if t.get("done") else "[ ]"
        print("  %d %s %s" % (i, mark, t.get("text", "")))


def cmd_todo(rest):
    parts = rest.split(None, 1)
    op = parts[0].lower() if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""
    if not op or op in ("list", "ls", "show"):
        cmd_todos()
        return
    if op in ("add", "a"):
        r = tool_todo_add(arg)
        if r.get("ok"):
            p_ok("  added #%d: %s" % (r["index"], r["text"]))
        else:
            p_err("  " + r.get("error", "?"))
        return
    if op in ("done", "toggle", "t", "d"):
        try:
            r = tool_todo_toggle(int(arg))
        except ValueError:
            p_err("  need an index, e.g. /todo done 0")
            return
        if r.get("ok"):
            p_ok("  #%d %s: %s" % (r["index"], "done" if r.get("done") else "undone", r.get("text", "")))
        else:
            p_err("  " + r.get("error", "?"))
        return
    if op in ("rm", "remove", "del", "delete"):
        try:
            r = tool_todo_remove(int(arg))
        except ValueError:
            p_err("  need an index, e.g. /todo rm 0")
            return
        if r.get("ok"):
            p_ok("  removed #%d: %s" % (r.get("index", "?"), r.get("removed", {}).get("text", "")))
        else:
            p_err("  " + r.get("error", "?"))
        return
    if op == "clear":
        _store_set(TODO_KEY, [])
        p_ok("  list cleared")
        return
    p_err("  usage: /todo <text> | /todo done <i> | /todo rm <i> | /todo clear")


def cmd_memory():
    facts = [(k[len(MEM_PREFIX):], v) for k, v in _store.items() if k.startswith(MEM_PREFIX)]
    if not facts:
        print("  (no saved facts)")
        return
    print("  %-16s %-20s %s" % ("Key", "Tags", "Value"))
    print("  " + "-"*50)
    for k, v in facts:
        val = v.get("value", v) if isinstance(v, dict) else v
        tags = ", ".join(v.get("tags", [])) if isinstance(v, dict) else ""
        print("  %-16s %-20s %s" % (k, tags, val))



def cmd_feedback(rest):
    """Record feedback on the agent's last response.

    Usage: /feedback good | /feedback bad <notes> | /feedback neutral
    """
    parts = rest.strip().split(None, 1)
    if not parts:
        p_err("usage: /feedback <good|bad|neutral> [notes]")
        return
    rating = parts[0].lower()
    if rating not in ("good", "bad", "neutral"):
        p_err("rating must be good, bad, or neutral")
        return
    notes = parts[1] if len(parts) > 1 else ""
    r = tool_feedback(rating, notes or None)
    if r.get("ok"):
        p_ok("feedback recorded: %s%s" % (rating, " - " + notes if notes else ""))
    else:
        p_err("  " + r.get("error", "?"))


def cmd_skills(rest=""):
    arg = (rest or "").strip()
    if arg.startswith("install "):
        target = arg[len("install "):].strip()
        if not target:
            p_err("usage: /skills install <url|path> [category]")
            return
        parts = target.split(None, 1)
        r = tool_skill_install(parts[0], parts[1].strip() if len(parts) > 1 else None)
        if r.get("ok"):
            p_ok("installed skill '%s' [OK]" % r.get("name"))
            if r.get("category"):
                print("    category: %s" % r["category"])
        else:
            p_err("  " + r.get("error", "?"))
        return
    if arg.startswith("sync "):
        parts = arg[len("sync "):].strip().split(None, 1)
        if not parts:
            p_err("usage: /skills sync <repo-url> [subdir]")
            return
        r = tool_skill_sync_repo(parts[0], parts[1].strip() if len(parts) > 1 else None)
        if r.get("ok"):
            p_ok("synced %d skills from repo [OK]" % r.get("count"))
            for s in r.get("installed", []):
                print("    - %s%s" % (s["name"], (" (" + s["category"] + ")") if s.get("category") else ""))
            if r.get("errors"):
                for name, err in r.get("errors", []):
                    p_err("    %s: %s" % (name, err))
        else:
            p_err("  " + r.get("error", "?"))
        return
    if arg:
        cmd_skill_category(arg)
        return
    skills = tool_skill_list().get("skills") or []
    if not skills:
        print("  (no skills yet - ask the agent to save one)")
        return
    # group by category
    by_cat = {}
    for s in skills:
        cat = s.get("category") or "(flat)"
        by_cat.setdefault(cat, []).append(s)
    print("  skills (%d):" % len(skills))
    for cat in sorted(by_cat):
        entries = by_cat[cat]
        print("    [%s] %d" % (cat, len(entries)))
        for s in entries:
            desc = s.get("description") or ""
            if desc:
                desc = "  " + desc[:55]
            tags = s.get("tags") or []
            tagstr = ("  " + ", ".join(str(t) for t in tags)) if tags else ""
            print("      - %s%s%s" % (col(C.BOLD, s["name"]),
                                      col(C.DIM, desc),
                                      col(C.DIM, tagstr)))


def cmd_skill_category(rest):
    """List skills in a category, or list all categories."""
    arg = (rest or "").strip().lower()
    skills = tool_skill_list().get("skills") or []
    if arg in ("ls", "list", "show"):
        # list categories
        cats = {}
        for s in skills:
            c = s.get("category") or "(flat)"
            cats.setdefault(c, 0)
            cats[c] += 1
        if not cats:
            print("  (no skills yet)")
            return
        print("  categories (%d):" % len(cats))
        for c in sorted(cats):
            print("    %s  (%d skill%s)" % (col(C.BOLD, c if c != "(flat)" else "flat"),
                                             cats[c], "" if cats[c] == 1 else "s"))
        return
    if not arg:
        # no arg: list all categories
        cmd_skill_category("ls")
        return
    # show skills in one category
    cat_skills = [s for s in skills if (s.get("category") or "(flat)") == arg]
    if not cat_skills:
        p_err("no skills in category '%s'" % arg)
        return
    print("  category '%s' (%d skill%s):" % (arg, len(cat_skills),
                                              "" if len(cat_skills) == 1 else "s"))
    for s in cat_skills:
        desc = s.get("description") or ""
        if desc:
            desc = "  " + desc[:50]
        tags = s.get("tags") or []
        tagstr = ("  " + ", ".join(str(t) for t in tags)) if tags else ""
        print("      - %s%s%s" % (col(C.BOLD, s["name"]),
                                  col(C.DIM, desc),
                                  col(C.DIM, tagstr)))




def cmd_reflect():
    """Review all feedback + improvement areas and propose actions."""
    fb = _store_get(FEEDBACK_KEY, [])
    imps = _store_get(IMPROVEMENT_KEY, [])
    if not fb and not imps:
        p_info("(no feedback or improvements yet)")
        return
    if fb:
        print("  --- recent feedback (%d entries) ---" % len(fb))
        for e in fb[-10:]:
            tag = {"good": "+", "bad": "-", "neutral": "~"}.get(e["rating"], "?")
            note = " - " + e["notes"] if e.get("notes") else ""
            print("    %s [%s]%s" % (tag, e["rating"], note))
    if imps:
        print("  --- improvement areas (%d) ---" % len(imps))
        for it in imps:
            mark = "[x]" if it.get("done") else "[ ]"
            print("    %s %s" % (mark, it["area"]))
            print("        -> %s" % it["action"])
    print("  --- suggested actions ---")
    bad = [e for e in fb if e["rating"] == "bad"]
    if bad:
        print("    - review %d negative feedback entries above" % len(bad))
    open_imgs = [it for it in imps if not it.get("done")]
    if open_imgs:
        print("    - %d improvement area(s) still open - act on them" % len(open_imgs))
    if not bad and not open_imgs:
        print("    - no open issues - keep doing what works")
    print("  (re-run /reflect after making changes to mark them done)")
def cmd_improve(rest):
    """Manage self-improvement areas.

    Usage:
      /improve list          - show all pending + done improvements
      /improve add <area> <action>  - record a new area to improve
      /improve done <area>   - mark an area as resolved
    """
    parts = rest.strip().split(None, 2)
    if not parts:
        print("  Usage:")
        print("    /improve list")
        print("    /improve add <area> <action>")
        print("    /improve done <area>")
        return
    sub = parts[0].lower()
    if sub in ("list", "ls", "show"):
        items = _store_get(IMPROVEMENT_KEY, [])
        if not items:
            p_info("(no improvement areas yet)")
            return
        pending = [it for it in items if not it.get("done")]
        done = [it for it in items if it.get("done")]
        if pending:
            print("  --- pending (%d) ---" % len(pending))
            for it in pending:
                print("    [ ] %s" % it["area"])
                print("        -> %s" % it["action"])
        if done:
            print("  --- done (%d) ---" % len(done))
            for it in done:
                print("    [x] %s" % it["area"])
        print("  (use /improve done <area> to mark resolved)")
    elif sub in ("add", "set"):
        if len(parts) < 3:
            p_err("usage: /improve add <area> <action>")
            return
        area = parts[1]
        action = parts[2]
        r = tool_improvement_set(area, action)
        if r.get("ok"):
            p_ok("improvement recorded: %s" % area)
        else:
            p_err("  " + r.get("error", "?"))
    elif sub in ("done", "mark", "resolve"):
        if len(parts) < 2:
            p_err("usage: /improve done <area>")
            return
        r = tool_improvement_done(parts[1])
        if r.get("ok"):
            p_ok("marked done: %s" % r.get("area", parts[1]))
        else:
            p_err("  " + r.get("error", "?"))
    else:
        p_err("unknown subcommand: %s (list/add/done)" % sub)





def cmd_install_skill(rest):
    # Install a skill from a local .md file or a URL (delegates to
    # tool_skill_install so GitHub/blob URLs are auto-rewritten to raw).
    r = tool_skill_install(rest.strip())
    if r.get("ok"):
        p_ok("installed skill '%s' [OK]" % r.get("name"))
        if r.get("category"):
            print("    category: %s" % r["category"])
    else:
        p_err("failed to install skill: %s" % r.get("error", "unknown error"))
def cmd_clear(history):
    if not history:
        p_info("(conversation is already empty)")
        return
    try:
        v = input("  clear %d messages? [y/N]: " % len(history)).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if v not in ("y", "yes"):
        p_info("(cleared skipped)")
        return
    history.clear()
    _store_set(HISTORY_KEY, [])
    p_ok("conversation cleared")


def cmd_export(history):
    """Export the conversation as plain text."""
    if not history:
        p_info("(no conversation to export)")
        return
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = os.path.join(DATA_DIR, "conversation_%s.txt" % ts)
    lines = []
    for m in history:
        role = m.get("role", "?")
        content = m.get("content", "")
        if role == "user" and content.startswith("[summary of earlier conversation]"):
            lines.append("## summary (compressed)\n%s\n" % content)
        elif role == "user":
            lines.append("## you\n%s\n" % content)
        elif role == "assistant":
            lines.append("## agent\n%s\n" % content)
        elif role == "tool":
            lines.append("## tool (%s)\n%s\n" % (m.get("tool_call_id", "?"), content[:500]))
    try:
        with open(fname, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        p_ok("exported to %s (%d messages)" % (fname, len(history)))
    except Exception as e:
        p_err("export failed: %s" % e)


def cmd_multi():
    """Collect multi-line input until '.' on its own line or Ctrl+D."""
    print(col(C.DIM, "  (multi-line mode - type '.' alone to submit, Ctrl+C to cancel)"))
    lines = []
    try:
        while True:
            line = input("  > ")
            if line.strip() == ".":
                break
            lines.append(line)
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not lines:
        return None
    return "\n".join(lines)


# ---------------- REPL ----------------
def new_session_name():
    """Name for a fresh, not-yet-titled session (auto-titled from the first message)."""
    return "sess-" + secrets.token_hex(2)


def setup_completion():
    """Tab-complete slash commands via readline (Hermes-style autocomplete,
    adapted to a line-oriented Termux prompt). Also loads + persists input
    history so up-arrow recall survives restarts (important on Termux, where
    you launch the TUI fresh each time you open the app)."""
    try:
        # Clear any in-memory history first so re-loading the file doesn't
        # duplicate entries (readline appends on read_history_file).
        readline.clear_history()
        # Load persisted command history (silent if absent)
        if os.path.exists(HISTORY_PATH):
            readline.read_history_file(HISTORY_PATH)
        readline.set_history_length(2000)  # keep last 2000 entries
        # '/' is a completer delimiter by default, so typing /he<Tab> hands the
        # completer 'he' and _slash_complete's startswith("/") check never
        # fires. Remove it so slash commands actually complete.
        delims = readline.get_completer_delims().replace("/", "")
        readline.set_completer_delims(delims)
        readline.set_completer(_slash_complete)
        readline.parse_and_bind("tab: complete")
    except Exception as e:
        p_info("(tab completion unavailable: %s)" % e)


def save_completion_history():
    """Flush readline history to disk (called on exit and after each turn)."""
    try:
        readline.write_history_file(HISTORY_PATH)
    except Exception:
        pass


def _slash_complete(text, state):
    if text.startswith("/"):
        opts = [c for c in _SLASH_COMMANDS if c.startswith(text.lower())]
        return opts[state] if state < len(opts) else None
    return None


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


def _banner_tools_lines():
    """Hermes-style 'Available Tools' grid: active tools grouped by toolset,
    with a footer noting how many advanced tools are hidden in core mode.

    Returns Rich-markup strings (Hermes' own convention inside Panels).
    """
    active = set(t["function"]["name"] for t in active_tools())
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


def _banner_skills_lines():
    """Hermes-style 'Available Skills' grid: skills grouped by category."""
    lines = ["", "[bold %s]Available Skills[/]" % HERMES_ACCENT]
    try:
        skills = tool_skill_list().get("skills") or []
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


def banner(state):
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
    cfg = active_cfg(state)
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
        "[dim %s]skin[/] %s" % (HERMES_DIM, state.get("skin") or DEFAULT_SKIN),
        "[dim %s]provider[/] %s" % (HERMES_DIM, _markup_safe(state["active"])),
        "[dim %s]config/store:[/] %s" % (HERMES_DIM, _markup_safe(DATA_DIR)),
    ]
    right_lines = _banner_tools_lines() + _banner_skills_lines()
    right_lines.append("")
    right_lines.append("[dim %s]%d/%d tools (%s) · v%s · you are here · /help for commands[/]"
                       % (HERMES_DIM, len(active_tools()), len(TOOLS), _tools._TOOLS_MODE, ALVA_VERSION))

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
        p_info("no API key set for '%s' - run /provider %s or /config" % (state["active"], state["active"]))


def render_status_bar(state, session, elapsed, tools, history):
    """Render a one-line status footer after each agent turn (Hermes-style).

    Uses normal print flow - no raw ANSI cursor jumps, which misalign on
    Termux (no reliable terminal height). Prints a dim, boxed-style line.
    """
    cfg = active_cfg(state)
    skin = CUR_SKIN
    tokens, window = context_usage(history, cfg)
    pct = tokens * 100 // window if window else 0
    ctx_col = skin["ok"] if pct < 60 else (C.YELLOW if pct < 85 else skin["err"])
    parts = [
        col(skin["dim"], session[:16]),
        col(skin["dim"], state["active"] + "/" + (cfg.get("model") or "?")),
        col(ctx_col, "ctx %d%%" % pct),
        col(skin["dim"], "%.1fs" % elapsed),
        col(skin["dim"], "%d tool calls" % (tools or 0)),
    ]
    # Hermes-style footer: dim '│' prefix + space-separated chips.
    print(col(C.DIM, "  " + "│".join([""] + parts)))


def send_message(text, history, state, session):
    """Render the 'you' bubble, run the agent, manage context + sessions.

    Returns the (possibly auto-renamed) session name.
    """
    cfg = active_cfg(state)
    # auto-title a fresh placeholder session from the first user message
    if session.startswith("sess-"):
        new_name = _unique_session_name(auto_title(text))
        if new_name != session:
            _rename_session_in_store(session, new_name)
            session = new_name
    print()
    print_user_turn(text)
    history.append({"role": "user", "content": text})
    trim_history(history)
    # pre-turn safety: only act if the window is nearly full (0.9) - the post-turn
    # check (0.75) is the normal compressor, so both rarely fire in one turn
    if cfg.get("auto_compress", True):
        compress_now(history, cfg, threshold=0.9)
    try:
        res = run_agent_tui(history, cfg)
    except KeyboardInterrupt:
        cancel_agent()
        p_info("cancelled")
        if history:
            history.pop()  # drop the unanswered message
        save_session(session, history)
        return session
    except Exception as e:
        p_err("agent error: %s" % e)
        if history:
            history.pop()
        save_session(session, history)
        return session
    # drop the internal system prompt that run_agent_stream prepends
    history[:] = [m for m in res["history"] if m.get("role") != "system"]
    if res.get("cancelled"):
        p_info("(request stopped)")
        save_session(session, history)
        return session
    streamed = bool(res.get("streamed"))
    tools = res.get("tools", 0)
    content = (res.get("content") or "").strip()
    if not streamed and not tools:
        # Dead turn: no text streamed and no tool calls. This is either a
        # failed request (run_agent_stream caught a RuntimeError and yielded
        # content="error: ...") or a gateway that answered with empty content.
        # Remove the unanswered user message (and any trailing empty assistant
        # ghost) so failed/empty turns don't pile up as consecutive duplicates
        # in the session - retrying via Up+Enter previously stacked one ghost
        # per attempt, which looked like the message was being duplicated.
        while history and history[-1].get("role") == "assistant":
            if history[-1].get("content") or history[-1].get("tool_calls"):
                break
            history.pop()
        if history and history[-1].get("role") == "user":
            history.pop()
        save_session(session, history)
        if content.startswith("error:"):
            render_agent_panel(content)
            p_warn("the turn failed - your message was not saved (retry when the endpoint is back)")
        else:
            p_warn("the model returned an empty response - is the endpoint/streaming working?"
                   "  (your message was not saved - retry)")
        return session
    if content and not streamed:
        render_agent_panel(content)
    # post-turn: auto-compress if the response pushed us past the threshold
    compressed = False
    if cfg.get("auto_compress", True):
        compressed = compress_now(history, cfg)
    tokens, window = context_usage(history, cfg)
    render_status_bar(state, session, res.get("elapsed", 0.0), res.get("tools", 0), history)
    pct = tokens * 100 // window if window else 0
    if not compressed and window and pct >= 85:
        p_warn("context at %d%% of %s - /new starts a fresh session | /compress summarizes older messages"
               % (pct, _fmt_k(window)))
    save_session(session, history)
    return session


def repl():
    state = load_state()
    _sync_tool_mode(state)
    set_active_skin(state)
    # resume the last active session (conversations persist across restarts)
    session = _store_get(ACTIVE_SESSION_KEY) or "default"
    history = load_session(session)
    # last completed turn, for /redo (session-scoped so it can't leak across
    # a /session switch)
    _last_turn = {"session": None, "text": None, "pre": None}
    while True:
        try:
            prompt = col(CUR_SKIN["accent"], "> ") if COLOR else "> "
            line = input(prompt)
        except EOFError:
            print()
            save_completion_history()
            break
        except KeyboardInterrupt:
            print()
            save_completion_history()
            break
        line = line.strip()
        if not line:
            continue

        if line.startswith("/"):
            cmd, _, rest = line.partition(" ")
            c = cmd.lower()
            if c in ("/help", "/h", "/?"):
                cmd_help()
            elif c == "/config":
                cmd_config(state)
            elif c == "/provider":
                cmd_provider(state, rest)
            elif c == "/test":
                cmd_test(state)
            elif c == "/tools":
                arg = rest.strip().lower()
                if arg in TOOL_MODES:
                    _set_tool_mode(state, arg)
                    p_ok("tool mode: %s (%d tools advertised to the model)"
                         % (_tools._TOOLS_MODE, len(active_tools())))
                else:
                    cmd_tools()
            elif c == "/trace":
                cmd_trace(rest)
            elif c == "/models":
                cmd_models(state)
            elif c == "/skin":
                cmd_skin(state, rest)
            elif c == "/sessions":
                cmd_sessions()
            elif c == "/session":
                arg, _, sub = rest.strip().partition(" ")
                arg = arg.strip().lower()
                sub = sub.strip()
                if not arg or arg in ("ls", "list", "show"):
                    cmd_sessions()
                elif arg in ("rm", "remove", "del", "delete"):
                    target = _find_session(sub)
                    if not sub:
                        p_err("usage: /session rm <name>")
                    elif target is None:
                        p_err("no session named '%s'" % sub)
                    elif target.lower() == session.lower():
                        p_err("that's the active session - switch first (/session <name>)")
                    else:
                        delete_session(target)
                        p_ok("deleted session '%s' [OK]" % target)
                elif arg in ("rename", "mv"):
                    old, _, new = sub.partition(" ")
                    old, new = old.strip(), new.strip()
                    target = _find_session(old)
                    if not old or not new:
                        p_err("usage: /session rename <old> <new>")
                    elif target is None:
                        p_err("no session named '%s'" % old)
                    elif _find_session(new):
                        p_err("a session named '%s' already exists" % new)
                    else:
                        _rename_session_in_store(target, new)
                        if session.lower() == target.lower():
                            session = new
                        p_ok("renamed '%s' -> '%s' [OK]" % (target, new))
                else:
                    save_session(session, history)  # persist the outgoing session
                    target = _find_session(arg)
                    if target is None:
                        target = arg
                        p_info("(new session '%s')" % target)
                    history[:] = load_session(target)
                    session = target
                    save_session(session, history)  # mark active + refresh timestamp
                    p_ok("switched to session '%s' | %d messages" % (session, len(history)))
            elif c == "/context":
                cmd_context(state, rest, history)
            elif c == "/compress":
                cmd_compress(history, state, session)
            elif c == "/new":
                save_session(session, history)
                cmd_clear(history)
                session = new_session_name()
                save_session(session, history)
                p_ok("new session: " + session)
            elif c == "/clear":
                cmd_clear(history)
            elif c == "/multi":
                text = cmd_multi()
                if text and text.strip():
                    session = send_message(text.strip(), history, state, session)
            elif c == "/install_skill":
                cmd_install_skill(rest)
            elif c == "/self-test":
                cmd_self_test()
            elif c == "/improve":
                cmd_improve(rest)
            elif c == "/skills":
                cmd_skills(rest)
            elif c == "/skill":
                op, _, arg = rest.strip().partition(" ")
                op = op.strip().lower()
                if op in ("rm", "remove", "del", "delete"):
                    if not arg:
                        p_err("usage: /skill rm <name>")
                        continue
                    r = tool_skill_remove(arg)
                    if r.get("ok"):
                        p_ok("removed skill '%s' (category: %s) [OK]"
                             % (r.get("name", "?"), r.get("category") or "(flat)"))
                    else:
                        p_err("  " + r.get("error", "?"))
                elif op in ("cat", "category", "cats", "categories"):
                    cmd_skill_category(arg)
                else:
                    p_err("usage: /skill rm <name>  |  /skill category [name]")
                continue
            elif c == "/memory":
                cmd_memory()
            elif c == "/export":
                cmd_export(history)
            elif c == "/stop":
                cancel_agent()
                p_info("stopping...")
            elif c == "/redo":
                if _last_turn.get("session") != session or _last_turn.get("text") is None:
                    p_err("nothing to redo - send a message first (in this session)")
                    continue
                history[:] = _last_turn["pre"]
                p_info("(re-running: %s)" % _last_turn["text"][:80])
                session = send_message(_last_turn["text"], history, state, session)
            elif c in ("/exit", "/quit", "/q"):
                break
            else:
                p_err("unknown command: " + c + "   (/help for the list)")
            continue

        _last_turn["session"] = session
        _last_turn["text"] = line
        _last_turn["pre"] = list(history)
        session = send_message(line, history, state, session)
        save_completion_history()  # persist input history after each turn
    save_session(session, history)
    save_completion_history()  # flush readline history to disk on exit
    print(col(C.DIM, "bye"))


def main():
    global ON_TOOL
    _load_store()
    setup_completion()
    ON_TOOL = on_tool        # live tool-progress blocks
    import alvaagent.permissions as _perms
    _perms.ON_PERMISSION = ask_permission  # interactive y/N for risky actions
    state = load_state()
    _sync_tool_mode(state)
    set_active_skin(state)

    # Guarantee screen restoration even on SIGTERM / OOM kill / crash.
    # SIGTERM and SIGINT both route through _cleanup so the alternate-screen
    # escape always lands; without this, `kill` from another session leaves the
    # stale TUI buffer on screen (the issue the user hit).
    _restored = threading.Event()

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

    def _on_sigterm(signum, frame):
        _cleanup(signum, frame)

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except Exception:
        pass
    # SIGINT is deliberately left untouched (Python's default raises
    # KeyboardInterrupt): the REPL's `except KeyboardInterrupt` (Ctrl+C during
    # input), the agent's `except KeyboardInterrupt` (Ctrl+C during a network
    # call), and any library that catches it all work as expected, and the
    # `finally: _cleanup()` below still runs on every exit path. Do NOT set
    # SIGINT to SIG_DFL here — that kills the process outright, skipping both
    # KeyboardInterrupt handling and _cleanup(), leaving the terminal stuck in
    # the alternate-screen buffer.

    # Alternate-screen buffer: take over the whole terminal like Hermes' TUI
    # (prior scrollback hidden on launch, restored on exit). Emit the enter
    # code, run, and always emit the leave code (even on Ctrl-C / error).
    sys.stdout.write("\x1b[?1049h")
    sys.stdout.flush()
    try:
        banner(state)
        repl()
    finally:
        _cleanup()


if __name__ == "__main__":
    main()
