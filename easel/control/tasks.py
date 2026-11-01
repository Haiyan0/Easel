"""任务类型表：所有会启动后台/浏览器任务的精确入口。

本表是 control 的统一盘点结果，只回答“某条已分类路由属于哪类任务”，不替代
``policy.authorize`` 的角色判断，也不替代 ``Store.reserve_run`` 的容量账本。

* ``LONG``：对话、SKILL、画像增强等长任务，必须经过 Run 接纳；
* ``BROWSER``：登录、发布、数据抓取等会驱动浏览器的任务，同样占用 Run 预算；
* ``implemented`` 表示本轮 control 是否装配了业务适配器。为 ``False`` 的
  入口必须在接纳前明确 503，不能用旧本地 handler 直通绕过账本。

新增任务入口时先登记本表，再用 policy 精确分类；测试会反向核对两边一致。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..worker.config import RUN_TASK_TYPES

LONG = "long"
BROWSER = "browser"


@dataclass(frozen=True)
class TaskRoute:
    """一条公开路由；``starts_task`` 区分启动入口与停止/回放等控制入口。"""

    method: str
    pattern: str
    starts_task: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "method", self.method.upper())
        if not self.pattern.startswith("^"):
            object.__setattr__(self, "pattern", "^" + self.pattern)
        if not self.pattern.endswith("$"):
            object.__setattr__(self, "pattern", self.pattern + "$")


@dataclass(frozen=True)
class TaskEntry:
    task_type: str
    label: str
    kind: str
    implemented: bool
    routes: tuple[TaskRoute, ...]

    def __post_init__(self) -> None:
        if self.task_type not in RUN_TASK_TYPES:
            raise ValueError(f"未知任务类型：{self.task_type}")
        if self.kind not in (LONG, BROWSER):
            raise ValueError(f"未知任务类别：{self.kind}")


TASK_ENTRIES: tuple[TaskEntry, ...] = (
    TaskEntry(
        task_type="chat",
        label="对话",
        kind=LONG,
        implemented=True,
        routes=(
            TaskRoute("POST", r"/api/chat"),
            TaskRoute("POST", r"/api/chat/stream"),
            TaskRoute("POST", r"/api/chat/stop", starts_task=False),
            TaskRoute("POST", r"/api/chat/question/answer", starts_task=False),
            TaskRoute("POST", r"/api/chat/question/status", starts_task=False),
            TaskRoute("GET", r"/api/chat/jobs/([^/]+)/stream",
                      starts_task=False),
            TaskRoute("GET", r"/api/chat/last/([^/]+)", starts_task=False),
            TaskRoute("DELETE", r"/api/session/([^/]+)", starts_task=False),
        ),
    ),
    TaskEntry(
        task_type="skill",
        label="SKILL 执行",
        kind=LONG,
        implemented=True,
        routes=(TaskRoute("POST", r"/api/skill"),),
    ),
    TaskEntry(
        task_type="profile",
        label="画像增强",
        kind=LONG,
        implemented=True,
        routes=(TaskRoute("POST", r"/api/profile/build"),),
    ),
    TaskEntry(
        task_type="analytics",
        label="创作数据抓取",
        kind=BROWSER,
        implemented=True,
        routes=(TaskRoute("POST", r"/api/analytics/([^/]+)"),),
    ),
    TaskEntry(
        task_type="publish",
        label="发布",
        kind=BROWSER,
        implemented=False,
        routes=(
            TaskRoute("POST", r"/api/publish/([^/]+)"),
            TaskRoute("POST", r"/api/publish/([^/]+)/sms"),
            TaskRoute("POST", r"/api/manual-publish/([^/]+)/export"),
        ),
    ),
    TaskEntry(
        task_type="browser",
        label="浏览器登录",
        kind=BROWSER,
        implemented=True,
        routes=(
            TaskRoute("POST", r"/api/login/([^/]+)"),
            TaskRoute("POST", r"/api/login/([^/]+)/sms", starts_task=False),
            TaskRoute("POST", r"/api/login/([^/]+)/stop", starts_task=False),
            TaskRoute("POST", r"/api/accounts/([^/]+)/mp-login"),
            TaskRoute("POST", r"/api/accounts/([^/]+)/mp-login/stop",
                      starts_task=False),
            TaskRoute("POST", r"/api/accounts/([^/]+)/whoami"),
            TaskRoute("POST", r"/api/accounts/([^/]+)/credentials"),
            TaskRoute("POST", r"/api/logout/([^/]+)", starts_task=False),
        ),
    ),
)

if {entry.task_type for entry in TASK_ENTRIES} != set(RUN_TASK_TYPES):
    raise RuntimeError("任务类型表必须覆盖 worker 支持的全部任务类型")

IMPLEMENTED_TASK_TYPES = frozenset(
    entry.task_type for entry in TASK_ENTRIES if entry.implemented)

_BY_TYPE = {entry.task_type: entry for entry in TASK_ENTRIES}
_COMPILED = tuple(
    (entry,
     tuple((route.method, re.compile(route.pattern))
           for route in entry.routes))
    for entry in TASK_ENTRIES)


def entry_for_type(task_type: str) -> TaskEntry | None:
    return _BY_TYPE.get(task_type)


def entry_for_route(method: str, path: str) -> TaskEntry | None:
    """按 method+完整路径查找任务类型；HEAD 与 policy 一样按 GET 处理。"""
    normalized = "GET" if method.upper() == "HEAD" else method.upper()
    for entry, patterns in _COMPILED:
        for allowed, pattern in patterns:
            if normalized == allowed and pattern.fullmatch(path):
                return entry
    return None
