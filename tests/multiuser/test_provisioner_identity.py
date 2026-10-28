"""A4 R1-04：provisioner 实际配置/身份只读核验契约。"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "deploy" / "multiuser" / "provisioner.py"
CLIENT_PATH = ROOT / "deploy" / "desktop" / "provisioner_identity.py"
IMAGE = "sha256:" + "a" * 64
GATEWAY_IMAGE = "sha256:" + "b" * 64


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


provisioner = _load("easel_provisioner_identity", MODULE_PATH)


def _keys(tmp_path: Path) -> tuple[Path, Path, Path]:
    worker = tmp_path / "worker-public.key"
    worker.write_bytes(Ed25519PrivateKey.generate().public_key()
                       .public_bytes_raw())
    gateway = tmp_path / "gateway-public.pem"
    gateway.write_bytes(Ed25519PrivateKey.generate().public_key()
                        .public_bytes(serialization.Encoding.PEM,
                                      serialization.PublicFormat.SubjectPublicKeyInfo))
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"approved_hosts": ["provider.test"]}),
                      encoding="utf-8")
    return worker, gateway, policy


class FakeDocker:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, argv, *, timeout=120.0, input=None):
        self.commands.append(list(argv))
        if argv[1:] == ["context", "show"]:
            return subprocess.CompletedProcess(argv, 0, "desktop-linux\n", "")
        if argv[1:] == ["info", "--format", "{{.ID}}"]:
            return subprocess.CompletedProcess(argv, 0, "engine-123\n", "")
        return subprocess.CompletedProcess(argv, 1, "", "unexpected")


def _settings(tmp_path: Path, socket_path: Path):
    worker, gateway, policy = _keys(tmp_path)
    return provisioner.ProvisionerSettings(
        image=IMAGE,
        workspace_root=tmp_path / "workspaces",
        socket_path=socket_path,
        allowed_uids=frozenset({10001}),
        sidecar_network="control-net",
        sidecar_policy=policy,
        broker_upstream="control:7861",
        worker_public_key=worker,
        gateway_image=GATEWAY_IMAGE,
        gateway_control_network="control-net",
        gateway_public_key=gateway,
    )


def test_identity_request_is_v2_only_and_has_no_workspace():
    request = provisioner.parse_request(
        {"version": 2, "action": "identity"})
    assert request.action == "identity"
    assert request.workspace_id == ""
    try:
        provisioner.parse_request({"version": 1, "action": "identity"})
    except provisioner.ProvisionerError as error:
        assert error.code == "unsupported_protocol"
    else:
        raise AssertionError("v1 identity 必须拒绝")


def test_identity_reports_actual_sanitized_configuration(tmp_path):
    socket_path = tmp_path / "provisioner.sock"
    socket_path.touch()
    docker = FakeDocker()
    instance = provisioner.Provisioner(
        _settings(tmp_path, socket_path), run=docker)
    identity = asyncio.run(instance.identity())
    data = json.dumps(identity, sort_keys=True)
    assert "engine-123" in data
    assert identity["engine"] == {
        "context": "desktop-linux", "id": "engine-123"}
    assert identity["images"] == {
        "worker": IMAGE, "gateway": GATEWAY_IMAGE}
    assert identity["workspaceRoot"] == str(
        (tmp_path / "workspaces").resolve())
    assert identity["networks"] == {
        "sidecar": "control-net", "gatewayControl": "control-net"}
    assert identity["brokerUpstream"] == "control:7861"
    assert identity["socket"]["allowedUids"] == [10001]
    assert len(identity["keys"]["workerPublicSha256"]) == 64
    assert len(identity["keys"]["gatewayPublicSha256"]) == 64
    assert len(identity["policySha256"]) == 64
    assert "master" not in data.lower()
    assert "private" not in data.lower()
    assert identity["socket"]["gid"] == socket_path.stat().st_gid


def test_identity_fails_closed_when_key_read_fails(tmp_path):
    socket_path = tmp_path / "provisioner.sock"
    socket_path.touch()
    settings = _settings(tmp_path, socket_path)
    instance = provisioner.Provisioner(settings, run=FakeDocker())
    settings.worker_public_key.unlink()
    try:
        asyncio.run(instance.identity())
    except provisioner.ProvisionerError as error:
        assert error.code == "identity_file_unavailable"
    else:
        raise AssertionError("缺失公钥必须失败关闭")


def test_identity_compare_rejects_image_root_network_and_key_drift():
    client = _load("easel_provisioner_identity_client", CLIENT_PATH)
    worker = hashlib.sha256(b"worker").hexdigest()
    gateway = hashlib.sha256(b"gateway").hexdigest()
    policy = hashlib.sha256(b"policy").hexdigest()
    expected = {
        "workerImage": IMAGE,
        "gatewayImage": GATEWAY_IMAGE,
        "workspaceRoot": "/srv/workspaces",
        "sidecarNetwork": "control-net",
        "gatewayControlNetwork": "control-net",
        "brokerUpstream": "control:7861",
        "workerPublicSha256": worker,
        "gatewayPublicSha256": gateway,
        "policySha256": policy,
        "socketGid": 1000,
        "socketMode": "660",
        "allowedUid": 10001,
    }
    actual = {
        "protocol": 2,
        "engine": {"context": "desktop-linux", "id": "engine-123"},
        "images": {"worker": IMAGE, "gateway": GATEWAY_IMAGE},
        "workspaceRoot": "/srv/workspaces",
        "networks": {"sidecar": "control-net",
                     "gatewayControl": "control-net"},
        "brokerUpstream": "control:7861",
        "keys": {"workerPublicSha256": worker,
                 "gatewayPublicSha256": gateway},
        "policySha256": policy,
        "socket": {"path": "/run/provisioner.sock", "uid": 1000,
                   "gid": 1000, "mode": "660",
                   "allowedUids": [10001]},
    }
    assert client.compare_identity(expected, actual) == []

    drifted = json.loads(json.dumps(actual))
    drifted["images"]["gateway"] = "sha256:" + "c" * 64
    drifted["workspaceRoot"] = "/other"
    drifted["networks"]["sidecar"] = "other-net"
    drifted["keys"]["workerPublicSha256"] = "0" * 64
    assert set(client.compare_identity(expected, drifted)) >= {
        "gatewayImage", "workspaceRoot", "sidecarNetwork",
        "workerPublicSha256"}

    missing_engine = json.loads(json.dumps(actual))
    missing_engine["engine"] = {}
    assert "engine" in client.compare_identity(expected, missing_engine)


def test_identity_over_real_unix_socket_and_permissions():
    """WSL 真实 Unix socket：同 UID 客户端可读脱敏身份并比较。"""
    if shutil.which("wsl") is None:
        pytest.skip("本机没有 wsl")
    script = textwrap.dedent(
        """
        import asyncio, importlib.util, json, os, stat, sys, tempfile
        from pathlib import Path

        ROOT = Path("/mnt/c/Code/Easel")
        sys.path.insert(0, str(ROOT))

        def load(name, path):
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            return module

        provisioner = load("easel_provisioner_identity_real",
                           ROOT / "deploy/multiuser/provisioner.py")
        client = load("easel_provisioner_identity_client_real",
                      ROOT / "deploy/desktop/provisioner_identity.py")
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey)

        root = Path(tempfile.mkdtemp(prefix="easel-identity-"))
        socket_path = root / "provisioner.sock"
        worker = root / "worker.key"
        worker.write_bytes(Ed25519PrivateKey.generate().public_key()
                           .public_bytes_raw())
        gateway = root / "gateway.pem"
        gateway.write_bytes(Ed25519PrivateKey.generate().public_key()
                            .public_bytes(
                                serialization.Encoding.PEM,
                                serialization.PublicFormat.SubjectPublicKeyInfo))
        policy = root / "policy.json"
        policy.write_text(json.dumps({"approved_hosts": []}))

        class Runner:
            def __call__(self, argv, *, timeout=120.0, input=None):
                import subprocess
                if argv[1:] == ["context", "show"]:
                    return subprocess.CompletedProcess(
                        argv, 0, "desktop-linux\\n", "")
                return subprocess.CompletedProcess(
                    argv, 0, "engine-real\\n", "")

        settings = provisioner.ProvisionerSettings(
            image="sha256:" + "a" * 64,
            workspace_root=root / "workspaces",
            socket_path=socket_path,
            allowed_uids=frozenset({os.getuid()}),
            sidecar_network="control-net",
            sidecar_policy=policy,
            broker_upstream="control:7861",
            worker_public_key=worker,
            gateway_image="sha256:" + "b" * 64,
            gateway_control_network="control-net",
            gateway_public_key=gateway,
        )
        instance = provisioner.Provisioner(settings, run=Runner())

        async def main():
            server = asyncio.create_task(instance.serve())
            try:
                for _ in range(100):
                    if socket_path.exists():
                        break
                    await asyncio.sleep(0.05)
                identity = await asyncio.to_thread(
                    client.request_identity, str(socket_path), 2.0)
                expected = {
                    "workerImage": settings.image,
                    "gatewayImage": settings.gateway_image,
                    "workspaceRoot": str((root / "workspaces").resolve()),
                    "sidecarNetwork": "control-net",
                    "gatewayControlNetwork": "control-net",
                    "brokerUpstream": "control:7861",
                    "workerPublicSha256": __import__("hashlib").sha256(
                        worker.read_bytes()).hexdigest(),
                    "gatewayPublicSha256": __import__("hashlib").sha256(
                        gateway.read_bytes()).hexdigest(),
                    "policySha256": __import__("hashlib").sha256(
                        policy.read_bytes()).hexdigest(),
                    "socketGid": os.stat(socket_path).st_gid,
                    "socketMode": "660",
                    "allowedUid": os.getuid(),
                }
                assert client.compare_identity(expected, identity) == [], (
                    expected, identity)
                assert stat.S_IMODE(os.stat(socket_path).st_mode) == 0o660
                print("SOCKET_IDENTITY_OK")
            finally:
                server.cancel()
                try:
                    await server
                except asyncio.CancelledError:
                    pass

        asyncio.run(main())
        """
    )
    result = subprocess.run(
        ["wsl", "-d", "Ubuntu-26.04", "--", "python3", "-c", script],
        cwd=ROOT, capture_output=True, text=True, check=False, timeout=120)
    assert result.returncode == 0, result.stderr[-4000:]
    assert "SOCKET_IDENTITY_OK" in result.stdout
