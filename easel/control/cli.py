"""本地管理 CLI：管理员引导、邀请签发与 control 服务启动。

这些命令只操作显式传入的控制目录，不读取开发者 .env、HOME 或平台凭证；
管理员引导与邀请都不存在未认证的远程等价接口。
"""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..worker.sessions import (
    MESSAGE_FORMAT,
    MESSAGE_FORMAT_VERSION,
    SUPPORTED_RUNTIMES,
    WorkspaceSessionStore,
)
from .app import ControlConfig, create_app
from .auth import AuthService
from .broker import BrokerService
from .config import ConfigService, load_master_key
from .egress import EgressGateway, load_policy
from .gateway_client import GatewayClient
from .proxy import BrokerDestination, PinnedRunConfig, WorkerRelay
from .runtime import RuntimeManager, UnixProvisionerClient
from .snapshot import (
    WORKSPACE_SIDECAR_MODE,
    load_managed_agent_config,
    load_managed_profile_models,
    load_signing_key,
)
from .store import SCHEMA_VERSION, Store
from .types import DomainError

CONTROL_DB_NAME = "control.sqlite3"
_MANAGED_PROFILE_INSTALLED = Path("/opt/easel/managed/acp-profile.json")
_MANAGED_PROFILE_REPO = (Path(__file__).resolve().parents[2]
                         / "deploy" / "multiuser" / "managed"
                         / "acp-profile.json")


def _managed_profile_path(explicit: object) -> Path:
    """受管 manifest 只来自显式文件、部署 env 或镜像/源码固定位置。"""
    if explicit:
        if not isinstance(explicit, (str, Path)):
            raise ValueError("managed profile path is invalid")
        return Path(explicit)
    env = os.environ.get("EASEL_MANAGED_PROFILE")
    if env:
        return Path(env)
    for candidate in (_MANAGED_PROFILE_INSTALLED, _MANAGED_PROFILE_REPO):
        if candidate.is_file():
            return candidate
    raise OSError("managed profile is missing")


BACKUP_FORMAT = "easel.control.backup"
BACKUP_VERSION = 2
BACKUP_V2 = 2
_ACTIVE_LEASE_STATES = ("starting", "ready", "draining", "stopping")
_BACKUP_DEPLOYMENT_FIELDS = frozenset({
    "project", "origin", "port", "control_image", "worker_image",
    "gateway_image", "frontend_sha256", "config_sha256", "engine_id",
    "engine_context", "source_commit", "harness_revision", "workspace_root",
    "data_root", "egress_ca_sha256",
})
_BACKUP_SECRET_FIELDS = frozenset({
    "secret", "secrets", "token", "password", "credential", "credentials",
    "api_key", "apikey", "private_key", "authtoken",
})
_BACKUP_EXCLUDED_RUNTIME = (
    {"path": ".credentials",
     "reason": "per-deployment worker service tokens must be regenerated"},
    {"path": ".gateway",
     "reason": "per-deployment start attempt tombstones are not durable data"},
)

_SKIP_BACKUP_NAMES = frozenset({".credentials"})
"""只排除秘密材料；.git、node_modules 等持久数据不得从完整备份静默丢弃。"""
_SKIP_MIGRATION_NAMES = frozenset({
    ".credentials", "_sessions", "_harness", "_browser", "browser_profiles",
})
"""迁移只排除会话/秘密/浏览器状态；.git 与普通构建产物属于源内容。"""
_SKIP_MIGRATION_PATTERNS = (".env", ".env.*")
_SKIP_MIGRATION_BACKUP_NAMES = frozenset({
    ".credentials", "_harness", "_browser", "browser_profiles",
})
"""迁移前源备份只排除秘密/浏览器状态；.git 等持久历史必须保留。"""
_WORKSPACE_ID = re.compile(r"^[0-9a-f]{32}$")
_SESSION_DIGEST = re.compile(r"^[0-9a-f]{64}$")
MIGRATION_BACKUP_FORMAT = "easel.local.migration.backup"


def _control_db(control_dir: str) -> Path:
    return Path(control_dir) / CONTROL_DB_NAME


def backup_control(store: Store, destination: Path) -> Path:
    """为控制库生成可独立重开的一致性快照。"""
    return store.backup_to(destination)


def _absolute_path(value: str | Path, code: str) -> Path:
    """只接受绝对路径；解析现有符号链接/junction，防止计算目标逃逸。"""
    raw = str(value)
    if (not raw or any(character in raw for character in ("\0", "\n", "\r"))
            or raw.startswith("~")):
        raise DomainError(code, 400)
    path = Path(raw)
    if not path.is_absolute():
        raise DomainError(code, 400)
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        raise DomainError(code, 400) from None


def _managed_root(control_dir: Path, workspace_root: Path | None) -> Path:
    """取控制/数据根的最近公共父目录，作为备份写目标的受管边界。"""
    if workspace_root is None:
        common = control_dir.parent
    else:
        try:
            common = Path(os.path.commonpath(
                [str(control_dir), str(workspace_root)]))
        except ValueError:
            raise DomainError("backup_destination_escape", 400) from None
    if common == Path(common.anchor):
        # 两个根不在同一受管目录下时，不允许在盘符/文件系统根随意落盘
        raise DomainError("backup_destination_escape", 400)
    return common


def _validate_backup_destination(destination: Path, *, control_dir: Path,
                                 workspace_root: Path | None) -> None:
    if not destination.is_absolute():
        raise DomainError("backup_destination_invalid", 400)
    root = _managed_root(control_dir, workspace_root)
    if destination == root or not destination.is_relative_to(root):
        raise DomainError("backup_destination_escape", 400)
    if destination.is_relative_to(control_dir):
        raise DomainError("backup_destination_inside_control", 400)
    if workspace_root is not None and destination.is_relative_to(workspace_root):
        raise DomainError("backup_destination_inside_data", 400)
    if destination.exists():
        raise DomainError("backup_exists", 409)


def _load_backup_key(path: Path) -> bytes:
    try:
        return load_master_key(path)
    except OSError:
        raise DomainError("master_key_unavailable", 400) from None
    except ValueError:
        raise DomainError("invalid_master_key", 400) from None


def _private_copy(path: Path) -> tuple[tempfile.TemporaryDirectory, Path]:
    """把待校验秘密复制到 0600 临时文件，兼容 Windows bind 的宽 POSIX mode。"""
    directory = tempfile.TemporaryDirectory(prefix="easel-backup-check-")
    target = Path(directory.name) / path.name
    try:
        shutil.copyfile(path, target)
        if os.name == "posix":
            target.chmod(0o600)
    except OSError:
        directory.cleanup()
        raise DomainError("backup_integrity_failed", 409) from None
    return directory, target


def _load_backup_master_key(path: Path) -> bytes:
    directory, target = _private_copy(path)
    try:
        return _load_backup_key(target)
    finally:
        directory.cleanup()


def _load_backup_signing_key(path: Path):
    directory, target = _private_copy(path)
    try:
        return load_signing_key(target)
    except (OSError, ValueError):
        raise DomainError("backup_integrity_failed", 409) from None
    finally:
        directory.cleanup()


def _load_backup_managed(path: Path):
    directory, target = _private_copy(path)
    try:
        return load_managed_agent_config(target)
    except (OSError, ValueError):
        raise DomainError("backup_integrity_failed", 409) from None
    finally:
        directory.cleanup()


def _load_backup_policy(path: Path):
    directory, target = _private_copy(path)
    try:
        return load_policy(target)
    except (OSError, ValueError):
        raise DomainError("backup_integrity_failed", 409) from None
    finally:
        directory.cleanup()


def _key_matches(store: Store, key: bytes) -> None:
    """已有密文必须能用本 key 解开；失败只返回稳定错误码，不回显秘密。"""
    try:
        ConfigService(store, key)
    except ValueError:
        raise DomainError("master_key_mismatch", 409) from None


def _active_leases(store: Store) -> list:
    return [lease for lease in store.list_worker_leases()
            if lease.state in _ACTIVE_LEASE_STATES]


def _drain_before_backup(store: Store,
                         drain: Callable[[], object] | None) -> None:
    active = _active_leases(store)
    if not active:
        return
    if drain is None:
        raise DomainError("worker_drain_required", 409)
    try:
        drain()
    except DomainError:
        raise
    except Exception:  # noqa: BLE001 - 外部 provisioner/事件循环失败统一失败关闭
        raise DomainError("worker_drain_failed", 503) from None
    if _active_leases(store):
        raise DomainError("worker_drain_incomplete", 503)


