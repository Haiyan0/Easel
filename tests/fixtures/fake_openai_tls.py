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
import time
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

    def _sse(self, payload: dict) -> None:
        self.wfile.write(
            b"data: " +
            json.dumps(payload, ensure_ascii=False,
                       separators=(",", ":")).encode("utf-8") + b"\n\n")
        self.wfile.flush()

    def _stream(self, model: str, *, drop: bool = False) -> None:
        """OpenAI chat.completion.chunk SSE；drop=True 时故意不发终止事件。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        base = {
            "id": "fixture-completion",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
        }
        chunks = [
            {**base, "choices": [{
                "index": 0, "delta": {"role": "assistant"},
                "finish_reason": None}]},
        ]
        text = "fake-provider-ok"
        for index in range(0, len(text), 7):
            chunks.append({**base, "choices": [{
                "index": 0, "delta": {"content": text[index:index + 7]},
                "finish_reason": None}]})
        chunks.append({**base, "choices": [{
            "index": 0, "delta": {}, "finish_reason": "stop"}]})
        for chunk in chunks:
            self._sse(chunk)
            if drop:
                # 只发第一块后断开，模拟上游中途断流。
                self.close_connection = True
                try:
                    self.connection.shutdown(2)
                except OSError:
                    pass
                return
        self._sse({**base, "choices": [], "usage": {
            "prompt_tokens": 1, "completion_tokens": 3,
            "total_tokens": 4}})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _stream_slow(self, model: str) -> None:
        """A6 慢速流：约 8 秒逐块输出，供浏览器断网后按 after 续接。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        base = {
            "id": "fixture-slow",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
        }
        pieces = ["slow-start "]
        pieces.extend(f"slow-chunk-{index:02d} " for index in range(1, 13))
        pieces.append("slow-end-ok")
        try:
            self._sse({**base, "choices": [{
                "index": 0, "delta": {"role": "assistant"},
                "finish_reason": None}]})
            for piece in pieces:
                self._sse({**base, "choices": [{
                    "index": 0, "delta": {"content": piece},
                    "finish_reason": None}]})
                time.sleep(0.8)
            self._sse({**base, "choices": [{
                "index": 0, "delta": {}, "finish_reason": "stop"}]})
            self._sse({**base, "choices": [], "usage": {
                "prompt_tokens": 1, "completion_tokens": 3,
                "total_tokens": 4}})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # 浏览器/worker 中断连接：慢流仅用于测试，安静退出。
            self.close_connection = True

    def _stream_hang(self, model: str) -> None:
        """A6 挂起流：持续输出直到 worker 停止或连接断开（最长 5 分钟）。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        base = {
            "id": "fixture-hang",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
        }
        try:
            self._sse({**base, "choices": [{
                "index": 0, "delta": {"role": "assistant"},
                "finish_reason": None}]})
            for index in range(600):
                self._sse({**base, "choices": [{
                    "index": 0,
                    "delta": {"content": f"hang-token-{index:03d} "},
                    "finish_reason": None}]})
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _stream_tool_call(self, model: str) -> None:
        """标准 OpenAI tool_calls 流：请求 Harness 以提权方式执行一次 echo。

        参数带 ``sandbox_permissions``/``justification``，Harness 必须先经
        ACP ``session/request_permission`` 征求用户同意，才执行工具。
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        base = {
            "id": "fixture-tool-call",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
        }
        arguments = json.dumps({
            "description": "Echo an A5 tool marker",
            "command": "echo harness-tool-ok",
            "sandbox_permissions": "danger-full-access",
            "justification": "A5 verification needs one unsandboxed echo",
        }, ensure_ascii=False, separators=(",", ":"))
        chunks = [
            {**base, "choices": [{
                "index": 0, "finish_reason": None,
                "delta": {"role": "assistant", "tool_calls": [{
                    "index": 0, "id": "call_a5_harness_tool",
                    "type": "function",
                    "function": {"name": "bash", "arguments": ""}}]}}]},
            {**base, "choices": [{
                "index": 0, "finish_reason": None,
                "delta": {"tool_calls": [{
                    "index": 0,
                    "function": {"arguments": arguments}}]}}]},
            {**base, "choices": [{
                "index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ]
        for chunk in chunks:
            self._sse(chunk)
        self._sse({**base, "choices": [], "usage": {
            "prompt_tokens": 1, "completion_tokens": 3,
            "total_tokens": 4}})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    @staticmethod
    def _has_tool_result(body: object) -> bool:
        """请求里是否已带工具结果（第二回合），避免再次发起工具调用。"""
        if not isinstance(body, dict):
            return False
        messages = body.get("messages")
        if not isinstance(messages, list):
            return False
        return any(isinstance(message, dict) and message.get("role") == "tool"
                   for message in messages)

    @staticmethod
    def _content_text(message: object) -> str:
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if (isinstance(block, dict)
                        and isinstance(block.get("text"), str)):
                    parts.append(block["text"])
            return "\n".join(parts)
        return ""

    @classmethod
    def _last_user_text(cls, body: object) -> str:
        """当前轮 user 文本：跳过 Harness 末尾的运行上下文快照消息。"""
        if not isinstance(body, dict):
            return ""
        messages = body.get("messages")
        if not isinstance(messages, list):
            return ""
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            text = cls._content_text(message)
            if text.lstrip().startswith("Current runtime context"):
                continue
            return text
        return ""

    @staticmethod
    def _prompt_text(body: object) -> str:
        if not isinstance(body, dict):
            return ""
        messages = body.get("messages")
        if not isinstance(messages, list):
            return ""
        parts: list[str] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        text = block.get("text")
                        if isinstance(text, str):
                            parts.append(text)
        return "\n".join(parts)

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
        try:
            payload = json.loads(body) if body else {}
        except ValueError:
            payload = {}
        model = payload.get("model") if isinstance(payload, dict) else None
        if not isinstance(model, str) or not model:
            model = "fixture-chat"
        prompt = self._last_user_text(payload)
        if "harness-401" in prompt:
            self._json(401, {"error": {
                "type": "authentication_error", "message": "fake auth"}})
            return
        if "harness-429" in prompt:
            self._json(429, {"error": {
                "type": "rate_limit_error", "message": "fake rate limit"}})
            return
        if "harness-500" in prompt:
            self._json(500, {"error": {
                "type": "api_error", "message": "fake server error"}})
            return
        stream = isinstance(payload, dict) and payload.get("stream") is True
        if stream:
            if "harness-hang" in prompt:
                # A6 停止/注销在途长流：持续输出直到 worker 终止连接。
                self._stream_hang(model)
                return
            if "harness-slow" in prompt:
                # A6 浏览器断网重连：约 8 秒慢速流，服务端继续执行。
                self._stream_slow(model)
                return
            if "harness-tool" in prompt and not self._has_tool_result(payload):
                # 首回合：让真实 Harness 发起一次需要审批的工具调用。
                self._stream_tool_call(model)
                return
            self._stream(model, drop="harness-drop" in prompt)
            return
        self._json(200, {
            "id": "fixture-completion",
            "object": "chat.completion",
            "created": 0,
            "model": model,
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
