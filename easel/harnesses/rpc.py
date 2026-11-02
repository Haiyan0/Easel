"""JSON-RPC over private stdio, with bounded reads and process-tree cleanup."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
from contextlib import asynccontextmanager

from .base import HarnessError, RunRequest
from .workspace import harness_instructions


_POOLS: dict[asyncio.AbstractEventLoop, dict[tuple, dict]] = {}
IDLE_SECONDS = 300
MAX_IDLE_PROCESSES = 8
# Bound outstanding reverse RPCs (permissions/questions), not tool executions.
MAX_CLIENT_REQUESTS = 64
# stderr 只保留固定上限供诊断；读取循环仍持续排空到 EOF，防止大输出/无换行
# 把子进程或父进程阻塞。
MAX_STDERR_BYTES = 4096

# 只有 JSON-RPC error.data 里的结构化字段才能作为分类证据；message 文本
# 可能混有提示词、Key 或用户路径，绝不参与分类。
_EVIDENCE_STATUS_FIELDS = ("status", "statusCode", "status_code",
                           "httpStatus", "http_status")
_EVIDENCE_CODE_CATEGORIES = {
    "unauthorized": "auth",
    "forbidden": "auth",
    "invalid_token": "auth",
    "authentication_failed": "auth",
    "rate_limited": "rate_limited",
    "rate_limit_exceeded": "rate_limited",
    "too_many_requests": "rate_limited",
    "invalid_request": "request_rejected",
    "bad_request": "request_rejected",
    "validation_error": "request_rejected",
    "service_unavailable": "service_unavailable",
    "upstream_unavailable": "service_unavailable",
    "overloaded": "service_unavailable",
}


def _status_value(value: object) -> int | None:
    if type(value) is int:
        status = value
    elif (type(value) is str and len(value) <= 3
          and value.isascii() and value.isdigit()):
        # 先限长再做 ASCII 十进制校验，最后才 int()；任意长度/Unicode 数字
        # 以及超长字符串都不能抛到 reader。
        status = int(value)
    else:
        return None
    return status if 100 <= status <= 599 else None


def _category_for_status(status: int) -> str | None:
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limited"
    if status in (400, 413, 415, 422):
        return "request_rejected"
    if 500 <= status <= 599:
        return "service_unavailable"
    return None


def classify_error_evidence(error: object) -> tuple[str | None, int | None]:
    """只读取结构化字段；没有证据时返回 (None, None)，不猜鉴权/限速。"""
    if not isinstance(error, dict):
        return None, None
    data = error.get("data")
    if not isinstance(data, dict):
        return None, None
    status = None
    for field in _EVIDENCE_STATUS_FIELDS:
        candidate = _status_value(data.get(field))
        if candidate is None:
            continue
        if status is None:
            status = candidate
        category = _category_for_status(candidate)
        if category is not None:
            return category, candidate
    # 状态码无效/未映射时继续检查已知 code/error；都无效才回 unknown。
    for field in ("code", "error"):
        value = data.get(field)
        if type(value) is str:
            category = _EVIDENCE_CODE_CATEGORIES.get(value.lower())
            if category is not None:
                return category, status
    return None, status


async def close_sessions(session_key: str | None = None,
                         workspace_id: str | None = None):
    """关闭指定 namespace 的会话；workspace_id 为空时匹配全部（local 兼容）。

    内部调用必须同时传入 workspace_id 与 session_key，避免两个 workspace
    使用相同 session_key 时错误关闭对方的私有 stdio 进程。
    """
    loop = asyncio.get_running_loop()
    pool = _POOLS.get(loop, {})
    selected = [(key, entry) for key, entry in list(pool.items())
                if (workspace_id is None or key[1] == workspace_id)
                and (session_key is None or key[2] == session_key)]
    for key, entry in selected:
        pool.pop(key, None)
        if entry["timer"]:
            entry["timer"].cancel()
    if not pool:
        _POOLS.pop(loop, None)
    for key, entry in selected:
        await entry["rpc"].__aexit__()


@asynccontextmanager
async def connection(request: RunRequest, command: list[str], notify, respond, *, acp=False):
    if not request.session_key:
        async with RPC(request, command, notify, respond, acp=acp) as rpc:
            yield rpc
        return
    loop = asyncio.get_running_loop()
    pool = _POOLS.setdefault(loop, {})
    # namespace 必须覆盖 workspace：不同空间的同名会话不能共用进程或锁
    key = (acp, request.workspace_id, request.session_key)
    signature = hashlib.sha256(json.dumps([
        str(request.cwd.resolve()), sorted(request.env.items()), command,
        request.model, request.sandbox, harness_instructions(request),
    ], ensure_ascii=False).encode("utf-8")).hexdigest()
    entry = pool.get(key)
    previous = None
    if entry and entry["busy"]:
        raise HarnessError("此 Agent 会话正在执行，请等待本轮完成")
    if entry and (entry["signature"] != signature
                  or entry["rpc"].native_id != request.native_id
                  or entry["rpc"].reader.done()
                  or entry["rpc"].process.returncode is not None):
        previous = entry
        if previous["timer"]:
            previous["timer"].cancel()
        pool.pop(key, None)
        entry = None
    if entry is None:
        rpc = RPC(request, command, notify, respond, acp=acp)
        entry = {"rpc": rpc, "signature": signature, "busy": True, "timer": None}
        pool[key] = entry
        try:
            if previous:
                await previous["rpc"].__aexit__()
            idle = [(old_key, value) for old_key, value in pool.items() if not value["busy"]]
            if len(idle) >= MAX_IDLE_PROCESSES:
                await close_sessions(idle[0][0][2],
                                     workspace_id=idle[0][0][1])
            await rpc.__aenter__()
        except BaseException:
            await close_sessions(request.session_key,
                                 workspace_id=request.workspace_id)
            raise
    rpc = entry["rpc"]
    if entry["timer"]:
        entry["timer"].cancel()
        entry["timer"] = None
    entry["busy"] = True
    rpc.notify = notify
    rpc.respond = respond
    rpc.client_error = None
    try:
        yield rpc
    except BaseException:
        await close_sessions(request.session_key,
                             workspace_id=request.workspace_id)
        raise
    else:
        while True:
            idle = [(old_key, value) for old_key, value in pool.items()
                    if not value["busy"] and old_key != key]
            if len(idle) < MAX_IDLE_PROCESSES:
                break
            await close_sessions(idle[0][0][2],
                                 workspace_id=idle[0][0][1])
        if pool.get(key) is not entry:
            return
        rpc.notify = lambda method, params: None

        async def reject_idle(method, params):
            raise HarnessError("会话空闲时不允许执行工具")

        rpc.respond = reject_idle

        async def expire():
            await asyncio.sleep(IDLE_SECONDS)
            entry["timer"] = None
            await close_sessions(request.session_key,
                                 workspace_id=request.workspace_id)

        entry["timer"] = asyncio.create_task(expire())
        entry["busy"] = False


class RPC:
    def __init__(self, request: RunRequest, command: list[str], notify, respond, *, acp=False):
        self.request = request
        self.command = command
        self.notify = notify
        self.respond = respond
        self.acp = acp
        self.pending: dict[int, asyncio.Future] = {}
        self.sequence = 0
        self.tasks: set[asyncio.Task] = set()
        self.process = None
        self.reader = None
        self.stderr = None
        self.stderr_buffer = b""
        self.stderr_total = 0
        self.write_lock = asyncio.Lock()
        self.client_error = None
        self.reader_error = None
        self.initialized = None
        self.native_id = ""

    async def __aenter__(self):
        env = {**self.request.env, "EASEL_ROOT": str(self.request.cwd.resolve()),
               "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.command, cwd=str(self.request.cwd), env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=4 * 1024 * 1024,
                start_new_session=os.name != "nt")
        except OSError as error:
            raise HarnessError("无法启动 Agent CLI，请检查安装与 PATH",
                               stage="cli_start") from error
        self.reader = asyncio.create_task(self._read())
        self.stderr = asyncio.create_task(self._drain_stderr())
        return self

    async def send(self, message: dict):
        if self.acp:
            message = {"jsonrpc": "2.0", **message}
        async with self.write_lock:
            try:
                self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as error:
                raise HarnessError("Agent 进程已退出，请检查 CLI 登录与模型配置") from error

    async def call(self, method: str, params: dict, timeout=60, *,
                   stage: str | None = None):
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"id": request_id, "method": method, "params": params})
            result = await asyncio.wait_for(future, timeout)
            if self.client_error:
                raise self.client_error
            return result
        except asyncio.TimeoutError as error:
            raise HarnessError(f"Agent 协议请求超时：{method}",
                               stage=stage or "rpc") from error
        except HarnessError as error:
            # 断连/协议错误发生在等待具体调用时，阶段归到该调用；
            # 已带具体阶段的错误保持原样，不覆盖真实来源。
            if stage and error.stage in (None, "rpc"):
                raise HarnessError(
                    str(error), stage=stage, category=error.category,
                    status=error.status) from error
            raise
        finally:
            self.pending.pop(request_id, None)

    async def _serve(self, message):
        try:
            result = await self.respond(message["method"], message.get("params") or {})
            await self.send({"id": message["id"], "result": result})
        except asyncio.CancelledError:
            raise
        except Exception:
            self.client_error = HarnessError("Agent 请求了当前操作不支持的交互或工具，请使用流式对话确认权限")
            try:
                await self.send({"id": message["id"], "error": {
                    "code": -32601, "message": "Unsupported or invalid client request"}})
            except HarnessError:
                pass

    async def _read(self):
        failure = HarnessError(
            "Agent 协议连接已断开，请检查 CLI 登录、模型与版本",
            stage="rpc")
        try:
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        raise ValueError
                except ValueError:
                    raise HarnessError("Agent 返回了无效 JSON-RPC 数据",
                                       stage="rpc") from None
                if "method" in message:
                    if "id" in message:
                        self.tasks.difference_update([task for task in self.tasks if task.done()])
                        if len(self.tasks) >= MAX_CLIENT_REQUESTS:
                            raise HarnessError(
                                "Agent 客户端请求并发超过上限，本轮已终止",
                                stage="rpc")
                        task = asyncio.create_task(self._serve(message))
                        self.tasks.add(task)
                        task.add_done_callback(self.tasks.discard)
                    else:
                        self.notify(message["method"], message.get("params") or {})
                elif "id" in message:
                    future = self.pending.get(message["id"])
                    if future is not None and not future.done():
                        if "error" in message:
                            error = message["error"]
                            detail = str(error.get("message", "请求失败"))[:1000]
                            for key, value in self.request.env.items():
                                if any(marker in key.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
                                    if len(value) >= 4:
                                        detail = detail.replace(value, "***")
                            category, status = classify_error_evidence(error)
                            future.set_exception(HarnessError(
                                f"Agent 协议错误：{detail}",
                                category=category, status=status))
                        else:
                            future.set_result(message.get("result") or {})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failure = error if isinstance(error, HarnessError) else \
                HarnessError("Agent 协议读取失败", stage="rpc")
        finally:
            self.reader_error = failure
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(failure)

    async def _drain_stderr(self):
        """持续排空 stderr；内存只保留固定上限，绝不按行或全量采集。"""
        while True:
            chunk = await self.process.stderr.read(8192)
            if not chunk:
                return
            self.stderr_total += len(chunk)
            if len(self.stderr_buffer) < MAX_STDERR_BYTES:
                remaining = MAX_STDERR_BYTES - len(self.stderr_buffer)
                self.stderr_buffer += chunk[:remaining]

    async def wait_notification(self, future: asyncio.Future):
        finished, _ = await asyncio.wait((future, self.reader), return_when=asyncio.FIRST_COMPLETED)
        if future in finished:
            result = await future
            if self.client_error:
                raise self.client_error
            return result
        raise self.reader_error or HarnessError(
            "Agent 在本轮完成前退出，请检查 CLI 登录与模型配置",
            stage="rpc")

    async def __aexit__(self, *arguments):
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.process is not None:
            if os.name == "nt":
                if self.process.returncode is None:
                    killer = await asyncio.create_subprocess_exec(
                        "taskkill", "/PID", str(self.process.pid), "/T", "/F",
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    try:
                        await asyncio.wait_for(killer.wait(), 5)
                    except asyncio.TimeoutError:
                        killer.kill()
                        await killer.wait()
            else:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except asyncio.TimeoutError:
                if os.name == "nt":
                    self.process.kill()
                else:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                await self.process.wait()
            if os.name != "nt":
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        for task in (self.reader, self.stderr):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (self.reader, self.stderr) if task is not None),
                             return_exceptions=True)
