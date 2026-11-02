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
import hmac
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
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from easel.control.types import WorkerHandle
from easel.gateway.runtime import (
    GATEWAY_PORT,
    GatewayRuntime,
    GatewayRuntimeError,
    GatewaySettings,
)

LOGGER = logging.getLogger("easel.provisioner")

PROTOCOL_VERSION = 2
LEGACY_PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 8 * 1024
REQUEST_TIMEOUT_SECONDS = 10.0
HEALTH_TIMEOUT_SECONDS = 60.0
HEALTH_POLL_SECONDS = 0.5
HEALTH_REQUEST_TIMEOUT = 3.0

WORKER_PORT = 7860
WORKER_PUBLIC_KEY_PATH = "/keys/worker-public.key"
WORKER_HEALTHCHECK_COMMAND = (
    "python", "-m", "easel.worker.server", "healthcheck",
    "--port", str(WORKER_PORT),
)
WORKER_USER = "10001:10001"
WORKER_UID = 10001
WORKER_GID = 10001
WORKER_PIDS_LIMIT = 256
WORKER_MEMORY_BYTES = 4 * 1024 ** 3
WORKER_NANO_CPUS = 2_000_000_000
WORKER_TMPFS = "/tmp:rw,noexec,nosuid,nodev,size=1g"
SIDECAR_BROKER_PORT = 8899
SIDECAR_EGRESS_PORT = 8898
SIDECAR_PIDS_LIMIT = 128
SIDECAR_MEMORY_BYTES = 512 * 1024 ** 2
SIDECAR_NANO_CPUS = 1_000_000_000
SIDECAR_TMPFS = "/tmp:rw,noexec,nosuid,nodev,size=64m"
SIDECAR_POLICY_CONTAINER_PATH = "/etc/easel/egress-policy.json"
WORKER_COMMAND = (
    "python", "-m", "easel.worker.server",
    "serve", "--host", "0.0.0.0", "--port", str(WORKER_PORT),
)
DEFAULT_QUOTA_BYTES = 10 * 1024 ** 3

ACTIONS = ("ensure", "stop", "inspect", "activate", "abort",
           "restore_gateway", "commit_attempt", "recover_abort", "identity")
# 进程启动时加载源码的摘要；身份核验用它区分“同一配置但仍在跑旧代码”。
# 必须在 import 时读取，运行中磁盘源码被替换不能改变已加载进程的身份。
SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

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
    version: int = LEGACY_PROTOCOL_VERSION
    gateway_instance_id: str = ""
    worker_container_id: str = ""
    boot_generation: str = ""
    worker_created: bool = False
    gateway_created: bool = False
    attempt_id: str = ""
    cleanup_scope: str = ""
    orphan_takeover: bool = False
    resolve: bool = False


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
    version = data.get("version")
    if version not in (LEGACY_PROTOCOL_VERSION, PROTOCOL_VERSION):
        raise _reject("unsupported_protocol")
    action = data.get("action")
    if action not in ACTIONS:
        raise _reject("invalid_action")
    if version == LEGACY_PROTOCOL_VERSION and action in (
            "activate", "abort", "restore_gateway", "commit_attempt",
            "recover_abort", "identity"):
        raise _reject("unsupported_protocol")
    if action == "identity":
        if set(data) != {"version", "action"}:
            raise _reject("unknown_field")
        return WorkerRequest(action, "")
    allowed = {"version", "action", "workspace_id"}
    if action in ("ensure", "activate", "restore_gateway",
                  "recover_abort"):
        allowed.add("config_version")
    if action == "ensure" and version == PROTOCOL_VERSION:
        allowed.update({"attempt_id", "orphan_takeover"})
    if action == "restore_gateway":
        allowed.update({"worker_container_id", "attempt_id"})
    if action == "stop":
        allowed.add("instance_id")
        if version == PROTOCOL_VERSION:
            allowed.add("gateway_instance_id")
    if action == "activate":
        allowed.update({"gateway_instance_id", "worker_container_id",
                        "boot_generation"})
    if action in ("abort", "recover_abort"):
        allowed.update({"gateway_instance_id", "worker_container_id",
                        "worker_created", "gateway_created", "attempt_id",
                        "scope"})
    if action == "commit_attempt":
        allowed.update({"gateway_instance_id", "worker_container_id",
                        "attempt_id", "resolve", "config_version"})
    unknown = {key for key in data if key not in allowed}
    if unknown:
        # 明确拒绝所有镜像/命令/挂载/网络/环境注入尝试
        raise _reject("unknown_field")

    workspace_id = data.get("workspace_id")
    if (not isinstance(workspace_id, str)
            or _WORKSPACE_ID.fullmatch(workspace_id) is None):
        raise _reject("invalid_workspace")

    config_version = data.get("config_version", 0)
    if action in ("ensure", "activate", "restore_gateway",
                  "recover_abort") and "config_version" not in data:
        raise _reject("invalid_config_version")
    if not _valid_config_version(config_version):
        raise _reject("invalid_config_version")

    instance_id = ""
    gateway_instance_id = ""
    worker_container_id = ""
    boot_generation = ""
    worker_created = False
    gateway_created = False
    attempt_id = ""
    cleanup_scope = ""
    orphan_takeover = False
    resolve = False
    if action == "stop":
        instance_id = data.get("instance_id")
        if (not isinstance(instance_id, str)
                or _CONTAINER_INSTANCE.fullmatch(instance_id) is None):
            raise _reject("invalid_instance")
        if version == PROTOCOL_VERSION:
            gateway_instance_id = data.get("gateway_instance_id")
            # gateway-only 恢复完成后 control 已没有 gateway ID；允许
            # 显式空串，由 stop 在确无同名 gateway 时只停 worker。
            if (not isinstance(gateway_instance_id, str)
                    or (gateway_instance_id != ""
                        and _CONTAINER_INSTANCE.fullmatch(
                            gateway_instance_id) is None)):
                raise _reject("invalid_gateway_instance")
    elif action == "activate":
        gateway_instance_id = data.get("gateway_instance_id")
        worker_container_id = data.get("worker_container_id")
        boot_generation = data.get("boot_generation")
        if (not isinstance(gateway_instance_id, str)
                or _CONTAINER_INSTANCE.fullmatch(
                    gateway_instance_id) is None):
            raise _reject("invalid_gateway_instance")
        if (not isinstance(worker_container_id, str)
                or _CONTAINER_INSTANCE.fullmatch(worker_container_id) is None):
            raise _reject("invalid_instance")
        if (not isinstance(boot_generation, str)
                or not re.fullmatch(r"[0-9a-f]{32}", boot_generation)):
            raise _reject("invalid_boot_generation")
    elif action in ("abort", "recover_abort"):
        gateway_instance_id = data.get("gateway_instance_id")
        worker_container_id = data.get("worker_container_id")
        worker_created = data.get("worker_created")
        gateway_created = data.get("gateway_created")
        if (not isinstance(gateway_instance_id, str)
                or _CONTAINER_INSTANCE.fullmatch(
                    gateway_instance_id) is None):
            raise _reject("invalid_gateway_instance")
        if (not isinstance(worker_container_id, str)
                or _CONTAINER_INSTANCE.fullmatch(worker_container_id) is None):
            raise _reject("invalid_instance")
        if type(worker_created) is not bool or type(gateway_created) is not bool:
            raise _reject("invalid_cleanup_flag")
        attempt_id = data.get("attempt_id")
        if (not isinstance(attempt_id, str)
                or re.fullmatch(r"[0-9a-f]{32}", attempt_id) is None):
            raise _reject("invalid_attempt_id")
        cleanup_scope = data.get("scope")
        if cleanup_scope not in ("gateway", "worker", "worker_gateway"):
            raise _reject("invalid_cleanup_scope")
        if (worker_created != (cleanup_scope in (
                "worker", "worker_gateway"))
                or gateway_created != (cleanup_scope in (
                    "gateway", "worker_gateway"))):
            raise _reject("invalid_cleanup_scope")
    elif action == "commit_attempt":
        resolve = data.get("resolve", False)
        if type(resolve) is not bool:
            raise _reject("invalid_resolve")
        gateway_instance_id = data.get("gateway_instance_id")
        worker_container_id = data.get("worker_container_id")
        # resolve 只按精确 attempt 读取已持久候选，允许不携带 gateway ID；
        # 普通提交仍必须给出精确目标，不能按目标猜测归属。
        if (not isinstance(gateway_instance_id, str)
                or (not resolve and _CONTAINER_INSTANCE.fullmatch(
                    gateway_instance_id) is None)
                or (resolve and gateway_instance_id
                    and _CONTAINER_INSTANCE.fullmatch(
                        gateway_instance_id) is None)):
            raise _reject("invalid_gateway_instance")
        if (not isinstance(worker_container_id, str)
                or _CONTAINER_INSTANCE.fullmatch(worker_container_id) is None):
            raise _reject("invalid_instance")
        attempt_id = data.get("attempt_id", "")
        if resolve:
            if (not isinstance(attempt_id, str)
                    or re.fullmatch(r"[0-9a-f]{32}", attempt_id) is None):
                raise _reject("invalid_attempt_id")
        elif attempt_id and (not isinstance(attempt_id, str)
                             or re.fullmatch(r"[0-9a-f]{32}", attempt_id)
                             is None):
            raise _reject("invalid_attempt_id")
    elif action == "ensure" and version == PROTOCOL_VERSION:
        attempt_id = data.get("attempt_id", "")
        if attempt_id and (not isinstance(attempt_id, str)
                           or re.fullmatch(r"[0-9a-f]{32}", attempt_id)
                           is None):
            raise _reject("invalid_attempt_id")
        orphan_takeover = data.get("orphan_takeover", False)
        if type(orphan_takeover) is not bool:
            raise _reject("invalid_orphan_takeover")
    elif action == "restore_gateway":
        worker_container_id = data.get("worker_container_id")
        if (not isinstance(worker_container_id, str)
                or _CONTAINER_INSTANCE.fullmatch(worker_container_id) is None):
            raise _reject("invalid_instance")
        attempt_id = data.get("attempt_id", "")
        if (not isinstance(attempt_id, str)
                or re.fullmatch(r"[0-9a-f]{32}", attempt_id) is None):
            raise _reject("invalid_attempt_id")
    return WorkerRequest(
        action, workspace_id, config_version, instance_id, version,
        gateway_instance_id, worker_container_id, boot_generation,
        worker_created, gateway_created, attempt_id, cleanup_scope,
        orphan_takeover, resolve)


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


def sidecar_container_name(workspace_id: str) -> str:
    if _WORKSPACE_ID.fullmatch(workspace_id) is None:
        raise _reject("invalid_workspace")
    return f"easel-sidecar-{workspace_id}"


def sidecar_labels(workspace_id: str) -> dict[str, str]:
    return {
        "easel.managed": "true",
        "easel.workspace-id": workspace_id,
        "easel.role": "sidecar",
    }


def sidecar_command(broker_upstream: str) -> tuple[str, ...]:
    return (
        "python", "deploy/multiuser/worker_sidecar.py",
        "--listen", "0.0.0.0",
        "--broker-port", str(SIDECAR_BROKER_PORT),
        "--egress-port", str(SIDECAR_EGRESS_PORT),
        "--broker-upstream", broker_upstream,
        "--policy", SIDECAR_POLICY_CONTAINER_PATH,
    )


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


def validate_worker_public_key(path: object) -> Path:
    """校验管理员固定的只读公钥文件；只接受合法 Ed25519 公钥。"""
    if path is None:
        raise _reject("worker_public_key_unavailable")
    candidate = Path(path)
    if candidate.is_symlink():
        raise _reject("worker_public_key_invalid")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        raise _reject("worker_public_key_unavailable") from None
    if not resolved.is_file():
        raise _reject("worker_public_key_unavailable")
    _safe_path_text(resolved)
    try:
        data = resolved.read_bytes()
    except OSError:
        raise _reject("worker_public_key_unavailable") from None
    if not data or len(data) > 32 * 1024:
        raise _reject("worker_public_key_invalid")
    try:
        if len(data) == 32:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )
            Ed25519PublicKey.from_public_bytes(data)
        else:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )
            key = serialization.load_pem_public_key(data)
            if not isinstance(key, Ed25519PublicKey):
                raise ValueError
    except (ImportError, ValueError, TypeError):
        raise _reject("worker_public_key_invalid") from None
    return resolved


