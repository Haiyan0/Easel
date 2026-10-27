"""A4 测试用 HTTPS OpenAI-compatible fake provider（仅测试）。

记录实际收到的 Authorization/模型/消息到审计 JSONL；不连接真实供应商，
不使用真实计费 Key。证书由测试临时 CA 签发，控制面以 SSL_CERT_FILE 校验。
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args) -> None:
        pass

    def _record(self, body: bytes) -> None:
        payload = None
        if body:
            try:
                payload = json.loads(body)
            except ValueError:
                payload = body.decode("utf-8", "replace")
        record = {
            "method": self.command,
            "path": self.path,
            "authorization": self.headers.get("Authorization", ""),
            "body": payload,
            "pid": os.getpid(),
        }
        with open(self.server.audit, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False,
                                    separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._record(b"")
        if self.path == "/v1/models":
            self._json(200, {"object": "list", "data": [
                {"id": "fixture-chat", "object": "model"}]})
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b""
        self._record(body)
        if self.path not in ("/v1/chat/completions",):
            self._json(404, {"error": "not_found"})
            return
        self._json(200, {
            "id": "fixture-completion",
            "object": "chat.completion",
            "created": 0,
            "model": "fixture-chat",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant",
                            "content": "fake-provider-ok"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2},
        })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--audit", required=True)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    server.audit = args.audit
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.cert, args.key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
