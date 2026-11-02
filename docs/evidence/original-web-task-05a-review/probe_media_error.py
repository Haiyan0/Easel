"""主审：真实 TCP 错误响应在流式 GatewayClient 中仍须有界收尾。"""
import asyncio
import sys
from pathlib import Path

IMPLEMENTATION = Path(__file__).resolve().parents[3] / '.claude/worktrees/original-web-task1-diagnostics'
sys.path.insert(0, str(IMPLEMENTATION))

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from easel.control.gateway_client import GatewayClient
from easel.control.types import WorkerHandle


async def main():
    async def serve(reader, writer):
        await reader.readuntil(b'\r\n\r\n')
        body = b'{"detail":"not found"}'
        writer.write(b'HTTP/1.1 404 Not Found\r\nContent-Type: application/json\r\nContent-Length: '
                     + str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    handle = WorkerHandle(
        'a' * 32, 'c' * 64, 'http://127.0.0.1:9999', 'fake', 0,
        gateway_instance_id='gateway', gateway_generation='generation',
        gateway_upstream=f'http://127.0.0.1:{port}', gateway_state='available')
    async with server, httpx.AsyncClient(trust_env=False) as client:
        for stream in (False, True):
            try:
                await GatewayClient(Ed25519PrivateKey.generate(), client).request(
                    handle, 'GET', '/api/media/missing.png', stream=stream,
                    status_mode='workspace')
            except Exception as error:
                print(f'stream={stream} type={type(error).__name__} status={getattr(error, "status", None)}')
    print('NO_USER_RUNTIME_TOUCHED=True')


if __name__ == '__main__':
    asyncio.run(main())
