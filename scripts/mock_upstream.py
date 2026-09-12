"""A dependency-free mock OpenAI-compatible upstream.

Purpose: prove the whole routing / retry / failover / accounting pipeline works
end to end **without spending a single token of real quota**.

Usage::

    uv run python scripts/mock_upstream.py --port 8099

    # point the openai provider at it, then run the gateway
    OPENAI_BASE_URL=http://127.0.0.1:8099/v1 OPENAI_KEY_01=mock-key \\
        uv run uvicorn app.main:app --port 8000

    curl -s http://127.0.0.1:8000/v1/chat/completions \\
        -H 'content-type: application/json' \\
        -d '{"model":"zk-coding","messages":[{"role":"user","content":"hi"}]}'

Fault injection - any model name containing a marker triggers it:
    contains ``fail-401`` / ``fail-429`` / ``fail-500`` / ``fail-400`` -> that status
    contains ``slow``    -> 3s delay before responding
    contains ``broken``  -> malformed JSON body
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ERROR_BODIES = {
    400: {"error": {"message": "mock: invalid request", "type": "invalid_request_error"}},
    401: {"error": {"message": "mock: invalid api key", "type": "authentication_error"}},
    403: {"error": {"message": "mock: permission denied", "type": "permission_denied"}},
    404: {"error": {"message": "mock: model not found", "type": "not_found_error"}},
    429: {"error": {"message": "mock: rate limit exceeded", "type": "rate_limit_error"}},
    500: {"error": {"message": "mock: internal error", "type": "server_error"}},
    503: {"error": {"message": "mock: service unavailable", "type": "server_error"}},
    529: {"error": {"message": "mock: overloaded", "type": "overloaded_error"}},
}


def injected_status(model: str) -> int | None:
    for code in ERROR_BODIES:
        if f"fail-{code}" in model:
            return code
    return None


class MockHandler(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible surface."""

    protocol_version = "HTTP/1.1"
    server_version = "zkai-mock/0.1"

    # ------------------------------------------------------------------ #
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("content-length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    def log_message(self, fmt: str, *args) -> None:
        print(f"[mock] {self.command} {self.path} {fmt % args}", flush=True)

    # ------------------------------------------------------------------ #
    def do_GET(self) -> None:
        if self.path.rstrip("/").endswith("/models"):
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": "mock-model", "object": "model", "owned_by": "zkai-mock"},
                        {"id": "mock-model-cheap", "object": "model", "owned_by": "zkai-mock"},
                        {"id": "mock-model-smart", "object": "model", "owned_by": "zkai-mock"},
                    ],
                },
            )
            return
        self._json(404, {"error": {"message": "unknown path"}})

    def do_POST(self) -> None:
        payload = self._read_body()
        model = str(payload.get("model") or "mock-model")
        stream = bool(payload.get("stream"))

        status = injected_status(model)
        if status is not None:
            self._json(status, ERROR_BODIES[status])
            return
        if "slow" in model:
            time.sleep(3)
        if "broken" in model:
            body = b"{not-json"
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        prompt_tokens = sum(
            len(str(message.get("content") or "")) // 4
            for message in payload.get("messages") or []
        ) or 1
        text = f"mock reply from {model}: " + " ".join(
            ["token"] * 8
        )

        if not stream:
            self._json(
                200,
                {
                    "id": f"mock-{int(time.time() * 1000)}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": text},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": len(text) // 4,
                        "total_tokens": prompt_tokens + len(text) // 4,
                    },
                },
            )
            return

        # --- SSE streaming -------------------------------------------------
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()

        chunk_id = f"mock-{int(time.time() * 1000)}"
        words = text.split(" ")
        for index, word in enumerate(words):
            delta: dict = {"content": word + (" " if index < len(words) - 1 else "")}
            if index == 0:
                delta["role"] = "assistant"
            chunk = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()

        final = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": len(words),
                "total_tokens": prompt_tokens + len(words),
            },
        }
        self.wfile.write(f"data: {json.dumps(final)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description="Mock OpenAI-compatible upstream")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), MockHandler)
    print(f"mock upstream listening on http://{args.host}:{args.port}/v1", flush=True)
    print("fault injection: model names containing fail-400/401/429/500/503/529, slow, broken")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
