"""worker 进程的显式路径边界。

local 保持原有行为：代码、产物与运行状态都在安装根，HOME 使用当前用户。
worker 必须显式给出代码根、数据根、状态根与本人 HOME，任何缺失、相对路径、
控制字符或代码根与可写数据根重叠都失败关闭，绝不静默回退 local。

本模块只解析纯值，不执行 chdir，也不修改进程环境；每个请求的私有路径由
调用方显式传递，避免多个 workspace 共用进程时互相污染。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from easel.control.types import DomainError, WorkspacePaths

SERVICE_MODE_ENV = "EASEL_SERVICE_MODE"
CODE_ROOT_ENV = "EASEL_CODE_ROOT"
WORKSPACE_ROOT_ENV = "EASEL_WORKSPACE_ROOT"
STATE_ROOT_ENV = "EASEL_STATE_ROOT"
EASEL_ROOT_ENV = "EASEL_ROOT"
HOME_ENV = "HOME"

LOCAL = "local"
WORKER = "worker"


def service_mode(env: Mapping[str, str] | None = None) -> str:
    """读取并校验服务模式；空值按 local，未知值失败关闭。"""
    values = os.environ if env is None else env
    mode = (values.get(SERVICE_MODE_ENV) or LOCAL).strip().lower()
    if mode not in (LOCAL, WORKER):
        raise DomainError("invalid_service_mode", 500)
    return mode


def _explicit_root(env: Mapping[str, str], name: str) -> Path:
    """读取显式绝对路径；缺失、空值、控制字符与相对路径都拒绝。"""
    raw = env.get(name)
    if not isinstance(raw, str) or not raw.strip():
        raise DomainError("worker_paths_missing", 500)
    if any(character in raw for character in ("\0", "\n", "\r")):
        raise DomainError("worker_paths_invalid", 500)
    if raw.strip().startswith("~"):
        # 显式私有根不能用 ~ 回填宿主 HOME
        raise DomainError("worker_paths_invalid", 500)
    path = Path(raw)
    if not path.is_absolute():
        raise DomainError("worker_paths_invalid", 500)
    return path.resolve()


def _overlaps(first: Path, second: Path) -> bool:
    """任一方向包含都视为重叠；代码根绝不能落入可写数据根。"""
    return (first == second
            or first.is_relative_to(second)
            or second.is_relative_to(first))


def resolve_paths(env: Mapping[str, str] | None,
                  default_code_root: Path) -> WorkspacePaths:
    """按服务模式返回路径；worker 只接受显式私有根。"""
    values = os.environ if env is None else env
    mode = service_mode(values)
    default_root = Path(default_code_root).resolve()
    if mode == LOCAL:
        return WorkspacePaths(
            code_root=default_root,
            workspace_root=default_root,
            state_root=default_root,
            home=Path.home().resolve())

    code_root = _explicit_root(values, CODE_ROOT_ENV)
    workspace_root = _explicit_root(values, WORKSPACE_ROOT_ENV)
    state_root = _explicit_root(values, STATE_ROOT_ENV)
    home = _explicit_root(values, HOME_ENV)

    # 代码根只读；数据/状态/HOME 都是可写位置，不能与代码根互相包含。
    for candidate in (workspace_root, state_root, home):
        if _overlaps(code_root, candidate):
            raise DomainError("worker_paths_invalid", 500)

    # 多人模式下 EASEL_ROOT 只作为数据根兼容入口，绝不指向代码根。
    legacy_root = values.get(EASEL_ROOT_ENV)
    if legacy_root is not None and str(legacy_root).strip():
        raw = str(legacy_root)
        if any(character in raw for character in ("\0", "\n", "\r")):
            raise DomainError("worker_paths_invalid", 500)
        legacy = Path(raw).expanduser()
        if not legacy.is_absolute() or legacy.resolve() != workspace_root:
            raise DomainError("worker_paths_invalid", 500)

    return WorkspacePaths(code_root=code_root,
                          workspace_root=workspace_root,
                          state_root=state_root,
                          home=home)
