"""独立的受控出口代理（CONNECT-only）。

broker 只把 HTTPS 请求发到本代理；本进程在连接阶段解析目标主机、校验全部
地址并把连接钉在验证过的 IP 上，避免 DNS 重绑定。默认只允许公网 HTTPS:443；
管理员可通过 policy 文件登记精确主机/网段例外。

运行示例：

    python deploy/multiuser/egress_proxy.py \
        --listen 127.0.0.1 --port 8899 \
        --policy deploy/multiuser/egress-policy.json
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from easel.control.egress import (  # noqa: E402
    EgressPolicy,
    load_policy,
    parse_proxy_authority,
)
from easel.control.types import DomainError  # noqa: E402

_CONNECT_TIMEOUT = 10.0


async def _respond(writer: asyncio.StreamWriter, status: int, reason: str) -> None:
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


async def _pipe(reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (OSError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                  policy: EgressPolicy) -> None:
    upstream_writer: asyncio.StreamWriter | None = None
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=_CONNECT_TIMEOUT)
        parts = line.decode("latin-1").strip().split(" ")
        if len(parts) != 3 or parts[0].upper() != "CONNECT":
            await _respond(writer, 405, "Method Not Allowed")
            return
        host, port = parse_proxy_authority(parts[1])
        if port != 443:
            # 只允许 HTTPS；不回退明文 HTTP
            await _respond(writer, 403, "Forbidden")
            return
        # 丢弃请求头，不信任客户端提交的任何转发/认证头
        while True:
            header = await asyncio.wait_for(reader.readline(),
                                            timeout=_CONNECT_TIMEOUT)
            if header in (b"\r\n", b"\n", b""):
                break

        loop = asyncio.get_running_loop()
        pinned = await loop.run_in_executor(
            None, policy.resolve_and_validate, host, port)
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(pinned, port), timeout=_CONNECT_TIMEOUT)
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(
            _pipe(reader, upstream_writer),
            _pipe(upstream_reader, writer),
        )
    except DomainError as error:
        await _respond(writer, error.status, error.code)
    except (ValueError, OSError, asyncio.TimeoutError):
        # 不记录客户端提交的内容；失败一律拒绝
        await _respond(writer, 403, "Forbidden")
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


async def serve(host: str, port: int, policy: EgressPolicy) -> None:
    server = await asyncio.start_server(
        lambda reader, writer: _handle(reader, writer, policy), host, port)
    async with server:
        await server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Easel 受控出口代理（CONNECT-only）")
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--policy", required=True,
                        help="管理员出口策略 JSON（approved_hosts/approved_cidrs）")
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy)
    except ValueError as error:
        print(f"egress_policy_invalid: {error}", file=sys.stderr)
        return 2
    try:
        asyncio.run(serve(args.listen, args.port, policy))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
