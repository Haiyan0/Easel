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
    last_auth_check = clock()
    last_heartbeat = clock()
    while True:
        events = page.get("events") or []
        for event in events:
            event_id = event.get("eventId")
            if type(event_id) is not int or event_id <= cursor:
                continue
            cursor = event_id
            yield _frame(event)
        last_event_id = page.get("lastEventId")
        if type(last_event_id) is int and last_event_id > cursor:
            cursor = last_event_id
        state = page.get("state")
        if state in TERMINAL_STATES and cursor >= (last_event_id or 0):
            return

        now = clock()
        if now - last_auth_check >= auth_check_seconds:
            # 撤销会抛 DomainError；只关闭连接，后台任务保持原状
            try:
                await recheck()
            except DomainError:
                return
            last_auth_check = now
        if now - last_heartbeat >= heartbeat_seconds:
            yield b": keepalive\n\n"
            last_heartbeat = now

        await asyncio.sleep(poll_interval)
        page = await relay.events(actor, run_id, after=cursor)
