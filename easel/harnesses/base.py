"""Internal contract for agent harness integrations."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol


class HarnessError(Exception):
    """适配器失败；可选诊断字段保持 ``HarnessError(message)`` 兼容。

    * ``stage``：失败阶段（cli_start/initialize/session/model/prompt/rpc）；
    * ``category``：仅在上游或 broker 提供结构化证据时填写
      （auth/rate_limited/request_rejected/service_unavailable）；
    * ``status``：结构化证据里的 HTTP 状态码，可为空。
    """

    def __init__(self, message: str, *, stage: str | None = None,
                 category: str | None = None,
                 status: int | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.category = category
        self.status = status


Emit = Callable[[str, str], None]
Ask = Callable[[list[dict]], Awaitable[dict[str, list[str]]]]
Bind = Callable[[str], None]


@dataclass(frozen=True)
class Descriptor:
    id: str
    label: str
    protocol: str
    install_hint: str


@dataclass(frozen=True)
class RunRequest:
    prompt: str
    cwd: Path
    env: dict[str, str]
    native_id: str = ""
    model: str = ""
    sandbox: str = "workspace-write"
    timeout: int = 1800
    command: tuple[str, ...] = ()
    session_key: str = ""
    codex_auto_approve: bool = False
    workspace_id: str = ""
    """worker 的 workspace namespace；local 为空，保持原有进程池行为。"""
    code_root: Path | None = None
    """只读代码根；为空时沿用 ``cwd``（local 兼容行为）。"""


class Adapter(Protocol):
    descriptor: Descriptor

    async def run(self, request: RunRequest, emit: Emit, ask: Ask, bind: Bind) -> None: ...
