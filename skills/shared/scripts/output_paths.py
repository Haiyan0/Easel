#!/usr/bin/env python3
"""Single source of truth for Easel output paths."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def _helper_workspace_root() -> Path:
    """脚本侧唯一路径来源；helper 不可用直接失败，不猜宿主目录。"""
    try:
        from runtime_paths import resolve_runtime_paths
    except ImportError as error:
        raise RuntimeError(
            "无法加载 runtime_paths，拒绝回退源码/宿主路径") from error
    return resolve_runtime_paths().workspace_root


def _discover_root() -> Path:
    # worker 只认显式路径 helper：导入失败、缺项、非法路径一律失败关闭，
    # 绝不回退 EASEL_ROOT 或源码扫描。local 保留原有 EASEL_ROOT/扫描行为。
    configured = os.environ.get("EASEL_ROOT", "").strip()
    mode = (os.environ.get("EASEL_SERVICE_MODE") or "local").strip().lower()
    if mode != "local":
        return _helper_workspace_root()
    if configured:
        try:
            from runtime_paths import resolve_runtime_paths
        except ImportError:
            # local 兼容：helper 不可用时仍按显式 EASEL_ROOT 工作
            return Path(configured).expanduser().resolve()
        return resolve_runtime_paths().workspace_root
    for parent in (Path(__file__).resolve().parent, *Path(__file__).resolve().parents):
        outputs = parent / "outputs"
        if outputs.exists():
            # The OpenClaw workspace exposes outputs as a symlink to the project.
            return outputs.resolve().parent
    raise RuntimeError("找不到 Easel 项目根；请设置 EASEL_ROOT")


PROJECT_ROOT = _discover_root()
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
GENERIC_PROJECT_NAMES = {
    "output", "outputs", "result", "results", "temp", "tmp", "test", "demo",
    "xhs", "douyin", "video", "audio", "image", "images", "project", "untitled",
    "主题", "主题名", "项目", "测试", "临时",
}
SYSTEM_DIRS = {
    "_analytics", "_debug", "_inbox", "_login", "_probe", "_profile_build",
    "_publish", "_scratch", "_sessions",
}
SYSTEM_FILES = {"_ideas.json", "_publish.log", "_schedule.json"}


class OutputPathError(ValueError):
    """Raised when a path violates the Easel outputs layout."""


def validate_output_path(
    value: str | os.PathLike[str], *, allow_system: bool = False, create_parent: bool = False,
) -> Path:
    """Return an absolute safe output path following the project layout.

    Content must be below ``outputs/<human-readable-topic>/``. System writers
    must opt in and may only use the registered underscore-prefixed locations.
    """
    raw = Path(value).expanduser()
    resolved = raw.resolve() if raw.is_absolute() else (PROJECT_ROOT / raw).resolve()
    root = OUTPUTS_DIR.resolve()
    try:
        rel = resolved.relative_to(root)
    except ValueError as exc:
        raise OutputPathError(f"输出必须位于 {OUTPUTS_DIR}内: {value}") from exc
    if not rel.parts:
        raise OutputPathError("不能把 outputs/ 根目录作为输出目标")

    top = rel.parts[0]
    if top.startswith("."):
        raise OutputPathError("不能写入 outputs/ 下的隐藏路径")
    if top.startswith("_") or top in SYSTEM_FILES:
        if not allow_system:
            raise OutputPathError("内容产物不能写入系统路径")
        if top not in SYSTEM_DIRS and top not in SYSTEM_FILES:
            raise OutputPathError(f"未注册的系统输出项: {top}")
    else:
        if len(rel.parts) < 2:
            raise OutputPathError("内容产物必须写入 outputs/<具体主题>/，不能散落在根目录")
        if top.casefold() in GENERIC_PROJECT_NAMES:
            raise OutputPathError(f"项目目录名过于泛化: {top}")

    if create_parent:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def validate_project_dir(value: str | os.PathLike[str], *, create: bool = False) -> Path:
    """Validate ``outputs/<human-readable-topic>/`` itself."""
    marker = Path(value) / ".easel-path-check"
    path = validate_output_path(marker).parent
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 Easel outputs 路径规约")
    parser.add_argument("path")
    parser.add_argument("--system", action="store_true", help="允许已注册的系统路径")
    args = parser.parse_args()
    print(validate_output_path(args.path, allow_system=args.system))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
