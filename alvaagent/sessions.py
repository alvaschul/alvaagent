import re
import secrets

from alvaagent.client import SYSTEM_PROMPT, chat_completion
from alvaagent.config import DEFAULT_CONTEXT_WINDOW, MODEL_CONTEXT
from alvaagent.store import (
    ACTIVE_SESSION_KEY, MAX_SESSIONS, SESSION_KEY,
    _store_get, _store_set,
)
from alvaagent.util import now_iso

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


def new_session_name():
    """Name for a fresh, not-yet-titled session (auto-titled from the first message)."""
    return "sess-" + secrets.token_hex(2)
