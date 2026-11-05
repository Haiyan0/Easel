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
import time
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


# ---------------------------------------------------------------------
# SSH 转发运行时：固定参数与失败码映射
# ---------------------------------------------------------------------

# 按优先级排列的 stderr 片段表；全部小写比较，只返回固定错误码。
_SSH_FAILURE_PATTERNS = (
    (("host key verification failed",), "host_key_mismatch"),
    (("load key", "identity file", "bad permissions"), "key_unreadable"),
    (("permission denied",), "auth_failed"),
    (("local forwarding", "forwarding failed", "cannot listen"),
     "forward_failed"),
    (("connect to host", "connection refused", "timed out",
      "could not resolve", "network is unreachable"), "connect_failed"),
)


def build_ssh_argv(forward: Forward) -> list[str]:
    """返回固定顺序、固定选项的 ssh argv；forward.mode 必须是 ssh。"""
    ssh = forward.ssh
    if forward.mode != "ssh" or ssh is None:
        raise TunnelConfigError()
    argv = ["/usr/bin/ssh", "-F", "/dev/null", "-N", "-T",
            "-i", str(ssh.private_key)]
    # 严格按 spec §6.3 的顺序；任何一项都不得被配置覆盖（fail closed）。
    for option in (
        "IdentitiesOnly=yes",
        "BatchMode=yes",
        "PreferredAuthentications=publickey",
        "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no",
        "IdentityAgent=none",
        "ForwardAgent=no",
        "StrictHostKeyChecking=yes",
        f"UserKnownHostsFile={ssh.known_hosts}",
        "GlobalKnownHostsFile=/dev/null",
        "UpdateHostKeys=no",
        "ExitOnForwardFailure=yes",
        "ConnectTimeout=10",
        "ServerAliveInterval=30",
        "ServerAliveCountMax=3",
        "ControlMaster=no",
        "ControlPath=none",
    ):
        argv.extend(("-o", option))
    argv.extend((
        "-p", str(ssh.port),
        "-L",
        f"0.0.0.0:{forward.listen_port}:"
        f"{forward.database.host}:{forward.database.port}",
        f"{ssh.user}@{ssh.host}",
    ))
    return argv


def classify_ssh_stderr(text: str) -> str:
    """把 ssh stderr 映射为固定错误码；不返回原文。"""
    lowered = text.lower()
    for fragments, reason in _SSH_FAILURE_PATTERNS:
        if any(fragment in lowered for fragment in fragments):
            return reason
    return "connect_failed"


# ---------------------------------------------------------------------
# SSH 转发运行时：常驻监督
# ---------------------------------------------------------------------

SSH_BACKOFF = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
SSH_STABLE_SECONDS = 120.0
SSH_STOP_TIMEOUT = 5.0

_SSH_READY_POLL = 0.1
_SSH_READY_TIMEOUT = 10.0
_SSH_STDERR_LIMIT = 65536
_SSH_STDERR_TIMEOUT = 1.0


async def _spawn_ssh(argv: list[str]) -> asyncio.subprocess.Process:
    """以最小环境启动 ssh；不继承 worker/agent 环境。"""
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env={"HOME": "/tmp", "LC_ALL": "C"},
    )


async def _probe_forward_port(port: int) -> bool:
    """尝试连接本地转发端口；连上即视为就绪。"""
    try:
        _reader, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