def _is_reparse(path: Path) -> bool:
    """符号链接、Windows junction 与其他重解析点一律视为不可跟随。

    路径不存在时返回 ``False``，由调用方按“缺失”处理；不能把不存在误判为
    不安全的现有链接。
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return bool(path.is_symlink() or getattr(info, "st_reparse_tag", 0))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json_object(path: Path, code: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise DomainError(code, 400) from None
    if not isinstance(payload, dict):
        raise DomainError(code, 400)
    return payload


def _reject_secret_fields(value: object, *, prefix: str = "") -> None:
    """拒绝部署元数据里出现疑似秘密字段；值本身不打印。"""
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key).lower().replace("-", "_")
            if name in _BACKUP_SECRET_FIELDS:
                raise DomainError("backup_metadata_contains_secret", 400)
            _reject_secret_fields(item, prefix=f"{prefix}{key}.")
    elif isinstance(value, list):
        for item in value:
            _reject_secret_fields(item, prefix=prefix)


def _validate_deployment_metadata(deployment: object) -> dict:
    """校验并复制非秘密部署身份；不接受未知字段或疑似凭证字段。"""
    if not isinstance(deployment, dict):
        raise DomainError("backup_deployment_metadata_invalid", 400)
    _reject_secret_fields(deployment)
    unknown = set(deployment) - _BACKUP_DEPLOYMENT_FIELDS
    if unknown:
        raise DomainError("backup_deployment_metadata_invalid", 400)
    required = {
        "project", "origin", "port", "control_image", "worker_image",
        "gateway_image", "frontend_sha256", "config_sha256", "engine_id",
        "engine_context", "source_commit", "harness_revision",
    }
    if not required.issubset(deployment):
        raise DomainError("backup_deployment_metadata_invalid", 400)
    project = deployment.get("project")
    origin = deployment.get("origin")
    port = deployment.get("port")
    if (not isinstance(project, str) or not project
            or not isinstance(origin, str) or not origin
            or type(port) is not int or not 1 <= port <= 65535):
        raise DomainError("backup_deployment_metadata_invalid", 400)
    for key in ("control_image", "worker_image", "gateway_image"):
        value = deployment.get(key)
        if (not isinstance(value, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None):
            raise DomainError("backup_deployment_metadata_invalid", 400)
    for key in ("frontend_sha256", "config_sha256", "egress_ca_sha256"):
        value = deployment.get(key)
        if value is None and key == "egress_ca_sha256":
            continue
        if (not isinstance(value, str)
                or re.fullmatch(r"[0-9a-f]{64}", value) is None):
            raise DomainError("backup_deployment_metadata_invalid", 400)
    if (not isinstance(deployment.get("engine_id"), str)
            or not deployment["engine_id"]
            or not isinstance(deployment.get("engine_context"), str)
            or not deployment["engine_context"]
            or not isinstance(deployment.get("source_commit"), str)
            or not deployment["source_commit"]
            or not isinstance(deployment.get("harness_revision"), str)):
        raise DomainError("backup_deployment_metadata_invalid", 400)
    for key in ("workspace_root", "data_root"):
        value = deployment.get(key)
        if value is not None and not isinstance(value, str):
            raise DomainError("backup_deployment_metadata_invalid", 400)
    return dict(deployment)


def _signing_public_derived(
        private: Ed25519PrivateKey) -> tuple[bytes, bytes]:
    public = private.public_key()
    return (
        public.public_bytes_raw(),
        public.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo),
    )


def _validate_backup_signing_material(
        source: Path, manifest: dict) -> None:
    """校验 v2 备份的签名私钥、派生公钥、受管配置与出口策略。"""
    identity = manifest.get("identity")
    if not isinstance(identity, dict):
        raise DomainError("backup_integrity_failed", 409)
    signing = source / "snapshot.pem"
    worker = source / "worker-public.key"
    gateway = source / "gateway-public.pem"
    managed = source / "managed-agent.json"
    policy = source / "egress-policy.json"
    try:
        key = _load_backup_signing_key(signing)
    except DomainError:
        raise
    raw, pem = _signing_public_derived(key)
    try:
        if (not worker.is_file() or worker.read_bytes() != raw
                or not gateway.is_file() or gateway.read_bytes() != pem):
            raise DomainError("backup_integrity_failed", 409)
    except OSError:
        raise DomainError("backup_integrity_failed", 409) from None
    for path in (signing, worker, gateway):
        if _is_reparse(path) or not path.is_file():
            raise DomainError("backup_integrity_failed", 409)
    try:
        _load_backup_managed(managed)
        _load_backup_policy(policy)
    except DomainError:
        raise
    for name, expected in (
            ("signing_key", identity.get("signing_key")),
            ("worker_public", identity.get("worker_public")),
            ("gateway_public", identity.get("gateway_public")),
            ("managed_config", manifest.get("managed_config")),
            ("egress_policy", manifest.get("egress_policy"))):
        if not isinstance(expected, dict):
            raise DomainError("backup_integrity_failed", 409)
        path = source.joinpath(*_manifest_path(expected.get("path")).parts)
        if (_is_reparse(path) or not path.is_file()
                or path.stat().st_size != expected.get("bytes")
                or _sha256_file(path) != expected.get("sha256")):
            raise DomainError("backup_integrity_failed", 409)
    ca = manifest.get("egress_ca")
    if ca is not None:
        if not isinstance(ca, dict):
            raise DomainError("backup_integrity_failed", 409)
        path = source.joinpath(*_manifest_path(ca.get("path")).parts)
        if (_is_reparse(path) or not path.is_file()
                or path.stat().st_size != ca.get("bytes")
                or _sha256_file(path) != ca.get("sha256")):
            raise DomainError("backup_integrity_failed", 409)


def _clean_stop_view(store: Store) -> tuple[bool, dict | None]:
    barrier = store.admission_barrier()
    completion = store.stop_completion()
    if (barrier is None or completion is None
            or completion.get("barrierId") != barrier.get("id")):
        return False, None
    return True, completion


def _copy_file(source: Path, destination: Path) -> dict:
    """复制单个普通文件并返回完整性元数据；不保留可执行位。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(source, destination)
        if os.name == "posix":
            # 保留普通持久文件的读/写/执行位；清除 setuid/setgid/sticky，
            # 避免恢复后把宿主特权位带进用户工作区。
            mode = source.stat().st_mode & 0o777
            destination.chmod(mode)
        return {
            "path": destination,
            "bytes": destination.stat().st_size,
            "sha256": _sha256_file(destination),
        }
    except OSError:
        raise DomainError("backup_write_failed", 500) from None


def _copy_tree(source: Path, destination: Path, *, prefix: str,
               skip_names: frozenset[str],
               skip_patterns: tuple[str, ...] = (),
               rejected: list[str] | None = None) -> list[dict]:
    """复制目录树；遇到符号链接/junction 时严格模式失败，迁移模式只报告。

    ``rejected`` 非空表示迁移预检：跳过重解析点等不可信条目并在报告中列出，
    绝不跟随到源根之外。严格模式（备份）遇到任何非普通文件都失败关闭。
    """
    files: list[dict] = []
    if _is_reparse(source):
        if rejected is None:
            raise DomainError("backup_symlink_rejected", 409)
        rejected.append(".: symlink_or_junction")
        return files

    def visit(directory: Path, relative: PurePosixPath) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError:
            raise DomainError("backup_read_failed", 500) from None
        for entry in entries:
            name = entry.name
            if name in skip_names or any(
                    fnmatch.fnmatch(name, pattern)
                    for pattern in skip_patterns):
                continue
            child_relative = relative / name
            shown = child_relative.as_posix()
            path = Path(entry.path)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                raise DomainError("backup_read_failed", 500) from None
            if path.is_symlink() or getattr(info, "st_reparse_tag", 0):
                if rejected is None:
                    raise DomainError("backup_symlink_rejected", 409)
                rejected.append(f"{shown}: symlink_or_junction")
                continue
            if entry.is_dir(follow_symlinks=False):
                visit(path, child_relative)
                continue
            if not entry.is_file(follow_symlinks=False):
                if rejected is None:
                    raise DomainError("backup_special_file_rejected", 409)
                rejected.append(f"{shown}: unsupported_file")
                continue
            copied = _copy_file(path, destination.joinpath(*child_relative.parts))
            files.append({
                "path": f"{prefix}/{shown}" if prefix else shown,
                "bytes": copied["bytes"],
                "sha256": copied["sha256"],
            })

    visit(source, PurePosixPath())
    return files


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "posix":
            path.chmod(0o600)
    except OSError:
        raise DomainError("backup_write_failed", 500) from None


def _new_staging(parent: Path, prefix: str) -> Path:
    try:
        parent.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(dir=parent, prefix=prefix))
    except OSError:
        raise DomainError("backup_write_failed", 500) from None


def _replace_directory(staging: Path, destination: Path) -> None:
    """目标必须新/空；先移除空目录，再用同卷 rename 提交完整快照。"""
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if any(destination.iterdir()):
                raise DomainError("backup_exists", 409)
            destination.rmdir()
        os.replace(staging, destination)
    except DomainError:
        raise
    except OSError:
        raise DomainError("backup_write_failed", 500) from None


def _snapshot_metadata(path: Path) -> tuple[list[str], dict[str, int], int]:
    """从备份快照读取 workspace/config 版本，避免使用快照之后的新写入。"""
    from sqlite3 import Row, connect

    connection = connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        connection.row_factory = Row
        store = Store(connection, path)
        latest = store.latest_managed_revision()
        return (
            store.list_workspace_ids(),
            store.workspace_config_versions(),
            int(latest["version"]) if latest else 0,
        )
    finally:
        connection.close()


