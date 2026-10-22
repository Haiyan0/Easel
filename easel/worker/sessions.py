"""worker 私有会话快照与原生绑定。

所有用户提供的标识只用于哈希，不进入路径；数据位于
``<root>/sessions/<workspace-hash>/<session-hash>/``。绑定和消息追加都在
跨进程文件锁内完成，写入使用临时文件 + fsync + replace，损坏或旧无归属
记录明确失败，不当作空历史。
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
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_ATTACHMENTS = 20

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


def _parse_messages(data: bytes) -> list[dict]:
    if not data:
        return []
    if not data.endswith(b"\n"):
        raise DomainError("session_corrupt", 500)
    messages: list[dict] = []
    for line in data.splitlines():
        if not line:
            raise DomainError("session_corrupt", 500)
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise DomainError("session_corrupt", 500) from None
        if not isinstance(message, dict):
            raise DomainError("session_corrupt", 500)
        messages.append(message)
    return messages


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
        session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(session_dir / "lock", os.O_CREAT | os.O_RDWR,
                             0o600)
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

    def append_message(self, session_id: str, message: dict) -> None:
        _session_id(session_id)
        _validate_message(message)
        session_dir = self._session_dir(session_id)
        with self._locked(session_dir):
            if self._read_binding_locked(session_id) is None:
                raise DomainError("session_unbound", 409)
            path = self.messages_path(session_id)
            if path.exists():
                try:
                    _parse_messages(path.read_bytes())
                except OSError:
                    raise DomainError("session_corrupt", 500) from None
            try:
                encoded = json.dumps(
                    message, ensure_ascii=False,
                    separators=(",", ":")).encode("utf-8") + b"\n"
                descriptor = os.open(
                    path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    view = memoryview(encoded)
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:
                            raise OSError("short write")
                        view = view[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except OSError:
                raise DomainError("session_write_failed", 500) from None

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
            try:
                data = path.read_bytes()
            except OSError:
                raise DomainError("session_corrupt", 500) from None
            return _parse_messages(data)[offset:offset + limit]
