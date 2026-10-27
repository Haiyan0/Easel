"""G2 gateway 受管容器 runtime：固定 IPv4、配置握手与精确停止。

本模块只负责从受限 runner 构造/核验 gateway 容器，不持有 Docker socket。
所有镜像、命令、挂载、网络与资源都由管理员设置派生；调用方只能提供
workspace、worker 实例、版本与已核验的 worker 目标。
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

GATEWAY_PORT = 8897
GATEWAY_USER = "10001:10001"
GATEWAY_PUBLIC_KEY_PATH = "/keys/control-public.key"
GATEWAY_CONTROL_SOCKET = "/run/easel/gateway-control.sock"
GATEWAY_TMPFS = (
    "/run/easel:rw,noexec,nosuid,nodev,size=1m,mode=0700,"
    "uid=10001,gid=10001")
GATEWAY_PIDS_LIMIT = 128
GATEWAY_MEMORY_BYTES = 256 * 1024 ** 2
GATEWAY_NANO_CPUS = 1_000_000_000

_WORKSPACE = re.compile(r"^[0-9a-f]{32}$")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_BOOT = re.compile(r"^[0-9a-f]{32}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PINNED_IMAGE = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9._/:@+-]*@)?sha256:[0-9a-f]{64}$")

_STATUS_KEYS = frozenset({
    "gateway_instance_id", "boot_generation", "workspace_id",
    "worker_container_id", "config_version", "worker_origin",
    "management_bind", "management_port", "public_key_sha256",
    "configured", "active", "available",
})


class GatewayRuntimeError(Exception):
    """稳定错误码；不包含路径、token、断言或上游响应。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class GatewaySettings:
    image: str
    control_network: str
    public_key: Path
    port: int = GATEWAY_PORT
    start_timeout: float = 30.0
    docker: str = "docker"

    def __post_init__(self) -> None:
        if not isinstance(self.image, str) or _PINNED_IMAGE.fullmatch(
                self.image) is None:
            raise GatewayRuntimeError("gateway_image_invalid")
        if (not isinstance(self.control_network, str)
                or _SAFE_NAME.fullmatch(self.control_network) is None):
            raise GatewayRuntimeError("gateway_network_invalid")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise GatewayRuntimeError("gateway_port_invalid")
        if (not isinstance(self.start_timeout, (int, float))
                or isinstance(self.start_timeout, bool)
                or self.start_timeout <= 0):
            raise GatewayRuntimeError("gateway_timeout_invalid")
        path = Path(self.public_key)
        if path.is_symlink():
            raise GatewayRuntimeError("gateway_public_key_invalid")
        try:
            resolved = path.resolve(strict=True)
            data = resolved.read_bytes()
        except OSError:
            raise GatewayRuntimeError("gateway_public_key_unavailable") from None
        if not data or len(data) > 32 * 1024:
            raise GatewayRuntimeError("gateway_public_key_invalid")
        try:
            _load_key_bytes(data)
        except (ValueError, TypeError):
            raise GatewayRuntimeError("gateway_public_key_invalid") from None
        object.__setattr__(self, "public_key", resolved)


@dataclass(frozen=True)
class GatewayCandidate:
    workspace_id: str
    config_version: int
    worker_container_id: str
    gateway_instance_id: str
    boot_generation: str
    gateway_upstream: str
    worker_created: bool = False
    gateway_created: bool = False
    active: bool = False


def _load_key_bytes(data: bytes) -> Ed25519PublicKey:
    if len(data) == 32:
        return Ed25519PublicKey.from_public_bytes(data)
    key = serialization.load_pem_public_key(data)
    if not isinstance(key, Ed25519PublicKey):
        raise TypeError("not Ed25519")
    return key


def gateway_public_key_sha256(path: str | Path) -> str:
    data = Path(path).read_bytes()
    return hashlib.sha256(_load_key_bytes(data).public_bytes_raw()).hexdigest()


def _workspace(value: object) -> str:
    if not isinstance(value, str) or _WORKSPACE.fullmatch(value) is None:
        raise GatewayRuntimeError("gateway_workspace_invalid")
    return value


def _container_id(value: object, code: str) -> str:
    if not isinstance(value, str) or _CONTAINER_ID.fullmatch(value) is None:
        raise GatewayRuntimeError(code)
    return value


def _version(value: object) -> int:
    if type(value) is not int or not 0 <= value < 2 ** 31:
        raise GatewayRuntimeError("gateway_config_version_invalid")
    return value


def gateway_container_name(workspace_id: str) -> str:
    return f"easel-gateway-{_workspace(workspace_id)}"


def gateway_worker_network(workspace_id: str) -> str:
    return f"easel-ws-{_workspace(workspace_id)}"


