import html
import json
import re
import time

from alvaagent.client import SYSTEM_PROMPT, _Cancelled, chat_completion, chat_completion_stream
from alvaagent.tools import dispatch_tool, visible
from alvaagent.trace import trace

# ---------------- agent loop ----------------
MAX_STEPS = 25
_TURN_TIMEOUT = 180
_MAX_CONSEC_TOOL_FAILURES = 4



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


def _report_tool(rt, tool_id, name, args, result, status):
    if rt.on_tool is not None:
        try:
            rt.on_tool(tool_id, name, args, result, status)
        except Exception:
            pass


def run_agent(rt, history_json):
    history = json.loads(str(history_json))
    config = rt.active_cfg
    rt.cancel.clear()
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
    trace(rt, {"event": "turn_start", "steps": 0})
    for step in range(MAX_STEPS):
        if rt.cancel.is_set():
            trace(rt, {"event": "turn_end", "reason": "cancelled", "steps": step})
            return json.dumps({"content": "(stopped by user)", "history": messages, "cancelled": True})
        if _TURN_TIMEOUT <= 0 or time.monotonic() - _t0 > _TURN_TIMEOUT:
            note = "(stopped: the turn exceeded the %d-second time budget)" % int(_TURN_TIMEOUT)
            messages.append({"role": "assistant", "content": note})
            trace(rt, {"event": "turn_end", "reason": "timeout", "steps": step})
            return json.dumps({"content": note, "history": messages, "cancelled": False})
        data = chat_completion(rt, messages, config, tools=visible(rt))
        msg = data["choices"][0]["message"]
        if msg.get("content") is None:
            msg["content"] = ""
        messages.append(msg)

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            trace(rt, {"event": "turn_end", "reason": "answer", "steps": step + 1})
            return json.dumps({"content": msg.get("content") or "", "history": messages, "cancelled": False})

        for tc in tool_calls:
            if rt.cancel.is_set():
                trace(rt, {"event": "turn_end", "reason": "cancelled", "steps": step + 1})
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
            _report_tool(rt, tool_id, name, args, None, "running")
            trace(rt, {"event": "tool", "name": name, "args": args})
            result = dispatch_tool(rt, name, args)
            status = "done" if (isinstance(result, dict) and "error" not in result) else "error"
            if status == "error":
                consec_failures += 1
            else:
                consec_failures = 0
            trace(rt, {"event": "tool", "name": name, "status": status})
            _report_tool(rt, tool_id, name, args, result, status)
            messages.append({"role": "tool", "tool_call_id": tool_id, "content": json.dumps(result)})

        if consec_failures >= _MAX_CONSEC_TOOL_FAILURES:
            trace(rt, {"event": "turn_end", "reason": "circuit_breaker", "steps": step + 1,
                    "consec_failures": consec_failures})
            note = "(stopped early: %d tools in a row failed - the current approach is not working)" % consec_failures
            messages.append({"role": "assistant", "content": note})
            return json.dumps({"content": note, "history": messages, "cancelled": False})

    trace(rt, {"event": "turn_end", "reason": "max_steps", "steps": MAX_STEPS})
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


def run_agent_stream(rt, history):
    """Generator that yields ('text', chunk) or ('tool', tool_info) or ('done', final_dict)."""
    config = rt.active_cfg
    rt.cancel.clear()
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
    trace(rt, {"event": "turn_start", "steps": 0})
    for step in range(MAX_STEPS):
        if rt.cancel.is_set():
            trace(rt, {"event": "turn_end", "reason": "cancelled", "steps": step})
            yield "done", {"content": "(stopped by user)", "history": messages, "cancelled": True}
            return
        if _TURN_TIMEOUT <= 0 or time.monotonic() - _t0 > _TURN_TIMEOUT:
            note = "(stopped: the turn exceeded the %d-second time budget)" % int(_TURN_TIMEOUT)
            messages.append({"role": "assistant", "content": note})
            trace(rt, {"event": "turn_end", "reason": "timeout", "steps": step})
            yield "done", {"content": note, "history": messages, "cancelled": False}
            return

        # Use streaming to detect tool calls and collect text
        content_parts = []
        tool_calls_result = None
        try:
            for chunk, tcs in chat_completion_stream(rt, messages, config, tools=visible(rt)):
                if rt.cancel.is_set():
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
                if rt.cancel.is_set():
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
                trace(rt, {"event": "tool", "name": name, "args": args})
                result = dispatch_tool(rt, name, args)
                status = "done" if (isinstance(result, dict) and "error" not in result) else "error"
                if status == "error":
                    consec_failures += 1
                else:
                    consec_failures = 0
                trace(rt, {"event": "tool", "name": name, "status": status})
                yield "tool_end", {"name": name, "args": args, "result": result, "status": status}
                messages.append({"role": "tool", "tool_call_id": tool_id, "content": json.dumps(result)})
        elif has_xml:
            xml_calls = _parse_xml_tool_calls(full_content)
            messages.append(msg)
            for i, (name, args) in enumerate(xml_calls):
                if rt.cancel.is_set():
                    yield "done", {"content": "(stopped by user)", "history": messages, "cancelled": True}
                    return
                yield "tool_start", {"name": name, "args": args}
                trace(rt, {"event": "tool", "name": name, "args": args})
                result = dispatch_tool(rt, name, args)
                status = "done" if (isinstance(result, dict) and "error" not in result) else "error"
                if status == "error":
                    consec_failures += 1
                else:
                    consec_failures = 0
                trace(rt, {"event": "tool", "name": name, "status": status})
                yield "tool_end", {"name": name, "args": args, "result": result, "status": status}
                messages.append({"role": "tool", "tool_call_id": "xml_%d" % i, "content": json.dumps(result)})
        else:
            messages.append(msg)
            trace(rt, {"event": "turn_end", "reason": "answer", "steps": step + 1})
            yield "done", {"content": msg["content"], "history": messages, "cancelled": False}
            return

        if consec_failures >= _MAX_CONSEC_TOOL_FAILURES:
            note = "(stopped early: %d tools in a row failed - the current approach is not working)" % consec_failures
            messages.append({"role": "assistant", "content": note})
            trace(rt, {"event": "turn_end", "reason": "circuit_breaker", "steps": step + 1, "consec_failures": consec_failures})
            yield "done", {"content": note, "history": messages, "cancelled": False}
            return

    trace(rt, {"event": "turn_end", "reason": "max_steps", "steps": MAX_STEPS})
    yield "done", {"content": "(reached the maximum number of tool steps)", "history": messages, "cancelled": False}
