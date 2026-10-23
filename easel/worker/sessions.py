"""worker 私有会话快照与原生绑定。

所有用户提供的标识只用于哈希，不进入路径；数据位于
``<root>/sessions/<workspace-hash>/<session-hash>/``。绑定和消息追加都在
跨进程文件锁内完成，写入使用临时文件 + fsync + replace。每条消息行带
format/version/workspace/session envelope，读取和追加都校验归属与格式；
损坏或旧无归属记录明确失败，不当作空历史。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from easel.control.types import DomainError

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

SUPPORTED_RUNTIMES = ("api", "openclaw", "opencode", "codex", "acp")
MESSAGE_ROLES = ("user", "assistant", "system", "tool")
MESSAGE_FORMAT = "easel.session.message"
MESSAGE_FORMAT_VERSION = 1
_MESSAGE_FIELDS = frozenset({
    "format", "version", "workspace_id", "session_id", "message"})
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_ATTACHMENTS = 100  # 单条消息与 worker 单次上传共享的附件上限

_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_ATTACHMENT_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_CONTROL_CHARS = ("\0", "\n", "\r", "\t")


def _invalid() -> DomainError:
    return DomainError("session_invalid", 400)


def _identifier(value: object) -> str:
    if (not isinstance(value, str) or not _IDENTIFIER.fullmatch(value)
            or value in (".", "..")):
        raise _invalid()
    return value


def _workspace_id(value: object) -> str:
    return _identifier(value)


def _session_id(value: object) -> str:
    return _identifier(value)


def _native_id(value: object) -> str:
    if (not isinstance(value, str) or len(value) > 512
            or any(char in value for char in _CONTROL_CHARS)):
        raise _invalid()
    return value


def _runtime(value: object) -> str:
    if value not in SUPPORTED_RUNTIMES:
        raise DomainError("session_runtime_unsupported", 400)
    return str(value)


def _agent_version(value: object) -> int:
    if type(value) is not int or value < 1:
        raise DomainError("session_version_unsupported", 400)
    return value


def _cwd(value: object) -> str | None:
    if value is None:
        return None
    if (not isinstance(value, str) or not value or len(value) > 1024
            or any(char in value for char in _CONTROL_CHARS)):
        raise _invalid()
    return value


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, name = tempfile.mkstemp(
            dir=path.parent, prefix=path.name, suffix=".tmp")
        temporary = Path(name)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False,
                          separators=(",", ":"), sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError:
        raise DomainError("session_write_failed", 500) from None


def _write_all(descriptor: int, data: bytes) -> None:
    """有界写入全部字节；用于临时文件的历史复制与新记录追加。"""
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DomainError("session_corrupt", 500) from None
    if not isinstance(data, dict):
        raise DomainError("session_corrupt", 500)
    return data


def _message_record(workspace_id: str, session_id: str, message: dict) -> dict:
    return {
        "format": MESSAGE_FORMAT,
        "version": MESSAGE_FORMAT_VERSION,
        "workspace_id": workspace_id,
        "session_id": session_id,
        "message": message,
    }


def _message_from_line(line: bytes, *, workspace_id: str,
                       session_id: str) -> dict:
    """解析一条带归属/版本 envelope 的消息；旧无身份 JSONL 不静默认领。"""
    if not line or not line.endswith(b"\n"):
        raise DomainError("session_corrupt", 500)
    try:
        record = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise DomainError("session_corrupt", 500) from None
    if not isinstance(record, dict):
        raise DomainError("session_corrupt", 500)
    if "format" not in record or "version" not in record:
        raise DomainError("session_legacy_unowned", 409)
    if set(record) != _MESSAGE_FIELDS:
        raise DomainError("session_corrupt", 500)
    if record["format"] != MESSAGE_FORMAT:
        raise DomainError("session_corrupt", 500)
    version = record["version"]
    if type(version) is not int:
        raise DomainError("session_corrupt", 500)
    if version != MESSAGE_FORMAT_VERSION:
        raise DomainError("session_version_unsupported", 409)
    if record["workspace_id"] != workspace_id:
        raise DomainError("session_workspace_mismatch", 409)
    if record["session_id"] != session_id:
        raise DomainError("session_identity_mismatch", 409)
    message = record["message"]
    try:
        _validate_message(message)
    except DomainError:
        raise DomainError("session_corrupt", 500) from None
    return message


def _validate_messages_file(path: Path, workspace_id: str,
                            session_id: str) -> None:
    try:
        with path.open("rb") as stream:
            for line in stream:
                _message_from_line(
                    line, workspace_id=workspace_id, session_id=session_id)
    except OSError:
        raise DomainError("session_corrupt", 500) from None


def _validate_attachment(attachment: object) -> None:
    if not isinstance(attachment, dict):
        raise _invalid()
    path = attachment.get("path")
    if path is not None:
        if (not isinstance(path, str) or not path or len(path) > 1024
                or any(char in path for char in _CONTROL_CHARS)
                or "\\" in path or path.startswith("~")
                or re.match(r"^[A-Za-z]:", path) is not None):
            raise _invalid()
        pure = PurePosixPath(path)
        if pure.is_absolute() or any(part in ("", ".", "..")
                                     for part in pure.parts):
            raise _invalid()
    attachment_id = attachment.get("id")
    if attachment_id is not None and (
            not isinstance(attachment_id, str)
            or not _ATTACHMENT_ID.fullmatch(attachment_id)):
        raise _invalid()


def _validate_message(message: object) -> None:
    if not isinstance(message, dict):
        raise _invalid()
    role = message.get("role")
    if role not in MESSAGE_ROLES:
        raise _invalid()
    attachments = message.get("attachments")
    if attachments is not None:
        if (not isinstance(attachments, list)
                or len(attachments) > MAX_ATTACHMENTS):
            raise _invalid()
        for attachment in attachments:
            _validate_attachment(attachment)
    try:
        encoded = json.dumps(message, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        raise _invalid() from None
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise _invalid()


class WorkspaceSessionStore:
    """单个 workspace 的私有会话快照；不同 root/workspace 不会共享索引。"""

    def __init__(self, root: Path, workspace_id: str):
        self.root = Path(root)
        if not self.root.is_absolute():
            raise _invalid()
        self.workspace_id = _workspace_id(workspace_id)
        self._base = self.root / "sessions" / _digest(self.workspace_id)

    def _session_dir(self, session_id: str) -> Path:
        return self._base / _digest(_session_id(session_id))

    def binding_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "binding.json"

    def messages_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "messages.jsonl"

    @contextmanager
    def _locked(self, session_dir: Path) -> Iterator[None]:
        try:
            session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(session_dir / "lock",
                                 os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            raise DomainError("session_write_failed", 500) from None
        try:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            elif msvcrt is not None:
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                elif msvcrt is not None:
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            finally:
                os.close(descriptor)

    def _read_binding_locked(self, session_id: str) -> dict | None:
        data = _read_json(self.binding_path(session_id))
        if data is None:
            if self.messages_path(session_id).exists():
                raise DomainError("session_unbound", 409)
            return None
        if "workspace_id" not in data or "agent_version" not in data:
            raise DomainError("session_legacy_unowned", 409)
        if data.get("workspace_id") != self.workspace_id:
            raise DomainError("session_workspace_mismatch", 409)
        if data.get("session_id") != session_id:
            raise DomainError("session_corrupt", 500)
        if data.get("runtime") not in SUPPORTED_RUNTIMES:
            raise DomainError("session_corrupt", 500)
        try:
            _agent_version(data.get("agent_version"))
            if not isinstance(data.get("native_id"), str):
                raise _invalid()
            _cwd(data.get("cwd"))
        except DomainError:
            raise DomainError("session_corrupt", 500) from None
        return data

    def binding(self, session_id: str) -> dict:
        _session_id(session_id)
        session_dir = self._session_dir(session_id)
        if not session_dir.exists():
            return {}
        with self._locked(session_dir):
            return dict(self._read_binding_locked(session_id) or {})

    def bind(self, session_id: str, runtime: str, native_id: str,
             agent_version: int, *, cwd: str | None = None) -> None:
        _session_id(session_id)
        runtime = _runtime(runtime)
        native_id = _native_id(native_id)
        agent_version = _agent_version(agent_version)
        cwd = _cwd(cwd)
        session_dir = self._session_dir(session_id)
        with self._locked(session_dir):
            current = self._read_binding_locked(session_id)
            if current is not None:
                if current["runtime"] != runtime:
                    raise DomainError("session_binding_conflict", 409)
                if current["agent_version"] != agent_version:
                    raise DomainError("session_version_unsupported", 409)
                current_native = current.get("native_id") or ""
                if current_native and native_id != current_native:
                    raise DomainError("session_binding_conflict", 409)
                if not native_id and current_native:
                    raise DomainError("session_binding_conflict", 409)
                current_cwd = current.get("cwd")
                if current_cwd and cwd is not None and cwd != current_cwd:
                    raise DomainError("session_cwd_incompatible", 409)
                cwd = current_cwd or cwd
                native_id = native_id or current_native
            record = {
                "workspace_id": self.workspace_id,
                "session_id": session_id,
                "runtime": runtime,
                "native_id": native_id,
                "agent_version": agent_version,
            }
            if cwd is not None:
                record["cwd"] = cwd
            _atomic_write(self.binding_path(session_id), record)

    def validate_message(self, message: dict) -> None:
        """纯输入校验：不建目录、不读写历史，供 Run 认领前调用。"""
        _validate_message(message)

    def _append_locked(self, session_id: str, message: dict) -> None:
        """在会话锁内原子追加一行：有界流式复制 + fsync + replace。

        逐行校验并复制既有记录到同目录临时文件，再原子替换；不把整份历史
        读入内存，内存峰值只与单条记录大小相关。直接 append 在进程写一半
        退出时会留下半行，因此绝不退回直接追加；也绝不截断或认领任意损坏/
        错误身份的 JSONL。
        """
        path = self.messages_path(session_id)
        directory = path.parent
        # 清理崩溃遗留的临时文件；它们从未提交，也不是可信消息记录
        for stale in directory.glob(f".{path.name}.*.tmp"):
            try:
                stale.unlink()
            except OSError:
                pass
        record = _message_record(
            self.workspace_id, session_id, message)
        encoded = json.dumps(
            record, ensure_ascii=False,
            separators=(",", ":")).encode("utf-8") + b"\n"
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                dir=directory, prefix=f".{path.name}.", suffix=".tmp")
            temporary = Path(name)
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(descriptor, 0o600)
                if path.exists():
                    # 读取/校验异常只归为损坏；写入临时文件失败仍按磁盘
                    # 写入失败处理，不能误报为消息损坏。
                    def validated_lines():
                        try:
                            with path.open("rb") as source:
                                for line in source:
                                    _message_from_line(
                                        line,
                                        workspace_id=self.workspace_id,
                                        session_id=session_id)
                                    if not line.endswith(b"\n"):
                                        raise DomainError(
                                            "session_corrupt", 500)
                                    yield line
                        except OSError:
                            raise DomainError(
                                "session_corrupt", 500) from None

                    for line in validated_lines():
                        _write_all(descriptor, line)
                _write_all(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, path)
            temporary = None
            if os.name == "posix":
                # 目录项持久化，确保 replace 在掉电后仍可见
                directory_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except DomainError:
            raise
        except OSError:
            raise DomainError("session_write_failed", 500) from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def append_message(self, session_id: str, message: dict) -> None:
        _session_id(session_id)
        self.validate_message(message)
        session_dir = self._session_dir(session_id)
        with self._locked(session_dir):
            if self._read_binding_locked(session_id) is None:
                raise DomainError("session_unbound", 409)
            self._append_locked(session_id, message)

    def append_message_once(self, session_id: str, run_id: str,
                            message: dict) -> bool:
        """按 Run 幂等追加最终助手输出；返回是否真正写入。

        落盘后、提交 done 前崩溃时，恢复流程会再次调用本方法；扫描已有
        ``runId`` 标记可避免重复追加。标记只存在消息内部，不改变文件格式。
        """
        _session_id(session_id)
        run_id = _identifier(run_id)
        self.validate_message(message)
        session_dir = self._session_dir(session_id)
        with self._locked(session_dir):
            if self._read_binding_locked(session_id) is None:
                raise DomainError("session_unbound", 409)
            path = self.messages_path(session_id)
            if path.exists():
                try:
                    with path.open("rb") as stream:
                        for line in stream:
                            existing = _message_from_line(
                                line, workspace_id=self.workspace_id,
                                session_id=session_id)
                            if existing.get("runId") == run_id:
                                return False
                except OSError:
                    raise DomainError("session_corrupt", 500) from None
            self._append_locked(session_id, message)
            return True

    def messages(self, session_id: str, *, limit: int = 100,
                 offset: int = 0) -> list[dict]:
        _session_id(session_id)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise _invalid()
        if type(offset) is not int or offset < 0:
            raise _invalid()
        session_dir = self._session_dir(session_id)
        if not session_dir.exists():
            return []
        with self._locked(session_dir):
            if self._read_binding_locked(session_id) is None:
                path = self.messages_path(session_id)
                if path.exists():
                    raise DomainError("session_unbound", 409)
                return []
            path = self.messages_path(session_id)
            if not path.exists():
                return []
            result: list[dict] = []
            index = 0
            try:
                with path.open("rb") as stream:
                    for line in stream:
                        message = _message_from_line(
                            line, workspace_id=self.workspace_id,
                            session_id=session_id)
                        if offset <= index < offset + limit:
                            result.append(message)
                        index += 1
            except OSError:
                raise DomainError("session_corrupt", 500) from None
            return result

    def count_messages(self, session_id: str) -> int:
        """只读统计消息条数；用于前端最新窗口与可分页历史。"""
        _session_id(session_id)
        session_dir = self._session_dir(session_id)
        if not session_dir.exists():
            return 0
        with self._locked(session_dir):
            if self._read_binding_locked(session_id) is None:
                path = self.messages_path(session_id)
                if path.exists():
                    raise DomainError("session_unbound", 409)
                return 0
            path = self.messages_path(session_id)
            if not path.exists():
                return 0
            total = 0
            try:
                with path.open("rb") as stream:
                    for line in stream:
                        _message_from_line(
                            line, workspace_id=self.workspace_id,
                            session_id=session_id)
                        total += 1
            except OSError:
                raise DomainError("session_corrupt", 500) from None
            return total