def gateway_labels(workspace_id: str, config_version: int,
                   worker_container_id: str) -> dict[str, str]:
    return {
        "easel.managed": "true",
        "easel.workspace-id": _workspace(workspace_id),
        "easel.config-version": str(_version(config_version)),
        "easel.worker-container-id": _container_id(
            worker_container_id, "gateway_worker_invalid"),
        "easel.role": "gateway",
    }


def gateway_command(settings: GatewaySettings, bind: str) -> tuple[str, ...]:
    return (
        "python", "-m", "easel.gateway.server", "serve",
        "--bind", bind, "--port", str(settings.port),
        "--public-key", GATEWAY_PUBLIC_KEY_PATH,
        "--control-socket", GATEWAY_CONTROL_SOCKET,
    )


def build_gateway_create_argv(
        settings: GatewaySettings, *, workspace_id: str,
        config_version: int, worker_container_id: str,
        control_ip: str) -> list[str]:
    """唯一允许的 gateway create argv；不发布端口，不挂用户/控制数据。"""
    address = _ipv4(control_ip)
    name = gateway_container_name(workspace_id)
    labels = gateway_labels(workspace_id, config_version, worker_container_id)
    argv = ["create", "--name", name]
    for key, value in labels.items():
        argv.extend(["--label", f"{key}={value}"])
    argv.extend([
        "--user", GATEWAY_USER,
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--cpus=1",
        "--memory=256m",
        "--memory-swap=256m",
        "--pids-limit", str(GATEWAY_PIDS_LIMIT),
        "--tmpfs", GATEWAY_TMPFS,
        "--network", settings.control_network,
        "--ip", str(address),
        "--mount",
        (f"type=bind,src={_safe_path(Path(settings.public_key))},"
         f"dst={GATEWAY_PUBLIC_KEY_PATH},readonly"),
        "--restart=no",
        "--stop-timeout=15",
        "--log-driver=local",
        "--log-opt=max-size=10m",
        "--log-opt=max-file=2",
        settings.image,
        *gateway_command(settings, str(address)),
    ])
    return argv


def _safe_path(path: Path) -> str:
    text = str(path)
    if any(character in text for character in (",", "\n", "\r", "\0")):
        raise GatewayRuntimeError("gateway_path_invalid")
    return text


def _ipv4(value: object) -> ipaddress.IPv4Address:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise GatewayRuntimeError("gateway_bind_invalid") from None
    if address.version != 4 or address.is_unspecified:
        raise GatewayRuntimeError("gateway_bind_invalid")
    return address


def allocate_gateway_ip(payload: object, network: str, *,
                        reserved: frozenset[str] = frozenset()) -> str:
    """从已存在控制网络中选择最低可用 IPv4；Docker 实际分配后仍须核对。"""
    if not isinstance(payload, list) or len(payload) != 1 \
            or not isinstance(payload[0], dict):
        raise GatewayRuntimeError("gateway_network_invalid")
    record = payload[0]
    if record.get("Name") != network or record.get("Driver") != "bridge" \
            or record.get("Internal") is not False \
            or record.get("EnableIPv6") is not False:
        raise GatewayRuntimeError("gateway_network_invalid")
    config = record.get("IPAM") or {}
    configs = config.get("Config") if isinstance(config, dict) else None
    if not isinstance(configs, list) or len(configs) != 1 \
            or not isinstance(configs[0], dict):
        raise GatewayRuntimeError("gateway_network_invalid")
    try:
        subnet = ipaddress.ip_network(configs[0]["Subnet"], strict=True)
    except (KeyError, TypeError, ValueError):
        raise GatewayRuntimeError("gateway_network_invalid") from None
    if subnet.version != 4:
        raise GatewayRuntimeError("gateway_network_invalid")
    used: set[str] = set()
    gateway = configs[0].get("Gateway")
    if isinstance(gateway, str):
        try:
            used.add(str(ipaddress.ip_address(gateway)))
        except ValueError:
            raise GatewayRuntimeError("gateway_network_invalid") from None
    containers = record.get("Containers")
    if containers is not None and not isinstance(containers, dict):
        raise GatewayRuntimeError("gateway_network_invalid")
    for entry in (containers or {}).values():
        if not isinstance(entry, dict):
            raise GatewayRuntimeError("gateway_network_invalid")
        raw = entry.get("IPv4Address")
        if isinstance(raw, str) and raw:
            try:
                used.add(str(ipaddress.ip_address(raw.split("/", 1)[0])))
            except ValueError:
                raise GatewayRuntimeError("gateway_network_invalid") from None
    for value in reserved:
        try:
            used.add(str(ipaddress.ip_address(value)))
        except ValueError:
            raise GatewayRuntimeError("gateway_address_invalid") from None
    for address in subnet.hosts():
        if str(address) not in used:
            return str(address)
    raise GatewayRuntimeError("gateway_ip_unavailable")


