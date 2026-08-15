import codecs
import json
import re
import select
import time
import urllib.error
import urllib.request

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


def chat_completion(rt, messages, config, tools=None):
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
        if rt.cancel.is_set():
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


def chat_completion_stream(rt, messages, config, tools=None):
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
        if rt.cancel.is_set():
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
                if rt.cancel.is_set():
                    resp.close()
                    raise _Cancelled()
                try:
                    rlist, _, _ = select.select([sock], [], [], _STREAM_POLL)
                except OSError:
                    rlist = []  # connection already consumed/closed: EOF
                    break
                if rlist:
                    break
                if time.monotonic() - last_byte_at > _STREAM_IDLE_LIMIT:
                    resp.close()
                    raise RuntimeError("LLM stream stalled (no data for %ds)" % _STREAM_IDLE_LIMIT)
            if not rlist:
                break  # socket gone (body fully consumed): nothing left to read
        elif rt.cancel.is_set():
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


def fetch_models(rt, base_url, api_key, timeout=20):
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


def cancel_agent(rt):
    rt.cancel.set()

