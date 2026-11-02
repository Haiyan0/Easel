"""R4-R2 真实 Unix socket 契约：commit 围栏经服务端落盘并可跨连接复核。

在 Linux 容器内运行（本机 Windows Python 无 AF_UNIX）。仅使用临时目录和
假 ID，不连接 Docker；服务端不执行任何容器动作。
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, "/src")

from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey)
from deploy.multiuser.provisioner import (  # noqa: E402
    Provisioner, ProvisionerSettings, ProvisionerError)
from easel.control.runtime import UnixProvisionerClient  # noqa: E402

WORKSPACE = "a" * 32
WORKER = "c" * 64
GATEWAY = "b" * 64
IMAGE = "sha256:" + "d" * 64


def _key(path: pathlib.Path) -> pathlib.Path:
    path.write_bytes(
        Ed25519PrivateKey.generate().public_key().public_bytes_raw())
    return path


async def main() -> None:
    root = pathlib.Path(tempfile.mkdtemp(prefix="easel-r4r2-socket-"))
    socket_path = root / "provisioner.sock"
    settings = ProvisionerSettings(
        image=IMAGE, workspace_root=root / "workspaces",
        socket_path=socket_path, quota_mode="none", allow_unquotaed=True,
        worker_public_key=_key(root / "worker.pub"),
        gateway_image=IMAGE, gateway_control_network="easel-control",
        gateway_public_key=_key(root / "gateway.pub"),
        allowed_uids=frozenset({os.getuid()}))
    service = Provisioner(settings)
    server = await asyncio.start_unix_server(
        service._handle_client, path=str(socket_path))
    os.chmod(socket_path, 0o660)
    a, b, c = "1" * 32, "2" * 32, "3" * 32
    async with server:
        # 两个独立连接走真实 JSON 行协议，验证服务端持久语义未回退。
        first_client = UnixProvisionerClient(
            str(socket_path), gateway_required=True)
        await first_client.commit_attempt(
            WORKSPACE, WORKER, GATEWAY, attempt_id=a)
        first = service._load_attempt(WORKSPACE)
        assert first["status"] == "completed"
        assert first["fenced_attempts"] == [a]

        second_client = UnixProvisionerClient(
            str(socket_path), gateway_required=True)
        await second_client.commit_attempt(
            WORKSPACE, WORKER, GATEWAY, attempt_id=b)
        second = service._load_attempt(WORKSPACE)
        assert second["status"] == "completed"
        assert second["fenced_attempts"] == [a, b]

        for fenced in (a, b):
            try:
                service._ensure_attempt_available(WORKSPACE, fenced)
            except ProvisionerError as error:
                assert error.code == "gateway_attempt_fenced"
            else:
                raise AssertionError("fenced attempt accepted")
        service._ensure_attempt_available(WORKSPACE, c)

    print(json.dumps({
        "client": "UnixProvisionerClient",
        "server": "Provisioner._handle_client",
        "attempt_a": a,
        "attempt_b": b,
        "fenced_attempts": second["fenced_attempts"],
        "record_status": second["status"],
        "new_attempt_allowed": c,
    }, sort_keys=True))
    print("UNIX_CONTRACT_OK")


asyncio.run(main())
