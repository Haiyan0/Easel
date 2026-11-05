"""database-query 多人部署的隧道拓扑解析与校验。

本模块只依赖标准库与 PyYAML；会被 sidecar 与测试以文件路径直接加载，
因此不使用包相对导入。所有校验失败统一抛 ``TunnelConfigError``，
对外只暴露固定错误码 ``config_invalid``，不拼接路径或配置值。
"""

from __future__ import annotations

import asyncio
import collections.abc
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DB_TUNNEL_CONFIG = Path(
    "/opt/easel/skills/builtin/database-query/assets/tunnel.multiuser.yaml")
DB_TUNNEL_KEYS = Path("/run/easel-secrets/keys")
DB_TUNNEL_ALIAS = "db-tunnel"

_SOURCE_ID_PATTERN = re.compile(r"[a-z][a-z0-9_-]{2,63}")
_HOST_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,253}")
_USER_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")

_LISTEN_PORT_MIN = 13300
_LISTEN_PORT_MAX = 13399
_PORT_MIN = 1
_PORT_MAX = 65535

_PLACEHOLDER_HOST = "example.com"
_PLACEHOLDER_SUFFIXES = (".example.com", ".example", ".invalid", ".test")

_FORWARD_KEYS = {
    "direct": frozenset({"source", "mode", "listen_port", "database"}),
    "ssh": frozenset(
        {"source", "mode", "listen_port", "database", "ssh"}),
}
_DATABASE_KEYS = frozenset({"host", "port"})
_SSH_KEYS = frozenset(
    {"host", "port", "user", "private_key", "known_hosts"})


class TunnelConfigError(Exception):
    """拓扑配置非法；对外只暴露固定错误码。"""

    code = "config_invalid"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True)
class DatabaseTarget:
    host: str
    port: int


@dataclass(frozen=True)
class SshTarget:
    host: str
    port: int
    user: str
    private_key: Path
    known_hosts: Path


@dataclass(frozen=True)
class Forward:
    source: str
    mode: str
    listen_port: int
    database: DatabaseTarget
    ssh: SshTarget | None


class _UniqueKeyLoader(yaml.SafeLoader):
    """在 SafeLoader 基础上拒绝重复的映射键。"""

    def construct_mapping(  # type: ignore[override]
            self, node: yaml.nodes.Node, deep: bool = False) -> dict:
        if not isinstance(node, yaml.nodes.MappingNode):
            raise TunnelConfigError()
        self.flatten_mapping(node)
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, collections.abc.Hashable):
                raise TunnelConfigError()
            if key in mapping:
                raise TunnelConfigError()
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def load_forwards(
    path: str | Path, *,
    keys_dir: str | Path | None = None,
    allow_placeholders: bool = False,
) -> tuple[Forward, ...]:
    """解析并严格校验拓扑；非法一律抛 TunnelConfigError。"""
    raw = _load_document(path)
    return _parse_forwards(
        raw,
        keys_dir=Path(keys_dir) if keys_dir is not None else DB_TUNNEL_KEYS,
        allow_placeholders=allow_placeholders,
    )


def _load_document(path: str | Path) -> Any:
    try:
        text = Path(path).read_text(encoding="utf-8")
        return yaml.load(text, Loader=_UniqueKeyLoader)
    except TunnelConfigError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError):
        raise TunnelConfigError() from None


def _parse_forwards(
    raw: Any, *, keys_dir: Path, allow_placeholders: bool,
) -> tuple[Forward, ...]:
    if not isinstance(raw, dict) or set(raw) != {"version", "forwards"}:
        raise TunnelConfigError()
    version = raw["version"]
    if type(version) is not int or version != 1:
        raise TunnelConfigError()
    raw_forwards = raw["forwards"]
    if not isinstance(raw_forwards, list) or not raw_forwards:
        raise TunnelConfigError()

    forwards: list[Forward] = []
    seen_sources: set[str] = set()
    seen_ports: set[int] = set()
    for item in raw_forwards:
        forward = _parse_forward(
            item, keys_dir=keys_dir, allow_placeholders=allow_placeholders)
        if (forward.source in seen_sources
                or forward.listen_port in seen_ports):
            raise TunnelConfigError()
        seen_sources.add(forward.source)
        seen_ports.add(forward.listen_port)
        forwards.append(forward)
    return tuple(forwards)


