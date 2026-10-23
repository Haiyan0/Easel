"""后台任务对账循环：与 SSE、浏览器登录和用户在线状态无关。

职责边界：

* 只调用 ``WorkerRelay.reconcile_once``；所有状态判定、原实例绑定与终态
  收尾都复用 relay/Store 的持久规则，不维护第二套 Run 账本。
* 应用启动时恢复扫描控制库；退出时先取消循环再关闭 HTTP 客户端。
* 单次网络/数据库异常不终止循环，下一轮继续对账；具体 Run 的收敛条件
  由 relay 的派发宽限期与任务截止保证。
"""
from __future__ import annotations

import asyncio

DEFAULT_INTERVAL_SECONDS = 1.0


class RunReconciler:
    """常驻对账任务；``interval`` 只在测试中调小。"""

    def __init__(self, relay, *,
                 interval: float = DEFAULT_INTERVAL_SECONDS) -> None:
        if not callable(getattr(relay, "reconcile_once", None)):
            raise TypeError("relay must implement reconcile_once")
        if not 0.01 <= float(interval) <= 60:
            raise ValueError("interval out of range")
        self._relay = relay
        self._interval = float(interval)
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def reconcile_once(self) -> dict:
        return await self._relay.reconcile_once()

    async def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.create_task(
            self._loop(), name="easel-run-reconciler")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            try:
                await self._relay.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 网络/数据库瞬时失败：保留持久记录，下一轮继续
                pass
            await asyncio.sleep(self._interval)