def sanitize_gateway_inspect(raw: object, *, workspace_id: str) -> dict:
    """只回传 gateway 静态身份/网络白名单，不含 Env/命令秘密。"""
    empty = {
        "managed": False,
        "workspace_id": "",
        "gateway_instance_id": "",
        "config_version": 0,
        "worker_container_id": "",
        "control_ip": "",
        "worker_ip": "",
        "networks": [],
        "image": "",
        "command": [],
        "security": {},
        "mounts": [],
    }
    if not isinstance(raw, dict):
        return empty
    instance_id = raw.get("Id")
    config = raw.get("Config")
    if (not isinstance(instance_id, str)
            or _CONTAINER_ID.fullmatch(instance_id) is None
            or not isinstance(config, dict)):
        return empty
    labels = config.get("Labels")
    if not isinstance(labels, dict):
        return empty
    workspace = labels.get("easel.workspace-id")
    version = labels.get("easel.config-version")
    worker = labels.get("easel.worker-container-id")
    try:
        config_version = int(version)
    except (TypeError, ValueError):
        return empty
    if (labels.get("easel.managed") != "true"
            or labels.get("easel.role") != "gateway"
            or workspace != _workspace(workspace_id)
            or _CONTAINER_ID.fullmatch(str(worker)) is None
            or not 0 <= config_version < 2 ** 31
            or str(raw.get("Name") or "").lstrip("/")
            != gateway_container_name(workspace_id)):
        return empty
    networks = (raw.get("NetworkSettings") or {}).get("Networks")
    if not isinstance(networks, dict):
        return empty
    addresses: dict[str, str] = {}
    for name, entry in networks.items():
        if not isinstance(entry, dict):
            return empty
        address = entry.get("IPAddress")
        if not address:
            # `docker create` 后、start 前 inspect 只填 IPAMConfig；
            # 仍必须核对它与请求的固定 IPv4 一致。
            ipam = entry.get("IPAMConfig")
            address = ipam.get("IPv4Address") if isinstance(ipam, dict) else ""
        if not isinstance(address, str):
            return empty
        addresses[str(name)] = address
    worker_network = gateway_worker_network(workspace_id)
    worker_ip = addresses.get(worker_network, "")
    control_addresses = {
        name: address for name, address in addresses.items()
        if name != worker_network
    }
    control_ip = (
        next(iter(control_addresses.values()))
        if len(control_addresses) == 1 else "")
    mounts = []
    for mount in raw.get("Mounts") or []:
        if not isinstance(mount, dict):
            return empty
        mounts.append({
            "type": mount.get("Type"),
            "source": mount.get("Source"),
            "destination": mount.get("Destination"),
            "rw": mount.get("RW"),
        })
    host = raw.get("HostConfig") or {}
    return {
        "managed": True,
        "workspace_id": workspace,
        "gateway_instance_id": instance_id,
        "config_version": config_version,
        "worker_container_id": str(worker),
        "control_ip": control_ip,
        "worker_ip": worker_ip,
        "networks": sorted(networks),
        "image": str(config.get("Image") or ""),
        "command": list(config.get("Cmd") or []),
        "security": {
            "user": str(config.get("User") or ""),
            "read_only": bool(host.get("ReadonlyRootfs")),
            "cap_drop": list(host.get("CapDrop") or []),
            "cap_add": list(host.get("CapAdd") or []),
            "security_opt": list(host.get("SecurityOpt") or []),
            "pids_limit": int(host.get("PidsLimit") or 0),
            "memory_bytes": int(host.get("Memory") or 0),
            "memory_swap": int(host.get("MemorySwap") or 0),
            "nano_cpus": int(host.get("NanoCpus") or 0),
        },
        "mounts": mounts,
    }


def _no_new_privileges(options: list[object]) -> bool:
    values: list[bool] = []
    for option in options:
        text = str(option)
        if text in ("no-new-privileges", "no-new-privileges:true"):
            values.append(True)
        elif text.startswith("no-new-privileges"):
            values.append(False)
    return values == [True]