def _parse_forward(
    item: Any, *, keys_dir: Path, allow_placeholders: bool,
) -> Forward:
    if not isinstance(item, dict):
        raise TunnelConfigError()
    mode = item.get("mode")
    if not isinstance(mode, str) or mode not in _FORWARD_KEYS:
        raise TunnelConfigError()
    if set(item) != _FORWARD_KEYS[mode]:
        raise TunnelConfigError()

    source = item["source"]
    if not isinstance(source, str) or _SOURCE_ID_PATTERN.fullmatch(
            source) is None:
        raise TunnelConfigError()

    listen_port = item["listen_port"]
    if type(listen_port) is not int or not (
            _LISTEN_PORT_MIN <= listen_port <= _LISTEN_PORT_MAX):
        raise TunnelConfigError()

    database = _parse_database(
        item["database"], allow_placeholders=allow_placeholders)

    ssh: SshTarget | None = None
    if mode == "ssh":
        ssh = _parse_ssh(
            item["ssh"], keys_dir=keys_dir,
            allow_placeholders=allow_placeholders)

    return Forward(
        source=source,
        mode=mode,
        listen_port=listen_port,
        database=database,
        ssh=ssh,
    )


def _parse_database(
    raw: Any, *, allow_placeholders: bool,
) -> DatabaseTarget:
    if not isinstance(raw, dict) or set(raw) != _DATABASE_KEYS:
        raise TunnelConfigError()
    return DatabaseTarget(
        host=_validate_host(
            raw["host"], allow_placeholders=allow_placeholders),
        port=_validate_port(raw["port"]),
    )


def _parse_ssh(
    raw: Any, *, keys_dir: Path, allow_placeholders: bool,
) -> SshTarget:
    if not isinstance(raw, dict) or set(raw) != _SSH_KEYS:
        raise TunnelConfigError()
    host = _validate_host(raw["host"], allow_placeholders=allow_placeholders)
    port = _validate_port(raw["port"])
    user = raw["user"]
    if not isinstance(user, str) or _USER_PATTERN.fullmatch(user) is None:
        raise TunnelConfigError()
    return SshTarget(
        host=host,
        port=port,
        user=user,
        private_key=_validate_key_path(
            raw["private_key"], keys_dir=keys_dir,
            allow_placeholders=allow_placeholders),
        known_hosts=_validate_key_path(
            raw["known_hosts"], keys_dir=keys_dir,
            allow_placeholders=allow_placeholders),
    )


def _validate_host(value: Any, *, allow_placeholders: bool) -> str:
    if not isinstance(value, str) or _HOST_PATTERN.fullmatch(value) is None:
        raise TunnelConfigError()
    if not allow_placeholders and _is_placeholder_host(value):
        raise TunnelConfigError()
    return value


def _is_placeholder_host(host: str) -> bool:
    lowered = host.lower()
    if lowered in {"localhost", _PLACEHOLDER_HOST}:
        return True
    return any(lowered.endswith(suffix) for suffix in _PLACEHOLDER_SUFFIXES)


def _validate_port(value: Any) -> int:
    if type(value) is not int or not (_PORT_MIN <= value <= _PORT_MAX):
        raise TunnelConfigError()
    return value


