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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import uvicorn

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
from .proxy import BrokerDestination, PinnedRunConfig, WorkerRelay
from .runtime import RuntimeManager, UnixProvisionerClient
from .snapshot import load_managed_agent_config, load_signing_key
from .store import SCHEMA_VERSION, Store
from .types import DomainError

CONTROL_DB_NAME = "control.sqlite3"
BACKUP_FORMAT = "easel.control.backup"
BACKUP_VERSION = 1
_ACTIVE_LEASE_STATES = ("starting", "ready", "draining", "stopping")

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
                  drain: Callable[[], object] | None = None) -> Path:
    """备份控制 SQLite、master key 与各 workspace 持久区。

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
    _drain_before_backup(store, drain)

    staging = _new_staging(
        destination.parent, f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
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
                        entry[part] = False
                        continue
                    files.extend(_copy_tree(
                        source,
                        staging / "workspaces" / workspace_id / part,
                        prefix=f"workspaces/{workspace_id}/{part}",
                        skip_names=_SKIP_BACKUP_NAMES))
                    entry[part] = True
            workspace_entries[workspace_id] = entry

        manifest = {
            "format": BACKUP_FORMAT,
            "version": BACKUP_VERSION,
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
        _write_json(staging / "manifest.json", manifest)
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
                        broker_destination: BrokerDestination):
    """正式 control 装配：接纳后为每个 Run 签发最小 broker token。

    该函数被 CLI 与装配测试共同使用；不得在测试里用自造 provider 另起
    一条与生产不同的签发路径。
    """
    def pinned_config(_actor):
        return PinnedRunConfig(
            revision=revision, routes={}, agent=agent,
            broker=broker_destination)

    def relay_factory(runtime, sessions, auth, store, http_client,
                      broker, db_runner):
        return WorkerRelay(
            runtime, sessions, auth, store=store,
            signing_key=signing_key, config_provider=pinned_config,
            http_client=http_client, broker=broker,
            db_runner=db_runner)

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


def _validate_restored_database(path: Path, key: bytes) -> None:
    """恢复前用新目录中的数据库和 key 复核 schema、完整性与密文。"""
    from sqlite3 import Error, Row, connect

    try:
        # 只读 URI：恢复校验不能向待提交目录写 WAL/shm 或改变数据库内容
        connection = connect(f"{path.as_uri()}?mode=ro", uri=True)
    except Error:
        raise DomainError("backup_integrity_failed", 409) from None
    try:
        connection.row_factory = Row
        check = connection.execute("PRAGMA integrity_check").fetchone()
        if check is None or check[0] != "ok":
            raise DomainError("backup_integrity_failed", 409)
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise DomainError("backup_incompatible", 409)
        store = Store(connection, path)
        try:
            ConfigService(store, key)
        except ValueError:
            raise DomainError("master_key_mismatch", 409) from None
    except DomainError:
        raise
    except (Error, OSError):
        raise DomainError("backup_integrity_failed", 409) from None
    finally:
        connection.close()


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
    key = _load_backup_key(key_path)

    staging = _new_staging(
        destination.parent, f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        _verify_and_copy(source, staging, expected)
        _copy_file(source / "manifest.json", staging / "manifest.json")
        _validate_restored_database(
            staging / manifest["control"]["path"], key)
        _replace_directory(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return destination


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
    """备份控制库、key 与 workspace 持久区；活动 worker 必须先 drain。"""
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
                drain=drain)
    except DomainError as error:
        return _report(error)
    print(f"backup={result}")
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
    snapshot_key_file = getattr(args, "snapshot_key_file", None)
    managed_config_file = getattr(args, "managed_agent_config", None)
    signing_key = None
    managed_agent = None
    if bool(snapshot_key_file) != bool(managed_config_file):
        print("错误：relay_config_incomplete", file=sys.stderr)
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
            destination = BrokerDestination(
                broker_config["url"], broker_config["channel"])
        except (KeyError, ValueError):
            print("错误：managed_config_invalid", file=sys.stderr)
            return 2
        relay_factory = build_relay_factory(
            signing_key=signing_key, revision=revision, agent=agent,
            broker_destination=destination)
        # 只投影可公开的只读元数据；argv、broker 地址与凭证不进入响应
        harness_summary = {
            "runtime": agent["runtime"],
            "revision": revision,
            "model": agent.get("model"),
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
        egress = EgressGateway(policy=policy, proxy_url=egress_proxy)
    except ValueError:
        print("错误：egress_proxy_invalid", file=sys.stderr)
        return 2

    # 受限 provisioner 必须可达才启动多人运行时；缺失时失败关闭，
    # 不回退到进程内 Docker 或“无隔离”模式。
    provisioner = None
    provisioner_socket = getattr(args, "provisioner_socket", None)
    if provisioner_socket:
        if not Path(provisioner_socket).exists():
            print("错误：provisioner_unavailable", file=sys.stderr)
            return 2
        provisioner = UnixProvisionerClient(provisioner_socket)

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
                                           egress_policy=policy)
        except ValueError:
            print("错误：invalid_master_key", file=sys.stderr)
            return 2
        broker = BrokerService(store, config_service, egress=egress)
        app = create_app(store=store, auth=AuthService(store), config=config,
                         config_service=config_service, broker=broker,
                         provisioner=provisioner, frontend_dir=frontend_dir,
                         relay_factory=relay_factory,
                         harness_summary=harness_summary)
    finally:
        # 应用会在 lifespan 中按线程边界重新打开连接
        store.close()
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=False)
    return 0