def build_run_argv(*, image: str, workspace_id: str, config_version: int,
                   paths: RuntimePaths, env_file: Path,
                   public_key_file: Path,
                   cidfile: Path | None = None) -> list[str]:
    """从批准配置构造唯一允许的 docker run argv。

    容器代码位于只读镜像 ``/opt/easel``；持久 bind 只有 ``/workspace`` 与
    ``/home/easel``，公钥只读 bind 到固定路径；网络是每空间内部网络
    （``--internal``），没有宿主端口和默认外网路由。
    """
    if not isinstance(image, str) or _PINNED_IMAGE.fullmatch(image) is None:
        raise _reject("invalid_image")
    if not _valid_config_version(config_version):
        raise _reject("invalid_config_version")
    public_key = validate_worker_public_key(public_key_file)
    name = container_name(workspace_id)
    labels = workload_labels(workspace_id, config_version)
    argv = [
        "run", "-d",
        "--name", name,
    ]
    if cidfile is not None:
        # 只信任 Docker 写出的本轮实例 ID；不能靠同名容器反推归属。
        argv.extend(["--cidfile", _safe_path_text(cidfile)])
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
        "--mount",
        (f"type=bind,src={_safe_path_text(public_key)},"
         f"dst={WORKER_PUBLIC_KEY_PATH},readonly"),
        "--env", f"EASEL_WORKER_PUBLIC_KEY_FILE={WORKER_PUBLIC_KEY_PATH}",
        "--env-file", _safe_path_text(env_file),
        "--restart=no",
        "--stop-timeout=30",
        "--log-driver=local",
        "--log-opt=max-size=10m",
        "--log-opt=max-file=3",
        image,
        *WORKER_COMMAND,
    ])
    return argv


_HOST_PORT = re.compile(r"^[A-Za-z0-9._-]{1,253}:[0-9]{1,5}$")