def _validate_key_path(
    value: Any, *, keys_dir: Path, allow_placeholders: bool,
) -> Path:
    name = _bare_filename(value)
    if allow_placeholders:
        # 模板只做相对路径约束，跳过存在性与符号链接检查。
        return keys_dir / name
    try:
        candidate = keys_dir / name
        if candidate.is_symlink():
            raise TunnelConfigError()
        resolved = candidate.resolve(strict=True)
        resolved_keys_dir = keys_dir.resolve(strict=True)
    except TunnelConfigError:
        raise
    except OSError:
        raise TunnelConfigError() from None
    if not resolved.is_file() or not resolved.is_relative_to(
            resolved_keys_dir):
        raise TunnelConfigError()
    return resolved


def _bare_filename(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise TunnelConfigError()
    if "/" in value or "\\" in value or value in {".", ".."}:
        raise TunnelConfigError()
    if Path(value).is_absolute() or Path(value).drive:
        raise TunnelConfigError()
    return value


# ---------------------------------------------------------------------
# direct 转发运行时
# ---------------------------------------------------------------------

DIRECT_CONNECT_TIMEOUT = 10.0
DIRECT_MAX_CONNECTIONS = 64

_PIPE_CHUNK_SIZE = 65536


def log_state(source: str, mode: str, state: str,
              reason: str = "") -> None:
    """固定格式写 stderr；source/mode 为空时省略对应字段。"""
    parts = ["db-tunnel"]
    if source:
        parts.append(f"source={source}")
    if mode:
        parts.append(f"mode={mode}")
    parts.append(f"state={state}")
    parts.append(f"reason={reason}")
    print(" ".join(parts), file=sys.stderr, flush=True)


async def serve_direct(
    forward: Forward, *,
    host: str = "0.0.0.0",
    connect_timeout: float = DIRECT_CONNECT_TIMEOUT,
    max_connections: int = DIRECT_MAX_CONNECTIONS,
    emit=log_state,
) -> asyncio.AbstractServer:
    """启动 direct 转发；绑定失败时记录 failed 并抛 OSError。"""
    forwarder = _DirectForward(
        forward,
        host=host,
        connect_timeout=connect_timeout,
        max_connections=max_connections,
        emit=emit,
    )
    return await forwarder.serve()


class _DirectForward:
    """单个 direct forward：监听本地端口并双向转发到数据库。"""

    def __init__(
        self, forward: Forward, *, host: str, connect_timeout: float,
        max_connections: int, emit,
    ) -> None:
        self._forward = forward
        self._host = host
        self._connect_timeout = connect_timeout
        self._max_connections = max_connections
        self._emit = emit
        self._active = 0

    async def serve(self) -> asyncio.AbstractServer:
        """监听端口并返回 server；绑定失败记录 failed 后重新抛出。"""
        source = self._forward.source
        self._emit(source, "direct", "starting", "")
        try:
            server = await asyncio.start_server(
                self._handle, self._host, self._forward.listen_port)
        except OSError:
            self._emit(source, "direct", "failed", "forward_failed")
            raise
        self._emit(source, "direct", "ready", "")
        return server

    async def _handle(
        self, client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        if self._active >= self._max_connections:
            # 超出并发上限：立即关闭，不建立上游连接。
            client_writer.close()
            return
        self._active += 1
        try:
            try:
                target_reader, target_writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        self._forward.database.host,
                        self._forward.database.port),
                    self._connect_timeout)
            except (OSError, asyncio.TimeoutError):
                await _close_writer(client_writer)
                return
            try:
                await asyncio.gather(
                    _pipe(client_reader, target_writer),
                    _pipe(target_reader, client_writer),
                    return_exceptions=True)
            finally:
                await asyncio.gather(
                    _close_writer(client_writer),
                    _close_writer(target_writer),
                    return_exceptions=True)
        finally:
            self._active -= 1


async def _pipe(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
) -> None:
    """单向复制直到 EOF，随后关闭目标写端。"""
    try:
        while chunk := await reader.read(_PIPE_CHUNK_SIZE):
            writer.write(chunk)
            await writer.drain()
    finally:
        writer.close()


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
