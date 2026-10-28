"""A6 测试用本地 HTTPS 终结器（仅测试基础设施）。

浏览器访问 ``https://localhost:<port>``，本进程终止 TLS 后把明文 HTTP
转发到本轮 control 容器；不修改产品路径，不关闭全局证书校验，也不写入
最终镜像。证书只在本轮临时目录生成，浏览器用 SPKI pin 限定信任。
"""
from __future__ import annotations

import argparse
import asyncio
import ssl
import sys

CHUNK = 64 * 1024
ACTIVE: set[asyncio.StreamWriter] = set()


async def _pipe(reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass


async def _handle(client_reader: asyncio.StreamReader,
                  client_writer: asyncio.StreamWriter,
                  upstream: tuple[str, int]) -> None:
    ACTIVE.add(client_writer)
    try:
        up_reader, up_writer = await asyncio.open_connection(*upstream)
    except OSError:
        ACTIVE.discard(client_writer)
        client_writer.close()
        return
    ACTIVE.add(up_writer)
    client_task = asyncio.create_task(
        _pipe(client_reader, up_writer))
    upstream_task = asyncio.create_task(
        _pipe(up_reader, client_writer))
    try:
        # 任一方向结束（上游容器重启/浏览器断开）即关闭两端，避免半关闭的
        # TCP 连接把浏览器 SSE 永远挂住。
        done, pending = await asyncio.wait(
            {client_task, upstream_task},
            return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.gather(*done, return_exceptions=True)
    finally:
        for writer in (client_writer, up_writer):
            ACTIVE.discard(writer)
            try:
                writer.close()
            except (OSError, RuntimeError):
                pass


async def _control(reader: asyncio.StreamReader,
                   writer: asyncio.StreamWriter) -> None:
    """A6 故障注入：``drop`` 关闭当前所有浏览器连接，模拟网络中断。"""
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=10)
        if line.strip() == b"drop":
            for active in list(ACTIVE):
                try:
                    active.close()
                except (OSError, RuntimeError):
                    pass
            ACTIVE.clear()
            writer.write(b"ok\n")
            await writer.drain()
    except (ConnectionError, asyncio.TimeoutError):
        pass
    finally:
        writer.close()


async def _serve(args: argparse.Namespace) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.cert, args.key)
    host, _, port = args.upstream.rpartition(":")
    server = await asyncio.start_server(
        lambda reader, writer: _handle(
            reader, writer, (host, int(port))),
        "0.0.0.0", args.port, ssl=context)
    control = None
    if args.control_port:
        control = await asyncio.start_server(
            _control, "0.0.0.0", args.control_port)
    print("tls_terminator_ready", flush=True)
    async with server:
        if control is not None:
            await control.start_serving()
        try:
            await server.serve_forever()
        finally:
            if control is not None:
                control.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="easel-a6-tls-terminator")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--upstream", required=True,
                        help="host:port，例如 control:7861")
    parser.add_argument("--control-port", type=int, default=0,
                        help="可选的故障注入控制端口（drop 命令）")
    args = parser.parse_args(argv)
    try:
        asyncio.run(_serve(args))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
