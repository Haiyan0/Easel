"""G2-R1-04：gateway create 超时/取消的唯一清理追踪。"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from easel.gateway.runtime import (
    GatewayRuntime,
    GatewayRuntimeError,
    GatewaySettings,
    gateway_labels,
)

WORKSPACE = "a" * 32
WORKER = "b" * 64
CID = "c" * 64
IMAGE = "sha256:" + "d" * 64
NETWORK = [{
    "Name": "easel-control", "Driver": "bridge",
    "Internal": False, "EnableIPv6": False,
    "IPAM": {"Config": [{
        "Subnet": "172.31.70.0/24", "Gateway": "172.31.70.1"}]},
    "Containers": {},
}]


def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout, "")


def _missing(target: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        [], 1, "", f"Error: No such container: {target}")


def _settings(tmp_path: Path) -> GatewaySettings:
    key = tmp_path / "control.pub"
    key.write_bytes(Ed25519PrivateKey.generate().public_key().public_bytes_raw())
    return GatewaySettings(
        image=IMAGE, control_network="easel-control", public_key=key,
        attempt_dir=tmp_path / "attempts", start_timeout=2.0)


def _raw(with_worker_network: bool = True) -> dict:
    networks = {"easel-control": {
        "IPAddress": "172.31.70.2", "GlobalIPv6Address": "",
    }}
    if with_worker_network:
        networks[f"easel-ws-{WORKSPACE}"] = {
            "IPAddress": "172.31.70.3", "GlobalIPv6Address": "",
        }
    return {
        "Id": CID,
        "Name": f"/easel-gateway-{WORKSPACE}",
        "Config": {"Labels": gateway_labels(WORKSPACE, 3, WORKER)},
        "State": {"Running": True, "Status": "running"},
        "NetworkSettings": {"Networks": networks, "Ports": {}},
        "Mounts": [],
        "HostConfig": {},
    }


class CreateFake:
    def __init__(self, *, action: str = "timeout", removed: bool = False,
                 stop_fails: bool = False):
        self.action = action
        self.removed = removed
        self.stop_fails = stop_fails
        self.calls: list[list[str]] = []
        self.created = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, argv, *, timeout=120.0, input_text=None):
        self.calls.append(list(argv))
        if argv[:2] == ["network", "inspect"]:
            return _ok(json.dumps(NETWORK))
        if argv[:1] == ["create"]:
            cidfile = Path(argv[argv.index("--cidfile") + 1])
            cidfile.parent.mkdir(parents=True, exist_ok=True)
            cidfile.write_text(CID, encoding="ascii")
            self.created.set()
            if self.action == "timeout":
                raise subprocess.TimeoutExpired(argv, timeout)
            if self.action == "cancel":
                await self.release.wait()
                return _ok(CID)
            return _ok(CID)
        if argv[:2] == ["container", "inspect"]:
            if self.removed:
                return _missing(argv[2])
            return _ok(json.dumps([_raw()]))
        if argv[:1] == ["stop"]:
            if self.stop_fails:
                return subprocess.CompletedProcess(
                    argv, 1, "", "stop failed")
            return _ok()
        if argv[:2] == ["rm", "-f"]:
            if not self.stop_fails:
                self.removed = True
            return _ok()
        return _ok()


def _run_create(runtime: GatewayRuntime):
    return runtime._create(
        WORKSPACE, 3, WORKER, "http://172.31.70.4:7860", "token")


def test_create_timeout_reads_cidfile_and_removes_exact_id(tmp_path):
    fake = CreateFake(action="timeout")
    runtime = GatewayRuntime(_settings(tmp_path), run=fake)
    with pytest.raises(GatewayRuntimeError) as rejected:
        asyncio.run(_run_create(runtime))
    assert rejected.value.code == "gateway_create_failed"
    commands = [" ".join(call) for call in fake.calls]
    assert any(command.startswith("stop ") and CID in command
               for command in commands)
    assert any(command.startswith("rm -f ") and CID in command
               for command in commands)
    assert not list((tmp_path / "attempts").glob("*.cid"))


def test_create_cancel_waits_cli_then_cleans_exact_id(tmp_path):
    fake = CreateFake(action="cancel")
    runtime = GatewayRuntime(_settings(tmp_path), run=fake)

    async def scenario():
        task = asyncio.create_task(_run_create(runtime))
        await asyncio.wait_for(fake.created.wait(), 1)
        task.cancel()
        fake.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fake.removed is True

    asyncio.run(scenario())
    assert not list((tmp_path / "attempts").glob("*.cid"))


def test_create_cleanup_failure_keeps_cidfile_and_is_explicit(tmp_path):
    fake = CreateFake(action="timeout", stop_fails=True)
    runtime = GatewayRuntime(_settings(tmp_path), run=fake)
    with pytest.raises(GatewayRuntimeError) as rejected:
        asyncio.run(_run_create(runtime))
    assert rejected.value.code == "gateway_create_cleanup_incomplete"
    assert list((tmp_path / "attempts").glob("*.cid"))


def test_create_cleanup_never_falls_back_to_same_name(tmp_path):
    fake = CreateFake(action="timeout")
    runtime = GatewayRuntime(_settings(tmp_path), run=fake)
    with pytest.raises(GatewayRuntimeError):
        asyncio.run(_run_create(runtime))
    assert all(
        f"easel-gateway-{WORKSPACE}" not in " ".join(call)
        for call in fake.calls
        if call[:2] in (["rm", "-f"], ["stop", "--time"]))
