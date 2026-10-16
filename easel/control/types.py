"""控制平面共享的不可变传输类型。

本模块只定义数据结构与领域错误，不含网络或存储逻辑。字段名与签名属于稳定契约，
后续任务不得私自更名。

注意：``Identity``、``RunRecord`` 等对象只描述"是谁、是哪条记录"，**不是授权凭证**。
任何按 workspace 限定的操作都必须回到数据库重查用户、workspace 关系与有效状态。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


class DomainError(Exception):
    """带错误码与 HTTP 状态的领域错误。

    ``code`` 用于 API 响应体，``status`` 直接映射到 HTTP 状态码：
    404 不存在或属于其他空间，403 已登录但无权操作，409 幂等/版本冲突，
    429 并发或调用预算耗尽。

    错误信息不包含令牌、模型 Key 或其他秘密。
    """

    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Identity:
    """当前请求的身份视图；不携带秘密，也不自带授权。"""

    user_id: str
    workspace_id: str
    role: Literal["admin", "member"]


@dataclass(frozen=True)
class ConversationRecord:
    """控制层保存的会话元数据。"""

    id: str
    workspace_id: str
    title: str


@dataclass(frozen=True)
class RunRecord:
    """一次任务接纳记录；``config_version`` 固定为接纳时的配置版本。"""

    id: str
    workspace_id: str
    conversation_id: str
    status: str
    config_version: int


@dataclass(frozen=True)
class BrowserSession:
    """登录会话。令牌字段不进入默认 repr，避免被日志意外记录。"""

    id: str
    token: str = field(repr=False)
    csrf_token: str = field(repr=False)


@dataclass(frozen=True)
class EffectiveRoute:
    """按 channel 解析后的生效路由；凭证只以引用形式出现。"""

    channel: str
    provider: str
    model: str
    base_url: str
    protocol: str
    credential_ref: str
    source: Literal["admin", "user"]
    version: int
    options: dict


@dataclass(frozen=True)
class WorkerHandle:
    """已就绪 worker 的句柄；内部服务凭证不进入默认 repr。"""

    workspace_id: str
    container_id: str
    upstream: str
    service_token: str = field(repr=False)
    config_version: int


@dataclass(frozen=True)
class WorkspacePaths:
    """代码根、工作区数据根、运行状态根与本人 HOME 三类位置。"""

    code_root: Path
    workspace_root: Path
    state_root: Path
    home: Path
