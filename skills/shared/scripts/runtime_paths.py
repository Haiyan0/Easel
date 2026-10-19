#!/usr/bin/env python3
"""脚本侧代码根/数据根/状态根解析。

脚本与技能规则从只读代码根解析，产物/画像写本人的工作区根，运行状态写
state_root，缓存与 harness 状态写本人 HOME。worker 模式缺少任一显式配置
直接失败，绝不按 ``__file__`` 或宿主 HOME 猜路径。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class RuntimePathError(RuntimeError):
    """路径配置缺失或不合法；调用方不得静默回退。"""


@dataclass(frozen=True)
class RuntimePaths:
    code_root: Path
    workspace_root: Path
    state_root: Path
    home: Path


def _absolute(env: Mapping[str, str], name: str) -> Path:
    raw = env.get(name)
    if not isinstance(raw, str) or not raw.strip():
        raise RuntimePathError(f"缺少显式路径配置：{name}")
    if any(character in raw for character in ("\0", "\n", "\r")):
        raise RuntimePathError(f"路径包含控制字符：{name}")
    if raw.strip().startswith("~"):
        raise RuntimePathError(f"路径不能使用 ~ 回填宿主 HOME：{name}")
    path = Path(raw)
    if not path.is_absolute():
        raise RuntimePathError(f"路径必须为绝对路径：{name}")
    return path.resolve()


def resolve_runtime_paths(
        env: Mapping[str, str] | None = None,
        default_code_root: str | Path | None = None) -> RuntimePaths:
    values = os.environ if env is None else env
    mode = (values.get("EASEL_SERVICE_MODE") or "local").strip().lower()
    if mode == "worker":
        return RuntimePaths(
            code_root=_absolute(values, "EASEL_CODE_ROOT"),
            workspace_root=_absolute(values, "EASEL_WORKSPACE_ROOT"),
            state_root=_absolute(values, "EASEL_STATE_ROOT"),
            home=_absolute(values, "HOME"))
    if mode != "local":
        raise RuntimePathError(f"未知服务模式：{mode}")
    if default_code_root is not None:
        root = Path(default_code_root).expanduser().resolve()
    elif values.get("EASEL_ROOT"):
        root = Path(str(values["EASEL_ROOT"])).expanduser().resolve()
    else:
        root = Path(__file__).resolve().parents[3]
    home = Path(values.get("HOME") or Path.home()).expanduser().resolve()
    return RuntimePaths(code_root=root, workspace_root=root,
                        state_root=root, home=home)


def runtime_paths() -> RuntimePaths:
    """当前进程的路径快照；脚本导入时调用。"""
    return resolve_runtime_paths()