def create_backup(store: Store, destination: Path, *, control_dir: Path,
                  master_key_file: Path, workspace_root: Path | None = None,
                  image_digest: str = "", harness_revision: str = "",
                  drain: Callable[[], object] | None = None,
                  signing_key_file: Path | None = None,
                  managed_config_file: Path | None = None,
                  egress_policy_file: Path | None = None,
                  egress_ca_file: Path | None = None,
                  deployment: dict | None = None,
                  require_clean_stop: bool = False) -> Path:
    """备份控制 SQLite、master key、签名材料与各 workspace 持久区。

    v2（显式给出签名私钥/受管配置/出口策略时）额外备份 Ed25519 私钥与
    派生公钥、受管 Agent 配置、出口策略和部署身份；v1 只备份控制库与
    master key，保持旧格式可恢复但不含完整 Desktop 材料。

    worker 仍在活动状态时，调用方必须提供 ``drain``；drain 返回后若租约仍
    未进入终态则失败关闭。所有内容先写同卷临时目录，校验完成后整体 rename，
    目标已存在或位于受管根之外时拒绝。
    """
    control_dir = _absolute_path(control_dir, "backup_control_dir_invalid")
    key_file = _absolute_path(master_key_file, "master_key_unavailable")
    destination = _absolute_path(destination, "backup_destination_invalid")
    data_root = (None if workspace_root is None else
                 _absolute_path(workspace_root, "backup_workspace_root_invalid"))
    if not control_dir.is_dir():
        raise DomainError("backup_control_dir_invalid", 400)
    if data_root is not None and not data_root.is_dir():
        raise DomainError("backup_workspace_root_invalid", 400)
    if data_root is not None and (
            data_root == control_dir
            or data_root.is_relative_to(control_dir)
            or control_dir.is_relative_to(data_root)):
        raise DomainError("backup_roots_overlap", 400)
    expected_db = control_dir / CONTROL_DB_NAME
    try:
        store_path = store.path.resolve()
    except OSError:
        raise DomainError("backup_control_dir_invalid", 400) from None
    if store_path != expected_db.resolve():
        raise DomainError("backup_control_mismatch", 400)

    _validate_backup_destination(
        destination, control_dir=control_dir, workspace_root=data_root)
    key = _load_backup_key(key_file)
    _key_matches(store, key)

    material_paths = (signing_key_file, managed_config_file,
                      egress_policy_file, egress_ca_file)
    full = any(path is not None for path in material_paths)
    if full:
        if data_root is None:
            raise DomainError("backup_workspace_root_missing", 409)
        # 同一宿主命名空间内，部署登记根必须与实参一致；容器内登记的
        # Windows 宿主路径通常不存在，此时由 PowerShell 入口负责绑定。
        for field, actual in (("workspace_root", data_root),):
            registered = (deployment.get(field)
                          if isinstance(deployment, dict) else None)
            if isinstance(registered, str) and registered:
                try:
                    registered_path = Path(registered).resolve()
                except (OSError, RuntimeError):
                    raise DomainError(
                        "backup_workspace_root_mismatch", 409) from None
                if registered_path.is_dir():
                    try:
                        actual_path = actual.resolve()
                    except (OSError, RuntimeError):
                        raise DomainError(
                            "backup_workspace_root_mismatch", 409) from None
                    if (os.path.normcase(str(registered_path))
                            != os.path.normcase(str(actual_path))):
                        raise DomainError(
                            "backup_workspace_root_mismatch", 409)
        if signing_key_file is None:
            raise DomainError("backup_signing_key_missing", 409)
        if managed_config_file is None:
            raise DomainError("backup_managed_config_missing", 409)
        if egress_policy_file is None:
            raise DomainError("backup_egress_policy_missing", 409)
        if deployment is None:
            raise DomainError("backup_materials_incomplete", 400)
    elif require_clean_stop or deployment is not None:
        raise DomainError("backup_materials_incomplete", 400)
    if full:
        clean, completion = _clean_stop_view(store)
        if require_clean_stop and not clean:
            raise DomainError("backup_requires_clean_stop", 409)
        deployment_meta = _validate_deployment_metadata(deployment)
        signing_path = _absolute_path(
            signing_key_file, "backup_signing_key_missing")
        if not signing_path.is_file() or _is_reparse(signing_path):
            raise DomainError("backup_signing_key_missing", 409)
        try:
            signing_key = load_signing_key(signing_path)
        except (OSError, ValueError):
            raise DomainError("backup_signing_key_invalid", 409) from None
        managed_path = _absolute_path(
            managed_config_file, "backup_managed_config_missing")
        if not managed_path.is_file() or _is_reparse(managed_path):
            raise DomainError("backup_managed_config_missing", 409)
        try:
            load_managed_agent_config(managed_path)
        except (OSError, ValueError):
            raise DomainError("backup_managed_config_invalid", 409) from None
        policy_path = _absolute_path(
            egress_policy_file, "backup_egress_policy_missing")
        if not policy_path.is_file() or _is_reparse(policy_path):
            raise DomainError("backup_egress_policy_missing", 409)
        try:
            load_policy(policy_path)
        except (OSError, ValueError):
            raise DomainError("backup_egress_policy_invalid", 409) from None
        if egress_ca_file is not None:
            ca_path = _absolute_path(
                egress_ca_file, "backup_egress_ca_missing")
            if not ca_path.is_file() or _is_reparse(ca_path):
                raise DomainError("backup_egress_ca_missing", 409)
        else:
            ca_path = None
    else:
        signing_path = managed_path = policy_path = ca_path = None
        signing_key = None
        completion = None
        clean = False
        deployment_meta = None

    _drain_before_backup(store, drain)
    if not store.drain_status(int(time.time()))["settled"]:
        raise DomainError("worker_drain_incomplete", 503)

    staging = _new_staging(
        destination.parent, f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        if os.name == "posix":
            staging.chmod(0o700)
        control_target = staging / "control" / CONTROL_DB_NAME
        backup_control(store, control_target)
        workspace_ids, workspace_versions, config_version = _snapshot_metadata(
            control_target)
        key_target = staging / "master.key"
        try:
            key_target.write_bytes(key)
            if os.name == "posix":
                key_target.chmod(0o600)
        except OSError:
            raise DomainError("backup_write_failed", 500) from None

        files = [
            {"path": f"control/{CONTROL_DB_NAME}",
             "bytes": control_target.stat().st_size,
             "sha256": _sha256_file(control_target)},
            {"path": "master.key", "bytes": len(key),
             "sha256": hashlib.sha256(key).hexdigest()},
        ]
        identity: dict | None = None
        if full:
            raw_public, pem_public = _signing_public_derived(signing_key)
            try:
                (staging / "snapshot.pem").write_bytes(
                    signing_path.read_bytes())
                (staging / "managed-agent.json").write_bytes(
                    managed_path.read_bytes())
                (staging / "egress-policy.json").write_bytes(
                    policy_path.read_bytes())
                (staging / "worker-public.key").write_bytes(raw_public)
                (staging / "gateway-public.pem").write_bytes(pem_public)
                if os.name == "posix":
                    for name in ("snapshot.pem", "managed-agent.json",
                                 "egress-policy.json"):
                        (staging / name).chmod(0o600)
                    for name in ("worker-public.key", "gateway-public.pem"):
                        (staging / name).chmod(0o644)
            except OSError:
                raise DomainError("backup_write_failed", 500) from None
            material_entries: dict[str, dict] = {}
            for name in ("snapshot.pem", "managed-agent.json",
                         "egress-policy.json", "worker-public.key",
                         "gateway-public.pem"):
                path = staging / name
                material_entries[name] = {
                    "path": name,
                    "bytes": path.stat().st_size,
                    "sha256": _sha256_file(path),
                }
            if ca_path is not None:
                try:
                    (staging / "egress-ca.pem").write_bytes(
                        ca_path.read_bytes())
                    if os.name == "posix":
                        (staging / "egress-ca.pem").chmod(0o600)
                except OSError:
                    raise DomainError("backup_write_failed", 500) from None
                material_entries["egress-ca.pem"] = {
                    "path": "egress-ca.pem",
                    "bytes": (staging / "egress-ca.pem").stat().st_size,
                    "sha256": _sha256_file(staging / "egress-ca.pem"),
                }
            files.extend(material_entries.values())
            identity = {
                "master_key": files[1],
                "signing_key": material_entries["snapshot.pem"],
                "worker_public": material_entries["worker-public.key"],
                "gateway_public": material_entries["gateway-public.pem"],
            }
            if ca_path is not None:
                identity["egress_ca"] = material_entries["egress-ca.pem"]
        workspace_entries: dict[str, dict] = {}
        for workspace_id in workspace_ids:
            # 数据库值不直接拼进宿主路径；损坏/伪造 ID 不能逃出备份临时目录
            if _WORKSPACE_ID.fullmatch(workspace_id) is None:
                raise DomainError("backup_workspace_id_invalid", 500)
            owner = data_root / workspace_id if data_root is not None else None
            if owner is not None:
                if _is_reparse(owner):
                    # 即使链接目标当前缺失，也不能当作“无数据”静默跳过
                    raise DomainError("backup_workspace_path_escape", 409)
                if owner.exists():
                    if not owner.is_dir():
                        raise DomainError(
                            "backup_workspace_path_escape", 409)
                    try:
                        owner_resolved = owner.resolve()
                    except (OSError, RuntimeError):
                        raise DomainError(
                            "backup_workspace_path_escape", 409) from None
                    if (os.path.normcase(str(owner_resolved))
                            != os.path.normcase(str(owner))):
                        # 祖先链接/junction 指向根外或另一 workspace
                        raise DomainError("backup_workspace_path_escape", 409)
            entry: dict[str, bool] = {}
            if data_root is not None:
                for part in ("workspace", "home"):
                    source = data_root / workspace_id / part
                    if _is_reparse(source):
                        # 包括目标缺失的链接；不能按“无数据”静默略过
                        raise DomainError("backup_symlink_rejected", 409)
                    if source.exists() and not source.is_dir():
                        raise DomainError(
                            "backup_workspace_path_escape", 409)
                    if not source.is_dir():
                        if full and store.workspace_has_history(workspace_id):
                            # 已有 Run 历史说明该 workspace 曾物化；根缺失
                            # 不是“新空间”，不能静默备成 complete 空目录。
                            raise DomainError(
                                "backup_workspace_missing", 409)
                        entry[part] = False
                        continue
                    files.extend(_copy_tree(
                        source,
                        staging / "workspaces" / workspace_id / part,
                        prefix=f"workspaces/{workspace_id}/{part}",
                        skip_names=_SKIP_BACKUP_NAMES))
                    entry[part] = True
                state = data_root / workspace_id / "workspace" / "state"
                entry["state"] = bool(
                    entry.get("workspace") and state.is_dir()
                    and not _is_reparse(state))
            workspace_entries[workspace_id] = entry

        manifest = {
            "format": BACKUP_FORMAT,
            "version": BACKUP_V2 if full else 1,
            "created_at": int(time.time()),
            "schema_version": SCHEMA_VERSION,
            "config_version": config_version,
            "workspace_config_versions": workspace_versions,
            "workspaces": workspace_entries,
            "image": str(image_digest),
            "harness_revision": str(harness_revision),
            "control": {"path": f"control/{CONTROL_DB_NAME}",
                        "sha256": files[0]["sha256"],
                        "bytes": files[0]["bytes"]},
            "master_key": {"path": "master.key",
                           "sha256": files[1]["sha256"],
                           "bytes": files[1]["bytes"]},
            "files": sorted(files, key=lambda item: item["path"]),
        }
        if full:
            manifest.update({
                "complete": True,
                "deployment": deployment_meta,
                "identity": identity,
                "managed_config": {
                    "path": "managed-agent.json",
                    "sha256": next(item["sha256"] for item in files
                                   if item["path"] == "managed-agent.json"),
                    "bytes": next(item["bytes"] for item in files
                                  if item["path"] == "managed-agent.json"),
                },
                "egress_policy": {
                    "path": "egress-policy.json",
                    "sha256": next(item["sha256"] for item in files
                                   if item["path"] == "egress-policy.json"),
                    "bytes": next(item["bytes"] for item in files
                                  if item["path"] == "egress-policy.json"),
                },
                **({"egress_ca": {
                    "path": "egress-ca.pem",
                    "sha256": identity["egress_ca"]["sha256"],
                    "bytes": identity["egress_ca"]["bytes"],
                }} if ca_path is not None else {}),
                "runtime_state": {
                    "clean_stop": bool(clean),
                    "stop": dict(completion) if completion else None,
                    "excluded": list(_BACKUP_EXCLUDED_RUNTIME),
                },
            })
        _write_json(staging / "manifest.json", manifest)
        if os.name == "posix":
            (staging / "manifest.json").chmod(0o600)
        _replace_directory(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return destination


def _report(error: DomainError) -> int:
    """只输出错误码，不回显令牌或内部细节。"""
    print(f"错误：{error.code}", file=sys.stderr)
    return 1


def build_relay_factory(*, signing_key, revision: int, agent: dict,
                        broker_config: Mapping[str, str] | None = None,
                        broker_destination: BrokerDestination | None = None,
                        gateway_required: bool = False):
    """正式 control 装配：接纳后为每个 Run 签发最小 broker token。

    该函数被 CLI 与装配测试共同使用；不得在测试里用自造 provider 另起
    一条与生产不同的签发路径。

    ``broker_config`` 为受管配置原始目的地标签：固定 URL 兼容模式或
    ``workspace-sidecar``。sidecar URL 只从认证后重查的 workspace UUID
    派生，绝不接受请求体/客户端地址；``broker_destination`` 仅保留给旧
    调用方的固定目的地。
    """
    if broker_config is not None and broker_destination is not None:
        raise ValueError("broker configuration is ambiguous")
    if broker_config is None and broker_destination is None:
        raise ValueError("broker configuration is required")
    if gateway_required and not isinstance(signing_key, Ed25519PrivateKey):
        raise TypeError("gateway_required needs an Ed25519 signing key")
    if broker_config is not None:
        mode = broker_config.get("mode")
        channel = broker_config.get("channel")
        url = broker_config.get("url")
        if mode == WORKSPACE_SIDECAR_MODE:
            if set(broker_config) != {"mode", "channel"}:
                raise ValueError("workspace-sidecar broker is invalid")
        elif mode is None and set(broker_config) == {"url", "channel"}:
            pass
        else:
            raise ValueError("managed broker configuration is invalid")
        if not isinstance(channel, str) or not channel:
            raise ValueError("managed broker channel is invalid")

        def destination_for(actor) -> BrokerDestination:
            if mode == WORKSPACE_SIDECAR_MODE:
                workspace_id = getattr(actor, "workspace_id", "")
                if (not isinstance(workspace_id, str)
                        or _WORKSPACE_ID.fullmatch(workspace_id) is None):
                    raise DomainError("invalid_workspace", 500)
                return BrokerDestination(
                    f"http://easel-sidecar-{workspace_id}:8899", channel)
            if not isinstance(url, str):
                raise TypeError("managed broker url is invalid")
            return BrokerDestination(url, channel)
    else:
        def destination_for(_actor):
            return broker_destination

    def pinned_config(actor):
        return PinnedRunConfig(
            revision=revision, routes={}, agent=agent,
            broker=destination_for(actor))

    def relay_factory(runtime, sessions, auth, store, http_client,
                      broker, db_runner):
        gateway_client = None
        if gateway_required:
            gateway_client = GatewayClient(signing_key, http_client)
        return WorkerRelay(
            runtime, sessions, auth, store=store,
            signing_key=signing_key, config_provider=pinned_config,
            http_client=http_client, broker=broker,
            db_runner=db_runner, gateway_client=gateway_client,
            gateway_required=gateway_required)

    return relay_factory


def _manifest_path(value: object) -> PurePosixPath:
    """清单路径只能是相对 POSIX 路径，禁止绝对路径、盘符和 .. 逃逸。"""
    if not isinstance(value, str) or not value or "\\" in value:
        raise DomainError("backup_invalid", 400)
    relative = PurePosixPath(value)
    if (relative.is_absolute() or not relative.parts
            or any(part in ("", ".", "..") for part in relative.parts)
            or ":" in relative.parts[0]):
        raise DomainError("backup_invalid", 400)
    return relative


def _read_manifest(source: Path) -> dict:
    path = source / "manifest.json"
    if not path.is_file():
        raise DomainError("backup_invalid", 400)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DomainError("backup_invalid", 400) from None
    if not isinstance(manifest, dict):
        raise DomainError("backup_invalid", 400)
    if manifest.get("format") != BACKUP_FORMAT:
        raise DomainError("backup_invalid", 400)
    version = manifest.get("version")
    if type(version) is not int or version < 1:
        raise DomainError("backup_invalid", 400)
    if version > BACKUP_VERSION:
        raise DomainError("backup_incompatible", 409)
    schema_version = manifest.get("schema_version")
    config_version = manifest.get("config_version")
    if (type(schema_version) is not int or schema_version < 1
            or type(config_version) is not int or config_version < 0
            or not isinstance(manifest.get("image"), str)
            or not isinstance(manifest.get("harness_revision"), str)
            or not isinstance(manifest.get("workspace_config_versions"), dict)):
        raise DomainError("backup_invalid", 400)
    if schema_version > SCHEMA_VERSION:
        raise DomainError("backup_incompatible", 409)
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise DomainError("backup_invalid", 400)
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise DomainError("backup_invalid", 400)
        relative = _manifest_path(entry.get("path"))
        shown = relative.as_posix()
        if shown == "manifest.json" or shown in seen:
            raise DomainError("backup_invalid", 400)
        if (type(entry.get("bytes")) is not int or entry["bytes"] < 0
                or not isinstance(entry.get("sha256"), str)
                or len(entry["sha256"]) != 64):
            raise DomainError("backup_invalid", 400)
        seen.add(shown)
    by_path = {entry["path"]: entry for entry in files}
    control_path = f"control/{CONTROL_DB_NAME}"
    control = manifest.get("control")
    master = manifest.get("master_key")
    if (not isinstance(control, dict)
            or control.get("path") != control_path
            or not isinstance(master, dict)
            or master.get("path") != "master.key"):
        raise DomainError("backup_invalid", 400)
    for key in ("control", "master_key"):
        pointer = manifest.get(key)
        if not isinstance(pointer, dict):
            raise DomainError("backup_invalid", 400)
        entry = by_path.get(pointer.get("path"))
        if (entry is None or pointer.get("sha256") != entry.get("sha256")
                or pointer.get("bytes") != entry.get("bytes")):
            raise DomainError("backup_invalid", 400)
    if version >= BACKUP_V2:
        if manifest.get("complete") is not True:
            raise DomainError("backup_invalid", 400)
        identity = manifest.get("identity")
        runtime_state = manifest.get("runtime_state")
        if not isinstance(identity, dict) or not isinstance(runtime_state, dict):
            raise DomainError("backup_invalid", 400)
        required = {
            "signing_key": "snapshot.pem",
            "worker_public": "worker-public.key",
            "gateway_public": "gateway-public.pem",
        }
        for key, name in required.items():
            pointer = identity.get(key)
            entry = by_path.get(name)
            if (not isinstance(pointer, dict) or pointer.get("path") != name
                    or entry is None
                    or pointer.get("sha256") != entry.get("sha256")
                    or pointer.get("bytes") != entry.get("bytes")):
                raise DomainError("backup_invalid", 400)
        for key, name in (
                ("managed_config", "managed-agent.json"),
                ("egress_policy", "egress-policy.json")):
            pointer = manifest.get(key)
            entry = by_path.get(name)
            if (not isinstance(pointer, dict) or pointer.get("path") != name
                    or entry is None
                    or pointer.get("sha256") != entry.get("sha256")
                    or pointer.get("bytes") != entry.get("bytes")):
                raise DomainError("backup_invalid", 400)
        if manifest.get("egress_ca") is not None:
            pointer = manifest.get("egress_ca")
            entry = by_path.get("egress-ca.pem")
            if (not isinstance(pointer, dict)
                    or pointer.get("path") != "egress-ca.pem"
                    or entry is None
                    or pointer.get("sha256") != entry.get("sha256")
                    or pointer.get("bytes") != entry.get("bytes")):
                raise DomainError("backup_invalid", 400)
        _validate_deployment_metadata(manifest.get("deployment"))
        excluded = runtime_state.get("excluded")
        if (not isinstance(excluded, list)
                or not all(isinstance(item, dict)
                           and isinstance(item.get("path"), str)
                           and isinstance(item.get("reason"), str)
                           for item in excluded)
                or type(runtime_state.get("clean_stop")) is not bool):
            raise DomainError("backup_invalid", 400)
        prepared = manifest.get("prepared")
        if prepared is not None:
            if (not isinstance(prepared, dict)
                    or not isinstance(prepared.get("source_manifest_sha256"), str)
                    or re.fullmatch(r"[0-9a-f]{64}",
                                    prepared["source_manifest_sha256"]) is None
                    or type(prepared.get("restored_at")) is not int
                    or not isinstance(prepared.get("cleared_barrier"), str)
                    or not isinstance(prepared.get("admin_user_id"), str)
                    or not isinstance(prepared.get("control"), dict)
                    or prepared["control"].get("path") != control_path
                    or prepared["control"].get("sha256")
                    != control.get("sha256")
                    or prepared["control"].get("bytes")
                    != control.get("bytes")):
                raise DomainError("backup_invalid", 400)
    return manifest


def _validate_bundle_tree(source: Path, manifest: dict) -> dict[str, dict]:
    """拒绝清单外的额外文件、符号链接和 junction；返回按路径索引的清单。"""
    expected = {entry["path"]: entry for entry in manifest["files"]}
    actual: set[str] = set()
    for directory, names, files in os.walk(source, followlinks=False):
        base = Path(directory)
        for name in names:
            path = base / name
            if _is_reparse(path):
                raise DomainError("backup_integrity_failed", 409)
        for name in files:
            path = base / name
            if _is_reparse(path):
                raise DomainError("backup_integrity_failed", 409)
            try:
                path.lstat()
            except OSError:
                raise DomainError("backup_integrity_failed", 409) from None
            relative = path.relative_to(source).as_posix()
            actual.add(relative)
    if actual != set(expected) | {"manifest.json"}:
        raise DomainError("backup_integrity_failed", 409)
    return expected


def _verify_bundle_digests(source: Path,
                           expected: dict[str, dict]) -> None:
    """逐项核对清单文件的大小与 SHA-256；只读，不跟随链接。"""
    for shown, entry in expected.items():
        relative = _manifest_path(shown)
        path = source.joinpath(*relative.parts)
        try:
            if _is_reparse(path) or not path.is_file():
                raise DomainError("backup_integrity_failed", 409)
            if (path.stat().st_size != entry["bytes"]
                    or _sha256_file(path) != entry["sha256"]):
                raise DomainError("backup_integrity_failed", 409)
        except DomainError:
            raise
        except OSError:
            raise DomainError("backup_integrity_failed", 409) from None


def _verify_and_copy(source: Path, staging: Path,
                     expected: dict[str, dict]) -> None:
    for shown, entry in expected.items():
        relative = _manifest_path(shown)
        path = source.joinpath(*relative.parts)
        try:
            if _is_reparse(path) or not path.is_file():
                raise DomainError("backup_integrity_failed", 409)
            size = path.stat().st_size
            if size != entry["bytes"] or _sha256_file(path) != entry["sha256"]:
                raise DomainError("backup_integrity_failed", 409)
            copied = _copy_file(path, staging.joinpath(*relative.parts))
        except DomainError:
            raise
        except OSError:
            raise DomainError("backup_integrity_failed", 409) from None
        if (copied["sha256"] != entry["sha256"]
                or copied["bytes"] != entry["bytes"]):
            raise DomainError("backup_integrity_failed", 409)


def _copy_verified_file(source: Path, destination: Path,
                        entry: dict) -> None:
    """复制清单文件并同时校验源与目标实际字节，不能只信一次复制成功。"""
    try:
        if _is_reparse(source) or not source.is_file():
            raise DomainError("backup_integrity_failed", 409)
        if (source.stat().st_size != entry["bytes"]
                or _sha256_file(source) != entry["sha256"]):
            raise DomainError("backup_integrity_failed", 409)
    except DomainError:
        raise
    except OSError:
        raise DomainError("backup_integrity_failed", 409) from None
    _copy_file(source, destination)
    try:
        if (destination.stat().st_size != entry["bytes"]
                or _sha256_file(destination) != entry["sha256"]):
            raise DomainError("backup_integrity_failed", 409)
    except DomainError:
        raise
    except OSError:
        raise DomainError("backup_integrity_failed", 409) from None


def _verify_materialized_tree(root: Path, expected: dict[str, dict],
                              prefix: str) -> None:
    """核对目标树文件集合、大小与摘要；拒绝额外文件或链接。"""
    wanted = {
        shown[len(prefix):]: entry
        for shown, entry in expected.items() if shown.startswith(prefix)
    }
    actual: set[str] = set()
    for directory, names, files in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in names + files:
            path = base / name
            if _is_reparse(path):
                raise DomainError("backup_integrity_failed", 409)
        for name in files:
            path = base / name
            try:
                actual.add(path.relative_to(root).as_posix())
            except ValueError:
                raise DomainError("backup_integrity_failed", 409) from None
    if actual != set(wanted):
        raise DomainError("backup_integrity_failed", 409)
    for shown, entry in wanted.items():
        path = root.joinpath(*PurePosixPath(shown).parts)
        try:
            if (path.stat().st_size != entry["bytes"]
                    or _sha256_file(path) != entry["sha256"]):
                raise DomainError("backup_integrity_failed", 409)
        except DomainError:
            raise
        except OSError:
            raise DomainError("backup_integrity_failed", 409) from None


def _validate_restored_database(path: Path, key: bytes) -> None:
    """在私有副本上复核 schema、完整性与密文，绝不改变源目录。

    已处于 WAL 模式的数据库用只读 URI 打开时，SQLite 仍可能创建
    ``-shm``/``-wal`` 辅助文件；备份树校验会把这些文件视为清单外内容。
    因此先把主库复制到临时目录，再在副本上打开连接；临时目录清理后
    源树字节与文件集合保持不变。
    """
    from sqlite3 import Error, Row, connect

    directory, copy = _private_copy(path)
    connection = None
    try:
        try:
            connection = connect(f"{copy.as_uri()}?mode=ro", uri=True)
        except Error:
            raise DomainError("backup_integrity_failed", 409) from None
        connection.row_factory = Row
        check = connection.execute("PRAGMA integrity_check").fetchone()
        if check is None or check[0] != "ok":
            raise DomainError("backup_integrity_failed", 409)
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise DomainError("backup_incompatible", 409)
        store = Store(connection, copy)
        try:
            ConfigService(store, key)
        except ValueError:
            raise DomainError("master_key_mismatch", 409) from None
    except DomainError:
        raise
    except (Error, OSError):
        raise DomainError("backup_integrity_failed", 409) from None
    finally:
        if connection is not None:
            connection.close()
        directory.cleanup()


def restore_backup(source: Path, destination: Path, *,
                   managed_root: Path | None = None) -> Path:
    """把备份恢复到全新目录；先全量校验，失败时不留下部分目标。

    恢复源只读；目标必须是受管根内的新目录或空目录。master key 缺失、长度
    错误、与密文不匹配或任何清单摘要/文件集合不一致都会明确失败，绝不生成
    新 key 或重置已有秘密。
    """
    source = _absolute_path(source, "backup_invalid")
    destination = _absolute_path(destination, "restore_destination_invalid")
    if not source.is_dir():
        raise DomainError("backup_invalid", 400)
    if destination == source or destination.is_relative_to(source):
        raise DomainError("restore_target_inside_source", 400)
    if managed_root is not None:
        root = _absolute_path(managed_root, "restore_target_escape")
        if destination == root or not destination.is_relative_to(root):
            raise DomainError("restore_target_escape", 400)
    if (destination == Path(destination.anchor)
            or destination == Path.home().resolve()):
        raise DomainError("restore_target_escape", 400)
    if destination.exists() and any(destination.iterdir()):
        raise DomainError("restore_target_not_empty", 409)

    manifest = _read_manifest(source)
    key_path = source / "master.key"
    if not key_path.is_file() or _is_reparse(key_path):
        raise DomainError("backup_master_key_missing", 409)
    expected = _validate_bundle_tree(source, manifest)
    key = _load_backup_master_key(key_path)

    staging = _new_staging(
        destination.parent, f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        _verify_and_copy(source, staging, expected)
        _copy_file(source / "manifest.json", staging / "manifest.json")
        _validate_restored_database(
            staging / manifest["control"]["path"], key)
        if manifest["version"] >= BACKUP_V2:
            _validate_backup_signing_material(staging, manifest)
        _replace_directory(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return destination


def verify_backup(source: Path) -> dict:
    """只读校验完整备份：清单、文件集合、摘要、schema、密文与派生公钥。

    不写源目录，也不生成新 key；任何缺项、篡改或未来 schema 都失败关闭。
    """
    source = _absolute_path(source, "backup_invalid")
    if not source.is_dir():
        raise DomainError("backup_invalid", 400)
    manifest = _read_manifest(source)
    expected = _validate_bundle_tree(source, manifest)
    _verify_bundle_digests(source, expected)
    key_path = source / "master.key"
    if not key_path.is_file() or _is_reparse(key_path):
        raise DomainError("backup_master_key_missing", 409)
    key = _load_backup_master_key(key_path)
    _validate_restored_database(
        source / manifest["control"]["path"], key)
    if manifest["version"] >= BACKUP_V2:
        _validate_backup_signing_material(source, manifest)
    return {
        "version": manifest["version"],
        "schema_version": manifest["schema_version"],
        "files": len(manifest["files"]),
        "workspaces": len(manifest["workspaces"]),
        "clean_stop": bool(manifest.get("runtime_state", {}).get(
            "clean_stop", False)),
        "deployment": manifest.get("deployment"),
        "image": manifest.get("image", ""),
        "harness_revision": manifest.get("harness_revision", ""),
    }


def prepare_restored_instance(destination: Path, *, now: int | None = None,
                              invite_ttl_seconds: int = 3600) -> dict:
    """把已恢复到新目录的 v2 备份准备为可启动实例。

    仅在源已完成干净 Stop 时允许：清屏障、保留带 reopenedAt 的完成记录、
    撤销旧登录/邀请/Run token、规范化旧 leases，并签发一次性管理员恢复邀请。
    """
    destination = _absolute_path(destination, "restore_destination_invalid")
    manifest_path = destination / "manifest.json"
    try:
        source_manifest = manifest_path.read_bytes()
    except OSError:
        raise DomainError("backup_invalid", 400) from None
    manifest = _read_manifest(destination)
    if manifest["version"] < BACKUP_V2:
        raise DomainError("backup_incompatible", 409)
    # 准备转换会修改控制库并登记新摘要；只有当前 staging 仍完整匹配
    # 原清单时才允许转换，不能用重算摘要掩盖 restore 后的未知损坏。
    verify_backup(destination)
    database = destination / "control" / CONTROL_DB_NAME
    with Store.open(database) as store:
        summary = store.prepare_restored_instance(
            int(time.time()) if now is None else int(now))
        identity = store.get_identity(summary["admin_user_id"])
        summary = dict(summary)
        summary["recovery_invite"] = AuthService(store).create_invite(
            identity, identity.user_id, invite_ttl_seconds)

    # 准备步骤会合法修改 staging 控制库；只更新 staging 清单及其来源摘要，
    # 原备份 manifest 保持不变。后续 materialize 仍按新清单逐项校验。
    entry = next((item for item in manifest["files"]
                  if item["path"] == manifest["control"]["path"]), None)
    if entry is None:
        raise DomainError("backup_integrity_failed", 409)
    try:
        size = database.stat().st_size
        digest = _sha256_file(database)
    except OSError:
        raise DomainError("backup_integrity_failed", 409) from None
    manifest["control"] = {
        "path": manifest["control"]["path"], "sha256": digest, "bytes": size}
    entry["sha256"] = digest
    entry["bytes"] = size
    manifest["prepared"] = {
        "source_manifest_sha256": hashlib.sha256(source_manifest).hexdigest(),
        "control": {"path": manifest["control"]["path"], "sha256": digest,
                    "bytes": size},
        "restored_at": int(summary["restored_at"]),
        "cleared_barrier": summary["cleared_barrier"],
        "admin_user_id": summary["admin_user_id"],
    }
    _write_json(manifest_path, manifest)
    return summary


def _empty_directory(path: Path, code: str) -> Path:
    path = _absolute_path(path, code)
    if not path.is_dir() or any(path.iterdir()):
        raise DomainError("restore_material_target_not_empty", 409)
    return path


def _clear_directory(path: Path) -> None:
    try:
        for child in path.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
    except OSError:
        pass


def materialize_restore(source: Path, *, control_dir: Path, keys_dir: Path,
                        egress_dir: Path, workspace_root: Path) -> dict:
    """把已校验的恢复包落到新部署的 control/keys/egress/workspace 目标。

    目标必须全部为空；失败时只清理本轮新建的目标内容，不触碰源或旧数据。
    """
    source = _absolute_path(source, "backup_invalid")
    control = _empty_directory(control_dir, "restore_target_escape")
    keys = _empty_directory(keys_dir, "restore_target_escape")
    egress = _empty_directory(egress_dir, "restore_target_escape")
    workspaces = _empty_directory(workspace_root, "restore_target_escape")
    for target in (control, keys, egress, workspaces):
        if (target == source or target.is_relative_to(source)
                or source.is_relative_to(target)):
            raise DomainError("restore_target_escape", 400)
    manifest = _read_manifest(source)
    if manifest["version"] < BACKUP_V2:
        raise DomainError("backup_incompatible", 409)
    verify_backup(source)
    expected = {entry["path"]: entry for entry in manifest["files"]}
    key_file = source / "master.key"
    key = _load_backup_master_key(key_file)
    copied = 0
    try:
        control_entry = expected[f"control/{CONTROL_DB_NAME}"]
        _copy_verified_file(
            source / "control" / CONTROL_DB_NAME,
            control / CONTROL_DB_NAME, control_entry)
        if os.name == "posix":
            (control / CONTROL_DB_NAME).chmod(0o600)
        copied += 1
        for name, mode in (
                ("master.key", 0o600), ("snapshot.pem", 0o600),
                ("managed-agent.json", 0o600), ("egress-policy.json", 0o600),
                ("worker-public.key", 0o644), ("gateway-public.pem", 0o644)):
            target = keys / name
            _copy_verified_file(source / name, target, expected[name])
            if os.name == "posix":
                target.chmod(mode)
            copied += 1
        ca_entry = expected.get("egress-ca.pem")
        if ca_entry is not None:
            target = keys / "egress-ca.pem"
            _copy_verified_file(source / "egress-ca.pem", target, ca_entry)
            if os.name == "posix":
                target.chmod(0o600)
            copied += 1
        _copy_verified_file(
            source / "egress-policy.json", egress / "egress-policy.json",
            expected["egress-policy.json"])
        if os.name == "posix":
            (egress / "egress-policy.json").chmod(0o600)
        copied += 1
        for workspace_id, parts in manifest["workspaces"].items():
            if _WORKSPACE_ID.fullmatch(workspace_id) is None:
                raise DomainError("backup_workspace_id_invalid", 500)
            base = source / "workspaces" / workspace_id
            target_base = workspaces / workspace_id
            target_base.mkdir(mode=0o700)
            for part in ("workspace", "home"):
                if not parts.get(part):
                    continue
                source_part = base / part
                target_part = target_base / part
                target_part.mkdir(mode=0o700)
                copied += len(_copy_tree(
                    source_part, target_part, prefix="",
                    skip_names=frozenset()))
        _verify_materialized_tree(workspaces, expected, "workspaces/")
        _validate_restored_database(control / CONTROL_DB_NAME, key)
        _validate_backup_signing_material(source, manifest)
    except BaseException:
        for path in (control, keys, egress, workspaces):
            _clear_directory(path)
        raise
    return {
        "workspaces": len(manifest["workspaces"]),
        "files": copied,
        "schema_version": manifest["schema_version"],
    }


@dataclass(frozen=True)
class MigrationReport:
    """一次本机迁移的确定性清单；不含消息内容、令牌或原生配置。"""

    imported: list[str]
    non_resumable_sessions: list[str]
    rejected: list[str]


@dataclass(frozen=True)
class _SessionPlan:
    session_id: str
    runtime: str
    native_id: str
    cwd: str | None
    messages: tuple[dict, ...] | None
    resumable: bool


@dataclass(frozen=True)
class _MigrationPlan:
    static_files: list[PurePosixPath]
    sessions: list[_SessionPlan]
    rejected: list[str]


def _scan_static_files(content_root: Path,
                       rejected: list[str]) -> list[PurePosixPath]:
    found: list[PurePosixPath] = []

    def visit(directory: Path, relative: PurePosixPath) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError:
            raise DomainError("migration_source_invalid", 400) from None
        for entry in entries:
            name = entry.name
            if name in _SKIP_MIGRATION_NAMES or any(
                    fnmatch.fnmatch(name, pattern)
                    for pattern in _SKIP_MIGRATION_PATTERNS):
                continue
            path = Path(entry.path)
            child = relative / name
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                raise DomainError("migration_source_invalid", 400) from None
            if path.is_symlink() or getattr(info, "st_reparse_tag", 0):
                rejected.append(f"{child.as_posix()}: symlink_or_junction")
                continue
            if entry.is_dir(follow_symlinks=False):
                visit(path, child)
            elif entry.is_file(follow_symlinks=False):
                found.append(child)
            else:
                rejected.append(f"{child.as_posix()}: unsupported_file")

    if not content_root.is_dir():
        raise DomainError("migration_source_invalid", 400)
    visit(content_root, PurePosixPath())
    return found


def _session_message_store(content_root: Path,
                           workspace_id: str) -> WorkspaceSessionStore:
    # 仅用于输入校验；该实例的 root 不会被写入。
    return WorkspaceSessionStore(content_root, workspace_id)


def _valid_native_id(value: object) -> bool:
    return (isinstance(value, str) and len(value) <= 512
            and not any(character in value for character in ("\0", "\n", "\r", "\t")))


def _valid_cwd(value: object) -> bool:
    return (isinstance(value, str) and bool(value) and len(value) <= 1024
            and not any(character in value for character in ("\0", "\n", "\r", "\t")))


def _read_session_json(path: Path, shown: str,
                       rejected: list[str]) -> object | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rejected.append(f"{shown}: invalid_json")
        return None
    if data is None:
        rejected.append(f"{shown}: invalid_json")
        return None
    return data


def _build_migration_plan(content_root: Path,
                          workspace_id: str) -> _MigrationPlan:
    rejected: list[str] = []
    static_files = _scan_static_files(content_root, rejected)
    sessions_root = content_root / "_sessions"
    plans: list[_SessionPlan] = []
    if sessions_root.exists():
        if _is_reparse(sessions_root) or not sessions_root.is_dir():
            rejected.append("_sessions: symlink_or_junction")
        else:
            api: dict[str, tuple[Path, object]] = {}
            bindings: dict[str, tuple[Path, object]] = {}
            for kind, target in (("api", api), ("harness", bindings)):
                directory = sessions_root / kind
                if not directory.exists():
                    continue
                if _is_reparse(directory) or not directory.is_dir():
                    rejected.append(f"_sessions/{kind}: symlink_or_junction")
                    continue
                try:
                    entries = sorted(os.scandir(directory),
                                     key=lambda item: item.name)
                except OSError:
                    raise DomainError("migration_source_invalid", 400) from None
                for entry in entries:
                    shown = f"_sessions/{kind}/{entry.name}"
                    path = Path(entry.path)
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        raise DomainError(
                            "migration_source_invalid", 400) from None
                    if path.is_symlink() or getattr(info, "st_reparse_tag", 0):
                        rejected.append(f"{shown}: symlink_or_junction")
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        rejected.append(f"{shown}: unsupported_file")
                        continue
                    if _SESSION_DIGEST.fullmatch(entry.name[:-5]) is None:
                        rejected.append(f"{shown}: invalid_name")
                        continue
                    digest = entry.name[:-5]
                    data = _read_session_json(path, shown, rejected)
                    if data is not None:
                        target[digest] = (path, data)

            validator = _session_message_store(content_root, workspace_id)
            for digest in sorted(set(api) | set(bindings)):
                session_id = f"local-{digest}"
                messages: tuple[dict, ...] | None = None
                api_entry = api.get(digest)
                if api_entry is not None:
                    data = api_entry[1]
                    if not isinstance(data, list):
                        rejected.append(
                            f"_sessions/api/{digest}.json: invalid_history")
                    else:
                        validated: list[dict] = []
                        valid = True
                        for message in data:
                            try:
                                validator.validate_message(message)
                            except DomainError:
                                valid = False
                                break
                            validated.append(message)
                        if valid:
                            messages = tuple(validated)
                        else:
                            rejected.append(
                                f"_sessions/api/{digest}.json: invalid_history")

                binding_entry = bindings.get(digest)
                binding: tuple[str, str, str | None] | None = None
                if binding_entry is not None:
                    data = binding_entry[1]
                    if (not isinstance(data, dict)
                            or data.get("runtime") not in SUPPORTED_RUNTIMES
                            or not _valid_native_id(data.get("native_id"))
                            or (data.get("cwd") is not None
                                and not _valid_cwd(data.get("cwd")))):
                        rejected.append(
                            f"_sessions/harness/{digest}.json: invalid_binding")
                    else:
                        binding = (
                            str(data["runtime"]), str(data.get("native_id") or ""),
                            data.get("cwd"))

                if binding is None:
                    if messages is not None:
                        plans.append(_SessionPlan(
                            session_id, "api", "", None, messages, True))
                    continue
                runtime, native_id, cwd = binding
                resumable = bool(native_id) and cwd == "/workspace"
                plans.append(_SessionPlan(
                    session_id, runtime, native_id, cwd, messages, resumable))
    return _MigrationPlan(sorted(static_files), plans, rejected)


def _source_fingerprint(root: Path) -> str:
    """对源树做只读指纹；迁移前后必须完全一致（含不可跟随的重解析点）。"""
    records: list[tuple] = []

    def visit(directory: Path, relative: PurePosixPath) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError:
            raise DomainError("migration_source_invalid", 400) from None
        for entry in entries:
            path = Path(entry.path)
            shown = (relative / entry.name).as_posix()
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                raise DomainError("migration_source_invalid", 400) from None
            if path.is_symlink() or getattr(info, "st_reparse_tag", 0):
                try:
                    target = os.readlink(path)
                except OSError:
                    target = "reparse"
                records.append(("link", shown, target))
            elif entry.is_dir(follow_symlinks=False):
                records.append(("dir", shown))
                visit(path, relative / entry.name)
            elif entry.is_file(follow_symlinks=False):
                records.append(("file", shown, info.st_size, _sha256_file(path)))
            else:
                records.append(("other", shown))

    if not root.is_dir():
        raise DomainError("migration_source_invalid", 400)
    visit(root, PurePosixPath())
    encoded = json.dumps(records, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _migration_problem(code: str, status: int = 400) -> DomainError:
    return DomainError(code, status)


def _empty_tree(path: Path) -> bool:
    """仅空目录/空子目录算空；任何文件、链接或特殊项都视为已有目标内容。"""
    if _is_reparse(path) or not path.is_dir():
        return False
    try:
        entries = list(os.scandir(path))
    except OSError:
        return False
    for entry in entries:
        child = Path(entry.path)
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            return False
        if child.is_symlink() or getattr(info, "st_reparse_tag", 0):
            return False
        if not entry.is_dir(follow_symlinks=False):
            return False
        if not _empty_tree(child):
            return False
    return True


def _remove_empty_tree(path: Path, *, owner: Path | None = None) -> None:
    """只删除空目录树；遇到任何文件、链接或新数据立即失败并保留原状。"""
    if _is_reparse(path):
        raise _migration_problem("migration_target_escape")
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        raise _migration_problem("migration_target_escape") from None
    if os.path.normcase(str(resolved)) != os.path.normcase(str(path)):
        # 父级可能是预检后被替换的 junction
        raise _migration_problem("migration_target_escape")
    if owner is not None and (
            _is_reparse(owner)
            or os.path.normcase(str(path.parent))
            != os.path.normcase(str(owner))):
        raise _migration_problem("migration_target_escape")
    if not path.is_dir():
        raise _migration_problem("migration_target_not_empty", 409)
    try:
        entries = list(os.scandir(path))
    except FileNotFoundError:
        return
    except OSError:
        raise _migration_problem("migration_target_not_empty", 409) from None
    for entry in entries:
        child = Path(entry.path)
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            raise _migration_problem("migration_target_not_empty", 409) from None
        if (child.is_symlink() or getattr(info, "st_reparse_tag", 0)
                or not entry.is_dir(follow_symlinks=False)):
            raise _migration_problem("migration_target_not_empty", 409)
        _remove_empty_tree(child)
    try:
        path.rmdir()
    except FileNotFoundError:
        return
    except OSError:
        # 扫描后新增的文件/链接让 rmdir 失败；绝不递归删除新数据
        if path.exists():
            raise _migration_problem("migration_target_not_empty", 409) from None
        return


def _revalidate_migration_owner(workspace_root: Path,
                                workspace: str) -> tuple[Path, Path]:
    """最终提交前重新核验 owner/target，防止预检后被替换为链接。

    这里先 lstat 重解析点，再做解析路径相等/根包含检查和空目录扫描；任何
    一步失败都不得继续删除目录或 rename。
    """
    owner = workspace_root / workspace
    target = owner / "workspace"
    if _is_reparse(owner) or _is_reparse(target):
        raise _migration_problem("migration_target_escape")
    try:
        owner_resolved = owner.resolve(strict=False)
        target_resolved = target.resolve(strict=False)
    except (OSError, RuntimeError):
        raise _migration_problem("migration_target_escape") from None
    if (os.path.normcase(str(owner_resolved))
            != os.path.normcase(str(owner))
            or os.path.normcase(str(target_resolved))
            != os.path.normcase(str(target))):
        raise _migration_problem("migration_target_escape")
    if (owner_resolved == workspace_root
            or not owner_resolved.is_relative_to(workspace_root)
            or not target_resolved.is_relative_to(owner_resolved)):
        raise _migration_problem("migration_target_escape")
    if owner.exists() and (
            not owner.is_dir() or not _empty_tree(owner)):
        raise _migration_problem("migration_target_not_empty", 409)
    if target.exists() and not target.is_dir():
        raise _migration_problem("migration_target_not_empty", 409)
    return owner, target


def _migration_target(workspace_root: Path, workspace: str) -> tuple[Path, Path]:
    if not isinstance(workspace, str) or _WORKSPACE_ID.fullmatch(workspace) is None:
        raise _migration_problem("migration_workspace_invalid")
    root = _absolute_path(workspace_root, "migration_workspace_root_invalid")
    if not root.is_dir():
        raise _migration_problem("migration_workspace_root_invalid")
    owner = root / workspace
    target = owner / "workspace"
    # 先拒绝重解析点，不能先遍历其目标；缺失链接也同样拒绝
    if _is_reparse(owner) or _is_reparse(target):
        raise _migration_problem("migration_target_escape")
    if owner.exists() and (
            not owner.is_dir() or not _empty_tree(owner)):
        raise _migration_problem("migration_target_not_empty", 409)
    try:
        resolved = target.resolve(strict=False)
    except (OSError, RuntimeError):
        raise _migration_problem("migration_target_escape") from None
    if resolved == root or not resolved.is_relative_to(root):
        raise _migration_problem("migration_target_escape")
    return root, target


def _migration_backup_path(workspace_root: Path, workspace_id: str,
                           target: Path, source: Path,
                           destination: Path | None) -> Path:
    if destination is None:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        destination = (workspace_root / ".migration-backups"
                       / f"{workspace_id}-{stamp}-{uuid.uuid4().hex[:8]}")
    # 显式与自动目标都必须解析现有链接/junction 后再做根包含检查
    destination = _absolute_path(destination, "migration_backup_escape")
    if (destination == workspace_root
            or not destination.is_relative_to(workspace_root)):
        raise _migration_problem("migration_backup_escape")
    if (destination == target or destination.is_relative_to(target)
            or destination == source or destination.is_relative_to(source)
            or source.is_relative_to(destination)):
        raise _migration_problem("migration_backup_escape")
    if destination.exists() and (
            not destination.is_dir() or any(destination.iterdir())):
        raise _migration_problem("migration_backup_not_empty", 409)
    return destination


def _create_migration_backup(content_root: Path, destination: Path,
                             workspace_id: str) -> None:
    staging = _new_staging(
        destination.parent, f".migration-backup-{uuid.uuid4().hex}.tmp")
    try:
        rejected: list[str] = []
        files = _copy_tree(
            content_root, staging, prefix="",
            skip_names=_SKIP_MIGRATION_BACKUP_NAMES,
            skip_patterns=_SKIP_MIGRATION_PATTERNS, rejected=rejected)
        _write_json(staging / "migration.json", {
            "format": MIGRATION_BACKUP_FORMAT,
            "version": 1,
            "created_at": int(time.time()),
            "workspace_id": workspace_id,
            "files": sorted(files, key=lambda item: item["path"]),
            "rejected": sorted(rejected),
        })
        _replace_directory(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _write_static_session(state_root: Path, workspace_id: str,
                          plan: _SessionPlan) -> None:
    directory = state_root / "migrated-sessions" / plan.session_id
    directory.mkdir(parents=True, exist_ok=True)
    record: dict = {
        "runtime": plan.runtime,
        "native_id": plan.native_id,
    }
    if plan.cwd is not None:
        record["cwd"] = plan.cwd
    _write_json(directory / "session.json", record)
    if plan.messages is None:
        return
    lines = []
    for message in plan.messages:
        lines.append(json.dumps({
            "format": MESSAGE_FORMAT,
            "version": MESSAGE_FORMAT_VERSION,
            "workspace_id": workspace_id,
            "session_id": plan.session_id,
            "message": message,
        }, ensure_ascii=False, separators=(",", ":")))
    try:
        with (directory / "messages.jsonl").open("w", encoding="utf-8") as stream:
            for line in lines:
                stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "posix":
            (directory / "messages.jsonl").chmod(0o600)
    except OSError:
        raise DomainError("migration_write_failed", 500) from None


def _import_sessions(state_root: Path, workspace_id: str,
                     plans: list[_SessionPlan],
                     report: MigrationReport) -> None:
    """会话写入真实 worker state_root，而不是 workspace 根或 outputs。"""
    store = WorkspaceSessionStore(state_root, workspace_id)
    for plan in plans:
        if plan.resumable:
            store.bind(plan.session_id, plan.runtime, plan.native_id, 1,
                       cwd=plan.cwd)
            for message in plan.messages or ():
                store.append_message(plan.session_id, message)
            report.imported.append(plan.session_id)
        else:
            _write_static_session(state_root, workspace_id, plan)
            report.imported.append(plan.session_id)
            report.non_resumable_sessions.append(plan.session_id)


def migrate_local(source: Path, *, workspace_root: Path, workspace: str,
                  dry_run: bool = False,
                  backup_destination: Path | None = None) -> MigrationReport:
    """显式迁移本机静态内容与旧会话；源只读，目标必须为空。

    ``source`` 若含 ``outputs/`` 则只迁移该目录，否则视为 outputs 本身。
    ``--dry-run`` 不写备份或目标；实际执行先创建同卷完整备份，再复制到
    临时目录，最终以 rename 提交，任何中断都不会留下半成品目标。
    """
    source = _absolute_path(source, "migration_source_invalid")
    if not source.is_dir():
        raise _migration_problem("migration_source_invalid")
    outputs = source / "outputs"
    if outputs.is_dir():
        if _is_reparse(outputs):
            raise _migration_problem("migration_source_escape")
        content_root = outputs
    else:
        content_root = source
    try:
        content_root = content_root.resolve(strict=True)
    except OSError:
        raise _migration_problem("migration_source_invalid") from None
    workspace_root_path, target = _migration_target(workspace_root, workspace)
    if (target == content_root or target.is_relative_to(content_root)
            or content_root.is_relative_to(target)):
        raise _migration_problem("migration_target_inside_source")

    plan = _build_migration_plan(content_root, workspace)
    report = MigrationReport(
        imported=[item.as_posix() for item in plan.static_files],
        non_resumable_sessions=[],
        rejected=list(plan.rejected),
    )
    if dry_run:
        report.imported.extend(item.session_id for item in plan.sessions)
        report.non_resumable_sessions.extend(
            item.session_id for item in plan.sessions if not item.resumable)
        return report

    destination = _migration_backup_path(
        workspace_root_path, workspace, target, content_root,
        backup_destination)
    before = _source_fingerprint(content_root)
    _create_migration_backup(content_root, destination, workspace)

    staging = _new_staging(
        workspace_root_path, f".migration-staging-{uuid.uuid4().hex}.tmp")
    try:
        for relative in plan.static_files:
            source_file = content_root.joinpath(*relative.parts)
            copied = _copy_file(
                source_file,
                staging / "outputs" / Path(*relative.parts))
            if copied["sha256"] != _sha256_file(source_file):
                raise _migration_problem("migration_source_changed", 409)
        # 会话属于真实 worker state_root；与静态 outputs 严格隔离
        _import_sessions(staging / "state", workspace, plan.sessions, report)
        if _source_fingerprint(content_root) != before:
            raise _migration_problem("migration_source_changed", 409)

        # 预检之后 owner/target 可能被替换；提交前不再相信旧路径判断
        owner, target = _revalidate_migration_owner(
            workspace_root_path, workspace)
        if target.exists():
            # 接受与 dry-run 相同的“递归空目录”，但只删除空目录
            _remove_empty_tree(target, owner=owner)
        if not owner.exists():
            try:
                # 非递归创建：预检后若出现同名链接/目录，明确冲突而不是跟随
                owner.mkdir()
            except FileExistsError:
                raise _migration_problem(
                    "migration_target_not_empty", 409) from None
        # rename 之前再次核验，拒绝扫描后/建目录后的路径替换
        owner, target = _revalidate_migration_owner(
            workspace_root_path, workspace)
        os.replace(staging, target)
    except DomainError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        raise _migration_problem("migration_write_failed", 500) from None
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return report


def cmd_admin_bootstrap(args) -> int:
    """引导首个管理员；邀请明文只在本命令输出一次。"""
    try:
        with Store.open(_control_db(args.control_dir)) as store:
            identity, invite = AuthService(store).bootstrap_admin(args.name)
    except DomainError as error:
        return _report(error)
    print(f"admin_user_id={identity.user_id}")
    print(f"workspace_id={identity.workspace_id}")
    print(f"invite={invite}")
    return 0


def cmd_admin_invite(args) -> int:
    """管理员为现有用户签发一次性邀请；角色由数据库复查。"""
    try:
        with Store.open(_control_db(args.control_dir)) as store:
            admin = store.get_identity(args.admin_user_id)
            invite = AuthService(store).create_invite(
                admin, args.user_id, args.ttl_seconds)
    except DomainError as error:
        return _report(error)
    print(f"invite={invite}")
    return 0


def cmd_admin_backup(args) -> int:
    """备份控制库、key、签名材料与 workspace 持久区；活动 worker 先 drain。"""
    control_dir = Path(args.control_dir)
    database = _control_db(args.control_dir)
    if not database.is_file():
        print("错误：backup_control_dir_invalid", file=sys.stderr)
        return 1
    drain = None
    socket = getattr(args, "provisioner_socket", None)
    if socket:
        if not Path(socket).exists():
            print("错误：provisioner_unavailable", file=sys.stderr)
            return 1
        provisioner = UnixProvisionerClient(socket)

        def drain():
            # CLI 是同步入口；使用同一线程内的短连接执行 drain 编排。
            asyncio.run(RuntimeManager(store, provisioner).drain_all())

    deployment = None
    deployment_path = getattr(args, "deployment_json", None)
    if deployment_path:
        try:
            deployment = _read_json_object(
                Path(deployment_path), "backup_deployment_metadata_invalid")
        except DomainError as error:
            return _report(error)

    try:
        with Store.open(database) as store:
            result = create_backup(
                store, Path(args.destination),
                control_dir=control_dir,
                master_key_file=Path(args.master_key_file),
                workspace_root=(Path(args.workspace_root)
                                if args.workspace_root else None),
                image_digest=args.image_digest,
                harness_revision=args.harness_revision,
                drain=drain,
                signing_key_file=(Path(args.signing_key_file)
                                  if getattr(args, "signing_key_file", None)
                                  else None),
                managed_config_file=(Path(args.managed_agent_config)
                                     if getattr(args, "managed_agent_config",
                                                None) else None),
                egress_policy_file=(Path(args.egress_policy_file)
                                    if getattr(args, "egress_policy_file",
                                               None) else None),
                egress_ca_file=(Path(args.egress_ca_file)
                                if getattr(args, "egress_ca_file", None)
                                else None),
                deployment=deployment,
                require_clean_stop=bool(
                    getattr(args, "require_clean_stop", False)))
    except DomainError as error:
        return _report(error)
    print(f"backup={result}")
    return 0


def cmd_admin_verify_backup(args) -> int:
    """只读校验完整备份；不写源、不生成 key。"""
    try:
        summary = verify_backup(Path(args.source))
    except DomainError as error:
        return _report(error)
    print(f"verified={Path(args.source)}")
    print(f"version={summary['version']}")
    print(f"schema_version={summary['schema_version']}")
    print(f"files={summary['files']}")
    print(f"workspaces={summary['workspaces']}")
    print(f"clean_stop={str(summary['clean_stop']).lower()}")
    deployment = summary.get("deployment") or {}
    for key in ("project", "config_sha256", "worker_image",
                "gateway_image", "control_image", "harness_revision"):
        if deployment.get(key):
            print(f"{key}={deployment[key]}")
    return 0


def cmd_admin_prepare_restore(args) -> int:
    """准备恢复副本：清运行期身份并签发一次性管理员恢复邀请。"""
    try:
        summary = prepare_restored_instance(
            Path(args.destination),
            invite_ttl_seconds=int(getattr(args, "invite_ttl_seconds", 3600)))
    except DomainError as error:
        return _report(error)
    print(f"prepared={Path(args.destination)}")
    print(f"cleared_barrier={summary['cleared_barrier']}")
    print(f"revoked_logins={summary['revoked_logins']}")
    print(f"revoked_invites={summary['revoked_invites']}")
    print(f"revoked_broker_tokens={summary['revoked_broker_tokens']}")
    # 仅此处输出一次；调用脚本必须写入受限文件，不得进入日志/证据。
    print(f"recovery_invite={summary['recovery_invite']}")
    return 0


def cmd_admin_materialize_restore(args) -> int:
    """把已准备恢复包落到新部署目标目录/卷。"""
    try:
        summary = materialize_restore(
            Path(args.source),
            control_dir=Path(args.control_dir),
            keys_dir=Path(args.keys_dir),
            egress_dir=Path(args.egress_dir),
            workspace_root=Path(args.workspace_root))
    except DomainError as error:
        return _report(error)
    print(f"materialized={Path(args.source)}")
    print(f"workspaces={summary['workspaces']}")
    print(f"files={summary['files']}")
    print(f"schema_version={summary['schema_version']}")
    return 0


def cmd_admin_stop_status(args) -> int:
    """只读打印 Stop 完成记录，供受控升级比对旧配置摘要。"""
    try:
        with Store.open(_control_db(args.control_dir)) as store:
            barrier = store.admission_barrier()
            completion = store.stop_completion()
    except DomainError as error:
        return _report(error)
    except Exception:  # noqa: BLE001 - 诊断失败不得伪报无记录
        print("错误：stop_status_failed", file=sys.stderr)
        return 1
    if barrier is None and completion is None:
        print("stop_completion=none")
        return 0
    print("stop_completion=" + json.dumps(
        {"barrier": barrier, "completion": completion},
        sort_keys=True, separators=(",", ":")))
    return 0


def cmd_admin_upgrade_complete(args) -> int:
    """受控升级：仅把匹配旧摘要的完成记录改写为新摘要。"""
    try:
        with Store.open(_control_db(args.control_dir)) as store:
            record = store.upgrade_stop_completion(
                args.barrier_id, args.project, args.from_config_sha256,
                args.to_config_sha256, args.backup_manifest_sha256,
                int(time.time()))
    except DomainError as error:
        return _report(error)
    except Exception:  # noqa: BLE001 - 升级失败必须保留旧记录
        print("错误：stop_upgrade_failed", file=sys.stderr)
        return 1
    print(f"upgraded={record['barrierId']}")
    print(f"from_config_sha256={record['upgradedFrom']}")
    print(f"to_config_sha256={record['configSha256']}")
    return 0


def cmd_admin_restore(args) -> int:
    """把备份恢复到新目录；绝不覆盖已有控制目录。"""
    try:
        restored = restore_backup(
            Path(args.source), Path(args.destination),
            managed_root=(Path(args.managed_root)
                          if args.managed_root else None))
    except DomainError as error:
        return _report(error)
    print(f"restored={restored}")
    return 0


def cmd_admin_migrate_local(args) -> int:
    """显式迁移本机 outputs/旧会话；dry-run 只输出清单。"""
    try:
        report = migrate_local(
            Path(args.source), workspace_root=Path(args.workspace_root),
            workspace=args.workspace, dry_run=args.dry_run,
            backup_destination=(Path(args.backup_destination)
                                if args.backup_destination else None))
    except DomainError as error:
        return _report(error)
    print(f"imported={len(report.imported)}")
    print(f"non_resumable={len(report.non_resumable_sessions)}")
    print(f"rejected={len(report.rejected)}")
    for item in report.imported:
        print(f"imported_item={item}")
    for item in report.non_resumable_sessions:
        print(f"non_resumable_item={item}")
    for item in report.rejected:
        print(f"rejected_item={item}")
    return 0


def cmd_admin_drain(args) -> int:
    """drain 所有活动 workspace；用于停止/备份前收尾。"""
    socket = Path(args.provisioner_socket)
    if not socket.exists():
        print("错误：provisioner_unavailable", file=sys.stderr)
        return 1
    provisioner = UnixProvisionerClient(
        socket, gateway_required=bool(getattr(args, "gateway_required", False)))
    try:
        with Store.open(_control_db(args.control_dir)) as store:
            barrier = None
            if bool(getattr(args, "barrier", False)):
                # 屏障必须先于 drain 写入控制库，才对所有 control 进程生效。
                barrier = store.set_admission_barrier(
                    f"stop-{uuid.uuid4().hex}", "desktop-stop",
                    int(time.time()))
                print(f"barrier={barrier['id']}")
            manager = RuntimeManager(
                store, provisioner,
                gateway_required=bool(
                    getattr(args, "gateway_required", False)))
            drained = asyncio.run(manager.drain_all())
            status = store.drain_status(int(time.time()))
            if not status["settled"]:
                print("错误：worker_drain_incomplete", file=sys.stderr)
                return 1
    except DomainError as error:
        return _report(error)
    except Exception:  # noqa: BLE001 - provisioner/drain failures统一失败关闭
        print("错误：worker_drain_failed", file=sys.stderr)
        return 1
    print(f"drained={len(drained)}")
    for workspace in drained:
        print(f"drained_workspace={workspace}")
    print("verified=settled")
    return 0


def cmd_admin_resume(args) -> int:
    """清除停止接纳屏障；只由显式恢复入口调用。"""
    try:
        with Store.open(_control_db(args.control_dir)) as store:
            barrier = store.admission_barrier()
            if barrier is None:
                print("barrier=none")
                return 0
            barrier_id = str(barrier.get("id", ""))
            requested = getattr(args, "barrier_id", None)
            if requested is not None and requested != barrier_id:
                print("错误：admission_barrier_mismatch", file=sys.stderr)
                return 1
            if not store.clear_admission_barrier(barrier_id):
                print("错误：admission_barrier_clear_failed", file=sys.stderr)
                return 1
            print(f"cleared={barrier_id}")
            return 0
    except DomainError as error:
        return _report(error)
    except Exception:  # noqa: BLE001 - 恢复必须失败关闭
        print("错误：admission_barrier_resume_failed", file=sys.stderr)
        return 1


def cmd_admin_stop_complete(args) -> int:
    """成功 Stop 后记录 barrier/项目/配置 fencing；不清除屏障。"""
    try:
        with Store.open(_control_db(args.control_dir)) as store:
            record = store.complete_stop(
                args.barrier_id, args.project, args.config_sha256,
                int(time.time()))
    except DomainError as error:
        return _report(error)
    except Exception:  # noqa: BLE001 - 完成记录失败不得假报成功
        print("错误：stop_completion_failed", file=sys.stderr)
        return 1
    print(f"stop_completed={record['barrierId']}")
    return 0


def cmd_admin_reopen(args) -> int:
    """仅按成功完成记录原子恢复接纳；失败/取消屏障保持关闭。"""
    try:
        with Store.open(_control_db(args.control_dir)) as store:
            barrier = store.admission_barrier()
            if barrier is None:
                print("barrier=none")
                return 0
            reopened = store.reopen_after_completed_stop(
                args.barrier_id, args.project, args.config_sha256,
                int(time.time()))
    except DomainError as error:
        return _report(error)
    except Exception:  # noqa: BLE001 - 重开失败不得假报已恢复
        print("错误：admission_barrier_reopen_failed", file=sys.stderr)
        return 1
    if not reopened:
        print("错误：admission_barrier_not_reopenable", file=sys.stderr)
        return 1
    print(f"reopened={args.barrier_id}")
    return 0


def cmd_admin_barrier_status(args) -> int:
    """只读打印当前停止接纳屏障，供 Start 诊断。"""
    try:
        with Store.open(_control_db(args.control_dir)) as store:
            barrier = store.admission_barrier()
    except DomainError as error:
        return _report(error)
    except Exception:  # noqa: BLE001 - 诊断失败不得报告无屏障
        print("错误：admission_barrier_status_failed", file=sys.stderr)
        return 1
    if barrier is None:
        print("barrier=none")
    else:
        print(f"barrier={barrier.get('id', '')}")
    return 0


def cmd_serve_control(args) -> int:
    """启动 control HTTP 服务；应用在 lifespan 中管理自己的数据库线程。"""
    try:
        # Origin 必须在打开控制库、启动 HTTP 之前校验失败
        config = ControlConfig(public_origin=args.public_origin)
    except ValueError:
        print("错误：invalid_origin", file=sys.stderr)
        return 2
    try:
        master_key = load_master_key(args.master_key_file)
    except OSError:
        print("错误：master_key_unavailable", file=sys.stderr)
        return 2
    except ValueError:
        print("错误：invalid_master_key", file=sys.stderr)
        return 2

    # 逐 Run 签名密钥与受管 Agent 配置必须成对显式给出；只给其一视为
    # 配置错误而不是静默降级。两者都不给时保持任务入口 503。
    gateway_required = bool(getattr(args, "gateway_required", False))
    snapshot_key_file = getattr(args, "snapshot_key_file", None)
    managed_config_file = getattr(args, "managed_agent_config", None)
    signing_key = None
    managed_agent = None
    model_catalog = None
    if bool(snapshot_key_file) != bool(managed_config_file):
        print("错误：relay_config_incomplete", file=sys.stderr)
        return 2
    if gateway_required and signing_key is None and not snapshot_key_file:
        # gateway 模式必须能逐请求签名；不得退回旧直连/无签名路径。
        print("错误：gateway_requires_relay_config", file=sys.stderr)
        return 2
    if snapshot_key_file:
        try:
            signing_key = load_signing_key(snapshot_key_file)
        except OSError:
            print("错误：snapshot_key_unavailable", file=sys.stderr)
            return 2
        except ValueError:
            print("错误：snapshot_key_invalid", file=sys.stderr)
            return 2
        try:
            managed_agent = load_managed_agent_config(managed_config_file)
        except OSError:
            print("错误：managed_config_unavailable", file=sys.stderr)
            return 2
        except ValueError:
            # 不回显字段值：配置中可能误放管理员凭证
            print("错误：managed_config_invalid", file=sys.stderr)
            return 2

    relay_factory = None
    harness_summary = None
    if signing_key is not None:
        revision, agent, broker_config = managed_agent
        try:
            profile_path = _managed_profile_path(
                getattr(args, "managed_profile", None))
            expected_commit = (
                getattr(args, "expected_harness_commit", None)
                or os.environ.get("EASEL_EXPECTED_HARNESS_COMMIT"))
            image_models = load_managed_profile_models(
                profile_path, expected_commit=expected_commit)
        except (OSError, ValueError):
            print("错误：managed_profile_invalid", file=sys.stderr)
            return 2
        configured_models = tuple(agent.get("models") or ())
        if configured_models and any(
                model not in image_models for model in configured_models):
            # 运行时配置只能收窄镜像可信目录，不能自报镜像不支持的模型。
            print("错误：managed_config_invalid", file=sys.stderr)
            return 2
        model_catalog = configured_models or image_models
        try:
            relay_factory = build_relay_factory(
                signing_key=signing_key, revision=revision, agent=agent,
                broker_config=broker_config,
                gateway_required=gateway_required)
        except (KeyError, TypeError, ValueError):
            print("错误：managed_config_invalid", file=sys.stderr)
            return 2
        # 只投影可公开的只读元数据；argv、broker 地址与凭证不进入响应
        harness_summary = {
            "runtime": agent["runtime"],
            "revision": revision,
            "model": agent.get("model"),
            "models": list(model_catalog),
            "sandbox": agent.get("sandbox"),
            "timeout": agent.get("timeout"),
            "codexAutoApprove": agent.get("codex_auto_approve"),
        }

    # 出口策略必须在打开控制库、启动 HTTP 之前校验失败
    policy_path = getattr(args, "egress_policy", None)
    egress_proxy = getattr(args, "egress_proxy", None)
    if policy_path is not None:
        try:
            policy = load_policy(policy_path)
        except ValueError:
            print("错误：egress_policy_invalid", file=sys.stderr)
            return 2
    else:
        # 未显式提交策略时保留“未配置”状态；没有 proxy 或策略都必须
        # 在 broker 联网前返回 503，不能构造隐式默认策略冒充管理员配置。
        policy = None
    try:
        egress = EgressGateway(
            policy=policy, proxy_url=egress_proxy,
            ca_file=getattr(args, "egress_ca_file", None))
    except ValueError:
        print("错误：egress_proxy_invalid", file=sys.stderr)
        return 2

    # 受限 provisioner 必须可达才启动多人运行时；缺失时失败关闭，
    # 不回退到进程内 Docker 或“无隔离”模式。
    provisioner = None
    provisioner_socket = getattr(args, "provisioner_socket", None)
    if gateway_required and not provisioner_socket:
        print("错误：gateway_required_needs_provisioner", file=sys.stderr)
        return 2
    if provisioner_socket:
        if not Path(provisioner_socket).exists():
            print("错误：provisioner_unavailable", file=sys.stderr)
            return 2
        provisioner = UnixProvisionerClient(
            provisioner_socket, gateway_required=gateway_required)

    # 固定前端构建目录：显式给出时必须存在，否则回退任意服务器路径是禁止的
    frontend_dir = getattr(args, "frontend_dir", None)
    if frontend_dir is not None:
        build = Path(frontend_dir).resolve()
        if not build.is_dir() or not (build / "index.html").is_file():
            print("错误：frontend_build_missing", file=sys.stderr)
            return 2

    store = Store.connect(_control_db(args.control_dir))
    try:
        try:
            # 已有密文与 key 不匹配必须在服务启动前明确失败
            config_service = ConfigService(store, master_key,
                                           egress_policy=policy,
                                           model_catalog=model_catalog)
        except ValueError:
            print("错误：invalid_master_key", file=sys.stderr)
            return 2
        broker = BrokerService(store, config_service, egress=egress,
                               model_catalog=model_catalog)
        app = create_app(store=store, auth=AuthService(store), config=config,
                         config_service=config_service, broker=broker,
                         provisioner=provisioner,
                         gateway_required=gateway_required,
                         frontend_dir=frontend_dir,
                         relay_factory=relay_factory,
                         harness_summary=harness_summary)
    finally:
        # 应用会在 lifespan 中按线程边界重新打开连接
        store.close()
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=False)
    return 0
