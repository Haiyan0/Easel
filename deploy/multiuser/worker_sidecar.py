#!/usr/bin/env python3
"""worker 每空间 sidecar：broker 精确路由转发 + 受控 CONNECT 出口。

sidecar 同时接入 workspace internal 网络和一条受控出站网络：
* 对 worker 暴露固定的 broker 入口，只转发 ``BROKER_ROUTE_TABLE`` 中的
  精确 channel/method/path 到 control broker；不转发其他管理接口。
* 对 worker 暴露 CONNECT-only 出口代理，复用 Task 5 的解析后固定 IP、
  私网/元数据拒绝与管理员策略。

worker 自身没有默认路由；不能直连 control、其他 workspace 或公网。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEPLOY = Path(__file__).resolve().parent
if str(DEPLOY) not in sys.path:
    sys.path.insert(0, str(DEPLOY))
# providers.openai 通过共享脚本导入 model_registry；镜像与部署目录均包含它。
SHARED = ROOT / "skills" / "shared" / "scripts"
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

from easel.control.egress import (  # noqa: E402
    EgressPolicy,
    load_policy,
    parse_proxy_authority,
)
from easel.providers.openai import BROKER_ROUTE_TABLE  # noqa: E402
import egress_proxy  # noqa: E402

MAX_HEADER_BYTES = 64 * 1024
MAX_BODY_BYTES = 64 * 1024 * 1024
CONNECT_TIMEOUT = 10.0
IO_TIMEOUT = 120.0
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host",
})

BROKER_PATHS = {
    (method.upper(), f"/internal/broker/{channel}/{relative}"): body_kind
    for channel, method, relative, body_kind in BROKER_ROUTE_TABLE
}


async def _error(writer: asyncio.StreamWriter, status: int,
                 reason: str) -> None:
    body = reason.encode("ascii", "replace")
    writer.write(
        f"HTTP/1.1 {status} {reason}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n".encode("ascii") + body)
    try:
        await writer.drain()
    except OSError:
        pass


def _parse_request_line(line: bytes) -> tuple[str, str, str] | None:
    try:
        text = line.decode("latin-1").strip()
    except UnicodeDecodeError:
        return None
    parts = text.split(" ")
    if len(parts) != 3:
        return None
    method, target, version = parts
    if version not in ("HTTP/1.1", "HTTP/1.0"):
        return None
    return method.upper(), target, version


def _approved_route(method: str, target: str) -> bool:
    # 只接受 origin-form 精确路径；拒绝 query、fragment、编码绕过和绝对 URI。
    if not target.startswith("/internal/broker/"):
        return False
    if "?" in target or "#" in target or "%" in target:
        return False
    try:
        parsed = urlsplit(target)
    except ValueError:
        return False
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return False
    return (method, target) in BROKER_PATHS


async def _handle_broker(reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter,
                         upstream: tuple[str, int]) -> None:
    upstream_writer: asyncio.StreamWriter | None = None
    try:
        line = await asyncio.wait_for(reader.readline(), CONNECT_TIMEOUT)
        parsed = _parse_request_line(line)
        if parsed is None:
            await _error(writer, 400, "Bad Request")
            return
        method, target, _version = parsed
        if method == "GET" and target == "/healthz":
            await _error(writer, 200, "OK")
            return
        if not _approved_route(method, target):
            await _error(writer, 404, "Not Found")
            return

        headers: list[tuple[str, str]] = []
        total = 0
        while True:
            raw = await asyncio.wait_for(reader.readline(), CONNECT_TIMEOUT)
            total += len(raw)
            if total > MAX_HEADER_BYTES:
                await _error(writer, 431, "Request Header Fields Too Large")
                return
            if raw in (b"\r\n", b"\n", b""):
                break
            try:
                name, value = raw.decode("latin-1").split(":", 1)
            except ValueError:
                await _error(writer, 400, "Bad Request")
                return
            lowered = name.strip().lower()
            if lowered == "transfer-encoding":
                await _error(writer, 411, "Length Required")
                return
            if lowered in _HOP_BY_HOP:
                continue
            headers.append((name.strip(), value.strip()))

        content_length = 0
        seen_length = False
        for name, value in headers:
            if name.lower() == "content-length":
                if not value.isdigit() or seen_length:
                    await _error(writer, 400, "Bad Request")
                    return
                seen_length = True
                content_length = int(value)
                if content_length > MAX_BODY_BYTES:
                    await _error(writer, 413, "Payload Too Large")
                    return
        if method in ("POST", "PUT", "PATCH") and content_length == 0:
            # broker 的 JSON/multipart 请求都必须显式长度；不支持 chunked。
            await _error(writer, 411, "Length Required")
            return
        if method not in ("POST", "PUT", "PATCH") and content_length > 0:
            await _error(writer, 400, "Bad Request")
            return

        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(*upstream), CONNECT_TIMEOUT)
        except (OSError, asyncio.TimeoutError):
            await _error(writer, 502, "Bad Gateway")
            return

        request_lines = [f"{method} {target} HTTP/1.1"]
        for name, value in headers:
            if name.lower() in ("content-length", "connection"):
                continue
            request_lines.append(f"{name}: {value}")
        request_lines.append(f"Host: {upstream[0]}:{upstream[1]}")
        request_lines.append("Connection: close")
        if method in ("POST", "PUT", "PATCH"):
            request_lines.append(f"Content-Length: {content_length}")
        upstream_writer.write(
            ("\r\n".join(request_lines) + "\r\n\r\n").encode("latin-1"))
        await upstream_writer.drain()

        remaining = content_length
        while remaining > 0:
            chunk = await asyncio.wait_for(
                reader.read(min(65536, remaining)), IO_TIMEOUT)
            if not chunk:
                await _error(writer, 400, "Bad Request")
                return
            remaining -= len(chunk)
            upstream_writer.write(chunk)
            await upstream_writer.drain()

        while True:
            chunk = await asyncio.wait_for(upstream_reader.read(65536),
                                           IO_TIMEOUT)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (asyncio.TimeoutError, OSError, ValueError):
        await _error(writer, 502, "Bad Gateway")
    finally:
        if upstream_writer is not None:
            try:
                upstream_writer.close()
            except OSError:
                pass
        try:
            writer.close()
        except OSError:
            pass


async def _serve(host: str, broker_port: int, egress_port: int,
                 upstream: tuple[str, int], policy: EgressPolicy) -> None:
    broker_server = await asyncio.start_server(
        lambda reader, writer: _handle_broker(reader, writer, upstream),
        host, broker_port)
    egress_server = await asyncio.start_server(
        lambda reader, writer: egress_proxy._handle(reader, writer, policy),
        host, egress_port)
    async with broker_server, egress_server:
        await asyncio.gather(
            broker_server.serve_forever(),
            egress_server.serve_forever())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="easel-worker-sidecar")
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--broker-port", type=int, default=8899)
    parser.add_argument("--egress-port", type=int, default=8898)
    parser.add_argument("--broker-upstream", required=True,
                        help="control broker HTTP host:port")
    parser.add_argument("--policy", required=True,
                        help="管理员出口策略 JSON")
    args = parser.parse_args(argv)
    try:
        host, port = parse_proxy_authority(args.broker_upstream)
    except ValueError:
        print("broker_upstream_invalid", file=sys.stderr)
        return 2
    if not (1 <= args.broker_port <= 65535
            and 1 <= args.egress_port <= 65535
            and args.broker_port != args.egress_port):
        print("listen_port_invalid", file=sys.stderr)
        return 2
    try:
        policy = load_policy(args.policy)
    except ValueError as error:
        print(f"egress_policy_invalid: {error}", file=sys.stderr)
        return 2
    try:
        asyncio.run(_serve(args.listen, args.broker_port, args.egress_port,
                           (host, port), policy))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
