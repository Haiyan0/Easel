"""受限 worker provisioner：唯一持有容器管理权限的进程。

安全边界：

* 只监听本地 Unix socket；协议为单行 JSON，只接受 ensure/stop/inspect 和
  workspace UUID + 配置版本。**绝不**接受调用方提交的镜像、命令、挂载、
  网络、环境变量或路径，argv 全部由本进程从批准配置构造。
* 启动前验证 workspace 目录落在固定根内且不是 symlink/junction 重定向；
  持久目录只创建、不删除，stop 仅回收可再建容器。
* 镜像必须按 ``sha256`` 摘要固定；容器强制非 root、只读 rootfs、
  ``cap-drop=ALL``、``no-new-privileges``、资源上限与每空间独立内网，
  不发布宿主端口、不挂 Docker socket。
* 配额默认要求经验证的 XFS project quota；能力不可验证时拒绝启动，绝不把
  ``docker --storage-opt`` 对 bind mount 的无效限制当成配额。
* ``docker inspect`` 的原始输出包含 Env；本模块只返回脱敏后的白名单字段。

服务 token 只写入 0600 的 env 文件并经 Unix socket 返回，不进入 argv 或日志。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import socket
import stat
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from easel.control.types import WorkerHandle

LOGGER = logging.getLogger("easel.provisioner")

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 8 * 1024
REQUEST_TIMEOUT_SECONDS = 10.0
HEALTH_TIMEOUT_SECONDS = 60.0
HEALTH_POLL_SECONDS = 0.5
HEALTH_REQUEST_TIMEOUT = 3.0

WORKER_PORT = 7860
WORKER_USER = "10001:10001"
WORKER_UID = 10001
WORKER_GID = 10001
WORKER_PIDS_LIMIT = 256
WORKER_MEMORY_BYTES = 4 * 1024 ** 3
WORKER_NANO_CPUS = 2_000_000_000
WORKER_TMPFS = "/tmp:rw,noexec,nosuid,nodev,size=1g"
WORKER_COMMAND = (
    "python", "-m", "easel.worker.lifecycle",
    "serve", "--host", "0.0.0.0", "--port", str(WORKER_PORT),
)
DEFAULT_QUOTA_BYTES = 10 * 1024 ** 3

ACTIONS = ("ensure", "stop", "inspect")

_WORKSPACE_ID = re.compile(r"^[0-9a-f]{32}$")
_PINNED_IMAGE = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9._/:@+-]*@)?sha256:[0-9a-f]{64}$")
_CONTAINER_INSTANCE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class ProvisionerError(Exception):
    """协议/策略错误；只暴露稳定错误码，不包含路径或秘密。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class QuotaUnavailable(ProvisionerError):
    """配额能力无法建立或验证；调用方必须失败关闭。"""

    def __init__(self, code: str = "quota_unavailable"):
        super().__init__(code)


@dataclass(frozen=True)
class WorkerRequest:
    action: str
    workspace_id: str
    config_version: int = 0
    instance_id: str = ""


@dataclass(frozen=True)
class RuntimePaths:
    """单个 workspace 的持久目录；root 是其私有根。

    ``directory_ids`` 是 prepare 时对 workspace/home/state 捕获的
    ``(st_dev, st_ino)``。后续特权 chown 必须重新核对同一身份，不能只信
    路径——worker 拥有这些目录本身，可在两阶段之间替换路径。
    """

    root: Path
    workspace: Path
    home: Path
    state: Path
    directory_ids: tuple[tuple[int, int], ...] = ()


# ----------------------------------------------------------------------
# 协议解析与路径
# ----------------------------------------------------------------------

def _reject(code: str) -> ProvisionerError:
    return ProvisionerError(code)