def verify_gateway_inspect(
        raw: object, settings: GatewaySettings, *, workspace_id: str,
        config_version: int, worker_container_id: str, control_ip: str,
        worker_network_connected: bool) -> bool:
    report = sanitize_gateway_inspect(raw, workspace_id=workspace_id)
    if not report["managed"]:
        return False
    if (report["config_version"] != config_version
            or report["worker_container_id"] != worker_container_id):
        return False
    assert isinstance(raw, dict)
    config = raw.get("Config") or {}
    host = raw.get("HostConfig") or {}
    if config.get("Image") != settings.image:
        return False
    if config.get("User") != GATEWAY_USER:
        return False
    if config.get("Entrypoint") not in (None, []):
        return False
    if list(config.get("Cmd") or []) != list(
            gateway_command(settings, control_ip)):
        return False
    security = report["security"]
    if security["read_only"] is not True:
        return False
    if host.get("Privileged") is not False:
        return False
    if set(security["cap_drop"]) != {"ALL"} or security["cap_add"]:
        return False
    if not _no_new_privileges(security["security_opt"]):
        return False
    if security["pids_limit"] != GATEWAY_PIDS_LIMIT:
        return False
    if security["memory_bytes"] != GATEWAY_MEMORY_BYTES:
        return False
    if security["memory_swap"] != GATEWAY_MEMORY_BYTES:
        return False
    if security["nano_cpus"] != GATEWAY_NANO_CPUS:
        return False
    if host.get("NetworkMode") != settings.control_network:
        return False
    if host.get("Binds") not in (None, []):
        return False
    if host.get("Devices") not in (None, []):
        return False
    if host.get("PidMode") not in (None, ""):
        return False
    if host.get("IpcMode") == "host":
        return False
    if host.get("PublishAllPorts") not in (None, False):
        return False
    if any(bool(bindings)
           for bindings in (host.get("PortBindings") or {}).values()):
        return False
    tmpfs = host.get("Tmpfs") or {}
    options = str(tmpfs.get("/run/easel") or "").split(",")
    if not {"rw", "noexec", "nosuid", "nodev", "size=1m",
            "mode=0700", "uid=10001", "gid=10001"}.issubset(set(options)):
        return False
    if len(report["mounts"]) != 1:
        return False
    mount = report["mounts"][0]
    if (mount["type"] != "bind" or mount["rw"] is not False
            or mount["destination"] != GATEWAY_PUBLIC_KEY_PATH
            or mount["source"] != str(settings.public_key)
            or "docker.sock" in str(mount["source"])):
        return False
    networks = (raw.get("NetworkSettings") or {}).get("Networks")
    if not isinstance(networks, dict):
        return False
    expected = {settings.control_network}
    if worker_network_connected:
        expected.add(gateway_worker_network(workspace_id))
    if set(networks) != expected:
        return False
    control = networks.get(settings.control_network)
    if not isinstance(control, dict):
        return False
    actual_control_ip = control.get("IPAddress")
    if not actual_control_ip:
        ipam = control.get("IPAMConfig")
        actual_control_ip = (
            ipam.get("IPv4Address") if isinstance(ipam, dict) else "")
    if actual_control_ip != control_ip:
        return False
    if control.get("GlobalIPv6Address") not in (None, ""):
        return False
    if worker_network_connected:
        worker = networks.get(gateway_worker_network(workspace_id))
        if (not isinstance(worker, dict)
                or not isinstance(worker.get("IPAddress"), str)
                or not worker.get("IPAddress")
                or worker.get("IPAddress") == control_ip
                or worker.get("GlobalIPv6Address") not in (None, "")):
            return False
    ports = (raw.get("NetworkSettings") or {}).get("Ports") or {}
    if not isinstance(ports, dict):
        return False
    return not any(bool(bindings) for bindings in ports.values())


def parse_gateway_status(stdout: object) -> dict:
    """解析 gateway status 最后一行；未知字段/类型异常一律空。"""
    if not isinstance(stdout, str):
        return {}
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        return {}
    try:
        payload = json.loads(lines[-1])
    except (TypeError, ValueError):
        return {}
    if (isinstance(payload, dict)
            and set(payload) == {"ok", "status"}
            and payload.get("ok") is True
            and isinstance(payload.get("status"), dict)):
        payload = payload["status"]
    if not isinstance(payload, dict) or set(payload) != _STATUS_KEYS:
        return {}
    try:
        instance_id = payload["gateway_instance_id"]
        boot = payload["boot_generation"]
        workspace = payload["workspace_id"]
        worker = payload["worker_container_id"]
        version = payload["config_version"]
        origin = payload["worker_origin"]
        bind = payload["management_bind"]
        port = payload["management_port"]
        key_sha = payload["public_key_sha256"]
        configured = payload["configured"]
        active = payload["active"]
        available = payload["available"]
    except KeyError:
        return {}
    if instance_id != "" and _CONTAINER_ID.fullmatch(str(instance_id)) is None:
        return {}
    if _BOOT.fullmatch(str(boot)) is None:
        return {}
    if workspace != "" and _WORKSPACE.fullmatch(str(workspace)) is None:
        return {}
    if worker != "" and _CONTAINER_ID.fullmatch(str(worker)) is None:
        return {}
    if type(version) is not int or version < 0:
        return {}
    if type(origin) is not str or type(bind) is not str:
        return {}
    try:
        _ipv4(bind)
    except GatewayRuntimeError:
        return {}
    if type(port) is not int or not 1 <= port <= 65535:
        return {}
    if not isinstance(key_sha, str) or len(key_sha) != 64:
        return {}
    if any(type(value) is not bool
           for value in (configured, active, available)):
        return {}
    return payload


