"""A scripted OpenAI-compatible chat server on a free local port, so a REAL AIAgent loop runs with no provider.

``StubServer(script)``: ``script(request_index, messages) -> Reply`` decides each reply. A ``Reply`` may carry text,
a tool call, a delay before the first byte (a slow model) and a hook run while the request is "in flight" (used to
write a kanban comment mid-run). ``server.requests`` records every call: ``(monotonic_time, messages)``. Requests with
no user turn (auxiliary probes such as titling) are answered with a plain text reply and are not recorded."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional


@dataclass
class Reply:
    text: str = "ok"
    tool_name: Optional[str] = None
    tool_args: Optional[dict] = None
    delay: float = 0.0
    on_request: Optional[Callable[[], None]] = None


class StubServer:
    def __init__(self, script: Callable[[int, list], Reply]):
        self.script = script
        self.requests: list[tuple[float, list]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    stub._answer(self, body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client cancelled the request (a redirect): exactly what some tests provoke

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def _answer(self, handler: BaseHTTPRequestHandler, body: dict) -> None:
        messages = body.get("messages", [])
        if any(m["role"] == "user" for m in messages):
            self.requests.append((time.monotonic(), messages))
            reply = self.script(len(self.requests) - 1, messages)
        else:
            reply = Reply(text="probe")
        if reply.on_request:
            reply.on_request()
        if reply.delay:
            time.sleep(reply.delay)
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        message: dict = {"role": "assistant", "content": reply.text}
        if reply.tool_name:
            message["tool_calls"] = [{"id": "call_1", "type": "function", "function": {
                "name": reply.tool_name, "arguments": json.dumps(reply.tool_args or {})}}]
        finish = "tool_calls" if reply.tool_name else "stop"
        if body.get("stream"):
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.end_headers()

            def chunk(delta, finish_reason=None, extra=None):
                payload = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "stub-model",
                           "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}], **(extra or {})}
                handler.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())

            chunk({"role": "assistant", "content": reply.text})
            if reply.tool_name:
                chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {
                    "name": reply.tool_name, "arguments": json.dumps(reply.tool_args or {})}}]})
            chunk({}, finish, {"usage": usage})
            handler.wfile.write(b"data: [DONE]\n\n")
        else:
            data = json.dumps({"id": "c1", "object": "chat.completion", "created": 0, "model": "stub-model",
                               "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                               "usage": usage}).encode()
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)


def user_texts(messages: list) -> list[str]:
    """Flattened text of every user row of a recorded request."""
    return [m["content"] if isinstance(m["content"], str) else json.dumps(m["content"]) for m in messages if m["role"] == "user"]
