"""G2 gateway 容器 argv、inspect、状态与精确停止的纯逻辑验收。"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from easel.gateway.runtime import (
    GatewayCandidate,
    GatewayRuntime,
    GatewayRuntimeError,
    GatewaySettings,
    allocate_gateway_ip,
    build_gateway_create_argv,
    gateway_container_name,
    gateway_labels,
    gateway_public_key_sha256,
    gateway_worker_network,
    parse_gateway_status,
    sanitize_gateway_inspect,
    verify_gateway_inspect,
    verify_gateway_status,
)

WORKSPACE = "a" * 32
WORKER = "b" * 64
GATEWAY_ID = "c" * 64
BOOT = "d" * 32
CONTROL_IP = "172.31.0.5"
WORKER_IP = "172.32.0.2"
IMAGE = "sha256:" + "e" * 64
TOKEN = "worker-service-token-secret"
ORIGIN = f"http://{WORKER_IP}:7860"


def _settings(tmp_path: Path) -> GatewaySettings:
    key = tmp_path / "control-public.key"
    key.write_bytes(Ed25519PrivateKey.generate().public_key().public_bytes_raw())
    return GatewaySettings(
        image=IMAGE, control_network="easel-control", public_key=key,
        port=8897, start_timeout=2.0)


def _command(settings: GatewaySettings, bind: str = CONTROL_IP) -> list[str]:
    return [
        "python", "-m", "easel.gateway.server", "serve",
        "--bind", bind, "--port", "8897",
        "--public-key", "/keys/control-public.key",
        "--control-socket", "/run/easel/gateway-control.sock",
    ]


def _inspect(settings: GatewaySettings, *, workspace=WORKSPACE,
             version=3, worker=WORKER, instance_id=GATEWAY_ID,
             control_ip=CONTROL_IP, worker_ip=None,
             command=None, image=None, user="10001:10001",
             read_only=True, cap_drop=("ALL",), security_opt=(
                 "no-new-privileges:true",), published=False,
             key_source=None, extra_mount=None, tmpfs=None,
             labels=None) -> dict:
    networks = {
        settings.control_network: {
            "IPAddress": control_ip, "GlobalIPv6Address": "",
        },
    }
    if worker_ip is not None:
        networks[gateway_worker_network(workspace)] = {
            "IPAddress": worker_ip, "GlobalIPv6Address": "",
        }
    mounts = [{
        "Type": "bind",
        "Source": str(key_source or settings.public_key),
        "Destination": "/keys/control-public.key",
        "RW": False,
    }]
    if extra_mount is not None:
        mounts.append(extra_mount)
    return {
        "Id": instance_id,
        "Name": "/" + gateway_container_name(workspace),
        "Config": {
            "Image": image or settings.image,
            "User": user,
            "Entrypoint": None,
            "Cmd": command or _command(settings, control_ip),
            "Labels": labels or gateway_labels(workspace, version, worker),
            "Env": ["PATH=/usr/local/bin"],
        },
        "HostConfig": {
            "ReadonlyRootfs": read_only,
            "Privileged": False,
            "CapDrop": list(cap_drop),
            "CapAdd": [],
            "SecurityOpt": list(security_opt),
            "PidsLimit": 128,
            "Memory": 256 * 1024 ** 2,
            "MemorySwap": 256 * 1024 ** 2,
            "NanoCpus": 1_000_000_000,
            "NetworkMode": settings.control_network,
            "Binds": [],
            "Devices": [],
            "PidMode": "",
            "IpcMode": "private",
            "PublishAllPorts": False,
            "PortBindings": {},
            "Tmpfs": tmpfs or {
                "/run/easel": (
                    "rw,noexec,nosuid,nodev,size=1m,mode=0700,"
                    "uid=10001,gid=10001"),
            },
            "RestartPolicy": {"Name": "no"},
        },
        "State": {"Running": True, "Status": "running"},
        "Mounts": mounts,
        "NetworkSettings": {
            "Networks": networks,
            "Ports": ({"8897/tcp": [{"HostIp": "0.0.0.0",
                                    "HostPort": "8897"}]} if published else {}),
        },
    }


def _status(settings: GatewaySettings, *, instance_id=GATEWAY_ID, boot=BOOT,
            workspace=WORKSPACE, worker=WORKER, version=3,
            active=True, available=True, configured=True,
            bind=CONTROL_IP) -> dict:
    return {
        "gateway_instance_id": instance_id,
        "boot_generation": boot,
        "workspace_id": workspace,
        "worker_container_id": worker,
        "config_version": version,
        "worker_origin": ORIGIN,
        "management_bind": bind,
        "management_port": 8897,
        "public_key_sha256": gateway_public_key_sha256(settings.public_key),
        "configured": configured,
        "active": active,
        "available": available,
    }


def test_gateway_create_argv_is_fixed_and_has_no_published_ports(tmp_path):
    settings = _settings(tmp_path)
    argv = build_gateway_create_argv(
        settings, workspace_id=WORKSPACE, config_version=3,
        worker_container_id=WORKER, control_ip=CONTROL_IP)
    text = " ".join(argv)
    assert argv[0] == "create"
    assert ["--name", gateway_container_name(WORKSPACE)] == argv[1:3]
    assert "--user" in argv and "10001:10001" in argv
    assert "--read-only" in argv
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges:true" in argv
    assert "--cpus=1" in argv and "--memory=256m" in argv
    assert "--publish" not in text and "-p" not in argv
    assert "--network" in argv
    assert argv[argv.index("--network") + 1] == "easel-control"
    assert argv[argv.index("--ip") + 1] == CONTROL_IP
    assert "/workspace" not in text and "docker.sock" not in text
    assert "/srv/easel/control" not in text
    assert "/keys/control-public.key" in text
    assert "readonly" in text
    assert "/run/easel" in text and "mode=0700" in text
    for label, value in gateway_labels(WORKSPACE, 3, WORKER).items():
        assert f"{label}={value}" in argv
    assert argv[-len(_command(settings)):] == _command(settings)


def test_gateway_inspect_accepts_only_exact_control_endpoint(tmp_path):
    settings = _settings(tmp_path)
    raw = _inspect(settings)
    report = sanitize_gateway_inspect(raw, workspace_id=WORKSPACE)
    assert report["managed"] is True
    assert report["gateway_instance_id"] == GATEWAY_ID
    assert report["control_ip"] == CONTROL_IP
    assert report["worker_container_id"] == WORKER
    assert verify_gateway_inspect(
        raw, settings, workspace_id=WORKSPACE, config_version=3,
        worker_container_id=WORKER, control_ip=CONTROL_IP,
        worker_network_connected=False) is True

    for bad in (
            _inspect(settings, control_ip="172.31.0.6"),
            _inspect(settings, worker_ip=WORKER_IP),
            _inspect(settings, published=True),
            _inspect(settings, user="0:0"),
            _inspect(settings, read_only=False),
            _inspect(settings, cap_drop=("NET_RAW",)),
            _inspect(settings, security_opt=()),
            _inspect(settings, image="sha256:" + "f" * 64),
            _inspect(settings, command=["python", "-c", "evil"]),
            _inspect(settings, labels={**gateway_labels(
                WORKSPACE, 3, WORKER), "easel.role": "worker"}),
            _inspect(settings, extra_mount={
                "Type": "bind", "Source": "/tmp", "Destination": "/workspace",
                "RW": True}),
            _inspect(settings, tmpfs={"/run/easel": "rw,size=1m"})):
        assert verify_gateway_inspect(
            bad, settings, workspace_id=WORKSPACE, config_version=3,
            worker_container_id=WORKER, control_ip=CONTROL_IP,
            worker_network_connected=False) is False


def test_gateway_inspect_requires_worker_network_after_connect(tmp_path):
    settings = _settings(tmp_path)
    raw = _inspect(settings, worker_ip=WORKER_IP)
    assert verify_gateway_inspect(
        raw, settings, workspace_id=WORKSPACE, config_version=3,
        worker_container_id=WORKER, control_ip=CONTROL_IP,
        worker_network_connected=True) is True
    assert verify_gateway_inspect(
        raw, settings, workspace_id=WORKSPACE, config_version=3,
        worker_container_id=WORKER, control_ip=CONTROL_IP,
        worker_network_connected=False) is False


def test_gateway_status_parser_and_exact_verifier(tmp_path):
    settings = _settings(tmp_path)
    parsed = parse_gateway_status(
        "noise\n" + json.dumps(_status(settings)) + "\n")
    assert parsed["boot_generation"] == BOOT
    assert verify_gateway_status(
        parsed, settings, workspace_id=WORKSPACE,
        worker_container_id=WORKER, config_version=3,
        gateway_instance_id=GATEWAY_ID, boot_generation=BOOT,
        active=True, available=True) is True
    assert verify_gateway_status(
        parsed, settings, workspace_id=WORKSPACE,
        worker_container_id=WORKER, config_version=3,
        gateway_instance_id=GATEWAY_ID, boot_generation="f" * 32,
        active=True, available=True) is False
    leaked = {**_status(settings), "worker_service_token": TOKEN}
    assert parse_gateway_status(json.dumps(leaked)) == {}


def test_allocate_gateway_ip_uses_lowest_free_host_and_excludes_reserved():
    payload = [{
        "Name": "easel-control", "Driver": "bridge",
        "Internal": False, "EnableIPv6": False,
        "IPAM": {"Config": [{
            "Subnet": "172.31.0.0/29", "Gateway": "172.31.0.1"}]},
        "Containers": {
            "one": {"IPv4Address": "172.31.0.2/29"},
            "three": {"IPv4Address": "172.31.0.3/29"},
        },
    }]
    assert allocate_gateway_ip(payload, "easel-control") == "172.31.0.4"
    assert allocate_gateway_ip(
        payload, "easel-control",
        reserved=frozenset({"172.31.0.4"})) == "172.31.0.5"
    with pytest.raises(GatewayRuntimeError):
        allocate_gateway_ip(payload, "missing")


class FakeDocker:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[tuple[list[str], str | None]] = []

    async def __call__(self, argv, *, timeout=120.0, input_text=None):
        self.calls.append((list(argv), input_text))
        if not self.responses:
            return subprocess.CompletedProcess(argv, 0, "", "")
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, str):
            return subprocess.CompletedProcess(argv, 0, value, "")
        return value


def test_gateway_stop_uses_exact_id_and_rejects_wrong_tuple(tmp_path):
    settings = _settings(tmp_path)
    raw = _inspect(settings, worker_ip=WORKER_IP)

    def missing():
        return subprocess.CompletedProcess(
            [], 1, "", f"Error: No such container: {GATEWAY_ID}")

    fake = FakeDocker(
        json.dumps([raw]), json.dumps([raw]), "", "", missing())
    runtime = GatewayRuntime(settings, run=fake)

    async def scenario():
        await runtime.stop(WORKSPACE, WORKER, GATEWAY_ID)
    asyncio.run(scenario())
    commands = [" ".join(call[0]) for call in fake.calls]
    assert commands[0] == f"container inspect {GATEWAY_ID}"
    assert any(command.startswith("stop") for command in commands)
    assert any(command.startswith("rm -f") for command in commands)
    assert all(gateway_container_name(WORKSPACE) not in command
               for command in commands)

    fake = FakeDocker(json.dumps([raw]))
    runtime = GatewayRuntime(settings, run=fake)
    with pytest.raises(GatewayRuntimeError) as rejected:
        asyncio.run(runtime.stop(WORKSPACE, "f" * 64, GATEWAY_ID))
    assert rejected.value.code == "gateway_target_mismatch"
    assert not any(call[0][0] in ("stop", "rm")
                   for call in fake.calls)


def test_gateway_candidate_never_repr_secret(tmp_path):
    candidate = GatewayCandidate(
        workspace_id=WORKSPACE, config_version=3, worker_container_id=WORKER,
        gateway_instance_id=GATEWAY_ID, boot_generation=BOOT,
        gateway_upstream=f"http://{CONTROL_IP}:8897",
        worker_created=False, gateway_created=False, active=False)
    assert TOKEN not in repr(candidate)


def test_gateway_inspect_stopped_container_is_still_managed(tmp_path):
    """停止后 IPAddress/IPAMConfig 为空，不得把受管 gateway 误判为不受管。"""
    settings = _settings(tmp_path)
    raw = _inspect(settings)
    raw["State"] = {"Running": False, "Status": "exited"}
    networks = raw["NetworkSettings"]["Networks"]
    for name, entry in networks.items():
        entry["IPAddress"] = ""
        entry["IPAMConfig"] = (
            {"IPv4Address": CONTROL_IP}
            if name == settings.control_network else {})
    report = sanitize_gateway_inspect(raw, workspace_id=WORKSPACE)
    assert report["managed"] is True
    assert report["gateway_instance_id"] == GATEWAY_ID
    assert report["control_ip"] == CONTROL_IP
    assert report["worker_ip"] == ""
    assert verify_gateway_inspect(
        raw, settings, workspace_id=WORKSPACE, config_version=3,
        worker_container_id=WORKER, control_ip=CONTROL_IP,
        worker_network_connected=True) is False
