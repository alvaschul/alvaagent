#!/usr/bin/env python3
"""
mock_llm_server.py — a tiny OpenAI-compatible chat-completions server (stdlib only)
used to test the alvaagent harness offline, with no real API key needed.

Behavior:
  * First request  (no tool messages yet)     -> assistant sends 2 tool calls:
                                                  calculator("6*7"), todo_add("buy milk")
  * Second request (1 tool result)            -> web_fetch() of the local /mock-page
                                                  (keeps the loop offline & deterministic)
  * Third request  (2 tool results)           -> memory_save(name=Alex) + memory_recall(name)
  * Fourth request (4 tool results)           -> final answer that VERIFIES every tool
                                                 result arrived intact, or a FAIL marker.
  * Any request whose latest user message     -> plain text answer, no tools.
    contains "[plain]"

Usage:  python3 mock_llm_server.py [port]     (default port: 8001)
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8001


def make_tool_call(tc_id, name, args):
    return {"id": tc_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def make_completion(content, tool_calls=None):
    msg = {"role": "assistant", "content": content}
    finish = "stop"
    if tool_calls:
        msg["tool_calls"] = tool_calls
        finish = "tool_calls"
    return {
        "id": "chatcmpl-mock-1",
        "object": "chat.completion",
        "created": 0,
        "model": "mock-model",
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def build_response(messages):
    latest_user = next((m["content"] for m in reversed(messages)
                        if m["role"] == "user"), "")
    tool_msgs = [m for m in messages if m["role"] == "tool"]
    received = {m.get("tool_call_id") for m in tool_msgs}

    if "[plain]" in latest_user:
        return make_completion("PLAIN_OK this is a direct answer, no tools were used.")

    # State machine: send each batch of tool calls until all have been executed.
    if "call_calc" not in received:
        return make_completion(None, tool_calls=[
            make_tool_call("call_calc", "calculator", {"expression": "6*7"}),
            make_tool_call("call_todo", "todo_add", {"text": "buy milk"}),
        ])
    if "call_web" not in received:
        return make_completion(None, tool_calls=[
            make_tool_call("call_web", "web_fetch",
                           {"url": "http://127.0.0.1:%d/mock-page" % PORT}),
        ])
    if "call_mrec" not in received:
        return make_completion(None, tool_calls=[
            make_tool_call("call_msave", "memory_save", {"key": "name", "value": "Alex"}),
            make_tool_call("call_mrec", "memory_recall", {"key": "name"}),
        ])

    # final turn: verify every tool result made it back to the model
    by_id = {m.get("tool_call_id"): m.get("content", "") for m in tool_msgs}

    def check(label, cond):
        return ("OK  " if cond else "FAIL") + " " + label

    findings = [
        check("calculator returned 42", "42" in by_id.get("call_calc", "")),
        check("todo_add returned ok", "buy milk" in by_id.get("call_todo", "")),
        check("web_fetch returned ok", '"ok": true' in by_id.get("call_web", "")),
        check("memory_save executed", '"ok": true' in by_id.get("call_msave", "")),
        check("memory_recall found Alex", '"Alex"' in by_id.get("call_mrec", "")),
    ]
    ok = all(f.startswith("OK") for f in findings)
    content = ("AGENT_LOOP_OK | " if ok else "AGENT_LOOP_FAIL | ") + " | ".join(findings)
    return make_completion(content)


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._send_json({"object": "list",
                             "data": [{"id": "mock-model", "object": "model"},
                                      {"id": "another-mock", "object": "model"}]})
        elif self.path.rstrip("/").endswith("/mock-page"):
            body = (b"<html><body><h1>Mock Page</h1>"
                    b"<p>Hello from the offline mock server.</p></body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send_json({"error": {"message": "not found"}}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json({"error": {"message": "invalid JSON body"}}, 400)
            return
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send_json({"error": {"message": "not found"}}, 404)
            return
        messages = body.get("messages", [])
        resp = build_response(messages)
        self._send_json(resp)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("READY port=%d" % PORT, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