def validate_sidecar_settings(settings: ProvisionerSettings
                              ) -> tuple[str, Path, str] | None:
    """sidecar 配置必须三件套齐全；其余情况按未接线零出口处理。"""
    network = (settings.sidecar_network or "").strip()
    policy = settings.sidecar_policy
    upstream = (settings.broker_upstream or "").strip()
    if not any((network, policy, upstream)):
        return None
    if not (network and policy is not None and upstream):
        raise _reject("sidecar_config_incomplete")
    if _SAFE_IDENTIFIER.fullmatch(network) is None:
        raise _reject("sidecar_network_invalid")
    if _HOST_PORT.fullmatch(upstream) is None:
        raise _reject("broker_upstream_invalid")
    try:
        port = int(upstream.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        raise _reject("broker_upstream_invalid") from None
    if not 1 <= port <= 65535:
        raise _reject("broker_upstream_invalid")
    try:
        resolved_policy = Path(policy).resolve(strict=True)
    except OSError:
        raise _reject("sidecar_policy_unavailable") from None
    if not resolved_policy.is_file():
        raise _reject("sidecar_policy_unavailable")
    return network, resolved_policy, upstream


def build_sidecar_run_argv(*, image: str, workspace_id: str,
                           policy_file: Path,
                           broker_upstream: str) -> list[str]:
    """受控 sidecar 的唯一 argv；worker 网络 + 受控出站网络由后续 connect 加入。"""
    if not isinstance(image, str) or _PINNED_IMAGE.fullmatch(image) is None:
        raise _reject("invalid_image")
    name = sidecar_container_name(workspace_id)
    argv = ["run", "-d", "--name", name]
    for key, value in sidecar_labels(workspace_id).items():
        argv.extend(["--label", f"{key}={value}"])
    argv.extend([
        "--user", WORKER_USER,
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--cpus=1",
        "--memory=512m",
        "--memory-swap=512m",
        "--pids-limit=128",
        "--tmpfs", SIDECAR_TMPFS,
        "--network", network_name(workspace_id),
        "--mount",
        ("type=bind,src=" + _safe_path_text(policy_file)
         + ",dst=" + SIDECAR_POLICY_CONTAINER_PATH + ",readonly"),
        "--restart=no",
        "--stop-timeout=15",
        "--log-driver=local",
        "--log-opt=max-size=10m",
        "--log-opt=max-file=2",
        "--health-cmd",
        ("python -c \"import urllib.request;"
         f"urllib.request.urlopen('http://127.0.0.1:{SIDECAR_BROKER_PORT}"
         "/healthz',timeout=2)\""),
        "--health-interval=2s",
        "--health-timeout=2s",
        "--health-retries=15",
        "--health-start-period=1s",
        image,
        *sidecar_command(broker_upstream),
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
        "worker_running": False,
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
    running = (state.get("Running") is True
               and state.get("Status") == "running")
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
        "worker_running": running,
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


def worker_wiring_env(workspace_id: str,
                      sidecar_host: str | None) -> dict[str, str]:
    """本次批准接线实际应写入容器 Env 的代理/broker 字段。"""
    if sidecar_host is None:
        return {}
    if sidecar_host != sidecar_container_name(workspace_id):
        raise _reject("sidecar_host_invalid")
    broker_url = f"http://{sidecar_host}:{SIDECAR_BROKER_PORT}"
    egress_url = f"http://{sidecar_host}:{SIDECAR_EGRESS_PORT}"
    return {
        "EASEL_BROKER_URL": broker_url,
        "EASEL_EGRESS_PROXY": egress_url,
        "HTTP_PROXY": egress_url,
        "HTTPS_PROXY": egress_url,
        "NO_PROXY": f"127.0.0.1,localhost,{sidecar_host}",
    }


def _worker_env_map(raw: dict) -> dict[str, str] | None:
    """解析实际 Docker Config.Env；重复键/非字符串失败关闭。"""
    config = raw.get("Config")
    if not isinstance(config, dict):
        return None
    env = config.get("Env")
    if env is None:
        env = []
    if not isinstance(env, list):
        return None
    parsed: dict[str, str] = {}
    for item in env:
        if not isinstance(item, str) or "=" not in item:
            return None
        name, value = item.split("=", 1)
        if not name:
            return None
        key = name.lower()
        if key in parsed:
            return None
        parsed[key] = value
    return parsed


def verify_worker_wiring(raw: dict | None, *, workspace_id: str,
                         sidecar_host: str | None) -> bool:
    """核对运行中 worker 的实际代理/broker 环境，而非宿主 env 文件。

    Docker 的 ``--env-file`` 只在创建时读取；更新宿主文件不会改变已存在
    容器的 ``Config.Env``。复用/接管前必须按实际 Env 验收，否则零出口旧
    实例会被误报为已接线。
    """
    if not isinstance(raw, dict):
        return False
    env = _worker_env_map(raw)
    if env is None:
        return False
    if not _valid_service_token(env.get("easel_service_token")):
        return False
    try:
        expected = {name.lower(): value
                    for name, value in worker_wiring_env(
                        workspace_id, sidecar_host).items()}
    except ProvisionerError:
        return False
    if sidecar_host is not None:
        # ALL_PROXY 会覆盖 HTTP(S)_PROXY；批准接线没有它。
        if "all_proxy" in env:
            return False
        return all(env.get(key) == value
                   for key, value in expected.items())
    # 零出口模式：不能继承旧 sidecar 入口；允许 Docker daemon 注入的
    # 普通代理变量，但任何仍指向本产品 sidecar 的代理都必须视为旧接线。
    if ("easel_broker_url" in env or "easel_egress_proxy" in env):
        return False
    return not any(
        "easel-sidecar-" in env.get(key, "")
        for key in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"))


def _verify_worker_static(raw: dict | None, *, workspace_id: str,
                          config_version: int, image: str,
                          paths: RuntimePaths,
                          public_key_file: Path | None = None) -> bool:
    """核对除运行接线外的静态安全配置。"""
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
    env = _worker_env_map(raw)
    if (env is None
            or env.get("easel_worker_public_key_file")
            != WORKER_PUBLIC_KEY_PATH):
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
    if not isinstance(mounts, list) or len(mounts) != 3:
        return False
    expected_mounts = {
        "/workspace": str(paths.workspace),
        "/home/easel": str(paths.home),
    }
    expected_key_source = (
        str(public_key_file) if public_key_file is not None else None)
    seen: dict[str, str] = {}
    key_seen = False
    for mount in mounts:
        if not isinstance(mount, dict):
            return False
        destination = mount.get("Destination")
        source = mount.get("Source")
        if destination == WORKER_PUBLIC_KEY_PATH:
            if (mount.get("Type") != "bind" or mount.get("RW") is not False
                    or not isinstance(source, str) or not source
                    or "docker.sock" in source
                    or (expected_key_source is not None
                        and source != expected_key_source)):
                return False
            key_seen = True
            continue
        if (mount.get("Type") != "bind" or mount.get("RW") is not True
                or destination not in expected_mounts
                or source != expected_mounts[destination]
                or "docker.sock" in str(source)):
            return False
        seen[destination] = str(source)
    if set(seen) != set(expected_mounts) or not key_seen:
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


def worker_confirmed_exited(raw: dict | None) -> bool:
    """只有 Docker 明确报告 exited/dead 才允许直接删除重建。

    Running、restarting、paused、状态缺失或结构异常都不能作为迁移授权；
    尤其不能仅凭一次 lifecycle 探针失败删除仍可能执行任务的实例。
    """
    if not isinstance(raw, dict):
        return False
    state = raw.get("State")
    return (isinstance(state, dict)
            and state.get("Running") is False
            and state.get("Status") in ("exited", "dead"))


def verify_worker_inspect(raw: dict | None, *, workspace_id: str,
                          config_version: int, image: str,
                          paths: RuntimePaths,
                          sidecar_host: str | None = None,
                          public_key_file: Path | None = None) -> bool:
    """严格核对实际容器配置；复用、接管、inspect 与健康循环共用同一门槛。

    标签只表示归属，不能证明镜像、权限、资源、挂载、网络或运行接线仍是
    批准配置。任何字段缺失、类型异常或与批准值不一致都返回 False。
    """
    if not _verify_worker_static(
            raw, workspace_id=workspace_id,
            config_version=config_version, image=image, paths=paths,
            public_key_file=public_key_file):
        return False
    return verify_worker_wiring(
        raw, workspace_id=workspace_id, sidecar_host=sidecar_host)


def verify_sidecar_inspect(raw: dict | None, *, workspace_id: str,
                           image: str, policy_file: Path,
                           sidecar_network: str,
                           broker_upstream: str) -> bool:
    """严格验收受控 sidecar：固定镜像、权限、资源、挂载与两条批准网络。"""
    if not isinstance(raw, dict):
        return False
    instance_id = raw.get("Id")
    if (not isinstance(instance_id, str)
            or _CONTAINER_INSTANCE.fullmatch(instance_id) is None):
        return False
    if str(raw.get("Name") or "").lstrip("/") != sidecar_container_name(
            workspace_id):
        return False
    config = raw.get("Config")
    host = raw.get("HostConfig")
    if not isinstance(config, dict) or not isinstance(host, dict):
        return False
    labels = config.get("Labels")
    expected_labels = sidecar_labels(workspace_id)
    if (not isinstance(labels, dict)
            or any(labels.get(key) != value
                   for key, value in expected_labels.items())):
        # Docker Desktop 会附加平台标签（如 wsl-distro）；只要求本项目
        # 三个归属标签存在且精确匹配，不能因额外平台标签拒绝。
        return False
    if config.get("Image") != image or config.get("User") != WORKER_USER:
        return False
    if config.get("Entrypoint") not in (None, []):
        return False
    if list(config.get("Cmd") or []) != list(
            sidecar_command(broker_upstream)):
        return False
    healthcheck = config.get("Healthcheck") or {}
    health_test = healthcheck.get("Test") or []
    if (not isinstance(health_test, list)
            or not any("healthz" in str(part) for part in health_test)):
        return False
    if host.get("ReadonlyRootfs") is not True:
        return False
    if host.get("Privileged") is not False:
        return False
    if set(host.get("CapDrop") or []) != {"ALL"}:
        return False
    if host.get("CapAdd") not in (None, []):
        return False
    options = host.get("SecurityOpt") or []
    if not _no_new_privileges_enabled(options):
        return False
    if any("unconfined" in str(option) for option in options):
        return False
    if host.get("PidsLimit") != SIDECAR_PIDS_LIMIT:
        return False
    if host.get("Memory") != SIDECAR_MEMORY_BYTES:
        return False
    if host.get("MemorySwap") != SIDECAR_MEMORY_BYTES:
        return False
    if host.get("NanoCpus") != SIDECAR_NANO_CPUS:
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
    required_tmpfs = {"rw", "noexec", "nosuid", "nodev", "size=64m"}
    if not required_tmpfs.issubset(
            set(str(tmpfs.get("/tmp") or "").split(","))):
        return False
    mounts = raw.get("Mounts")
    if not isinstance(mounts, list) or len(mounts) != 1:
        return False
    mount = mounts[0]
    if not isinstance(mount, dict):
        return False
    if (mount.get("Type") != "bind"
            or mount.get("Source") != str(policy_file)
            or mount.get("Destination") != SIDECAR_POLICY_CONTAINER_PATH
            or mount.get("RW") is not False):
        return False
    network_settings = raw.get("NetworkSettings")
    if not isinstance(network_settings, dict):
        return False
    networks = network_settings.get("Networks")
    expected = {network_name(workspace_id), sidecar_network}
    if not isinstance(networks, dict) or set(networks) != expected:
        return False
    if any(not isinstance(networks[name], dict)
           or not networks[name].get("IPAddress") for name in expected):
        return False
    if any(bool(bindings)
           for bindings in (network_settings.get("Ports") or {}).values()):
        return False
    state = raw.get("State")
    if not isinstance(state, dict) or state.get("Running") is not True:
        return False
    health = (state.get("Health") or {}).get("Status")
    return health == "healthy"


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

def _valid_service_token(value: object) -> bool:
    """token 必须是无首尾空白的 ASCII 单行值；大小写敏感。"""
    return (isinstance(value, str)
            and bool(value)
            and value.isascii()
            and value == value.strip()
            and len(value) <= 512
            and not any(character in value
                        for character in ("\n", "\r", "\0")))


def _token_path(workspace_root: Path,
                workspace_id: str) -> tuple[Path, Path]:
    directory = credentials_dir(workspace_root)
    if directory.is_symlink():
        raise _reject("credentials_escape")
    path = directory / f"{workspace_id}.token"
    if path.is_symlink():
        raise _reject("credentials_escape")
    return directory, path


def _read_token(workspace_root: Path, workspace_id: str) -> str:
    """只读既有宿主凭据；不存在时明确缺失，绝不在这里生成新 token。"""
    _directory, path = _token_path(workspace_root, workspace_id)
    try:
        raw = path.read_text(encoding="ascii")
    except FileNotFoundError:
        raise _reject("worker_credentials_missing") from None
    except (OSError, UnicodeError):
        raise _reject("credentials_invalid") from None
    token = raw.strip()
    if not _valid_service_token(token):
        raise _reject("credentials_invalid")
    return token


def _load_or_create_token(workspace_root: Path, workspace_id: str) -> str:
    """仅给本轮新建实例生成凭据；既有实例走 :func:`_read_token`。"""
    try:
        return _read_token(workspace_root, workspace_id)
    except ProvisionerError as error:
        if error.code != "worker_credentials_missing":
            raise
    directory, path = _token_path(workspace_root, workspace_id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = secrets.token_urlsafe(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, token.encode("ascii"))
    finally:
        os.close(descriptor)
    return token


def _new_cidfile(workspace_root: Path, workspace_id: str) -> Path:
    """在私有凭证目录生成本轮 cidfile 路径；Docker 负责写入实例 ID。"""
    directory = credentials_dir(workspace_root)
    if directory.is_symlink():
        raise _reject("credentials_escape")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory / f".{workspace_id}.cid.{secrets.token_hex(8)}"


def _read_cidfile(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        content = path.read_text(encoding="ascii").strip()
    except OSError:
        return None
    return content if _CONTAINER_INSTANCE.fullmatch(content) else None


def _worker_env_token(raw: dict | None) -> str | None:
    """从已核验 inspect 的实际 Env 取 token；重复键/非法值返回 None。"""
    env = _worker_env_map(raw)
    if env is None:
        return None
    token = env.get("easel_service_token")
    return token if _valid_service_token(token) else None


def _actual_token_matches(raw: dict | None, expected: str) -> bool:
    """比较待返回 token 与容器实际 Env；只做常量时间比较，不回显值。"""
    if not _valid_service_token(expected):
        return False
    actual = _worker_env_token(raw)
    return actual is not None and hmac.compare_digest(actual, expected)


def write_env_file(workspace_root: Path, workspace_id: str,
                   config_version: int, token: str,
                   paths: RuntimePaths, *,
                   sidecar_host: str | None = None) -> Path:
    """原子写入 0600 env 文件；argv 与日志不含 token。"""
    if not _valid_service_token(token):
        raise _reject("credentials_invalid")
    directory = credentials_dir(workspace_root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / f"{workspace_id}.env"
    if target.is_symlink():
        raise _reject("credentials_escape")
    lines = [
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
    ]
    if sidecar_host is not None:
        broker_url = f"http://{sidecar_host}:{SIDECAR_BROKER_PORT}"
        egress_url = f"http://{sidecar_host}:{SIDECAR_EGRESS_PORT}"
        lines.extend((
            f"EASEL_BROKER_URL={broker_url}",
            f"EASEL_EGRESS_PROXY={egress_url}",
            f"HTTPS_PROXY={egress_url}",
            f"HTTP_PROXY={egress_url}",
            f"NO_PROXY=127.0.0.1,localhost,{sidecar_host}",
        ))
    content = "\n".join(lines) + "\n"
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
    sidecar_network: str = ""
    sidecar_policy: Path | None = None
    broker_upstream: str = ""
    worker_public_key: Path | None = None
    gateway_image: str = ""
    gateway_control_network: str = ""
    gateway_public_key: Path | None = None
    gateway_port: int = GATEWAY_PORT
    gateway_start_timeout: float = 30.0


class Provisioner:
    """实现 ensure/stop/inspect；所有 Docker 调用都经过白名单 argv。"""

    def __init__(self, settings: ProvisionerSettings,
                 run: Callable[..., subprocess.CompletedProcess] | None = None):
        self.settings = settings
        self._runner = run or self._subprocess_run
        self._locks: dict[str, asyncio.Lock] = {}
        self._sidecar = validate_sidecar_settings(settings)
        self._worker_public_key = (
            validate_worker_public_key(settings.worker_public_key)
            if settings.worker_public_key is not None else None)
        configured = (
            bool(settings.gateway_image),
            bool(settings.gateway_control_network),
            settings.gateway_public_key is not None,
        )
        if any(configured) and not all(configured):
            raise _reject("gateway_config_incomplete")
        self._gateway = None
        self._attempts: dict[str, dict] = {}
        if all(configured):
            try:
                gateway_settings = GatewaySettings(
                    image=settings.gateway_image,
                    control_network=settings.gateway_control_network,
                    public_key=settings.gateway_public_key,
                    port=settings.gateway_port,
                    start_timeout=settings.gateway_start_timeout,
                    docker=settings.docker,
                    attempt_dir=settings.workspace_root / ".gateway")
            except GatewayRuntimeError as error:
                raise _reject(error.code) from None
            self._gateway = GatewayRuntime(
                gateway_settings, run=self._gateway_runner)

    @property
    def gateway_enabled(self) -> bool:
        return self._gateway is not None

    async def _gateway_runner(self, argv, *, timeout=120.0,
                              input_text=None):
        """把 gateway runtime 的无前缀 argv 交给受限 Docker runner。"""
        return await self._docker(
            *argv, timeout=timeout, input_text=input_text)

    # -- Docker 调用 ---------------------------------------------------

    def _subprocess_run(self, argv: list[str], *,
                        timeout: float = 120.0, input: str | None = None
                        ) -> subprocess.CompletedProcess:
        return subprocess.run(argv, input=input, capture_output=True,
                              text=True, check=False, timeout=timeout)

    async def _docker(self, *args: str, timeout: float = 120.0,
                      input_text: str | None = None
                      ) -> subprocess.CompletedProcess:
        argv = [self.settings.docker, *args]
        if input_text is not None:
            # 可信配置/激活只经固定命令的 stdin；旧 runner 不接收该参数。
            return await asyncio.to_thread(
                self._runner, argv, timeout=timeout, input=input_text)
        return await asyncio.to_thread(self._runner, argv, timeout=timeout)

    @staticmethod
    def _not_found(result: subprocess.CompletedProcess, *, kind: str,
                   target: str) -> bool:
        """只把明确指向本次目标对象的 Docker absent 响应当作不存在。

        ``context not found``、endpoint/daemon/权限/CLI 初始化错误以及任何
        未知文本都必须失败关闭；不能再用任意 ``not found`` 子串判断。
        """
        if result.returncode == 0:
            return False
        stdout = str(getattr(result, "stdout", "") or "").strip()
        if stdout not in ("", "[]"):
            return False
        text = str(getattr(result, "stderr", "") or "").strip()
        escaped = re.escape(target)
        if kind == "container":
            pattern = (
                rf"(?:Error(?: response from daemon)?:\s*)?"
                rf"No such (?:container|object):\s*{escaped}")
        elif kind == "network":
            pattern = (
                rf"(?:Error(?: response from daemon)?:\s*)?"
                rf"(?:No such network:\s*{escaped}"
                rf"|network\s+{escaped}\s+not found)")
        else:
            return False
        return re.fullmatch(pattern, text, re.IGNORECASE) is not None

    @staticmethod
    def _single_object(stdout: str, error_code: str) -> dict:
        """成功响应必须是单对象列表；空/非数组/非对象不得当作 absent。"""
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            raise ProvisionerError(error_code) from None
        if (not isinstance(payload, list) or len(payload) != 1
                or not isinstance(payload[0], dict)):
            raise ProvisionerError(error_code)
        return payload[0]

    async def _inspect_name(self, target: str) -> dict | None:
        result = await self._docker("container", "inspect", target)
        if result.returncode != 0:
            if self._not_found(result, kind="container", target=target):
                return None
            raise ProvisionerError("docker_inspect_failed")
        return self._single_object(result.stdout, "inspect_invalid")

    async def _inspect_raw(self, workspace_id: str) -> dict | None:
        return await self._inspect_name(container_name(workspace_id))

    async def _inspect_instance(self, instance_id: str) -> dict | None:
        result = await self._docker("container", "inspect", instance_id)
        if result.returncode != 0:
            if self._not_found(result, kind="container",
                               target=instance_id):
                return None
            raise ProvisionerError("docker_inspect_failed")
        return self._single_object(result.stdout, "inspect_invalid")

    async def _network_payload(self, workspace_id: str) -> list | None:
        target = network_name(workspace_id)
        result = await self._docker("network", "inspect", target)
        if result.returncode != 0:
            if self._not_found(result, kind="network", target=target):
                return None
            raise ProvisionerError("network_inspect_failed")
        return [self._single_object(result.stdout, "network_invalid")]

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

    async def _remove_instance(self, instance_id: str,
                               workspace_id: str) -> None:
        """只删除已核验的不可复用实例 ID；同名新代次绝不受影响。"""
        if (not isinstance(instance_id, str)
                or _CONTAINER_INSTANCE.fullmatch(instance_id) is None):
            raise ProvisionerError("container_conflict")
        raw = await self._inspect_instance(instance_id)
        if raw is None:
            return
        report = sanitize_inspect(raw)
        if (not report["managed"]
                or report["workspace_id"] != workspace_id):
            raise ProvisionerError("container_conflict")
        removed = await self._docker("rm", "-f", instance_id, timeout=60.0)
        if removed.returncode != 0:
            if await self._inspect_instance(instance_id) is None:
                return
            raise ProvisionerError("container_remove_failed")
        if await self._inspect_instance(instance_id) is not None:
            raise ProvisionerError("container_remove_failed")

    # -- 受控 sidecar ----------------------------------------------------

    async def _require_sidecar_network(self, network: str) -> None:
        result = await self._docker("network", "inspect", network)
        if result.returncode != 0:
            if self._not_found(result, kind="network", target=network):
                raise ProvisionerError("sidecar_network_missing")
            raise ProvisionerError("docker_inspect_failed")
        payload = self._single_object(result.stdout, "network_invalid")
        if (payload.get("Name") != network
                or payload.get("Driver") != "bridge"
                or payload.get("Internal") is not False
                or payload.get("EnableIPv6") is not False):
            raise ProvisionerError("sidecar_network_invalid")

    def _sidecar_verified(self, raw: dict | None, workspace_id: str) -> bool:
        if self._sidecar is None:
            return False
        network, policy, upstream = self._sidecar
        return verify_sidecar_inspect(
            raw, workspace_id=workspace_id, image=self.settings.image,
            policy_file=policy, sidecar_network=network,
            broker_upstream=upstream)

    def _sidecar_host(self, workspace_id: str) -> str | None:
        if self._sidecar is None:
            return None
        return sidecar_container_name(workspace_id)

    async def _sidecar_ready(self, workspace_id: str) -> bool:
        """只读核对 sidecar 依赖；缺失/不安全返回 False，Docker 故障抛出。"""
        if self._sidecar is None:
            return True
        network, _policy, _upstream = self._sidecar
        try:
            await self._require_sidecar_network(network)
        except ProvisionerError as error:
            if error.code in ("sidecar_network_missing",
                              "sidecar_network_invalid"):
                return False
            raise
        raw = await self._inspect_name(
            sidecar_container_name(workspace_id))
        return raw is not None and self._sidecar_verified(raw, workspace_id)

    async def _remove_sidecar_instance(self, workspace_id: str) -> None:
        name = sidecar_container_name(workspace_id)
        raw = await self._inspect_name(name)
        if raw is None:
            return
        labels = (raw.get("Config") or {}).get("Labels") or {}
        if (labels.get("easel.managed") != "true"
                or labels.get("easel.workspace-id") != workspace_id
                or labels.get("easel.role") != "sidecar"):
            raise ProvisionerError("sidecar_conflict")
        instance_id = raw.get("Id")
        if (not isinstance(instance_id, str)
                or _CONTAINER_INSTANCE.fullmatch(instance_id) is None):
            raise ProvisionerError("sidecar_conflict")
        result = await self._docker("rm", "-f", instance_id, timeout=60.0)
        if result.returncode != 0:
            if await self._inspect_instance(instance_id) is None:
                return
            raise ProvisionerError("sidecar_remove_failed")
        if await self._inspect_instance(instance_id) is not None:
            raise ProvisionerError("sidecar_remove_failed")

    async def _ensure_sidecar(self, workspace_id: str) -> None:
        """确保本空间 sidecar 存在且仅连接批准的 internal/出站网络。"""
        if self._sidecar is None:
            return
        network, policy, upstream = self._sidecar
        await self._require_sidecar_network(network)
        existing = await self._inspect_name(
            sidecar_container_name(workspace_id))
        if existing is not None:
            labels = (existing.get("Config") or {}).get("Labels") or {}
            if (labels.get("easel.managed") != "true"
                    or labels.get("easel.workspace-id") != workspace_id
                    or labels.get("easel.role") != "sidecar"):
                raise ProvisionerError("sidecar_conflict")
            if self._sidecar_verified(existing, workspace_id):
                return
            await self._remove_sidecar_instance(workspace_id)

        argv = build_sidecar_run_argv(
            image=self.settings.image, workspace_id=workspace_id,
            policy_file=policy, broker_upstream=upstream)
        try:
            result = await self._docker(*argv, timeout=60.0)
            if result.returncode != 0:
                raise ProvisionerError("sidecar_start_failed")
            attached = await self._docker(
                "network", "connect", network,
                sidecar_container_name(workspace_id), timeout=30.0)
            if attached.returncode != 0:
                raise ProvisionerError("sidecar_network_failed")
            deadline = time.monotonic() + min(
                self.settings.health_timeout, 30.0)
            while time.monotonic() < deadline:
                raw = await self._inspect_name(
                    sidecar_container_name(workspace_id))
                if raw is None:
                    raise ProvisionerError("sidecar_not_ready")
                if self._sidecar_verified(raw, workspace_id):
                    return
                await asyncio.sleep(HEALTH_POLL_SECONDS)
            raise ProvisionerError("sidecar_not_ready")
        except BaseException:
            try:
                await self._remove_sidecar_instance(workspace_id)
            except ProvisionerError:
                pass
            raise

    @staticmethod
    def _discard_cidfile(cidfile: Path) -> None:
        """清理终态确认后删除追踪材料；失败只记录，不伪装清理成功。"""
        try:
            cidfile.unlink(missing_ok=True)
        except OSError:
            LOGGER.error("worker cidfile 清理失败，保留追踪材料")

    async def _await_cleanup(self, cleanup: asyncio.Task) -> None:
        """等待本轮清理任务终结；外层取消不能把后台清理一起取消。"""
        while not cleanup.done():
            try:
                await asyncio.wait({cleanup})
            except asyncio.CancelledError:
                continue
        cleanup.result()

    async def _cleanup_start_attempt(
            self, workspace_id: str, cidfile: Path,
            run_task: asyncio.Task | None,
            instance_id: str | None = None) -> None:
        """等本轮 Docker CLI 终结后，按 cidfile ID 唯一清理本轮实例。

        ``docker run`` 的 CLI 超时或取消不代表 daemon 未创建；必须等
        to_thread 里的 CLI 真正结束后再读 cidfile。worker 未确认删除时
        不停止 sidecar，也不删除 cidfile。清理失败向调用方抛出，由上层
        返回可恢复错误。
        """
        if run_task is not None:
            try:
                await run_task
            except BaseException:  # noqa: BLE001 - CLI 失败仍可能已创建
                LOGGER.debug("worker run CLI 异常结束，仍读取 cidfile")
        if instance_id is None:
            instance_id = _read_cidfile(cidfile)
        if instance_id is None:
            # 无法得到精确目标：保留 sidecar 与追踪材料，明确失败。
            raise ProvisionerError("worker_start_cleanup_incomplete")
        await self._remove_instance(instance_id, workspace_id)
        # 目标 ID 已确认消失；同名新代次或改动过的容器存在时，sidecar
        # 仍可能被使用，不能为了清理本轮半成品而先拆它。
        current = await self._inspect_raw(workspace_id)
        if current is None:
            await self._stop_sidecar(workspace_id)
        self._discard_cidfile(cidfile)

    async def _stop_sidecar(self, workspace_id: str) -> None:
        """停止并确认删除本空间 sidecar；已不存在按幂等成功处理。"""
        if self._sidecar is None:
            return
        name = sidecar_container_name(workspace_id)
        raw = await self._inspect_name(name)
        if raw is None:
            return
        labels = (raw.get("Config") or {}).get("Labels") or {}
        if (labels.get("easel.managed") != "true"
                or labels.get("easel.workspace-id") != workspace_id
                or labels.get("easel.role") != "sidecar"):
            raise ProvisionerError("sidecar_conflict")
        instance_id = raw.get("Id")
        if (not isinstance(instance_id, str)
                or _CONTAINER_INSTANCE.fullmatch(instance_id) is None):
            raise ProvisionerError("sidecar_conflict")
        stopped = await self._docker("stop", "--time", "15", instance_id,
                                     timeout=40.0)
        if stopped.returncode != 0:
            current = await self._inspect_instance(instance_id)
            if current is None:
                return
            if (current.get("State") or {}).get("Running") is True:
                raise ProvisionerError("sidecar_stop_failed")
        removed = await self._docker("rm", "-f", instance_id, timeout=40.0)
        if removed.returncode != 0:
            if await self._inspect_instance(instance_id) is None:
                return
            raise ProvisionerError("sidecar_remove_failed")
        if await self._inspect_instance(instance_id) is not None:
            raise ProvisionerError("sidecar_remove_failed")

    # -- 对外动作 ------------------------------------------------------

    async def ensure(self, workspace_id: str, config_version: int, *,
                     protocol_version: int = LEGACY_PROTOCOL_VERSION,
                     attempt_id: str = "",
                     orphan_takeover: bool = False) -> WorkerHandle:
        if protocol_version >= PROTOCOL_VERSION and not attempt_id:
            attempt_id = uuid.uuid4().hex
        payload = {"version": protocol_version, "action": "ensure",
                   "workspace_id": workspace_id,
                   "config_version": config_version}
        if protocol_version >= PROTOCOL_VERSION:
            payload["attempt_id"] = attempt_id
        if orphan_takeover:
            if protocol_version < PROTOCOL_VERSION:
                raise ProvisionerError("unsupported_protocol")
            payload["orphan_takeover"] = True
        parse_request(payload)
        lock = self._locks.setdefault(workspace_id, asyncio.Lock())
        async with lock:
            return await self._ensure_locked(
                workspace_id, config_version, protocol_version, attempt_id,
                orphan_takeover=orphan_takeover)

    async def _ensure_locked(self, workspace_id: str, config_version: int,
                             protocol_version: int,
                             attempt_id: str = "",
                             *, orphan_takeover: bool = False) -> WorkerHandle:
        if self._worker_public_key is None:
            raise ProvisionerError("worker_public_key_unavailable")
        if attempt_id:
            # 在任何目录/容器动作前拒绝未消解的旧清理承诺；只有 control
            # 明确声明 DB 无 attempt 的新代次可以接管孤儿记录。
            self._ensure_attempt_available(
                workspace_id, attempt_id,
                allow_supersede=orphan_takeover)
        paths = await asyncio.to_thread(
            prepare_workspace, self.settings.workspace_root, workspace_id)
        await asyncio.to_thread(ensure_worker_ownership, paths)
        await asyncio.to_thread(
            ensure_quota, paths.root, workspace_id,
            mode=self.settings.quota_mode,
            limit_bytes=self.settings.quota_bytes,
            allow_unquotaed=self.settings.allow_unquotaed)
        sidecar_host = self._sidecar_host(workspace_id)
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
            if not _verify_worker_static(
                    existing, workspace_id=workspace_id,
                    config_version=config_version,
                    image=self.settings.image, paths=paths,
                    public_key_file=self._worker_public_key):
                raise ProvisionerError("container_conflict")
            if not verify_worker_wiring(
                    existing, workspace_id=workspace_id,
                    sidecar_host=sidecar_host):
                # 实际 Env 只在创建时读取。Docker 仍报告 Running（无论探针
                # 结果）就证明没有停止授权，不能为了迁移直接删容器；保留
                # 实例，交控制面 drain/stop 后再重建。只有明确 exited/dead
                # 的实例才可安全重建，持久目录不删除。
                if not worker_confirmed_exited(existing):
                    raise ProvisionerError("worker_wiring_mismatch")
                await self._remove_instance(report["instance_id"],
                                            workspace_id)
                existing = None
            elif worker_confirmed_exited(existing):
                # 退出实例按已核验实例 ID 清理；绝不按同名容器删除。
                await self._remove_instance(report["instance_id"],
                                            workspace_id)
                existing = None
            else:
                # Running/restarting/paused 等非明确退出状态都没有停止
                # 授权。探针失败、ready=false（包括 drain 中的 active Run）
                # 只返回未就绪，保留 worker、凭据、sidecar 与持久目录。
                if not report["ready"]:
                    raise ProvisionerError("worker_not_ready")
                token = await asyncio.to_thread(
                    _read_token, self.settings.workspace_root, workspace_id)
                if not _actual_token_matches(existing, token):
                    raise ProvisionerError("worker_credentials_mismatch")
                if not await self._probe(
                        workspace_id, config_version, report["instance_id"]):
                    raise ProvisionerError("worker_not_ready")
                await self._ensure_sidecar(workspace_id)
                handle = WorkerHandle(
                    workspace_id, report["instance_id"],
                    report["upstream"], token, config_version)
                return await self._with_gateway(
                    handle, protocol_version, worker_created=False,
                    attempt_id=attempt_id,
                    orphan_takeover=orphan_takeover)

        # 只有本轮新建实例才允许生成 token；既有实例已在上面严格比较。
        token = await asyncio.to_thread(
            _load_or_create_token, self.settings.workspace_root, workspace_id)
        env_file = await asyncio.to_thread(
            write_env_file, self.settings.workspace_root, workspace_id,
            config_version, token, paths, sidecar_host=sidecar_host)
        await self._ensure_sidecar(workspace_id)
        await asyncio.to_thread(verify_prepared_paths, paths)
        cidfile = _new_cidfile(self.settings.workspace_root, workspace_id)
        argv = build_run_argv(
            image=self.settings.image, workspace_id=workspace_id,
            config_version=config_version, paths=paths, env_file=env_file,
            public_key_file=self._worker_public_key, cidfile=cidfile)
        # build_run_argv 以 run 开头；_docker 自行添加 docker 可执行程序，
        # 必须传完整 argv，不能丢弃 run。run_task 与 cidfile 归本次
        # ensure 所有：CLI 超时/取消都必须等它终结后再清理。
        instance_id: str | None = None
        run_task: asyncio.Task | None = None
        try:
            run_task = asyncio.create_task(
                self._docker(*argv, timeout=60.0))
            result = await asyncio.shield(run_task)
            instance_id = _read_cidfile(cidfile)
            if result.returncode != 0 or instance_id is None:
                raise ProvisionerError("worker_start_failed")

            deadline = time.monotonic() + self.settings.health_timeout
            while time.monotonic() < deadline:
                raw = await self._inspect_instance(instance_id)
                if raw is None:
                    break
                if not verify_worker_inspect(
                        raw, workspace_id=workspace_id,
                        config_version=config_version,
                        image=self.settings.image, paths=paths,
                        sidecar_host=sidecar_host,
                        public_key_file=self._worker_public_key):
                    raise ProvisionerError("container_conflict")
                report = sanitize_inspect(raw)
                if not _actual_token_matches(raw, token):
                    raise ProvisionerError("worker_credentials_mismatch")
                if (report["ready"]
                        and await self._probe(workspace_id, config_version,
                                              instance_id)):
                    self._discard_cidfile(cidfile)
                    handle = WorkerHandle(
                        workspace_id, instance_id, report["upstream"], token,
                        config_version)
                    return await self._with_gateway(
                        handle, protocol_version, worker_created=True,
                        attempt_id=attempt_id,
                        orphan_takeover=orphan_takeover)
                await asyncio.sleep(HEALTH_POLL_SECONDS)
            raise ProvisionerError("worker_not_ready")
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(self._cleanup_start_attempt(
                workspace_id, cidfile, run_task, instance_id))
            try:
                await self._await_cleanup(cleanup)
            except ProvisionerError as error:
                LOGGER.error(
                    "worker 启动取消后的清理未完成 code=%s，保留 cidfile",
                    error.code)
            except BaseException:  # noqa: BLE001 - 取消仍保留追踪材料
                LOGGER.error("worker 启动取消后的清理未完成，保留 cidfile")
            raise
        except BaseException as failure:
            cleanup = asyncio.create_task(self._cleanup_start_attempt(
                workspace_id, cidfile, run_task, instance_id))
            try:
                await self._await_cleanup(cleanup)
            except asyncio.CancelledError:
                raise
            except BaseException:  # noqa: BLE001 - 转成可恢复清理错误
                raise ProvisionerError(
                    "worker_start_cleanup_incomplete") from failure
            if isinstance(failure, ProvisionerError):
                raise
            raise ProvisionerError("worker_start_failed") from failure

    def _attempt_path(self, workspace_id: str) -> Path:
        return (Path(self.settings.workspace_root) / ".gateway"
                / f"attempt-{workspace_id}.json")

    def _record_attempt(self, workspace_id: str, attempt_id: str,
                        handle: WorkerHandle, *,
                        worker_created: bool, gateway_created: bool,
                        allow_supersede: bool = False) -> None:
        """只为真正创建了资源的尝试写清理承诺。

        纯复用没有可清理资源，只能写 completed/no-op tombstone，绝不能
        覆盖其他 attempt 的 pending 清理承诺。pending 只允许同 attempt
        更新或由 control 明确 commit 后回收；任何其他 attempt 都必须
        先消解旧承诺，否则失败关闭。
        """
        if not worker_created and not gateway_created:
            self._record_noop_attempt(
                workspace_id, attempt_id, handle,
                allow_supersede=allow_supersede)
            return
        if worker_created and gateway_created:
            scope = "worker_gateway"
        elif worker_created:
            scope = "worker"
        else:
            scope = "gateway"
        existing = self._load_attempt(workspace_id)
        if existing is not None:
            if attempt_id in existing.get("fenced_attempts", ()):
                raise ProvisionerError("gateway_attempt_fenced")
            if existing["attempt_id"] == attempt_id:
                if existing["status"] in ("committed", "completed"):
                    if self._attempt_matches(
                            existing, attempt_id, handle,
                            worker_created=worker_created,
                            gateway_created=gateway_created):
                        return
                    raise ProvisionerError(
                        "gateway_attempt_record_unreadable")
            elif (existing["status"] not in ("committed", "completed")
                  and not allow_supersede):
                raise ProvisionerError("gateway_cleanup_pending")
        elif self._attempt_path(workspace_id).exists():
            raise ProvisionerError("gateway_attempt_record_unreadable")
        record = {
            "attempt_id": attempt_id,
            "worker_container_id": handle.container_id,
            "gateway_instance_id": handle.gateway_instance_id,
            "gateway_generation": handle.gateway_generation,
            "gateway_upstream": handle.gateway_upstream,
            "worker_created": bool(worker_created),
            "gateway_created": bool(gateway_created),
            "scope": scope,
            "status": "pending",
            "config_version": int(handle.config_version),
        }
        if existing is not None and existing.get("fenced_attempts"):
            # 新 attempt 的正常记录必须携带既有围栏；被围栏的旧 attempt
            # 仍可能在途，不能因为记录被覆盖而重新获得执行权。
            record["fenced_attempts"] = list(existing["fenced_attempts"])
        self._persist_attempt(workspace_id, record)

    def _record_noop_attempt(self, workspace_id: str, attempt_id: str,
                             handle: WorkerHandle, *,
                             allow_supersede: bool = False) -> None:
        """纯复用写 completed/no-op tombstone，作为迟到 abort 的代次围栏。"""
        existing = self._load_attempt(workspace_id)
        if (existing is not None
                and attempt_id in existing.get("fenced_attempts", ())):
            raise ProvisionerError("gateway_attempt_fenced")
        if (existing is not None and existing["status"] == "pending"
                and existing["attempt_id"] == attempt_id):
            # 同一 attempt 的纯复用重试不能把已创建的清理承诺降级成
            # no-op；保留原 pending 记录。
            return
        if (existing is not None
                and existing["status"] not in ("committed", "completed")
                and not allow_supersede):
            raise ProvisionerError("gateway_cleanup_pending")
        if existing is None and self._attempt_path(workspace_id).exists():
            raise ProvisionerError("gateway_attempt_record_unreadable")
        record = {
            "attempt_id": attempt_id,
            "worker_container_id": handle.container_id,
            "gateway_instance_id": handle.gateway_instance_id,
            "gateway_generation": handle.gateway_generation,
            "gateway_upstream": handle.gateway_upstream,
            "worker_created": False,
            "gateway_created": False,
            "scope": "none",
            "status": "completed",
            "config_version": int(handle.config_version),
        }
        if existing is not None and existing.get("fenced_attempts"):
            record["fenced_attempts"] = list(existing["fenced_attempts"])
        self._persist_attempt(workspace_id, record)

    @staticmethod
    def _valid_fenced_attempts(value: object) -> list[str]:
        """提取原始记录中的合法围栏 ID；坏项丢弃但不恢复执行权。"""
        if not isinstance(value, list):
            return []
        return [item for item in value
                if isinstance(item, str)
                and re.fullmatch(r"[0-9a-f]{32}", item) is not None]

    @staticmethod
    def _attempt_matches(record: dict, attempt_id: str,
                         handle: WorkerHandle, *, worker_created: bool,
                         gateway_created: bool) -> bool:
        return (
            record["attempt_id"] == attempt_id
            and record["worker_container_id"] == handle.container_id
            and record["gateway_instance_id"]
            == handle.gateway_instance_id
            and record["worker_created"] == bool(worker_created)
            and record["gateway_created"] == bool(gateway_created))

    def _ensure_attempt_available(self, workspace_id: str,
                                  attempt_id: str, *,
                                  allow_supersede: bool = False) -> None:
        """在创建任何资源前拒绝未消解的清理承诺或不可读坏记录。

        ``fenced_attempts`` 是接管方对“尚未抵达本服务”的旧 attempt 的
        精确执行权围栏：它可能仍在途，之后到达时必须在这里失败关闭，
        不能因为当前记录已 committed/completed 就放行并写出无主 pending。
        """
        if not attempt_id:
            return
        record = self._load_attempt(workspace_id)
        if record is not None:
            if attempt_id in record.get("fenced_attempts", ()):
                raise ProvisionerError("gateway_attempt_fenced")
            if (record["attempt_id"] == attempt_id
                    or record["status"] in ("committed", "completed")
                    or allow_supersede):
                return
            raise ProvisionerError("gateway_cleanup_pending")
        if self._attempt_path(workspace_id).exists():
            raise ProvisionerError("gateway_attempt_record_unreadable")

    def _persist_attempt(self, workspace_id: str, record: dict) -> None:
        """原子持久 attempt 状态；completed tombstone 不得被提前删除。"""
        path = self._attempt_path(workspace_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(record, sort_keys=True, separators=(",", ":")),
            encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        try:
            os.replace(temporary, path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        self._attempts[workspace_id] = record

    def _read_attempt_payload(self, workspace_id: str) -> dict | None:
        """读取原始 attempt JSON；用于识别旧版坏记录，不做兼容放行。"""
        path = self._attempt_path(workspace_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _load_attempt(self, workspace_id: str) -> dict | None:
        cached = self._attempts.get(workspace_id)
        if cached is not None:
            return cached
        payload = self._read_attempt_payload(workspace_id)
        if payload is None:
            return None
        attempt_id = payload.get("attempt_id")
        worker = payload.get("worker_container_id")
        gateway = payload.get("gateway_instance_id")
        scope = payload.get("scope")
        wc = payload.get("worker_created")
        gc = payload.get("gateway_created")
        status = payload.get("status", "pending")
        config_version = payload.get("config_version", 0)
        gateway_generation = payload.get("gateway_generation", "")
        gateway_upstream = payload.get("gateway_upstream", "")
        fenced_attempts = payload.get("fenced_attempts", [])
        if (not isinstance(fenced_attempts, list)
                or any(not isinstance(item, str)
                       or re.fullmatch(r"[0-9a-f]{32}", item) is None
                       for item in fenced_attempts)
                or len(set(fenced_attempts)) != len(fenced_attempts)):
            return None
        if (not isinstance(attempt_id, str)
                or re.fullmatch(r"[0-9a-f]{32}", attempt_id) is None
                or not isinstance(worker, str)
                or _CONTAINER_INSTANCE.fullmatch(worker) is None
                or not isinstance(gateway, str)
                or _CONTAINER_INSTANCE.fullmatch(gateway) is None
                or not isinstance(gateway_generation, str)
                or not isinstance(gateway_upstream, str)
                or scope not in ("gateway", "worker", "worker_gateway",
                                 "none")
                or type(wc) is not bool or type(gc) is not bool
                or type(config_version) is not int or config_version < 0
                or (scope == "none"
                    and (wc or gc or status != "completed"))
                or (scope != "none"
                    and (wc != (scope in ("worker", "worker_gateway"))
                         or gc != (scope in ("gateway",
                                             "worker_gateway"))))
                or status not in ("pending", "committed", "completed")):
            return None
        record = {
            "attempt_id": attempt_id,
            "worker_container_id": worker,
            "gateway_instance_id": gateway,
            "gateway_generation": gateway_generation,
            "gateway_upstream": gateway_upstream,
            "worker_created": wc,
            "gateway_created": gc,
            "scope": scope,
            "status": status,
            "config_version": config_version,
        }
        if fenced_attempts:
            record["fenced_attempts"] = list(fenced_attempts)
        self._attempts[workspace_id] = record
        return record

    def _clear_attempt(self, workspace_id: str, *,
                       worker_container_id: str = "",
                       gateway_instance_id: str = "") -> None:
        record = self._load_attempt(workspace_id)
        if record is None:
            return
        if worker_container_id and record["worker_container_id"] != \
                worker_container_id:
            return
        if gateway_instance_id and record["gateway_instance_id"] != \
                gateway_instance_id:
            return
        fenced = list(record.get("fenced_attempts", ()))
        if fenced:
            # 物理资源已按精确实例停止，只证明记录所属 attempt 不再需要
            # 清理承诺；清理不能恢复已被围栏的 A 的执行权，已停止实例的
            # attempt 本身也不得迟到复活。保留合法无资源记录，让围栏跨
            # Stop 与重启继续生效；新 attempt 仍可正常覆盖该记录。
            if record["attempt_id"] not in fenced:
                fenced.append(record["attempt_id"])
            kept = {
                "attempt_id": record["attempt_id"],
                "worker_container_id": record["worker_container_id"],
                "gateway_instance_id": record["gateway_instance_id"],
                "worker_created": False,
                "gateway_created": False,
                "scope": "none",
                "status": "completed",
                "config_version": int(record.get("config_version", 0)),
                "fenced_attempts": fenced,
            }
            try:
                self._persist_attempt(workspace_id, kept)
            except OSError:
                # 落盘失败时原记录（含围栏）仍在，绝不能改为删除。
                LOGGER.error(
                    "gateway attempt 围栏记录持久化失败，保留原记录")
            return
        self._attempts.pop(workspace_id, None)
        try:
            self._attempt_path(workspace_id).unlink(missing_ok=True)
        except OSError:
            LOGGER.error("gateway attempt 记录清理失败，保留追踪材料")

    async def _with_gateway(self, handle: WorkerHandle, protocol_version: int,
                            *, worker_created: bool,
                            attempt_id: str = "",
                            replace_unavailable: bool = False,
                            orphan_takeover: bool = False) -> WorkerHandle:
        """gateway 启用时只返回候选；v1 缺失元数据必须失败关闭。"""
        if not self.gateway_enabled:
            if protocol_version >= PROTOCOL_VERSION:
                raise ProvisionerError("gateway_disabled")
            return handle
        if protocol_version < PROTOCOL_VERSION:
            raise ProvisionerError("gateway_required")
        assert self._gateway is not None
        if attempt_id:
            # 必须在 gateway 创建前失败，避免制造无法归属的新资源。
            self._ensure_attempt_available(
                handle.workspace_id, attempt_id,
                allow_supersede=orphan_takeover)
        try:
            candidate = await self._gateway.ensure(
                workspace_id=handle.workspace_id,
                config_version=handle.config_version,
                worker_container_id=handle.container_id,
                worker_origin=handle.upstream,
                worker_service_token=handle.service_token,
                replace_unavailable=replace_unavailable)
        except GatewayRuntimeError as error:
            raise ProvisionerError(error.code) from None
        result = replace(
            handle,
            gateway_instance_id=candidate.gateway_instance_id,
            gateway_generation=candidate.boot_generation,
            gateway_upstream=candidate.gateway_upstream,
            gateway_state="available" if candidate.active else "staged",
            attempt_id=attempt_id,
            worker_created=worker_created,
            gateway_created=candidate.gateway_created)
        if attempt_id:
            try:
                self._record_attempt(
                    result.workspace_id, attempt_id, result,
                    worker_created=worker_created,
                    gateway_created=candidate.gateway_created,
                    allow_supersede=orphan_takeover)
            except BaseException:
                # 归属写盘失败时不能留下无主 gateway；按本轮精确 ID 清理。
                if candidate.gateway_created:
                    try:
                        await self._gateway.stop(
                            handle.workspace_id, handle.container_id,
                            candidate.gateway_instance_id)
                    except BaseException:  # noqa: BLE001 - 保留原始失败
                        LOGGER.error(
                            "attempt 记录失败后 gateway 清理未完成，"
                            "保留追踪材料")
                raise
        return result

    async def restore_gateway(self, workspace_id: str,
                              worker_container_id: str,
                              config_version: int, *,
                              attempt_id: str = "") -> WorkerHandle:
        """只为同一已核验 worker 重配/更换 gateway；绝不创建/替换 worker。

        控制面既有 Run 在 gateway 重启或实例失效后调用；所有目标由精确
        worker ID、workspace 与 config_version 派生，调用方不能提供镜像、
        命令、挂载或网络。worker 缺失/身份不符时失败关闭，绝不回退到
        ensure 的冷启动路径。
        """
        if not self.gateway_enabled:
            raise ProvisionerError("gateway_disabled")
        if not attempt_id:
            attempt_id = uuid.uuid4().hex
        parse_request({
            "version": PROTOCOL_VERSION, "action": "restore_gateway",
            "workspace_id": workspace_id,
            "worker_container_id": worker_container_id,
            "config_version": config_version,
            "attempt_id": attempt_id})
        lock = self._locks.setdefault(workspace_id, asyncio.Lock())
        async with lock:
            # restore 同样不能在未消解的旧清理承诺下创建新 gateway。
            self._ensure_attempt_available(workspace_id, attempt_id)
            raw = await self._inspect_instance(worker_container_id)
            if raw is None:
                raise ProvisionerError("worker_identity_mismatch")
            report = sanitize_inspect(raw)
            if (not report["managed"]
                    or report["workspace_id"] != workspace_id
                    or report["instance_id"] != worker_container_id
                    or report["config_version"] != config_version):
                raise ProvisionerError("worker_identity_mismatch")
            try:
                paths = await asyncio.to_thread(
                    existing_runtime_paths, self.settings.workspace_root,
                    workspace_id)
            except ProvisionerError:
                raise ProvisionerError("worker_identity_mismatch") from None
            sidecar_host = self._sidecar_host(workspace_id)
            if not _verify_worker_static(
                    raw, workspace_id=workspace_id,
                    config_version=config_version,
                    image=self.settings.image, paths=paths,
                    public_key_file=self._worker_public_key):
                raise ProvisionerError("worker_identity_mismatch")
            if not verify_worker_wiring(
                    raw, workspace_id=workspace_id,
                    sidecar_host=sidecar_host):
                raise ProvisionerError("worker_wiring_mismatch")
            if not report["ready"]:
                raise ProvisionerError("worker_not_ready")
            try:
                token = await asyncio.to_thread(
                    _read_token, self.settings.workspace_root, workspace_id)
            except ProvisionerError:
                raise ProvisionerError("worker_credentials_mismatch") from None
            if not _actual_token_matches(raw, token):
                raise ProvisionerError("worker_credentials_mismatch")
            if not await self._probe(
                    workspace_id, config_version, worker_container_id):
                raise ProvisionerError("worker_not_ready")
            await self._ensure_sidecar(workspace_id)
            handle = WorkerHandle(
                workspace_id, worker_container_id, report["upstream"], token,
                config_version)
            return await self._with_gateway(
                handle, PROTOCOL_VERSION, worker_created=False,
                attempt_id=attempt_id, replace_unavailable=True)

    async def stop(self, workspace_id: str, instance_id: str,
                   gateway_instance_id: str = "", *,
                   protocol_version: int = LEGACY_PROTOCOL_VERSION) -> None:
        """只停止调用方取得的那个物理实例；同名新容器绝不受影响。

        物理动作使用 Docker 不可复用的容器 ID，而不是当前同名容器名。
        即使旧 stop 在租约被新代次接管后才送达，ID 不匹配即拒绝。
        """
        if self.gateway_enabled:
            if protocol_version < PROTOCOL_VERSION:
                raise ProvisionerError("gateway_required")
            parse_request({
                "version": PROTOCOL_VERSION, "action": "stop",
                "workspace_id": workspace_id, "instance_id": instance_id,
                "gateway_instance_id": gateway_instance_id})
        else:
            if protocol_version >= PROTOCOL_VERSION:
                raise ProvisionerError("gateway_disabled")
            parse_request({"version": LEGACY_PROTOCOL_VERSION,
                           "action": "stop", "workspace_id": workspace_id,
                           "instance_id": instance_id})
        lock = self._locks.setdefault(workspace_id, asyncio.Lock())
        async with lock:
            if self.gateway_enabled:
                assert self._gateway is not None
                if gateway_instance_id:
                    try:
                        # 先按精确 gateway 实例停止；失败时绝不继续删除 worker。
                        await self._gateway.stop(
                            workspace_id, instance_id, gateway_instance_id)
                    except GatewayRuntimeError as error:
                        raise ProvisionerError(error.code) from None
                    # 旧 gateway 已消失但同名新 gateway 仍存在时，旧 tuple
                    # 不能据此停止当前 worker。
                    current_gateway = await self._gateway.current_instance_id(
                        workspace_id)
                    if (current_gateway
                            and current_gateway != gateway_instance_id):
                        raise ProvisionerError("gateway_target_mismatch")
                else:
                    # gateway-only 恢复后 control 已没有 gateway ID；只有
                    # 确认没有同名 gateway 时才允许继续停 worker/sidecar。
                    current_gateway = await self._gateway.current_instance_id(
                        workspace_id)
                    if current_gateway:
                        raise ProvisionerError("gateway_target_mismatch")
            await self._stop_worker_locked(workspace_id, instance_id)
            self._clear_attempt(
                workspace_id, worker_container_id=instance_id,
                gateway_instance_id=gateway_instance_id)

    async def _stop_worker_locked(self, workspace_id: str,
                                  instance_id: str) -> None:
        raw = await self._inspect_raw(workspace_id)
        if raw is not None:
            report = sanitize_inspect(raw)
            if not report["managed"]:
                raise ProvisionerError("container_conflict")
            if (not isinstance(raw.get("Id"), str)
                    or raw.get("Id") != instance_id):
                raise ProvisionerError("container_conflict")

            stopped = await self._docker(
                "stop", "--time", "30", instance_id, timeout=60.0)
            if stopped.returncode != 0:
                # 旧管理者可能已先删除；按实例 ID 确认终态。
                current = await self._inspect_instance(instance_id)
                if (current is not None
                        and (current.get("State") or {}).get(
                            "Running") is True):
                    raise ProvisionerError("container_stop_failed")

            removed = await self._docker("rm", "-f", instance_id,
                                         timeout=60.0)
            if (removed.returncode != 0
                    and await self._inspect_instance(instance_id) is not None):
                raise ProvisionerError("container_remove_failed")
            # 命令成功不等于实例已消失；最终确认原实例确实不存在。
            if await self._inspect_instance(instance_id) is not None:
                raise ProvisionerError("container_remove_failed")

        # worker 已确认删除（或本就不存在）后，sidecar 也必须清理；
        # worker 存在但 ID 不符时已在上面拒绝，不会误停新代次 sidecar。
        await self._stop_sidecar(workspace_id)

    async def abort(self, workspace_id: str, worker_container_id: str,
                    gateway_instance_id: str, *, attempt_id: str, scope: str,
                    worker_created: bool, gateway_created: bool) -> None:
        """只按持久 attempt 记录和精确 scope 清理本轮新建资源。"""
        if not self.gateway_enabled:
            raise ProvisionerError("gateway_disabled")
        parse_request({
            "version": PROTOCOL_VERSION, "action": "abort",
            "workspace_id": workspace_id,
            "worker_container_id": worker_container_id,
            "gateway_instance_id": gateway_instance_id,
            "worker_created": worker_created,
            "gateway_created": gateway_created,
            "attempt_id": attempt_id, "scope": scope})
        lock = self._locks.setdefault(workspace_id, asyncio.Lock())
        async with lock:
            record = self._load_attempt(workspace_id)
            if (record is None
                    or record["attempt_id"] != attempt_id
                    or record["worker_container_id"] != worker_container_id
                    or record["gateway_instance_id"] != gateway_instance_id
                    or record["worker_created"] != worker_created
                    or record["gateway_created"] != gateway_created
                    or record["scope"] != scope):
                raise ProvisionerError("gateway_abort_superseded")
            await self._abort_locked(workspace_id, record)

    async def _abort_locked(self, workspace_id: str, record: dict) -> None:
        """调用方已按持久记录完成精确匹配，此处只执行记录中的范围。"""
        if record["status"] == "completed":
            # 物理清理已完成、control 可能尚未提交 DB；同 attempt/ID/
            # scope 的重试直接幂等成功，不再次执行破坏操作。
            return
        if record["status"] == "committed":
            # 成功启动已收口，资源归成功代次所有；abort 不能把它当作
            # “已清理完成”而静默成功，否则会丢掉真正的停止动作。
            raise ProvisionerError("gateway_abort_superseded")
        scope = record["scope"]
        if scope in ("gateway", "worker_gateway"):
            assert self._gateway is not None
            try:
                await self._gateway.stop(
                    workspace_id, record["worker_container_id"],
                    record["gateway_instance_id"])
            except GatewayRuntimeError as error:
                raise ProvisionerError(error.code) from None
        if scope in ("worker", "worker_gateway"):
            await self._stop_worker_locked(
                workspace_id, record["worker_container_id"])
        completed = dict(record)
        completed["status"] = "completed"
        try:
            self._persist_attempt(workspace_id, completed)
        except OSError:
            # 物理已完成但 tombstone 未持久化：保持 pending，重试可
            # 幂等重做终态核验；绝不能假报已收口。
            raise ProvisionerError("gateway_abort_record_failed") from None

    async def commit_attempt(self, workspace_id: str,
                             worker_container_id: str,
                             gateway_instance_id: str, *,
                             attempt_id: str = "",
                             resolve: bool = False,
                             config_version: int = 0) -> dict | None:
        """control 已提交成功启动后收口 attempt，允许后续尝试接管。

        只匹配同一 attempt 或明确给出的精确目标；pending 记录被其他
        attempt 占用时失败关闭。文件不存在表示纯复用从未写记录，按无
        cleanup 承诺处理。

        ``resolve=True`` 是受限只读模式：按精确 attempt 返回 provisioner
        已持久化的真实候选，供 control 在响应丢失后恢复归属；不创建、
        不删除任何资源，也不信任调用方给出的目标 ID。
        """
        if not self.gateway_enabled:
            raise ProvisionerError("gateway_disabled")
        parse_request({
            "version": PROTOCOL_VERSION, "action": "commit_attempt",
            "workspace_id": workspace_id,
            "worker_container_id": worker_container_id,
            "gateway_instance_id": gateway_instance_id,
            "attempt_id": attempt_id,
            "resolve": resolve,
            "config_version": config_version})
        lock = self._locks.setdefault(workspace_id, asyncio.Lock())
        async with lock:
            if resolve:
                return await self._resolve_attempt_locked(
                    workspace_id, worker_container_id, attempt_id,
                    config_version)
            self._commit_attempt_locked(
                workspace_id, worker_container_id, gateway_instance_id,
                attempt_id)
            return None

    async def _resolve_attempt_locked(self, workspace_id: str,
                                      worker_container_id: str,
                                      attempt_id: str,
                                      config_version: int) -> dict:
        """按持久 attempt 返回真实候选；只校验归属，不执行副作用。"""
        record = self._load_attempt(workspace_id)
        path = self._attempt_path(workspace_id)
        if record is None:
            if path.exists():
                raise ProvisionerError("gateway_attempt_record_unreadable")
            return {"found": False}
        if record["attempt_id"] != attempt_id:
            if record["status"] in ("committed", "completed"):
                # 目标 attempt 从未写记录，且当前槽位已无 pending 承诺。
                return {"found": False}
            raise ProvisionerError("gateway_cleanup_pending")
        if record["worker_container_id"] != worker_container_id:
            raise ProvisionerError("gateway_commit_superseded")
        recorded_config = record.get("config_version", 0)
        if (config_version and recorded_config
                and config_version != recorded_config):
            raise ProvisionerError("gateway_recovery_config_mismatch")
        result = {
            "found": True,
            "attempt_id": record["attempt_id"],
            "status": record["status"],
            "worker_container_id": record["worker_container_id"],
            "gateway_instance_id": record["gateway_instance_id"],
            "worker_created": record["worker_created"],
            "gateway_created": record["gateway_created"],
            "scope": record["scope"],
            "config_version": recorded_config,
        }
        if record["status"] == "completed":
            # 已完成/纯复用 tombstone 没有可接管资源，不能当作候选。
            return result
        if not record["gateway_instance_id"]:
            raise ProvisionerError("gateway_recovery_target_mismatch")
        gateway = await self._gateway.inspect(
            workspace_id=workspace_id,
            gateway_instance_id=record["gateway_instance_id"])
        if not isinstance(gateway, dict) or gateway.get("managed") is not True:
            raise ProvisionerError("gateway_recovery_target_mismatch")
        if (gateway.get("workspace_id") != workspace_id
                or gateway.get("gateway_instance_id")
                != record["gateway_instance_id"]
                or gateway.get("worker_container_id")
                != worker_container_id
                or (config_version
                    and gateway.get("config_version") != config_version)):
            raise ProvisionerError("gateway_recovery_target_mismatch")
        generation = record.get("gateway_generation", "")
        upstream = record.get("gateway_upstream", "")
        if not generation or not upstream:
            generation = gateway.get("boot_generation")
            upstream = gateway.get("gateway_upstream")
        if (not isinstance(generation, str)
                or re.fullmatch(r"[0-9a-f]{32}", generation) is None
                or not isinstance(upstream, str) or not upstream):
            raise ProvisionerError("gateway_recovery_target_mismatch")
        result.update({
            "gateway_generation": generation,
            "gateway_upstream": upstream,
            # active 只表示 gateway 进程仍在；available 才表示网络/挂载/
            # 安全配置与状态核验通过。只有 available 才能跳过 activate；
            # 否则必须返回 staged，让 control 重新执行完整验证与激活。
            "gateway_state": ("available"
                              if gateway.get("available") is True
                              else "staged"),
        })
        return result

    def _commit_attempt_locked(self, workspace_id: str,
                               worker_container_id: str,
                               gateway_instance_id: str,
                               attempt_id: str) -> None:
        record = self._load_attempt(workspace_id)
        path = self._attempt_path(workspace_id)
        if record is None:
            if not path.exists():
                if attempt_id:
                    # 本服务没有该 attempt 的执行记录：控制面正在收口一个
                    # 可能尚未抵达工作区锁的请求。必须持久围栏它的执行
                    # 权，之后它迟到时才能在创建任何资源前被拒绝，而不是
                    # 把无主 pending 写回本 workspace。
                    self._persist_fenced_attempt(
                        workspace_id, worker_container_id,
                        gateway_instance_id, attempt_id)
                return
            # 旧版纯复用覆盖产生的不可读记录：只有在它明确声明未创建
            # 任何资源、且目标完全一致时，才允许用 completed/no-op 收口。
            raw = self._read_attempt_payload(workspace_id)
            if (raw is None
                    or raw.get("status", "pending") != "pending"
                    or raw.get("worker_created") is not False
                    or raw.get("gateway_created") is not False
                    or raw.get("worker_container_id") != worker_container_id
                    or raw.get("gateway_instance_id")
                    != gateway_instance_id):
                raise ProvisionerError("gateway_attempt_record_unreadable")
            raw_attempt = raw.get("attempt_id")
            recovered_attempt = (
                raw_attempt if isinstance(raw_attempt, str)
                and re.fullmatch(r"[0-9a-f]{32}", raw_attempt) is not None
                else attempt_id or uuid.uuid4().hex)
            record = {
                "attempt_id": recovered_attempt,
                "worker_container_id": worker_container_id,
                "gateway_instance_id": gateway_instance_id,
                "worker_created": False,
                "gateway_created": False,
                "scope": "none",
                "status": "completed",
            }
            raw_fenced = raw.get("fenced_attempts")
            fenced = []
            if isinstance(raw_fenced, list):
                fenced = [item for item in raw_fenced
                          if isinstance(item, str)
                          and re.fullmatch(r"[0-9a-f]{32}", item) is not None]
            if attempt_id and attempt_id != recovered_attempt:
                # 请求收口的 attempt 与旧记录不同：它同样没有执行记录，
                # 必须进入围栏，不能因旧记录被修复而重新获得执行权。
                if attempt_id not in fenced:
                    fenced.append(attempt_id)
            if fenced:
                record["fenced_attempts"] = fenced
            self._persist_attempt(workspace_id, record)
            return
        if attempt_id and attempt_id in record.get("fenced_attempts", ()):
            # 已围栏 attempt 的重复收口幂等成功，不再改写当前记录。
            return
        if not attempt_id and record["status"] == "pending":
            # 空 attempt 只能做已完成 tombstone 的幂等复核；不能按目标
            # 猜测归属，提前消解其他 owner 仍在执行的清理承诺。
            raise ProvisionerError("gateway_commit_superseded")
        if (attempt_id and record["attempt_id"] != attempt_id) or (
                record["worker_container_id"] != worker_container_id
                or record["gateway_instance_id"] != gateway_instance_id):
            if record["status"] in ("committed", "completed"):
                # 当前记录已无 pending 清理承诺。请求的 attempt 也可能因
                # 上一轮进程在写记录前崩溃而根本不存在；精确 commit 不能
                # 证明它不会迟到执行，必须把它持久围栏后才能返回成功。
                if attempt_id and record["attempt_id"] != attempt_id:
                    self._remember_fenced_attempt(
                        workspace_id, record, attempt_id)
                return
            raise ProvisionerError("gateway_commit_superseded")
        if record["status"] == "pending":
            committed = dict(record)
            committed["status"] = "committed"
            self._persist_attempt(workspace_id, committed)

    def _persist_fenced_attempt(self, workspace_id: str,
                                worker_container_id: str,
                                gateway_instance_id: str,
                                attempt_id: str) -> None:
        """持久“该 attempt 永不得再执行”的已完成语义记录。

        记录本身无资源可清理；``fenced_attempts`` 保存被收口的精确 ID。
        后续任何 attempt 的正常记录都会携带该列表，避免记录被覆盖后旧
        请求重新获得执行权。
        """
        self._persist_attempt(workspace_id, {
            "attempt_id": attempt_id,
            "worker_container_id": worker_container_id,
            "gateway_instance_id": gateway_instance_id,
            "worker_created": False,
            "gateway_created": False,
            "scope": "none",
            "status": "completed",
            "config_version": 0,
            "fenced_attempts": [attempt_id],
        })

    def _remember_fenced_attempt(self, workspace_id: str, record: dict,
                                 attempt_id: str) -> None:
        fenced = list(record.get("fenced_attempts", ()))
        if attempt_id in fenced:
            return
        fenced.append(attempt_id)
        updated = dict(record)
        updated["fenced_attempts"] = fenced
        self._persist_attempt(workspace_id, updated)

    async def recover_abort(self, workspace_id: str,
                            worker_container_id: str,
                            gateway_instance_id: str, config_version: int, *,
                            attempt_id: str, scope: str,
                            worker_created: bool,
                            gateway_created: bool) -> None:
        """受限恢复旧版无资源记录覆盖后的 gateway-only 清理承诺。

        只在以下条件全部满足时重建 attempt 归属并执行原 scope 清理：
        现有记录缺失或为旧版“未创建任何资源”的 pending 坏记录、目标
        worker/gateway 与请求精确一致、worker 与 gateway 的真实身份
        和 config_version 均已核验。任何不符都拒绝，不按名称删除。
        """
        if not self.gateway_enabled:
            raise ProvisionerError("gateway_disabled")
        parse_request({
            "version": PROTOCOL_VERSION, "action": "recover_abort",
            "workspace_id": workspace_id,
            "config_version": config_version,
            "worker_container_id": worker_container_id,
            "gateway_instance_id": gateway_instance_id,
            "worker_created": worker_created,
            "gateway_created": gateway_created,
            "attempt_id": attempt_id, "scope": scope})
        lock = self._locks.setdefault(workspace_id, asyncio.Lock())
        async with lock:
            self._require_recoverable_scope(
                worker_created=worker_created,
                gateway_created=gateway_created, scope=scope)
            record = self._load_attempt(workspace_id)
            if record is not None:
                if (record["attempt_id"] != attempt_id
                        or record["worker_container_id"]
                        != worker_container_id
                        or record["gateway_instance_id"]
                        != gateway_instance_id
                        or record["worker_created"] != worker_created
                        or record["gateway_created"] != gateway_created
                        or record["scope"] != scope):
                    raise ProvisionerError("gateway_abort_superseded")
                await self._abort_locked(workspace_id, record)
                return
            path = self._attempt_path(workspace_id)
            if not path.exists():
                raise ProvisionerError("gateway_abort_superseded")
            raw = self._read_attempt_payload(workspace_id)
            if (raw is None
                    or raw.get("status", "pending") != "pending"
                    or raw.get("worker_created") is not False
                    or raw.get("gateway_created") is not False
                    or raw.get("worker_container_id") != worker_container_id
                    or raw.get("gateway_instance_id")
                    != gateway_instance_id):
                raise ProvisionerError("gateway_abort_recovery_refused")
            await self._verify_recovery_identity(
                workspace_id, worker_container_id, gateway_instance_id,
                config_version)
            reconstructed = {
                "attempt_id": attempt_id,
                "worker_container_id": worker_container_id,
                "gateway_instance_id": gateway_instance_id,
                "worker_created": False,
                "gateway_created": True,
                "scope": "gateway",
                "status": "pending",
            }
            fenced = self._valid_fenced_attempts(
                raw.get("fenced_attempts"))
            if fenced:
                # 修复旧坏记录时不能丢掉已有的执行权事实；abort 收口后
                # _abort_locked 会带着该列表写 completed。
                reconstructed["fenced_attempts"] = fenced
            self._persist_attempt(workspace_id, reconstructed)
            await self._abort_locked(workspace_id, reconstructed)

    @staticmethod
    def _require_recoverable_scope(*, worker_created: bool,
                                   gateway_created: bool, scope: str) -> None:
        # 只恢复 gateway-only：worker 不删除，历史/Run/凭证不受影响。
        if (scope != "gateway" or worker_created is not False
                or gateway_created is not True):
            raise ProvisionerError("gateway_abort_recovery_refused")

    async def _verify_recovery_identity(
            self, workspace_id: str, worker_container_id: str,
            gateway_instance_id: str, config_version: int) -> None:
        raw = await self._inspect_instance(worker_container_id)
        report = sanitize_inspect(raw)
        if (not report["managed"]
                or report["workspace_id"] != workspace_id
                or report["instance_id"] != worker_container_id
                or report["config_version"] != config_version):
            raise ProvisionerError("gateway_abort_recovery_refused")
        assert self._gateway is not None
        try:
            gateway = await self._gateway.inspect(
                workspace_id=workspace_id,
                gateway_instance_id=gateway_instance_id)
        except (GatewayRuntimeError, AttributeError, TypeError):
            raise ProvisionerError(
                "gateway_abort_recovery_refused") from None
        if (not isinstance(gateway, dict)
                or gateway.get("managed") is not True
                or gateway.get("workspace_id") != workspace_id
                or gateway.get("gateway_instance_id")
                != gateway_instance_id
                or gateway.get("worker_container_id")
                != worker_container_id
                or gateway.get("config_version") != config_version):
            raise ProvisionerError("gateway_abort_recovery_refused")

    async def activate(self, workspace_id: str, worker_container_id: str,
                       gateway_instance_id: str, boot_generation: str,
                       config_version: int) -> dict:
        """按本轮实际 worker/gateway 身份激活；不接受名称或旧代次。"""
        if not self.gateway_enabled:
            raise ProvisionerError("gateway_disabled")
        parse_request({
            "version": PROTOCOL_VERSION, "action": "activate",
            "workspace_id": workspace_id,
            "config_version": config_version,
            "worker_container_id": worker_container_id,
            "gateway_instance_id": gateway_instance_id,
            "boot_generation": boot_generation})
        lock = self._locks.setdefault(workspace_id, asyncio.Lock())
        async with lock:
            raw = await self._inspect_instance(worker_container_id)
            if raw is None:
                raise ProvisionerError("worker_identity_mismatch")
            report = sanitize_inspect(raw)
            if (not report["managed"]
                    or report["workspace_id"] != workspace_id
                    or report["instance_id"] != worker_container_id
                    or report["config_version"] != config_version):
                raise ProvisionerError("worker_identity_mismatch")
            try:
                paths = await asyncio.to_thread(
                    existing_runtime_paths, self.settings.workspace_root,
                    workspace_id)
            except ProvisionerError:
                raise ProvisionerError("worker_identity_mismatch") from None
            if not _verify_worker_static(
                    raw, workspace_id=workspace_id,
                    config_version=config_version,
                    image=self.settings.image, paths=paths,
                    public_key_file=self._worker_public_key):
                raise ProvisionerError("worker_identity_mismatch")
            try:
                token = await asyncio.to_thread(
                    _read_token, self.settings.workspace_root, workspace_id)
            except ProvisionerError:
                raise ProvisionerError("worker_credentials_mismatch") from None
            if not _actual_token_matches(raw, token):
                raise ProvisionerError("worker_credentials_mismatch")
            if not await self._probe(
                    workspace_id, config_version, worker_container_id):
                raise ProvisionerError("worker_not_ready")
            assert self._gateway is not None
            try:
                status = await self._gateway.activate(
                    workspace_id=workspace_id,
                    config_version=config_version,
                    worker_container_id=worker_container_id,
                    gateway_instance_id=gateway_instance_id,
                    boot_generation=boot_generation)
            except GatewayRuntimeError as error:
                raise ProvisionerError(error.code) from None
            return {"gateway": status}

    async def inspect(self, workspace_id: str, *,
                      protocol_version: int = LEGACY_PROTOCOL_VERSION) -> dict:
        if self.gateway_enabled and protocol_version < PROTOCOL_VERSION:
            raise ProvisionerError("gateway_required")
        if not self.gateway_enabled and protocol_version >= PROTOCOL_VERSION:
            raise ProvisionerError("gateway_disabled")
        parse_request({"version": protocol_version, "action": "inspect",
                       "workspace_id": workspace_id})
        gateway_report: dict | None = None
        if self.gateway_enabled:
            assert self._gateway is not None
            try:
                gateway_report = await self._gateway.inspect(
                    workspace_id=workspace_id)
            except GatewayRuntimeError as error:
                raise ProvisionerError(error.code) from None
        raw = await self._inspect_raw(workspace_id)
        if raw is None:
            return self._attach_gateway({
                "workspace_id": workspace_id,
                "container_id": "",
                "config_version": 0,
                "ready": False,
                "worker_running": False,
                "managed": False,
                "labels": {},
                "upstream": "",
                "security": {},
            }, gateway_report)
        report = sanitize_inspect(raw)
        if not report["managed"]:
            return self._attach_gateway(report, gateway_report)
        try:
            paths = await asyncio.to_thread(
                existing_runtime_paths, self.settings.workspace_root,
                workspace_id)
        except ProvisionerError:
            raise ProvisionerError("container_conflict") from None
        if not _verify_worker_static(
                raw, workspace_id=workspace_id,
                config_version=report["config_version"],
                image=self.settings.image, paths=paths,
                public_key_file=self._worker_public_key):
            raise ProvisionerError("container_conflict")
        sidecar_host = self._sidecar_host(workspace_id)
        wiring_ready = verify_worker_wiring(
            raw, workspace_id=workspace_id, sidecar_host=sidecar_host)
        # 接管前复核实际网络的 internal/IPv4-only/bridge 属性。
        await self._require_existing_network(workspace_id)
        # 对账必须验收完整批准依赖：缺 sidecar、sidecar 被篡改或其受控
        # 出站网络失效时，worker 自身健康不能证明接线可用。
        sidecar_ready = await self._sidecar_ready(workspace_id)
        report["sidecar_ready"] = sidecar_ready
        worker_ready = bool(
            report["ready"] and wiring_ready and sidecar_ready)
        if worker_ready:
            try:
                token = _read_token(
                    self.settings.workspace_root, workspace_id)
            except ProvisionerError:
                worker_ready = False
            # 返回/控制面使用的 token 必须与已核验实例的实际 Env 一致；
            # 缺宿主凭据或旧 token 不得静默接管。
            if worker_ready and not _actual_token_matches(raw, token):
                worker_ready = False
            if worker_ready:
                worker_ready = await self._probe(
                    workspace_id, report["config_version"],
                    report["instance_id"])
        report["worker_ready"] = worker_ready
        report["ready"] = worker_ready
        return self._attach_gateway(report, gateway_report)

    @staticmethod
    def _attach_gateway(report: dict, gateway: dict | None) -> dict:
        """v1 保持旧报告；v2 明确报告 gateway 可用性和固定目标。"""
        if gateway is None:
            return report
        report["gateway"] = gateway
        report["gateway_instance_id"] = gateway.get(
            "gateway_instance_id", "")
        report["gateway_generation"] = gateway.get("boot_generation", "")
        report["gateway_upstream"] = gateway.get("gateway_upstream", "")
        report["gateway_available"] = gateway.get("available") is True
        report["ready"] = bool(
            report.get("worker_ready", report.get("ready"))
            and report["gateway_available"])
        return report

    async def identity(self) -> dict:
        """只读返回实际运行配置的脱敏身份；不返回任何秘密内容。"""
        try:
            socket_stat = self.settings.socket_path.stat()
        except OSError:
            raise ProvisionerError("identity_socket_unavailable") from None
        context = await self._docker("context", "show", timeout=15.0)
        if context.returncode != 0:
            raise ProvisionerError("docker_context_unavailable")
        engine = await self._docker(
            "info", "--format", "{{.ID}}", timeout=15.0)
        if engine.returncode != 0:
            raise ProvisionerError("docker_engine_unavailable")
        engine_id = str(engine.stdout or "").strip()
        if not engine_id:
            raise ProvisionerError("docker_engine_unavailable")
        context_name = str(context.stdout or "").strip()
        if not context_name:
            raise ProvisionerError("docker_context_unavailable")

        def digest(path: Path | None) -> str:
            if path is None:
                return ""
            try:
                data = Path(path).read_bytes()
            except OSError:
                raise ProvisionerError("identity_file_unavailable") from None
            return hashlib.sha256(data).hexdigest()

        worker_key = digest(self._worker_public_key)
        gateway_key = digest(self.settings.gateway_public_key)
        policy = digest(self.settings.sidecar_policy)
        return {
            "protocol": PROTOCOL_VERSION,
            "sourceSha256": SOURCE_SHA256,
            "actions": list(ACTIONS),
            "engine": {"context": context_name, "id": engine_id},
            "images": {
                "worker": self.settings.image,
                "gateway": self.settings.gateway_image,
            },
            "workspaceRoot": str(Path(self.settings.workspace_root).resolve()),
            "networks": {
                "sidecar": self.settings.sidecar_network,
                "gatewayControl": self.settings.gateway_control_network,
            },
            "brokerUpstream": self.settings.broker_upstream,
            "keys": {
                "workerPublicSha256": worker_key,
                "gatewayPublicSha256": gateway_key,
            },
            "policySha256": policy,
            "socket": {
                "path": str(self.settings.socket_path),
                "uid": int(socket_stat.st_uid),
                "gid": int(socket_stat.st_gid),
                "mode": f"{stat.S_IMODE(socket_stat.st_mode):03o}",
                "allowedUids": sorted(int(uid)
                                      for uid in self.settings.allowed_uids),
            },
        }

    async def _probe(self, workspace_id: str, config_version: int,
                     instance_id: str) -> bool:
        """对不可复用容器 ID 执行固定 healthcheck，不探测宿主到容器 IP。"""
        if (not isinstance(instance_id, str)
                or _CONTAINER_INSTANCE.fullmatch(instance_id) is None):
            return False
        try:
            result = await self._docker(
                "exec", instance_id, *WORKER_HEALTHCHECK_COMMAND,
                timeout=10.0)
        except Exception:
            return False
        if result.returncode != 0:
            return False
        lines = [line.strip() for line in str(result.stdout or "").splitlines()
                 if line.strip()]
        if not lines:
            return False
        try:
            payload = json.loads(lines[-1])
        except (TypeError, ValueError):
            return False
        return (
            isinstance(payload, dict)
            and payload.get("workspace_id") == workspace_id
            and payload.get("config_version") == config_version
            and payload.get("container_id") == container_name(workspace_id)
            and payload.get("ready") is True
            and payload.get("execution_capable") is True)

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
        if request.action == "identity":
            return {"ok": True, "identity": await self.identity()}
        if request.action == "ensure":
            handle = await self.ensure(
                request.workspace_id, request.config_version,
                protocol_version=request.version,
                attempt_id=request.attempt_id,
                orphan_takeover=request.orphan_takeover)
            payload = {
                "workspace_id": handle.workspace_id,
                "container_id": handle.container_id,
                "upstream": handle.upstream,
                "service_token": handle.service_token,
                "config_version": handle.config_version,
            }
            if request.version >= PROTOCOL_VERSION:
                payload.update({
                    "gateway_instance_id": handle.gateway_instance_id,
                    "gateway_generation": handle.gateway_generation,
                    "gateway_upstream": handle.gateway_upstream,
                    "gateway_state": handle.gateway_state,
                    "worker_created": handle.worker_created,
                    "gateway_created": handle.gateway_created,
                })
            return {"ok": True, "handle": payload}
        if request.action == "activate":
            status = await self.activate(
                request.workspace_id, request.worker_container_id,
                request.gateway_instance_id, request.boot_generation,
                request.config_version)
            return {"ok": True, **status}
        if request.action == "restore_gateway":
            handle = await self.restore_gateway(
                request.workspace_id, request.worker_container_id,
                request.config_version, attempt_id=request.attempt_id)
            return {"ok": True, "handle": {
                "workspace_id": handle.workspace_id,
                "container_id": handle.container_id,
                "upstream": handle.upstream,
                "service_token": handle.service_token,
                "config_version": handle.config_version,
                "gateway_instance_id": handle.gateway_instance_id,
                "gateway_generation": handle.gateway_generation,
                "gateway_upstream": handle.gateway_upstream,
                "gateway_state": handle.gateway_state,
                "worker_created": handle.worker_created,
                "gateway_created": handle.gateway_created,
            }}
        if request.action == "abort":
            await self.abort(
                request.workspace_id, request.worker_container_id,
                request.gateway_instance_id, attempt_id=request.attempt_id,
                scope=request.cleanup_scope,
                worker_created=request.worker_created,
                gateway_created=request.gateway_created)
            return {"ok": True}
        if request.action == "commit_attempt":
            resolved = await self.commit_attempt(
                request.workspace_id, request.worker_container_id,
                request.gateway_instance_id,
                attempt_id=request.attempt_id,
                resolve=request.resolve,
                config_version=request.config_version)
            if request.resolve:
                return {"ok": True, "resolved": resolved}
            return {"ok": True}
        if request.action == "recover_abort":
            await self.recover_abort(
                request.workspace_id, request.worker_container_id,
                request.gateway_instance_id, request.config_version,
                attempt_id=request.attempt_id,
                scope=request.cleanup_scope,
                worker_created=request.worker_created,
                gateway_created=request.gateway_created)
            return {"ok": True}
        if request.action == "stop":
            await self.stop(
                request.workspace_id, request.instance_id,
                request.gateway_instance_id,
                protocol_version=request.version)
            return {"ok": True}
        report = await self.inspect(
            request.workspace_id, protocol_version=request.version)
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
    parser.add_argument("--sidecar-network", default="",
                        help="sidecar 受控出站网络（已存在、非 internal）")
    parser.add_argument("--sidecar-policy", default="",
                        help="宿主侧管理员 egress policy JSON 路径")
    parser.add_argument("--broker-upstream", default="",
                        help="sidecar 转发的 control broker host:port")
    parser.add_argument("--worker-public-key", required=True,
                        help="管理员固定的 worker Ed25519 只读公钥文件")
    parser.add_argument("--gateway-image", default="",
                        help="固定摘要的 gateway 镜像；启用时必填")
    parser.add_argument("--gateway-control-network", default="",
                        help="gateway 管理监听所在的共享控制网络")
    parser.add_argument("--gateway-public-key", default="",
                        help="gateway 只读加载的 control Ed25519 公钥")
    parser.add_argument("--gateway-port", type=int, default=GATEWAY_PORT)
    parser.add_argument("--gateway-start-timeout", type=float, default=30.0)
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
        sidecar_network=args.sidecar_network,
        sidecar_policy=(Path(args.sidecar_policy)
                        if args.sidecar_policy else None),
        broker_upstream=args.broker_upstream,
        worker_public_key=Path(args.worker_public_key),
        gateway_image=args.gateway_image,
        gateway_control_network=args.gateway_control_network,
        gateway_public_key=(
            Path(args.gateway_public_key) if args.gateway_public_key else None),
        gateway_port=args.gateway_port,
        gateway_start_timeout=args.gateway_start_timeout,
    )
    try:
        provisioner = Provisioner(settings)
    except ProvisionerError as error:
        print(f"错误：{error.code}", file=sys.stderr)
        return 2
    try:
        asyncio.run(provisioner.serve())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
