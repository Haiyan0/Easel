#!/usr/bin/env python3
"""脚本侧代码根/数据根/状态根解析。

脚本与技能规则从只读代码根解析，产物/画像写本人的工作区根，运行状态写
state_root，缓存与 harness 状态写本人 HOME。worker 模式缺少任一显式配置
直接失败，绝不按 ``__file__`` 或宿主 HOME 猜路径。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

# 平台 → 旧版 profile 目录名；local 继续沿用，worker 改为 <platform>/<account>。
LEGACY_PROFILE_NAMES = {
    "xiaohongshu": "XiaohongshuProfile",
    "douyin": "DouyinProfile",
    "kuaishou": "KuaishouProfile",
    "zhihu": "ZhihuProfile",
    "weixin-channels": "ChannelsProfile",
    "wechat-oa": "WeixinMpProfile",
}
STATE_KINDS = frozenset({
    "wechat-config", "wechat-token", "platform-cookie",
    "browser-profile", "skill-cache",
})
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


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


def _overlaps(first: Path, second: Path) -> bool:
    """任一方向包含都算重叠：只读代码根不能落入可写数据根。"""
    return (first == second
            or first.is_relative_to(second)
            or second.is_relative_to(first))


def resolve_runtime_paths(
        env: Mapping[str, str] | None = None,
        default_code_root: str | Path | None = None) -> RuntimePaths:
    values = os.environ if env is None else env
    mode = (values.get("EASEL_SERVICE_MODE") or "local").strip().lower()
    if mode == "worker":
        code_root = _absolute(values, "EASEL_CODE_ROOT")
        workspace_root = _absolute(values, "EASEL_WORKSPACE_ROOT")
        state_root = _absolute(values, "EASEL_STATE_ROOT")
        home = _absolute(values, "HOME")
        # 与主 resolver 保持一致：代码根只读，不能与任何可写根互相包含。
        for candidate in (workspace_root, state_root, home):
            if _overlaps(code_root, candidate):
                raise RuntimePathError("worker 代码根不能与可写数据根重叠")
        # EASEL_ROOT 在 worker 只兼容指向本人数据根，不能借此换代码根。
        legacy_root = values.get("EASEL_ROOT")
        if legacy_root is not None and str(legacy_root).strip():
            raw = str(legacy_root)
            if any(character in raw for character in ("\0", "\n", "\r")):
                raise RuntimePathError("EASEL_ROOT 包含控制字符")
            legacy = Path(raw).expanduser()
            if not legacy.is_absolute() or legacy.resolve() != workspace_root:
                raise RuntimePathError("worker 的 EASEL_ROOT 必须指向本人数据根")
        return RuntimePaths(code_root=code_root, workspace_root=workspace_root,
                            state_root=state_root, home=home)
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


def service_mode(env: Mapping[str, str] | None = None) -> str:
    """当前服务模式；未知值失败关闭。"""
    values = os.environ if env is None else env
    mode = (values.get("EASEL_SERVICE_MODE") or "local").strip().lower()
    if mode not in ("local", "worker"):
        raise RuntimePathError(f"未知服务模式：{mode}")
    return mode


def safe_segment(value: str, label: str = "account_id") -> str:
    """账号/平台标识只能是不含分隔符、点段和控制字符的单个安全段。"""
    if not isinstance(value, str) or not _SEGMENT.fullmatch(value):
        raise RuntimePathError(f"{label} 非法：{value!r}")
    if value in (".", "..") or ".." in value:
        raise RuntimePathError(f"{label} 非法：{value!r}")
    return value


def state_path(kind: str, account_id: str = "default") -> Path:
    """本人私有状态路径；所有 kind 都落在 state_root 或本人 HOME。

    ``browser-profile`` 返回 ``<home>/.easel-browser-profiles/<account_id>``
    这一单段分区；平台/账号的规范映射见 :func:`browser_profile_dir`。
    """
    if kind not in STATE_KINDS:
        raise RuntimePathError(f"未知状态类别：{kind}")
    account = safe_segment(account_id)
    paths = resolve_runtime_paths()
    if kind == "wechat-config":
        return paths.state_root / "skills" / "wechat" / f"{account}.yaml"
    if kind == "wechat-token":
        return (paths.state_root / "skills" / "wechat" / "tokens"
                / f"{account}.json")
    if kind == "platform-cookie":
        return (paths.state_root / "skills" / "platform-cookies"
                / f"{account}.json")
    if kind == "skill-cache":
        return paths.state_root / "skills" / "cache" / account
    return paths.home / ".easel-browser-profiles" / account


def browser_profile_dir(platform: str, account_id: str = "default",
                        base: str | None = None) -> Path:
    """平台/账号持久 profile 目录。

    worker 必须使用本人 HOME 下的 ``<platform>/<account>`` 分区，且禁止用
    ``--profile-base`` 选择任意宿主目录；local 保留旧版 ``<ProfileName>``
    兼容布局。所有实际浏览器调用点共用此映射。
    """
    platform = safe_segment(platform, "platform")
    account = safe_segment(account_id)
    if service_mode() == "worker":
        if base:
            raise RuntimePathError("worker 不接受 --profile-base")
        return (resolve_runtime_paths().home / ".easel-browser-profiles"
                / platform / account)
    legacy = LEGACY_PROFILE_NAMES.get(platform)
    if not legacy:
        raise RuntimePathError(f"未知平台：{platform}")
    paths = resolve_runtime_paths()
    root = Path(base).expanduser() if base else (
        paths.home / ".easel-browser-profiles")
    return root / legacy
