"""R4-R3 真实 Unix socket 契约：Stop 保留围栏，重启后迟到 A 被拒绝。

在 Linux 容器内运行（本机 Windows Python 无 AF_UNIX）。仅使用临时目录和
假 ID/假 gateway；物理 stop 由假 gateway 与空 worker inspect 模拟。
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
from deploy.multiuser import provisioner as provisioner_module  # noqa: E402
from deploy.multiuser.provisioner import (  # noqa: E402
    Provisioner, ProvisionerSettings, ProvisionerError)
from easel.control.runtime import (  # noqa: E402
    RuntimeUnavailable, UnixProvisionerClient)

WORKSPACE = "a" * 32
WORKER = "c" * 64
GATEWAY = "b" * 64
IMAGE = "sha256:" + "d" * 64
CONFIG_VERSION = 1


class FakeGateway:
    async def stop(self, workspace_id, worker_container_id,
                   gateway_instance_id):
        return None

    async def current_instance_id(self, workspace_id):
        return ""


async def _no_worker(workspace_id):
    return None


async def _no_sidecar(workspace_id):
    return None


def _key(path: pathlib.Path) -> pathlib.Path:
    path.write_bytes(
        Ed25519PrivateKey.generate().public_key().public_bytes_raw())
    return path


def _settings(root: pathlib.Path, socket_path: pathlib.Path):
    return ProvisionerSettings(
        image=IMAGE, workspace_root=root / "workspaces",
        socket_path=socket_path, quota_mode="none", allow_unquotaed=True,
        worker_public_key=_key(root / "worker.pub"),
        gateway_image=IMAGE, gateway_control_network="easel-control",
        gateway_public_key=_key(root / "gateway.pub"),
        allowed_uids=frozenset({os.getuid()}))


def _service(settings):
    service = Provisioner(settings)
    service._gateway = FakeGateway()
    service._inspect_raw = _no_worker
    service._stop_sidecar = _no_sidecar
    return service


async def _serve(service, socket_path: pathlib.Path):
    socket_path.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(
        service._handle_client, path=str(socket_path))
    os.chmod(socket_path, 0o660)
    return server


async def main() -> None:
    root = pathlib.Path(tempfile.mkdtemp(prefix="easel-r4r3-socket-"))
    socket_path = root / "provisioner.sock"
    settings = _settings(root, socket_path)
    old, middle, fresh = "1" * 32, "2" * 32, "3" * 32

    # 第一段：通过真实连接提交围栏，再通过真实连接 Stop。
    service = _service(settings)
    server = await _serve(service, socket_path)
    async with server:
        first = UnixProvisionerClient(
            str(socket_path), gateway_required=True)
        await first.commit_attempt(
            WORKSPACE, WORKER, GATEWAY, attempt_id=old)
        second = UnixProvisionerClient(
            str(socket_path), gateway_required=True)
        await second.commit_attempt(
            WORKSPACE, WORKER, GATEWAY, attempt_id=middle)
        record = service._load_attempt(WORKSPACE)
        assert record["fenced_attempts"] == [old, middle]
        await second.stop(WORKSPACE, WORKER, GATEWAY)
        kept = service._load_attempt(WORKSPACE)
        assert kept is not None
        assert kept["status"] == "completed"
        assert kept["scope"] == "none"
        assert kept["worker_created"] is False
        assert kept["gateway_created"] is False
        assert kept["fenced_attempts"] == [old, middle]
        print("STOP_KEPT_FENCE", json.dumps(
            {"fenced_attempts": kept["fenced_attempts"],
             "status": kept["status"], "scope": kept["scope"]},
            sort_keys=True))

    # 第二段：重启 provisioner，迟到 A 在正式 ensure 入口被拒绝；
    # 新 attempt 仍能越过准入（由 sentinel 证明抵达副作用边界）。
    restarted = _service(settings)
    server = await _serve(restarted, socket_path)
    crossed = {"value": False}

    def prepare_workspace(*args, **kwargs):
        crossed["value"] = True
        raise AssertionError("prepare_workspace reachable")

    async with server:
        client = UnixProvisionerClient(
            str(socket_path), gateway_required=True)
        original = provisioner_module.prepare_workspace
        provisioner_module.prepare_workspace = prepare_workspace
        try:
            try:
                await client.ensure(
                    WORKSPACE, CONFIG_VERSION, attempt_id=old)
            except RuntimeUnavailable as error:
                assert error.code == "gateway_attempt_fenced"
            else:
                raise AssertionError("late A crossed admission")
            assert crossed["value"] is False
            try:
                await client.ensure(
                    WORKSPACE, CONFIG_VERSION, attempt_id=fresh)
            except RuntimeUnavailable as error:
                assert error.code == "internal_error"
            else:
                raise AssertionError("fresh attempt did not reach boundary")
            assert crossed["value"] is True
        finally:
            provisioner_module.prepare_workspace = original
        kept = restarted._load_attempt(WORKSPACE)
        assert kept["fenced_attempts"] == [old, middle]
        assert kept["status"] == "completed"
        print("RESTART_LATE_A_FENCED", json.dumps(
            {"rejected": old, "fresh_reached_boundary": True,
             "fenced_attempts": kept["fenced_attempts"]}, sort_keys=True))
    print("UNIX_CONTRACT_OK")


asyncio.run(main())