def verify_gateway_status(
        status: object, settings: GatewaySettings, *, workspace_id: str,
        worker_container_id: str, config_version: int,
        gateway_instance_id: str, boot_generation: str, active: bool,
        available: bool, control_ip: str | None = None) -> bool:
    if not isinstance(status, dict) or set(status) != _STATUS_KEYS:
        return False
    if (status.get("workspace_id") != workspace_id
            or status.get("worker_container_id") != worker_container_id
            or status.get("config_version") != config_version
            or status.get("gateway_instance_id") != gateway_instance_id
            or status.get("boot_generation") != boot_generation
            or status.get("public_key_sha256")
            != gateway_public_key_sha256(settings.public_key)
            or status.get("management_port") != settings.port
            or status.get("configured") is not True
            or status.get("active") is not active
            or status.get("available") is not available):
        return False
    return control_ip is None or status.get("management_bind") == control_ip


class GatewayRuntime:
    """受管 gateway 容器操作；runner 接收不含 docker 可执行文件参数的 argv。"""

    def __init__(self, settings: GatewaySettings, *,
                 run: Callable[..., Awaitable[subprocess.CompletedProcess]]
                 | None = None) -> None:
        if not isinstance(settings, GatewaySettings):
            raise TypeError("settings must be GatewaySettings")
        self.settings = settings
        self._run = run or self._default_run
        self._lock = asyncio.Lock()

    async def _default_run(self, argv, *, timeout=120.0, input_text=None):
        return await asyncio.to_thread(
            subprocess.run, [self.settings.docker, *argv],
            input=input_text, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, check=False, timeout=timeout)

    async def _docker(self, *args: str, timeout: float = 120.0,
                      input_text: str | None = None
                      ) -> subprocess.CompletedProcess:
        return await self._run(list(args), timeout=timeout,
                               input_text=input_text)

    @staticmethod
    def _not_found(result: subprocess.CompletedProcess, target: str) -> bool:
        if result.returncode == 0:
            return False
        if str(getattr(result, "stdout", "") or "").strip() not in ("", "[]"):
            return False
        text = str(getattr(result, "stderr", "") or "").strip()
        escaped = re.escape(target)
        pattern = (
            rf"(?:Error(?: response from daemon)?:\s*)?"
            rf"No such (?:container|object):\s*{escaped}")
        return re.fullmatch(pattern, text, re.IGNORECASE) is not None

    def _single(self, stdout: str) -> dict:
        try:
            payload = json.loads(stdout)
        except (TypeError, ValueError):
            raise GatewayRuntimeError("gateway_inspect_invalid") from None
        if (not isinstance(payload, list) or len(payload) != 1
                or not isinstance(payload[0], dict)):
            raise GatewayRuntimeError("gateway_inspect_invalid")
        return payload[0]

    async def _inspect_name(self, name: str) -> dict | None:
        result = await self._docker("container", "inspect", name)
        if result.returncode != 0:
            if self._not_found(result, name):
                return None
            raise GatewayRuntimeError("gateway_inspect_failed")
        return self._single(result.stdout)

    async def _inspect_id(self, instance_id: str) -> dict | None:
        result = await self._docker("container", "inspect", instance_id)
        if result.returncode != 0:
            if self._not_found(result, instance_id):
                return None
            raise GatewayRuntimeError("gateway_inspect_failed")
        return self._single(result.stdout)

    async def _status(self, instance_id: str) -> dict:
        result = await self._docker(
            "exec", "--user", GATEWAY_USER, instance_id,
            "python", "-m", "easel.gateway.server", "status",
            "--control-socket", GATEWAY_CONTROL_SOCKET,
            timeout=10.0)
        if result.returncode != 0:
            raise GatewayRuntimeError("gateway_status_unavailable")
        status = parse_gateway_status(result.stdout)
        if not status:
            raise GatewayRuntimeError("gateway_status_invalid")
        return status

    async def _control(self, instance_id: str, action: str,
                       payload: dict) -> dict:
        # CLI 自己构造 action 信封；这里只传受信 payload，避免双重包裹。
        data = json.dumps(payload, separators=(",", ":"))
        result = await self._docker(
            "exec", "-i", "--user", GATEWAY_USER, instance_id,
            "python", "-m", "easel.gateway.server", action,
            "--control-socket", GATEWAY_CONTROL_SOCKET,
            timeout=10.0, input_text=data)
        lines = [line.strip() for line in str(result.stdout).splitlines()
                 if line.strip()]
        response = None
        if lines:
            try:
                response = json.loads(lines[-1])
            except (TypeError, ValueError):
                response = None
        if result.returncode != 0:
            if isinstance(response, dict) and response.get("ok") is False:
                code = response.get("error")
                if isinstance(code, str) and re.fullmatch(
                        r"gateway_[a-z0-9_]{1,64}", code):
                    raise GatewayRuntimeError(code)
            raise GatewayRuntimeError("gateway_control_failed")
        if not isinstance(response, dict) or response.get("ok") is not True:
            code = response.get("error") if isinstance(response, dict) else None
            raise GatewayRuntimeError(
                code if isinstance(code, str)
                and re.fullmatch(r"gateway_[a-z0-9_]{1,64}", code)
                else "gateway_control_failed")
        status = response.get("status")
        if not isinstance(status, dict):
            raise GatewayRuntimeError("gateway_control_invalid")
        return status

    async def _wait_started(self, instance_id: str) -> dict:
        deadline = asyncio.get_running_loop().time() + \
            float(self.settings.start_timeout)
        last: GatewayRuntimeError | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                return await self._status(instance_id)
            except GatewayRuntimeError as error:
                last = error
                await asyncio.sleep(0.05)
        raise last or GatewayRuntimeError("gateway_status_unavailable")

    async def _network_payload(self) -> list:
        result = await self._docker(
            "network", "inspect", self.settings.control_network)
        if result.returncode != 0:
            raise GatewayRuntimeError("gateway_network_unavailable")
        try:
            payload = json.loads(result.stdout)
        except (TypeError, ValueError):
            raise GatewayRuntimeError("gateway_network_invalid") from None
        return payload if isinstance(payload, list) else []

    async def ensure(self, *, workspace_id: str, config_version: int,
                     worker_container_id: str, worker_origin: str,
                     worker_service_token: str) -> GatewayCandidate:
        """创建/复用/重启 gateway 并完成配置；返回未激活候选。"""
        workspace = _workspace(workspace_id)
        version = _version(config_version)
        worker = _container_id(worker_container_id, "gateway_worker_invalid")
        if not isinstance(worker_origin, str) or not worker_origin:
            raise GatewayRuntimeError("gateway_worker_invalid")
        if not isinstance(worker_service_token, str) or not worker_service_token:
            raise GatewayRuntimeError("gateway_worker_invalid")
        async with self._lock:
            name = gateway_container_name(workspace)
            existing = await self._inspect_name(name)
            if existing is not None:
                report = sanitize_gateway_inspect(
                    existing, workspace_id=workspace)
                if not report["managed"]:
                    raise GatewayRuntimeError("gateway_container_conflict")
                target_matches = (
                    report["config_version"] == version
                    and report["worker_container_id"] == worker)
                if target_matches:
                    try:
                        static_ok = verify_gateway_inspect(
                            existing, self.settings, workspace_id=workspace,
                            config_version=version,
                            worker_container_id=worker,
                            control_ip=report["control_ip"],
                            worker_network_connected=True)
                    except Exception:  # noqa: BLE001 - 核验失败即失败关闭
                        static_ok = False
                    if static_ok:
                        try:
                            status = await self._status(
                                report["gateway_instance_id"])
                        except GatewayRuntimeError:
                            status = {}
                        if status.get("active") is True:
                            if verify_gateway_status(
                                    status, self.settings,
                                    workspace_id=workspace,
                                    worker_container_id=worker,
                                    config_version=version,
                                    gateway_instance_id=report[
                                        "gateway_instance_id"],
                                    boot_generation=status.get(
                                        "boot_generation", ""),
                                    active=True, available=True,
                                    control_ip=report["control_ip"]):
                                return self._candidate(
                                    workspace, version, worker, report, status,
                                    active=True, created=False)
                            # 已隔离/状态矛盾的 active gateway 不能热配置，
                            # 只按精确 ID 替换 gateway，worker 不动。
                            await self._remove_gateway(
                                report["gateway_instance_id"], workspace,
                                worker)
                            return await self._create(
                                workspace, version, worker, worker_origin,
                                worker_service_token)
                        await self._configure(
                            report, workspace, version, worker,
                            worker_origin, worker_service_token)
                        return self._candidate(
                            workspace, version, worker, report,
                            await self._status(report["gateway_instance_id"]),
                            active=False, created=False)
                # 目标已变更时按旧 gateway 标签中的旧 worker ID 精确删除，
                # 不把新 worker 误当成旧 gateway 的归属。
                await self._remove_gateway(
                    report["gateway_instance_id"], workspace,
                    report["worker_container_id"])
            return await self._create(
                workspace, version, worker, worker_origin,
                worker_service_token)

    async def _create(self, workspace: str, version: int, worker: str,
                      worker_origin: str, worker_service_token: str
                      ) -> GatewayCandidate:
        payload = await self._network_payload()
        control_ip = allocate_gateway_ip(
            payload, self.settings.control_network)
        argv = build_gateway_create_argv(
            self.settings, workspace_id=workspace, config_version=version,
            worker_container_id=worker, control_ip=control_ip)
        result = await self._docker(*argv, timeout=60.0)
        if result.returncode != 0:
            raise GatewayRuntimeError("gateway_create_failed")
        instance_id = str(result.stdout or "").strip()
        if _CONTAINER_ID.fullmatch(instance_id) is None:
            raise GatewayRuntimeError("gateway_create_failed")
        created = True
        try:
            raw = await self._inspect_id(instance_id)
            if raw is None or not verify_gateway_inspect(
                    raw, self.settings, workspace_id=workspace,
                    config_version=version, worker_container_id=worker,
                    control_ip=control_ip,
                    worker_network_connected=False):
                raise GatewayRuntimeError("gateway_container_conflict")
            result = await self._docker("start", instance_id, timeout=30.0)
            if result.returncode != 0:
                raise GatewayRuntimeError("gateway_start_failed")
            status = await self._wait_started(instance_id)
            if (status.get("gateway_instance_id") != ""
                    or status.get("configured") is not False
                    or status.get("boot_generation") is None):
                raise GatewayRuntimeError("gateway_status_invalid")
            report = sanitize_gateway_inspect(
                raw, workspace_id=workspace)
            await self._configure(
                report, workspace, version, worker, worker_origin,
                worker_service_token)
            connected = await self._docker(
                "network", "connect", gateway_worker_network(workspace),
                instance_id, timeout=30.0)
            if connected.returncode != 0:
                raise GatewayRuntimeError("gateway_network_failed")
            after = await self._inspect_id(instance_id)
            if after is None or not verify_gateway_inspect(
                    after, self.settings, workspace_id=workspace,
                    config_version=version, worker_container_id=worker,
                    control_ip=control_ip,
                    worker_network_connected=True):
                raise GatewayRuntimeError("gateway_network_failed")
            status = await self._status(instance_id)
            report = sanitize_gateway_inspect(
                after, workspace_id=workspace)
            return self._candidate(
                workspace, version, worker, report, status,
                active=False, created=created)
        except BaseException:
            try:
                await self._remove_gateway(instance_id, workspace, worker)
            except GatewayRuntimeError:
                pass
            raise

    async def _configure(self, report: dict, workspace: str, version: int,
                         worker: str, worker_origin: str,
                         worker_service_token: str) -> dict:
        instance_id = report["gateway_instance_id"]
        status = await self._status(instance_id)
        payload = {
            "workspace_id": workspace,
            "gateway_instance_id": instance_id,
            "expected_boot_generation": status["boot_generation"],
            "worker_container_id": worker,
            "config_version": version,
            "worker_origin": worker_origin,
            "worker_service_token": worker_service_token,
        }
        configured = await self._control(instance_id, "configure", payload)
        if not verify_gateway_status(
                configured, self.settings, workspace_id=workspace,
                worker_container_id=worker, config_version=version,
                gateway_instance_id=instance_id,
                boot_generation=status["boot_generation"],
                active=False, available=False,
                control_ip=report["control_ip"]):
            raise GatewayRuntimeError("gateway_config_invalid")
        return configured

    def _candidate(self, workspace: str, version: int, worker: str,
                   report: dict, status: dict, *, active: bool,
                   created: bool) -> GatewayCandidate:
        return GatewayCandidate(
            workspace_id=workspace, config_version=version,
            worker_container_id=worker,
            gateway_instance_id=report["gateway_instance_id"],
            boot_generation=status["boot_generation"],
            gateway_upstream=(
                f"http://{report['control_ip']}:{self.settings.port}"),
            worker_created=False, gateway_created=created, active=active)

    async def activate(self, *, workspace_id: str, config_version: int,
                       worker_container_id: str,
                       gateway_instance_id: str,
                       boot_generation: str) -> dict:
        workspace = _workspace(workspace_id)
        version = _version(config_version)
        worker = _container_id(worker_container_id, "gateway_worker_invalid")
        gateway = _container_id(
            gateway_instance_id, "gateway_instance_invalid")
        if _BOOT.fullmatch(str(boot_generation)) is None:
            raise GatewayRuntimeError("gateway_boot_invalid")
        raw = await self._inspect_id(gateway)
        if raw is None:
            raise GatewayRuntimeError("gateway_instance_missing")
        report = sanitize_gateway_inspect(raw, workspace_id=workspace)
        if (not report["managed"]
                or report["gateway_instance_id"] != gateway
                or report["config_version"] != version
                or report["worker_container_id"] != worker):
            raise GatewayRuntimeError("gateway_target_mismatch")
        if not verify_gateway_inspect(
                raw, self.settings, workspace_id=workspace,
                config_version=version, worker_container_id=worker,
                control_ip=report["control_ip"],
                worker_network_connected=True):
            raise GatewayRuntimeError("gateway_container_conflict")
        status = await self._status(gateway)
        if not verify_gateway_status(
                status, self.settings, workspace_id=workspace,
                worker_container_id=worker, config_version=version,
                gateway_instance_id=gateway,
                boot_generation=boot_generation,
                active=False, available=False,
                control_ip=report["control_ip"]):
            raise GatewayRuntimeError("gateway_status_mismatch")
        payload = {
            "workspace_id": workspace,
            "gateway_instance_id": gateway,
            "boot_generation": boot_generation,
            "worker_container_id": worker,
            "config_version": version,
        }
        active = await self._control(gateway, "activate", payload)
        if not verify_gateway_status(
                active, self.settings, workspace_id=workspace,
                worker_container_id=worker, config_version=version,
                gateway_instance_id=gateway,
                boot_generation=boot_generation,
                active=True, available=True,
                control_ip=report["control_ip"]):
            raise GatewayRuntimeError("gateway_activation_invalid")
        return active

    async def inspect(self, *, workspace_id: str,
                      gateway_instance_id: str = "") -> dict:
        workspace = _workspace(workspace_id)
        raw = (await self._inspect_id(gateway_instance_id)
               if gateway_instance_id else
               await self._inspect_name(gateway_container_name(workspace)))
        if raw is None:
            return {
                "managed": False, "workspace_id": workspace,
                "gateway_instance_id": "", "config_version": 0,
                "worker_container_id": "", "control_ip": "", "worker_ip": "",
                "networks": [], "image": "", "command": [], "security": {},
                "mounts": [], "available": False, "active": False,
                "configured": False, "boot_generation": "",
                "gateway_upstream": "", "worker_origin": "",
            }
        report = sanitize_gateway_inspect(raw, workspace_id=workspace)
        if not report["managed"]:
            return {**report, "available": False, "active": False,
                    "configured": False, "boot_generation": "",
                    "gateway_upstream": "", "worker_origin": ""}
        status: dict = {}
        try:
            status = await self._status(report["gateway_instance_id"])
        except GatewayRuntimeError:
            pass
        report["boot_generation"] = status.get("boot_generation", "")
        report["active"] = status.get("active") is True
        report["configured"] = status.get("configured") is True
        report["available"] = bool(
            status and status.get("available") is True
            and verify_gateway_inspect(
                raw, self.settings, workspace_id=workspace,
                config_version=report["config_version"],
                worker_container_id=report["worker_container_id"],
                control_ip=report["control_ip"],
                worker_network_connected=True))
        report["gateway_upstream"] = (
            f"http://{report['control_ip']}:{self.settings.port}"
            if report["control_ip"] else "")
        report["worker_origin"] = status.get("worker_origin", "")
        return report

    async def stop(self, workspace_id: str, worker_container_id: str,
                   gateway_instance_id: str) -> None:
        workspace = _workspace(workspace_id)
        worker = _container_id(worker_container_id, "gateway_worker_invalid")
        gateway = _container_id(
            gateway_instance_id, "gateway_instance_invalid")
        raw = await self._inspect_id(gateway)
        if raw is None:
            return
        report = sanitize_gateway_inspect(raw, workspace_id=workspace)
        if (not report["managed"]
                or report["gateway_instance_id"] != gateway
                or report["worker_container_id"] != worker):
            raise GatewayRuntimeError("gateway_target_mismatch")
        await self._remove_gateway(gateway, workspace, worker)

    async def _remove_gateway(self, gateway: str, workspace: str,
                              worker: str) -> None:
        raw = await self._inspect_id(gateway)
        if raw is None:
            return
        report = sanitize_gateway_inspect(raw, workspace_id=workspace)
        if (not report["managed"]
                or report["gateway_instance_id"] != gateway
                or report["worker_container_id"] != worker):
            raise GatewayRuntimeError("gateway_target_mismatch")
        stopped = await self._docker(
            "stop", "--time", "15", gateway, timeout=40.0)
        if stopped.returncode != 0:
            current = await self._inspect_id(gateway)
            if current is not None and (
                    (current.get("State") or {}).get("Running") is True):
                raise GatewayRuntimeError("gateway_stop_failed")
        removed = await self._docker("rm", "-f", gateway, timeout=40.0)
        if removed.returncode != 0 and await self._inspect_id(gateway) is not None:
            raise GatewayRuntimeError("gateway_remove_failed")
        if await self._inspect_id(gateway) is not None:
            raise GatewayRuntimeError("gateway_remove_failed")