def parse_request(payload: bytes | str | dict) -> WorkerRequest:
    """严格解析协议；未知字段立即拒绝，不进入任何 Docker 调用。"""
    if isinstance(payload, (bytes, bytearray)):
        if len(payload) > MAX_REQUEST_BYTES:
            raise _reject("request_too_large")
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _reject("invalid_json") from None
    elif isinstance(payload, str):
        if len(payload.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise _reject("request_too_large")
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            raise _reject("invalid_json") from None
    elif isinstance(payload, dict):
        data = payload
    else:
        raise _reject("invalid_json")

    if not isinstance(data, dict):
        raise _reject("invalid_json")
    if type(data.get("version")) is not int or data.get("version") != PROTOCOL_VERSION:
        raise _reject("unsupported_protocol")
    action = data.get("action")
    if action not in ACTIONS:
        raise _reject("invalid_action")
    allowed = {"version", "action", "workspace_id"}
    if action == "ensure":
        allowed.add("config_version")
    elif action == "stop":
        allowed.add("instance_id")
    unknown = {key for key in data if key not in allowed}
    if unknown:
        # 明确拒绝所有镜像/命令/挂载/网络/环境注入尝试
        raise _reject("unknown_field")

    workspace_id = data.get("workspace_id")
    if (not isinstance(workspace_id, str)
            or _WORKSPACE_ID.fullmatch(workspace_id) is None):
        raise _reject("invalid_workspace")

    config_version = data.get("config_version", 0)
    if action == "ensure" and "config_version" not in data:
        raise _reject("invalid_config_version")
    if not _valid_config_version(config_version):
        raise _reject("invalid_config_version")

    instance_id = ""
    if action == "stop":
        instance_id = data.get("instance_id")
        if (not isinstance(instance_id, str)
                or _CONTAINER_INSTANCE.fullmatch(instance_id) is None):
            raise _reject("invalid_instance")
    return WorkerRequest(action, workspace_id, config_version, instance_id)


def _valid_config_version(value: object) -> bool:
    return type(value) is int and 0 <= value < 2 ** 31


def container_name(workspace_id: str) -> str:
    """受管容器名唯一且由服务端派生，不接受调用方指定。"""
    if (_WORKSPACE_ID.fullmatch(workspace_id) is None):
        raise _reject("invalid_workspace")
    return f"easel-worker-{workspace_id}"


def network_name(workspace_id: str) -> str:
    if (_WORKSPACE_ID.fullmatch(workspace_id) is None):
        raise _reject("invalid_workspace")
    return f"easel-ws-{workspace_id}"


def workload_labels(workspace_id: str, config_version: int) -> dict[str, str]:
    """只使用这三个标签识别归属；inspect 也只回传这三个。"""
    return {
        "easel.managed": "true",
        "easel.workspace-id": workspace_id,
        "easel.config-version": str(config_version),
    }


def credentials_dir(workspace_root: Path) -> Path:
    """凭证目录位于全局根，不挂载给任何容器。"""
    return Path(workspace_root) / ".credentials"


def prepare_workspace(workspace_root: Path, workspace_id: str) -> RuntimePaths:
    """在固定根下创建私有目录，拒绝 symlink/junction 越界。"""
    container_name(workspace_id)  # 复用 UUID 校验
    root = Path(workspace_root)
    if root.is_symlink():
        raise _reject("workspace_escape")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved_root = root.resolve(strict=True)

    base = resolved_root / workspace_id
    if base.is_symlink():
        raise _reject("workspace_escape")
    try:
        base.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        raise _reject("workspace_unavailable") from None
    resolved_base = base.resolve(strict=True)
    if resolved_base.parent != resolved_root:
        raise _reject("workspace_escape")

    children: dict[str, Path] = {}
    for name in ("workspace", "home"):
        directory = resolved_base / name
        if directory.is_symlink():
            raise _reject("workspace_escape")
        directory.mkdir(mode=0o700, exist_ok=True)
        resolved = directory.resolve(strict=True)
        if resolved.parent != resolved_base:
            raise _reject("workspace_escape")
        children[name] = resolved

    # state 必须位于唯一数据挂载 /workspace 之内，否则 env 指向的容器路径
    # 不存在；单独建一个未挂载的兄弟目录没有意义。
    workspace = children["workspace"]
    state = workspace / "state"
    if state.is_symlink():
        raise _reject("workspace_escape")
    state.mkdir(mode=0o700, exist_ok=True)
    resolved_state = state.resolve(strict=True)
    if resolved_state.parent != workspace:
        raise _reject("workspace_escape")
    directory_ids = _capture_directory_ids(resolved_base)
    return RuntimePaths(resolved_base, workspace, children["home"],
                        resolved_state, directory_ids)


def existing_runtime_paths(workspace_root: Path, workspace_id: str
                           ) -> RuntimePaths:
    """只解析已存在的持久目录，不创建；inspect/接管不得有写副作用。"""
    container_name(workspace_id)  # 复用 UUID 校验
    try:
        root = Path(workspace_root).resolve(strict=True)
        base = (root / workspace_id).resolve(strict=True)
    except (OSError, RuntimeError):
        raise _reject("workspace_unavailable") from None
    if base.parent != root:
        raise _reject("workspace_escape")
    resolved: dict[str, Path] = {}
    for name in ("workspace", "home"):
        try:
            directory = (base / name).resolve(strict=True)
        except (OSError, RuntimeError):
            raise _reject("workspace_unavailable") from None
        if directory.parent != base or not directory.is_dir():
            raise _reject("workspace_escape")
        resolved[name] = directory
    try:
        state = (resolved["workspace"] / "state").resolve(strict=True)
    except (OSError, RuntimeError):
        raise _reject("workspace_unavailable") from None
    if state.parent != resolved["workspace"] or not state.is_dir():
        raise _reject("workspace_escape")
    directory_ids = _capture_directory_ids(base)
    return RuntimePaths(base, resolved["workspace"], resolved["home"], state,
                        directory_ids)


def _stat_directory(path: Path) -> os.stat_result:
    """单独封装，便于测试在受控 gate 中模拟身份变化。"""
    return os.stat(path)


def _directory_identity(path: Path) -> tuple[int, int]:
    info = _stat_directory(path)
    if not stat.S_ISDIR(info.st_mode):
        raise _reject("workspace_escape")
    return (int(info.st_dev), int(info.st_ino))


def _verify_directory(path: Path,
                      expected: tuple[int, int]) -> os.stat_result:
    """按身份核对目录；路径被替换/链接到别处时拒绝。"""
    try:
        info = _stat_directory(path)
    except OSError:
        raise _reject("workspace_owner_failed") from None
    if (not stat.S_ISDIR(info.st_mode)
            or (int(info.st_dev), int(info.st_ino)) != expected):
        raise _reject("workspace_escape")
    return info


def _directory_flags() -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _supports_fd_traversal() -> bool:
    """生产 provisioner 只支持 Linux；其他平台仅做身份降级核对。"""
    return (os.name == "posix" and hasattr(os, "O_DIRECTORY")
            and hasattr(os, "fchown"))


def _openat_directory(parent_fd: int, name: str,
                      expected: tuple[int, int] | None = None) -> int:
    """相对可信父目录打开子目录，不跟随最终链接并核对身份。"""
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except OSError:
        raise _reject("workspace_owner_failed") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise _reject("workspace_escape")
        if expected is not None and (
                int(info.st_dev), int(info.st_ino)) != expected:
            raise _reject("workspace_escape")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _capture_directory_ids(base: Path) -> tuple[tuple[int, int], ...]:
    """捕获 workspace/home/state 身份。

    POSIX 从 base 的 fd 逐级 openat，避免在 resolve 与 stat 之间被 worker
    用 symlink 替换任意中间目录；非 POSIX 使用路径 stat 降级（无生产承诺）。
    """
    if not _supports_fd_traversal():
        return (
            _directory_identity(base / "workspace"),
            _directory_identity(base / "home"),
            _directory_identity(base / "workspace" / "state"),
        )
    try:
        base_fd = os.open(base, _directory_flags())
    except OSError:
        raise _reject("workspace_owner_failed") from None
    workspace_fd = home_fd = state_fd = None
    try:
        workspace_fd = _openat_directory(base_fd, "workspace")
        home_fd = _openat_directory(base_fd, "home")
        state_fd = _openat_directory(workspace_fd, "state")
        return tuple(
            (int(os.fstat(descriptor).st_dev),
             int(os.fstat(descriptor).st_ino))
            for descriptor in (workspace_fd, home_fd, state_fd))
    finally:
        for descriptor in (state_fd, home_fd, workspace_fd, base_fd):
            if descriptor is not None:
                os.close(descriptor)


def _open_verified_tree(paths: RuntimePaths) -> tuple[int, int, int, int]:
    """打开 base/workspace/home/state 并核对身份；返回 fd，调用方负责关闭。"""
    identities = paths.directory_ids
    if len(identities) != 3:
        raise _reject("workspace_escape")
    try:
        base_fd = os.open(paths.workspace.parent, _directory_flags())
    except OSError:
        raise _reject("workspace_owner_failed") from None
    workspace_fd = home_fd = state_fd = None
    try:
        workspace_fd = _openat_directory(base_fd, "workspace",
                                         identities[0])
        home_fd = _openat_directory(base_fd, "home", identities[1])
        state_fd = _openat_directory(workspace_fd, "state", identities[2])
        return base_fd, workspace_fd, home_fd, state_fd
    except BaseException:
        for descriptor in (state_fd, home_fd, workspace_fd, base_fd):
            if descriptor is not None:
                os.close(descriptor)
        raise


def _close_tree(fds: tuple[int, int, int, int]) -> None:
    for descriptor in reversed(fds):
        os.close(descriptor)


def ensure_worker_ownership(paths: RuntimePaths) -> None:
    """让容器内的 uid 10001 能写持久目录。

    关键约束：worker 已拥有这些目录的写权限，prepare 之后可替换
    workspace/state。因此这里从 base 的 fd 逐级 openat，核对 prepare 时
    捕获的 ``(st_dev, st_ino)``；POSIX 上对已核对 fd 执行 ``fchown``，
    不再按路径 chown，彻底避免跟随替换后的链接。
    """
    if not hasattr(os, "geteuid"):
        return
    directories = (paths.workspace, paths.home, paths.state)
    identities = paths.directory_ids
    if len(identities) != len(directories):
        raise _reject("workspace_escape")
    if os.geteuid() == 0:
        if _supports_fd_traversal():
            fds = _open_verified_tree(paths)
            try:
                for descriptor in fds[1:]:
                    try:
                        os.fchown(descriptor, WORKER_UID, WORKER_GID)
                    except OSError:
                        raise _reject("workspace_owner_failed") from None
            finally:
                _close_tree(fds)
            return
        # 非 POSIX 降级：先核对全部身份，再执行路径 chown。
        for directory, identity in zip(directories, identities):
            _verify_directory(directory, identity)
        for directory in directories:
            try:
                os.chown(directory, WORKER_UID, WORKER_GID)
            except OSError:
                raise _reject("workspace_owner_failed") from None
        return
    if _supports_fd_traversal():
        fds = _open_verified_tree(paths)
        try:
            for descriptor in fds[1:]:
                info = os.fstat(descriptor)
                if (info.st_uid != WORKER_UID
                        and not (info.st_mode & 0o002)):
                    raise _reject("workspace_owner_mismatch")
        finally:
            _close_tree(fds)
        return
    for directory, identity in zip(directories, identities):
        info = _verify_directory(directory, identity)
        if info.st_uid != WORKER_UID and not (info.st_mode & 0o002):
            raise _reject("workspace_owner_mismatch")


def verify_prepared_paths(paths: RuntimePaths) -> None:
    """挂载/启动前再次核对身份；base 由 root 拥有，挂载源不可被替换。"""
    if not _supports_fd_traversal():
        directories = (paths.workspace, paths.home, paths.state)
        if len(paths.directory_ids) != len(directories):
            raise _reject("workspace_escape")
        for directory, identity in zip(directories, paths.directory_ids):
            _verify_directory(directory, identity)
        return
    fds = _open_verified_tree(paths)
    _close_tree(fds)


def _safe_path_text(path: Path) -> str:
    text = str(path)
    if any(character in text for character in (",", "\n", "\r", "\0")):
        raise _reject("unsafe_path")
    return text


def build_run_argv(*, image: str, workspace_id: str, config_version: int,
                   paths: RuntimePaths, env_file: Path) -> list[str]:
    """从批准配置构造唯一允许的 docker run argv。

    容器代码位于只读镜像 ``/opt/easel``；仅两个持久 bind：``/workspace`` 与
    ``/home/easel``，来源都经过 prepare_workspace 校验。网络是每空间内部
    网络（``--internal``），没有宿主端口和默认外网路由。
    """
    if not isinstance(image, str) or _PINNED_IMAGE.fullmatch(image) is None:
        raise _reject("invalid_image")
    if not _valid_config_version(config_version):
        raise _reject("invalid_config_version")
    name = container_name(workspace_id)
    labels = workload_labels(workspace_id, config_version)
    argv = [
        "run", "-d",
        "--name", name,
    ]
    for key, value in labels.items():
        argv.extend(["--label", f"{key}={value}"])
    argv.extend([
        "--user", WORKER_USER,
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--cpus=2",
        "--memory=4g",
        "--memory-swap=4g",
        "--pids-limit=256",
        "--tmpfs", WORKER_TMPFS,
        "--network", network_name(workspace_id),
        "--mount",
        f"type=bind,src={_safe_path_text(paths.workspace)},dst=/workspace",
        "--mount",
        f"type=bind,src={_safe_path_text(paths.home)},dst=/home/easel",
        "--env-file", _safe_path_text(env_file),
        "--restart=no",
        "--stop-timeout=30",
        "--log-driver=local",
        "--log-opt=max-size=10m",
        "--log-opt=max-file=3",
        image,
        "python", "-m", "easel.worker.lifecycle",
        "serve", "--host", "0.0.0.0", "--port", str(WORKER_PORT),
    ])
    return argv


def sanitize_inspect(raw: dict | None) -> dict:
    """把 docker inspect 压成白名单字段；Env、宿主路径与未知标签全部丢弃。"""
    empty = {
        "workspace_id": "",
        "container_id": "",
        "instance_id": "",
        "config_version": 0,
        "ready": False,
        "managed": False,
        "labels": {},
        "upstream": "",
        "security": {},
    }
    if not isinstance(raw, dict):
        return empty
    config = raw.get("Config") or {}
    labels = config.get("Labels") if isinstance(config, dict) else None
    if not isinstance(labels, dict):
        return empty
    workspace_id = labels.get("easel.workspace-id")
    name = str(raw.get("Name") or "").lstrip("/")
    if (labels.get("easel.managed") != "true"
            or not isinstance(workspace_id, str)
            or _WORKSPACE_ID.fullmatch(workspace_id) is None
            or name != container_name(workspace_id)):
        return empty
    try:
        config_version = int(labels.get("easel.config-version", ""))
    except (TypeError, ValueError):
        return empty
    if not _valid_config_version(config_version):
        return empty

    network_settings = raw.get("NetworkSettings") or {}
    ports = network_settings.get("Ports") or {}
    if any(bool(bindings) for bindings in ports.values()):
        # 发布宿主端口直接视为不受管，避免误接管
        return empty
    networks = network_settings.get("Networks") or {}
    address = ""
    if isinstance(networks, dict):
        entry = networks.get(network_name(workspace_id)) or {}
        if isinstance(entry, dict):
            address = entry.get("IPAddress") or ""
    state = raw.get("State") or {}
    running = state.get("Running") is True
    health = (state.get("Health") or {}).get("Status")
    ready = running and health in (None, "healthy")

    host = raw.get("HostConfig") or {}
    security_options = host.get("SecurityOpt") or []
    security = {
        "user": config.get("User") or "",
        "read_only": bool(host.get("ReadonlyRootfs")),
        "privileged": bool(host.get("Privileged")),
        "cap_drop": list(host.get("CapDrop") or []),
        "no_new_privileges": any(
            "no-new-privileges" in str(option)
            for option in security_options),
        "pids_limit": int(host.get("PidsLimit") or 0),
        "memory_bytes": int(host.get("Memory") or 0),
        "nano_cpus": int(host.get("NanoCpus") or 0),
    }
    return {
        "workspace_id": workspace_id,
        "container_id": name,
        "instance_id": str(raw.get("Id") or ""),
        "config_version": config_version,
        "ready": ready,
        "managed": True,
        "labels": workload_labels(workspace_id, config_version),
        "upstream": (f"http://{address}:{WORKER_PORT}" if address else ""),
        "security": security,
    }


def _no_new_privileges_enabled(options: object) -> bool:
    """只接受 Docker 的规范启用形式；false/伪造包含/冲突一律不启用。

    Docker 合法写法为 ``no-new-privileges`` 或
    ``no-new-privileges:true``。子串匹配会把
    ``not-no-new-privileges:true`` 或 ``no-new-privileges:false`` 误判为
    已启用，因此逐项按规范值解析并要求恰好一个启用项。
    """
    if not isinstance(options, (list, tuple)):
        return False
    values: list[bool] = []
    for option in options:
        text = str(option)
        if text in ("no-new-privileges", "no-new-privileges:true"):
            values.append(True)
        elif text.startswith("no-new-privileges"):
            values.append(False)
    return values == [True]


def verify_worker_inspect(raw: dict | None, *, workspace_id: str,
                          config_version: int, image: str,
                          paths: RuntimePaths) -> bool:
    """严格核对实际容器配置；复用、接管、inspect 与健康循环共用同一门槛。

    标签只表示归属，不能证明镜像、权限、资源、挂载或网络仍是批准配置。
    任何字段缺失、类型异常或与批准值不一致都返回 False。
    """
    if not isinstance(raw, dict):
        return False
    instance_id = raw.get("Id")
    if (not isinstance(instance_id, str)
            or _CONTAINER_INSTANCE.fullmatch(instance_id) is None):
        return False
    report = sanitize_inspect(raw)
    if not report["managed"] or report["config_version"] != config_version:
        return False

    config = raw.get("Config")
    host = raw.get("HostConfig")
    if not isinstance(config, dict) or not isinstance(host, dict):
        return False
    if config.get("Image") != image:
        return False
    if config.get("User") != WORKER_USER:
        return False
    if config.get("Entrypoint") not in (None, []):
        return False
    if config.get("Cmd") != list(WORKER_COMMAND):
        return False

    if host.get("ReadonlyRootfs") is not True:
        return False
    if host.get("Privileged") is not False:
        return False
    if set(host.get("CapDrop") or []) != {"ALL"}:
        return False
    if host.get("CapAdd") not in (None, []):
        return False
    security_options = host.get("SecurityOpt") or []
    if not _no_new_privileges_enabled(security_options):
        return False
    if any("unconfined" in str(option) for option in security_options):
        return False
    if host.get("PidsLimit") != WORKER_PIDS_LIMIT:
        return False
    if host.get("Memory") != WORKER_MEMORY_BYTES:
        return False
    if host.get("MemorySwap") != WORKER_MEMORY_BYTES:
        return False
    if host.get("NanoCpus") != WORKER_NANO_CPUS:
        return False
    if host.get("NetworkMode") != network_name(workspace_id):
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
    tmp_options = str(tmpfs.get("/tmp") or "").split(",")
    required = {"rw", "noexec", "nosuid", "nodev", "size=1g"}
    if not required.issubset(set(tmp_options)):
        return False

    mounts = raw.get("Mounts")
    if not isinstance(mounts, list) or len(mounts) != 2:
        return False
    expected_mounts = {
        "/workspace": str(paths.workspace),
        "/home/easel": str(paths.home),
    }
    seen: dict[str, str] = {}
    for mount in mounts:
        if not isinstance(mount, dict):
            return False
        destination = mount.get("Destination")
        source = mount.get("Source")
        if (mount.get("Type") != "bind" or mount.get("RW") is not True
                or destination not in expected_mounts
                or source != expected_mounts[destination]
                or "docker.sock" in str(source)):
            return False
        seen[destination] = str(source)
    if set(seen) != set(expected_mounts):
        return False

    network_settings = raw.get("NetworkSettings")
    if not isinstance(network_settings, dict):
        return False
    networks = network_settings.get("Networks")
    approved = network_name(workspace_id)
    if not isinstance(networks, dict) or set(networks) != {approved}:
        return False
    # 静态安全验收不要求运行时 IP：已退出容器没有活动 endpoint。
    # 是否可复用由调用方结合 ready/upstream 与健康探测判断。
    if not isinstance(networks.get(approved), dict):
        return False
    ports = network_settings.get("Ports") or {}
    if not isinstance(ports, dict) or any(
            bool(bindings) for bindings in ports.values()):
        return False
    return True


def verify_network_inspect(payload: object, workspace_id: str) -> bool:
    """已有的受管网络必须仍具备 internal、IPv4-only、bridge 驱动。"""
    if not isinstance(payload, list) or len(payload) != 1:
        return False
    entry = payload[0]
    if not isinstance(entry, dict):
        return False
    labels = entry.get("Labels")
    return (
        entry.get("Name") == network_name(workspace_id)
        and entry.get("Driver") == "bridge"
        and entry.get("Internal") is True
        and entry.get("EnableIPv6") is False
        and isinstance(labels, dict)
        and labels.get("easel.managed") == "true"
        and labels.get("easel.workspace-id") == workspace_id)


# ----------------------------------------------------------------------
# 配额
# ----------------------------------------------------------------------

def _project_id(workspace_id: str) -> int:
    digest = hashlib.sha256(workspace_id.encode("ascii")).hexdigest()
    value = int(digest[:8], 16) % (2 ** 31 - 1)
    return value or 1


def _find_mount(path: Path) -> tuple[Path, str, tuple[str, ...]]:
    """从 /proc/mounts 找到最长匹配挂载点；仅 Linux 可用。"""
    target = path.resolve(strict=True)
    best: tuple[Path, str, tuple[str, ...]] | None = None
    try:
        mounts = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    except OSError:
        raise QuotaUnavailable("quota_mounts_unavailable") from None
    for line in mounts:
        parts = line.split()
        if len(parts) < 4:
            continue
        mountpoint = Path(_unescape_mount(parts[1]))
        if not target.is_relative_to(mountpoint):
            continue
        if best is None or len(str(mountpoint)) > len(str(best[0])):
            best = (mountpoint, parts[2], tuple(parts[3].split(",")))
    if best is None:
        raise QuotaUnavailable("quota_mounts_unavailable")
    return best


def _unescape_mount(value: str) -> str:
    return (value.replace("\\040", " ").replace("\\011", "\t")
            .replace("\\012", "\n").replace("\\134", "\\"))


def _format_limit(limit_bytes: int) -> str:
    if limit_bytes % (1024 ** 3) == 0:
        return f"{limit_bytes // (1024 ** 3)}g"
    if limit_bytes % (1024 ** 2) == 0:
        return f"{limit_bytes // (1024 ** 2)}m"
    return f"{limit_bytes // 1024}k"


def _quota_verified(output: str, project_id: int, limit_bytes: int) -> bool:
    """确认 report 中该 project 的 hard 限额与期望一致。

    xfs_quota 的块单位随文件系统块大小变化，因此接受字节或 1KiB 单位两种
    表示；两者都不匹配即视为未验证。真实 XFS 上的输出仍需人工复核。
    """
    for line in output.splitlines():
        fields = line.replace("#", " ").split()
        if len(fields) < 4 or fields[0] != str(project_id):
            continue
        for candidate in fields[3:]:
            try:
                value = int(candidate.rstrip("*"))
            except ValueError:
                continue
            if value in (limit_bytes, limit_bytes // 1024):
                return True
    return False


def _default_run(argv: list[str]) -> int:
    return subprocess.run(argv, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, check=False).returncode


def _default_capture(argv: list[str]) -> tuple[int, str]:
    result = subprocess.run(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, check=False)
    return result.returncode, result.stdout


def ensure_quota(path: Path, workspace_id: str, *, mode: str,
                 limit_bytes: int = DEFAULT_QUOTA_BYTES,
                 allow_unquotaed: bool = False,
                 run: Callable[[list[str]], int] | None = None,
                 capture: Callable[[list[str]], tuple[int, str]] | None = None
                 ) -> None:
    """建立并验证持久区配额；任何一步无法验证都必须失败关闭。"""
    run = run or _default_run
    capture = capture or _default_capture
    if mode not in ("project", "none"):
        raise QuotaUnavailable("quota_mode_invalid")
    if mode == "none":
        if not allow_unquotaed:
            raise QuotaUnavailable("quota_disabled")
        return
    if type(limit_bytes) is not int or limit_bytes <= 0:
        raise QuotaUnavailable("quota_limit_invalid")

    if run(["xfs_quota", "-V"]) != 0:
        raise QuotaUnavailable("quota_tool_unavailable")
    mountpoint, fstype, options = _find_mount(Path(path))
    if fstype != "xfs" or not ({"prjquota", "pquota"} & set(options)):
        # bind mount 上的 docker --storage-opt 不构成配额
        raise QuotaUnavailable("quota_filesystem_unsupported")

    project_id = _project_id(workspace_id)
    if run(["xfs_quota", "-x", "-c",
            f"project -s -p {Path(path).resolve()} {project_id}",
            str(mountpoint)]) != 0:
        raise QuotaUnavailable("quota_project_failed")
    if run(["xfs_quota", "-x", "-c",
            f"limit -p bhard={_format_limit(limit_bytes)} {project_id}",
            str(mountpoint)]) != 0:
        raise QuotaUnavailable("quota_limit_failed")
    code, output = capture(["xfs_quota", "-x", "-c",
                            f"quota -p -b -N {project_id}", str(mountpoint)])
    if code != 0 or not _quota_verified(output, project_id, limit_bytes):
        raise QuotaUnavailable("quota_unverified")


# ----------------------------------------------------------------------
# 凭证与环境文件
# ----------------------------------------------------------------------

def _load_or_create_token(workspace_root: Path, workspace_id: str) -> str:
    directory = credentials_dir(workspace_root)
    if directory.is_symlink():
        raise _reject("credentials_escape")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{workspace_id}.token"
    if path.is_symlink():
        raise _reject("credentials_escape")
    try:
        token = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        token = secrets.token_urlsafe(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(path, flags, 0o600)
        try:
            os.write(descriptor, token.encode("ascii"))
        finally:
            os.close(descriptor)
        return token
    if not token or not token.isascii() or len(token) > 512:
        raise _reject("credentials_invalid")
    return token


def write_env_file(workspace_root: Path, workspace_id: str,
                   config_version: int, token: str,
                   paths: RuntimePaths) -> Path:
    """原子写入 0600 env 文件；argv 与日志不含 token。"""
    if not token or not token.isascii() or any(
            character in token for character in ("\n", "\r", "\0")):
        raise _reject("credentials_invalid")
    directory = credentials_dir(workspace_root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / f"{workspace_id}.env"
    if target.is_symlink():
        raise _reject("credentials_escape")
    content = "\n".join((
        "EASEL_SERVICE_MODE=worker",
        f"EASEL_WORKSPACE_ID={workspace_id}",
        f"EASEL_SERVICE_TOKEN={token}",
        f"EASEL_CONFIG_VERSION={config_version}",
        f"EASEL_CONTAINER_ID={container_name(workspace_id)}",
        "EASEL_CODE_ROOT=/opt/easel",
        "EASEL_WORKSPACE_ROOT=/workspace",
        "EASEL_STATE_ROOT=/workspace/state",
        "HOME=/home/easel",
        "PYTHONDONTWRITEBYTECODE=1",
    )) + "\n"
    temporary = directory / f".{workspace_id}.env.{secrets.token_hex(8)}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, content.encode("utf-8"))
    finally:
        os.close(descriptor)
    os.replace(temporary, target)
    return target


def peer_allowed(uid: int | None, allowed_uids: set[int]) -> bool:
    """Unix socket 对端必须属于允许的 UID；无法读取凭据时拒绝。"""
    return uid is not None and uid in allowed_uids


# ----------------------------------------------------------------------
# Provisioner 服务
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class ProvisionerSettings:
    image: str
    workspace_root: Path
    socket_path: Path
    quota_mode: str = "project"
    quota_bytes: int = DEFAULT_QUOTA_BYTES
    allow_unquotaed: bool = False
    allowed_uids: frozenset[int] = frozenset()
    health_timeout: float = HEALTH_TIMEOUT_SECONDS
    docker: str = "docker"


class Provisioner:
    """实现 ensure/stop/inspect；所有 Docker 调用都经过白名单 argv。"""

    def __init__(self, settings: ProvisionerSettings,
                 run: Callable[..., subprocess.CompletedProcess] | None = None):
        self.settings = settings
        self._runner = run or self._subprocess_run
        self._locks: dict[str, asyncio.Lock] = {}

    # -- Docker 调用 ---------------------------------------------------

    def _subprocess_run(self, argv: list[str], *,
                        timeout: float = 120.0) -> subprocess.CompletedProcess:
        return subprocess.run(argv, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, check=False,
                              timeout=timeout)

    async def _docker(self, *args: str, timeout: float = 120.0
                      ) -> subprocess.CompletedProcess:
        argv = [self.settings.docker, *args]
        return await asyncio.to_thread(self._runner, argv, timeout=timeout)

    async def _inspect_raw(self, workspace_id: str) -> dict | None:
        result = await self._docker("container", "inspect",
                                    container_name(workspace_id))
        if result.returncode != 0:
            return None
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise ProvisionerError("inspect_invalid") from None
        if not isinstance(payload, list) or not payload:
            return None
        return payload[0] if isinstance(payload[0], dict) else None

    async def _network_payload(self, workspace_id: str) -> list | None:
        result = await self._docker("network", "inspect",
                                    network_name(workspace_id))
        if result.returncode != 0:
            return None
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise ProvisionerError("network_invalid") from None
        return payload if isinstance(payload, list) else None

    async def _ensure_network(self, workspace_id: str) -> None:
        """已有网络严格复核；新建后也必须再次复核实际隔离属性。"""
        name = network_name(workspace_id)
        payload = await self._network_payload(workspace_id)
        if payload is not None:
            if not verify_network_inspect(payload, workspace_id):
                raise ProvisionerError("network_conflict")
            return
        result = await self._docker(
            "network", "create", "--internal",
            "--label", "easel.managed=true",
            "--label", f"easel.workspace-id={workspace_id}",
            name)
        if result.returncode != 0:
            raise ProvisionerError("network_create_failed")
        payload = await self._network_payload(workspace_id)
        if payload is None or not verify_network_inspect(payload,
                                                         workspace_id):
            raise ProvisionerError("network_conflict")

    async def _require_existing_network(self, workspace_id: str) -> None:
        payload = await self._network_payload(workspace_id)
        if payload is None or not verify_network_inspect(payload,
                                                         workspace_id):
            raise ProvisionerError("network_conflict")

    async def _remove_container(self, workspace_id: str) -> None:
        await self._docker("rm", "-f", container_name(workspace_id))

    # -- 对外动作 ------------------------------------------------------

    async def ensure(self, workspace_id: str,
                     config_version: int) -> WorkerHandle:
        parse_request({"version": PROTOCOL_VERSION, "action": "ensure",
                       "workspace_id": workspace_id,
                       "config_version": config_version})
        lock = self._locks.setdefault(workspace_id, asyncio.Lock())
        async with lock:
            return await self._ensure_locked(workspace_id, config_version)

    async def _ensure_locked(self, workspace_id: str,
                             config_version: int) -> WorkerHandle:
        paths = await asyncio.to_thread(
            prepare_workspace, self.settings.workspace_root, workspace_id)
        await asyncio.to_thread(ensure_worker_ownership, paths)
        await asyncio.to_thread(
            ensure_quota, paths.root, workspace_id,
            mode=self.settings.quota_mode,
            limit_bytes=self.settings.quota_bytes,
            allow_unquotaed=self.settings.allow_unquotaed)
        token = await asyncio.to_thread(
            _load_or_create_token, self.settings.workspace_root, workspace_id)
        env_file = await asyncio.to_thread(
            write_env_file, self.settings.workspace_root, workspace_id,
            config_version, token, paths)
        await self._ensure_network(workspace_id)

        existing = await self._inspect_raw(workspace_id)
        if existing is not None:
            report = sanitize_inspect(existing)
            if not report["managed"]:
                raise ProvisionerError("container_conflict")
            if report["config_version"] != config_version:
                raise ProvisionerError("config_version_conflict")
            # 标签与健康响应不能证明安全配置；不符时失败关闭，不自动
            # 删除来源不明的容器，交部署方核查。
            if not verify_worker_inspect(
                    existing, workspace_id=workspace_id,
                    config_version=config_version,
                    image=self.settings.image, paths=paths):
                raise ProvisionerError("container_conflict")
            if report["ready"] and await self._probe(
                    workspace_id, config_version, report["upstream"], token):
                return WorkerHandle(workspace_id, report["instance_id"],
                                    report["upstream"], token, config_version)
            await self._remove_container(workspace_id)

        await asyncio.to_thread(verify_prepared_paths, paths)
        argv = build_run_argv(
            image=self.settings.image, workspace_id=workspace_id,
            config_version=config_version, paths=paths, env_file=env_file)
        # build_run_argv 以 run 开头；_docker 自行添加 docker 可执行程序，
        # 必须传完整 argv，不能丢弃 run。
        result = await self._docker(*argv, timeout=60.0)
        if result.returncode != 0:
            await self._remove_container(workspace_id)
            raise ProvisionerError("worker_start_failed")

        deadline = time.monotonic() + self.settings.health_timeout
        try:
            while time.monotonic() < deadline:
                raw = await self._inspect_raw(workspace_id)
                if raw is None:
                    break
                if not verify_worker_inspect(
                        raw, workspace_id=workspace_id,
                        config_version=config_version,
                        image=self.settings.image, paths=paths):
                    raise ProvisionerError("container_conflict")
                report = sanitize_inspect(raw)
                if (report["ready"]
                        and await self._probe(workspace_id, config_version,
                                              report["upstream"], token)):
                    return WorkerHandle(workspace_id, report["instance_id"],
                                        report["upstream"], token,
                                        config_version)
                await asyncio.sleep(HEALTH_POLL_SECONDS)
        except BaseException:
            await self._remove_container(workspace_id)
            raise
        await self._remove_container(workspace_id)
        raise ProvisionerError("worker_not_ready")

    async def stop(self, workspace_id: str, instance_id: str) -> None:
        """只停止调用方取得的那个物理实例；同名新容器绝不受影响。

        物理动作使用 Docker 不可复用的容器 ID，而不是当前同名容器名。
        即使旧 stop 在租约被新代次接管后才送达，ID 不匹配即拒绝。
        """
        parse_request({"version": PROTOCOL_VERSION, "action": "stop",
                       "workspace_id": workspace_id,
                       "instance_id": instance_id})
        lock = self._locks.setdefault(workspace_id, asyncio.Lock())
        async with lock:
            raw = await self._inspect_raw(workspace_id)
            if raw is None:
                return
            report = sanitize_inspect(raw)
            if not report["managed"]:
                raise ProvisionerError("container_conflict")
            if (not isinstance(raw.get("Id"), str)
                    or raw.get("Id") != instance_id):
                raise ProvisionerError("container_conflict")
            await self._docker("stop", "--time", "30", instance_id,
                               timeout=60.0)
            await self._docker("rm", "-f", instance_id, timeout=60.0)

    async def inspect(self, workspace_id: str) -> dict:
        parse_request({"version": PROTOCOL_VERSION, "action": "inspect",
                       "workspace_id": workspace_id})
        raw = await self._inspect_raw(workspace_id)
        if raw is None:
            return {
                "workspace_id": workspace_id,
                "container_id": "",
                "config_version": 0,
                "ready": False,
                "managed": False,
                "labels": {},
                "upstream": "",
                "security": {},
            }
        report = sanitize_inspect(raw)
        if not report["managed"]:
            return report
        try:
            paths = await asyncio.to_thread(
                existing_runtime_paths, self.settings.workspace_root,
                workspace_id)
        except ProvisionerError:
            raise ProvisionerError("container_conflict") from None
        if not verify_worker_inspect(
                raw, workspace_id=workspace_id,
                config_version=report["config_version"],
                image=self.settings.image, paths=paths):
            raise ProvisionerError("container_conflict")
        # 接管前复核实际网络的 internal/IPv4-only/bridge 属性。
        await self._require_existing_network(workspace_id)
        if report["ready"]:
            try:
                token = _load_or_create_token(
                    self.settings.workspace_root, workspace_id)
            except ProvisionerError:
                report["ready"] = False
                return report
            report["ready"] = await self._probe(
                workspace_id, report["config_version"], report["upstream"],
                token)
        return report

    async def _probe(self, workspace_id: str, config_version: int,
                     upstream: str, token: str) -> bool:
        """核对 lifecycle 身份响应；不执行 prompt，不接纳快照。"""
        if not upstream:
            return False
        parsed = urlsplit(upstream)
        host, port = parsed.hostname, parsed.port
        if not host or not port:
            return False
        headers = {
            "X-Easel-Workspace-Id": workspace_id,
            "X-Easel-Service-Token": token,
        }
        try:
            payload = await asyncio.to_thread(
                _http_get_json, host, port, "/internal/lifecycle/health",
                headers, HEALTH_REQUEST_TIMEOUT)
        except Exception:
            return False
        return (
            isinstance(payload, dict)
            and payload.get("workspace_id") == workspace_id
            and payload.get("config_version") == config_version
            and payload.get("container_id") == container_name(workspace_id)
            and payload.get("ready") is True)

    # -- Unix socket 服务 ----------------------------------------------

    async def serve(self) -> None:
        if not hasattr(socket, "AF_UNIX"):
            raise RuntimeError("provisioner 需要 Unix socket（Linux）")
        socket_path = Path(self.settings.socket_path)
        socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        if socket_path.exists():
            if not stat.S_ISSOCK(socket_path.stat().st_mode):
                raise RuntimeError("provisioner socket 路径已被非 socket 占用")
            socket_path.unlink()
        server = await asyncio.start_unix_server(
            self._handle_client, path=str(socket_path))
        os.chmod(socket_path, 0o660)
        LOGGER.info("provisioner 已监听本地 Unix socket")
        async with server:
            await server.serve_forever()

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        allowed = set(self.settings.allowed_uids)
        if not allowed and hasattr(os, "getuid"):
            allowed = {os.getuid()}
        try:
            uid = _peer_uid(writer)
            if not peer_allowed(uid, allowed):
                await self._respond(writer, {"ok": False,
                                             "error": "unauthorized"})
                return
            try:
                line = await asyncio.wait_for(
                    reader.readline(), timeout=REQUEST_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                await self._respond(writer, {"ok": False,
                                             "error": "request_timeout"})
                return
            if len(line) > MAX_REQUEST_BYTES:
                await self._respond(writer, {"ok": False,
                                             "error": "request_too_large"})
                return
            try:
                request = parse_request(line)
                response = await self._dispatch(request)
            except ProvisionerError as error:
                response = {"ok": False, "error": error.code}
            except Exception:
                # 内部失败不回显路径、环境或异常文本
                LOGGER.exception("provisioner 请求失败")
                response = {"ok": False, "error": "internal_error"}
            await self._respond(writer, response)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _dispatch(self, request: WorkerRequest) -> dict:
        if request.action == "ensure":
            handle = await self.ensure(request.workspace_id,
                                       request.config_version)
            return {"ok": True, "handle": {
                "workspace_id": handle.workspace_id,
                "container_id": handle.container_id,
                "upstream": handle.upstream,
                "service_token": handle.service_token,
                "config_version": handle.config_version,
            }}
        if request.action == "stop":
            await self.stop(request.workspace_id, request.instance_id)
            return {"ok": True}
        report = await self.inspect(request.workspace_id)
        return {"ok": True, "report": report}

    @staticmethod
    async def _respond(writer: asyncio.StreamWriter, payload: dict) -> None:
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        writer.write(data)
        try:
            await writer.drain()
        except OSError:
            pass


def _peer_uid(writer: asyncio.StreamWriter) -> int | None:
    """读取 Linux SO_PEERCRED；不可用时返回 None（拒绝，而不是放行）。"""
    raw_socket = writer.get_extra_info("socket")
    option = getattr(socket, "SO_PEERCRED", None)
    if raw_socket is None or option is None:
        return None
    try:
        credentials = raw_socket.getsockopt(
            socket.SOL_SOCKET, option, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", credentials)
        return int(uid)
    except OSError:
        return None


def _http_get_json(host: str, port: int, path: str,
                   headers: dict[str, str], timeout: float) -> dict:
    import http.client

    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = response.read(64 * 1024)
        if response.status != 200:
            return {}
        payload = json.loads(body.decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    finally:
        connection.close()


def _allowed_uids(values: list[str]) -> frozenset[int]:
    parsed: set[int] = set()
    for value in values:
        try:
            uid = int(value)
        except ValueError:
            raise SystemExit("--allowed-uid 必须是整数") from None
        if uid < 0:
            raise SystemExit("--allowed-uid 不能为负")
        parsed.add(uid)
    return frozenset(parsed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="easel-provisioner")
    parser.add_argument("--socket", required=True,
                        help="本地 Unix socket 路径")
    parser.add_argument("--workspace-root", required=True,
                        help="所有 workspace 持久目录的固定根")
    parser.add_argument("--image", required=True,
                        help="固定摘要的 worker 镜像（sha256:<64hex>）")
    parser.add_argument("--allowed-uid", action="append", default=[],
                        help="允许调用 socket 的 UID，可重复")
    parser.add_argument("--quota-mode", choices=("project", "none"),
                        default="project")
    parser.add_argument("--quota-bytes", type=int, default=DEFAULT_QUOTA_BYTES)
    parser.add_argument("--allow-unquotaed", action="store_true",
                        help="仅测试用：显式关闭配额（会记录警告）")
    parser.add_argument("--docker", default="docker")
    args = parser.parse_args(argv)

    if _PINNED_IMAGE.fullmatch(args.image) is None:
        print("错误：invalid_image（必须使用 sha256 摘要）", file=sys.stderr)
        return 2
    if args.quota_mode == "none" and not args.allow_unquotaed:
        print("错误：quota_disabled（requires --allow-unquotaed）",
              file=sys.stderr)
        return 2
    if args.allow_unquotaed:
        LOGGER.warning("配额已显式关闭：不得用于真实多人部署")

    settings = ProvisionerSettings(
        image=args.image,
        workspace_root=Path(args.workspace_root),
        socket_path=Path(args.socket),
        quota_mode=args.quota_mode,
        quota_bytes=args.quota_bytes,
        allow_unquotaed=args.allow_unquotaed,
        allowed_uids=_allowed_uids(args.allowed_uid),
        docker=args.docker,
    )
    provisioner = Provisioner(settings)
    try:
        asyncio.run(provisioner.serve())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