class SshForwardSupervisor:
    """常驻监督单个 ssh 转发：退出或失败后退避重启，直到 stop()。"""

    def __init__(
        self, forward: Forward, *, emit=log_state,
        spawn=None, sleep=asyncio.sleep, clock=time.monotonic,
        ready_probe=None, backoff: tuple[float, ...] = SSH_BACKOFF,
        stable_after: float = SSH_STABLE_SECONDS,
        stop_timeout: float = SSH_STOP_TIMEOUT,
    ) -> None:
        self._forward = forward
        self._emit = emit
        self._spawn = _spawn_ssh if spawn is None else spawn
        self._sleep = sleep
        self._clock = clock
        self._ready_probe = (
            _probe_forward_port if ready_probe is None else ready_probe)
        self._backoff = tuple(backoff) or SSH_BACKOFF
        self._stable_after = stable_after
        self._stop_timeout = stop_timeout
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._process = None
        self._wait_task: asyncio.Task | None = None

    async def run(self) -> None:
        """常驻监督 ssh 子进程，直到 stop() 被调用。"""
        self._task = asyncio.current_task()
        argv = build_ssh_argv(self._forward)
        source = self._forward.source
        index = 0
        try:
            while not self._stop_event.is_set():
                self._emit(source, "ssh", "starting", "")
                started_at = self._clock()
                try:
                    process = await self._spawn(argv)
                except FileNotFoundError:
                    reason = "ssh_unavailable"
                except OSError:
                    reason = "connect_failed"
                else:
                    reason = ""
                if reason:
                    # 进程都没能启动：同样按退避重试。
                    self._emit(source, "ssh", "failed", reason)
                    if await self._wait_backoff(index):
                        break
                    index = self._next_index(index)
                    continue

                self._process = process
                # 只创建一次 wait() 任务，之后只 await 该任务。
                wait_task = asyncio.create_task(process.wait())
                self._wait_task = wait_task
                state = await self._wait_ready(wait_task)
                if state == "stopped":
                    break
                if state == "ready":
                    self._emit(source, "ssh", "ready", "")
                    if await self._wait_exit_or_stop(wait_task):
                        break
                    reason = classify_ssh_stderr(
                        await self._read_stderr(process))
                elif state == "timeout":
                    # 端口 10 秒未就绪：固定 forward_failed 并回收进程。
                    reason = "forward_failed"
                    await self._terminate(process, wait_task)
                else:
                    reason = classify_ssh_stderr(
                        await self._read_stderr(process))
                self._process = None
                if self._stop_event.is_set():
                    break
                self._emit(source, "ssh", "failed", reason)
                if self._clock() - started_at >= self._stable_after:
                    index = 0
                if await self._wait_backoff(index):
                    break
                index = self._next_index(index)
        finally:
            await self._shutdown()
            self._emit(source, "ssh", "stopped", "")

    async def stop(self) -> None:
        """请求停止：先 SIGTERM 当前子进程，再等待 run 收尾。"""
        self._stop_event.set()
        process = self._process
        if process is not None and process.returncode is None:
            process.terminate()
        task = self._task
        if task is not None and task is not asyncio.current_task():
            await task

    def _next_index(self, index: int) -> int:
        """前进到下一档退避，封顶为最后一档（30 秒）。"""
        return min(index + 1, len(self._backoff) - 1)

    async def _wait_ready(self, wait_task: asyncio.Task) -> str:
        """竞速端口就绪与进程退出；返回 ready/exited/timeout/stopped。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _SSH_READY_TIMEOUT
        port = self._forward.listen_port
        while True:
            if self._stop_event.is_set():
                return "stopped"
            if wait_task.done():
                return "exited"
            remaining = deadline - loop.time()
            if remaining <= 0:
                return "timeout"
            try:
                ready = await asyncio.wait_for(
                    self._ready_probe(port),
                    min(_SSH_READY_POLL, remaining))
            except (asyncio.TimeoutError, OSError):
                ready = False
            if wait_task.done():
                return "exited"
            if self._stop_event.is_set():
                return "stopped"
            if ready:
                return "ready"
            # 未就绪：最多等一个轮询间隔，进程退出时立即返回。
            await asyncio.wait({wait_task}, timeout=_SSH_READY_POLL)

    async def _wait_exit_or_stop(self, wait_task: asyncio.Task) -> bool:
        """等待子进程退出或停止信号；返回 True 表示因停止结束。"""
        stop_task = asyncio.ensure_future(self._stop_event.wait())
        try:
            await asyncio.wait(
                {wait_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not stop_task.done():
                stop_task.cancel()
                await asyncio.gather(stop_task, return_exceptions=True)
        return self._stop_event.is_set()

    async def _wait_backoff(self, index: int) -> bool:
        """可被停止中断的退避等待；返回 True 表示已收到停止。"""
        if self._stop_event.is_set():
            return True
        sleep_task = asyncio.ensure_future(self._sleep(self._backoff[index]))
        stop_task = asyncio.ensure_future(self._stop_event.wait())
        try:
            await asyncio.wait(
                {sleep_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pending in (sleep_task, stop_task):
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
        return self._stop_event.is_set()

    async def _read_stderr(self, process) -> str:
        """读取有限 stderr 供映射错误码；任何失败都视为空。"""
        stream = getattr(process, "stderr", None)
        if stream is None:
            return ""
        try:
            chunk = await asyncio.wait_for(
                stream.read(_SSH_STDERR_LIMIT), _SSH_STDERR_TIMEOUT)
        except (asyncio.TimeoutError, OSError, ValueError):
            return ""
        if not isinstance(chunk, bytes):
            return ""
        return chunk.decode("utf-8", errors="replace")

    async def _terminate(self, process, wait_task: asyncio.Task) -> None:
        """先 SIGTERM，超时后 SIGKILL，并等待进程退出。"""
        if process.returncode is not None:
            return
        process.terminate()
        if await self._wait_task_done(wait_task):
            return
        if process.returncode is None:
            process.kill()
        await self._wait_task_done(wait_task)

    async def _wait_task_done(self, wait_task: asyncio.Task) -> bool:
        """等待 wait() 任务，最多 stop_timeout 秒；返回是否已退出。"""
        try:
            await asyncio.wait_for(
                asyncio.shield(wait_task), self._stop_timeout)
        except asyncio.TimeoutError:
            return False
        return True

    async def _shutdown(self) -> None:
        """run 收尾：回收仍存活的子进程。"""
        process = self._process
        wait_task = self._wait_task
        if process is not None and wait_task is not None:
            await self._terminate(process, wait_task)


# ---------------------------------------------------------------------
# sidecar 集成：按固定拓扑启动 direct 与 ssh 转发
# ---------------------------------------------------------------------


@dataclass
class TunnelRuntime:
    """一次启动产生的转发资源；stop() 可安全重复调用。"""

    tasks: list[asyncio.Task]
    servers: list[asyncio.AbstractServer]
    supervisors: list[SshForwardSupervisor]

    async def stop(self) -> None:
        """先停 ssh 监督器，再取消任务，最后关闭 direct 监听。"""
        # 并发停止所有监督器：单个 ssh 的 terminate/kill 已有界，串行等待会
        # 让多个 forward 的总停机时间超过 sidecar 的 --stop-timeout。
        active = [supervisor for supervisor, task
                  in zip(self.supervisors, self.tasks) if not task.done()]
        if active:
            # return_exceptions：单个监督器故障不能阻止其余清理。
            await asyncio.gather(
                *(supervisor.stop() for supervisor in active),
                return_exceptions=True)
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        for server in self.servers:
            server.close()
        for server in self.servers:
            await server.wait_closed()


async def start_tunnel(
    *, keys_dir: Path = DB_TUNNEL_KEYS,
    config_path: Path = DB_TUNNEL_CONFIG,
    emit=log_state,
    listen_host: str = "0.0.0.0",
) -> TunnelRuntime | None:
    """keys_dir 不存在返回 None；存在但配置非法返回空 runtime 并记录。"""
    keys_path = Path(keys_dir)
    if not keys_path.exists():
        # 未启用多人数据库隧道是预期状态，静默禁用。
        return None
    if not keys_path.is_dir():
        # 存在但不是目录属于配置错误，不能与“未启用”一样静默。
        emit("", "", "failed", "config_invalid")
        return TunnelRuntime(tasks=[], servers=[], supervisors=[])
    try:
        forwards = load_forwards(config_path, keys_dir=keys_dir)
    except TunnelConfigError:
        # 数据库配置错误不能拖垮 broker/egress：只记录固定错误码。
        emit("", "", "failed", "config_invalid")
        return TunnelRuntime(tasks=[], servers=[], supervisors=[])

    tasks: list[asyncio.Task] = []
    servers: list[asyncio.AbstractServer] = []
    supervisors: list[SshForwardSupervisor] = []
    for forward in forwards:
        if forward.mode == "direct":
            try:
                server = await serve_direct(
                    forward, host=listen_host, emit=emit)
            except OSError:
                # serve_direct 已记录 failed/forward_failed；单条失败继续下一条。
                continue
            servers.append(server)
        else:
            supervisor = SshForwardSupervisor(forward, emit=emit)
            supervisors.append(supervisor)
            tasks.append(asyncio.create_task(supervisor.run()))
    return TunnelRuntime(tasks=tasks, servers=servers, supervisors=supervisors)
