"""公开 SSE 事件流：按游标回放、认证复查与心跳。

事件与任务执行解耦：本模块只轮询 worker 的持久事件，不启动、不重发任务
prompt。每条连接：

* 首帧前由调用方完成游标校验与资源归属检查，无效游标在开始流式响应前报错；
* 每 ``auth_check_seconds`` 秒重新检查当前登录，撤销后立即结束连接；
* 上游静默时发送注释心跳，避免反向代理提前断开；
* 客户端断开或终态事件回放完成后关闭，不取消后台任务。
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from starlette.responses import StreamingResponse

from .types import DomainError, Identity

TERMINAL_STATES = frozenset({"done", "failed", "interrupted", "cancelled"})
DEFAULT_POLL_SECONDS = 0.5
# 撤销窗口要求 5 秒内关闭；复查间隔留在 4 秒，给轮询与调度抖动留出余量。
DEFAULT_AUTH_CHECK_SECONDS = 4.0
DEFAULT_HEARTBEAT_SECONDS = 15.0


def _frame(event: dict) -> bytes:
    """SSE 帧：data 使用 JSON，换行已被转义；id 为逐 Run 单调事件号。"""
    data = json.dumps(event.get("data"), ensure_ascii=False,
                      separators=(",", ":"))
    return (
        f"id: {int(event['eventId'])}\n"
        f"event: {event['event']}\n"
        f"data: {data}\n\n"
    ).encode("utf-8")


class AuthGuardedStreamingResponse(StreamingResponse):
    """认证监视独立于响应生成器推进的流式响应。

    生成器自身的复查会在 ``yield`` 处暂停；当下游 ASGI send 被慢消费者
    阻塞时，生成器无法继续，撤销检查也就无法运行。这里把认证检查放进独立
    任务，与 ``StreamingResponse.stream_response`` 竞速；撤销/失败时取消
    发送任务、关闭响应体迭代器并等待清理，不取消 Run supervisor。
    """

    def __init__(self, content, *, recheck: Callable[[], Awaitable[None]],
                 auth_check_seconds: float = DEFAULT_AUTH_CHECK_SECONDS,
                 **kwargs) -> None:
        super().__init__(content, **kwargs)
        if not callable(recheck):
            raise TypeError("recheck must be callable")
        self._recheck = recheck
        self._auth_check_seconds = max(0.05, float(auth_check_seconds))

    async def _watch_auth(self) -> None:
        loop = asyncio.get_running_loop()
        interval = self._auth_check_seconds
        # 固定节拍：下一次检查从本次开始时刻计时，而不是成功完成后再等
        # 满 4 秒；本次检查耗时会计入窗口预算。
        deadline = loop.time() + interval
        while True:
            now = loop.time()
            if now < deadline:
                await asyncio.sleep(deadline - now)
            started = loop.time()
            try:
                # 复查自身也受窗口约束：4 秒复查 + 0.8 秒上限仍 < 5 秒
                await asyncio.wait_for(
                    self._recheck(), timeout=min(0.8, interval))
            except asyncio.CancelledError:
                raise
            except Exception:
                return
            deadline = started + interval

    async def stream_response(self, send) -> None:
        stream = asyncio.create_task(super().stream_response(send))
        watch = asyncio.create_task(self._watch_auth())
        try:
            done, _ = await asyncio.wait(
                {stream, watch}, return_when=asyncio.FIRST_COMPLETED)
            if watch in done:
                # 撤销/复查失败：只终止该连接及其在途读取
                stream.cancel()
                await asyncio.gather(stream, return_exceptions=True)
            else:
                watch.cancel()
                await asyncio.gather(watch, return_exceptions=True)
                await stream
        finally:
            for task in (stream, watch):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stream, watch, return_exceptions=True)
            aclose = getattr(self.body_iterator, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:
                    pass


async def run_event_stream(
        *, relay, actor: Identity, run_id: str, page: dict,
        recheck: Callable[[], Awaitable[None]],
        clock: Callable[[], float] = time.monotonic,
        poll_interval: float = DEFAULT_POLL_SECONDS,
        auth_check_seconds: float = DEFAULT_AUTH_CHECK_SECONDS,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
) -> AsyncIterator[bytes]:
    """回放持久事件并在终态或撤销时结束。

    ``page`` 是调用方首次读取的事件页；之后每次按 ``lastEventId`` 游标增量
    读取。``recheck`` 抛 ``DomainError`` 表示登录已撤销或资源不再属于本人。
    """
    cursor = 0
    now = clock()
    last_auth_check = now
    last_heartbeat = now
    next_fetch = now
    pending: asyncio.Task | None = None
    try:
        while True:
            events = page.get("events") or []
            for event in events:
                event_id = event.get("eventId")
                if type(event_id) is not int or event_id <= cursor:
                    continue
                # 逐帧复查：大页回放或慢消费者也不能把撤销推迟到 5 秒后
                now = clock()
                if now - last_auth_check >= auth_check_seconds:
                    try:
                        await recheck()
                    except DomainError:
                        return
                    last_auth_check = clock()
                cursor = event_id
                yield _frame(event)
            last_event_id = page.get("lastEventId")
            if type(last_event_id) is int and last_event_id > cursor:
                cursor = last_event_id
            state = page.get("state")
            if state in TERMINAL_STATES and cursor >= (last_event_id or 0):
                return

            while True:
                now = clock()
                # 认证截止与事件请求竞速：慢上游/心跳/轮询都不能推迟撤销。
                if pending is None and now >= next_fetch:
                    pending = asyncio.create_task(
                        relay.events(actor, run_id, after=cursor))
                auth_due = last_auth_check + auth_check_seconds
                heartbeat_due = last_heartbeat + heartbeat_seconds
                deadlines = [auth_due, heartbeat_due]
                if pending is None:
                    deadlines.append(next_fetch)
                timeout = max(0.0, min(deadlines) - now)
                if pending is not None:
                    done, _ = await asyncio.wait({pending}, timeout=timeout)
                else:
                    await asyncio.sleep(timeout)
                    done = set()
                now = clock()

                if pending is not None and pending in done:
                    try:
                        page = pending.result()
                    except DomainError as error:
                        if error.status != 503:
                            raise
                        # gateway 暂时不可用：不结束已建立的 SSE，保留游标，
                        # 按 Retry-After/轮询间隔继续；登录撤销仍由 recheck
                        # 独立终止连接。POST 不在此路径自动重发。
                        pending = None
                        next_fetch = clock() + max(
                            poll_interval,
                            min(float(error.retry_after or 1), 5.0))
                        continue
                    else:
                        pending = None
                    next_fetch = now + poll_interval
                    break  # 处理新事件页

                if now >= last_auth_check + auth_check_seconds:
                    # 撤销会抛 DomainError；关闭连接与在途读取，后台任务保留
                    try:
                        await recheck()
                    except DomainError:
                        return
                    last_auth_check = clock()
                if now >= last_heartbeat + heartbeat_seconds:
                    yield b": keepalive\n\n"
                    last_heartbeat = clock()
    finally:
        # 客户端断开或撤销：取消并等待在途事件请求，释放其请求租约。
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
