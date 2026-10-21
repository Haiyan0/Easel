#!/usr/bin/env python3
"""worker 镜像构建期冒烟：文件、依赖、worker 模式参数与假 Harness 启动。

不加载真实 DSH commit / 管理员快照，也不把生命周期 health 当作最终镜像
可用。``--fake-harness`` 只用于证明当前权限模型下可以启动子进程；真实
Harness 门槛仍由 ``--require-dsh`` 和部署方提供已验收 commit/profile 决定。
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

REQUIRED_FILES = (
    "web/app.py",
    "web/static/index.html",
    "skills/shared/scripts/runtime_paths.py",
    "skills/shared/scripts/browser_state.py",
    "skills/builtin/paper-explainer/SKILL.md",
    "skills/builtin/paper-explainer/scripts/render_slides.py",
)

FORBIDDEN_FILE_NAMES = {
    "wechat-publisher.yaml",
    "wechat_publisher.yaml",
    "cookies.json",
}
FORBIDDEN_SUFFIXES = (".token", ".secret")

REQUIRED_MODULES = (
    "fastapi",
    "uvicorn",
    "httpx",
    "pydantic",
    "sse_starlette",
    "python_multipart",
    "cryptography",
    "requests",
    "bs4",
    "websocket",
    "segno",
    "markdown",
    "PIL",
    "numpy",
    "pandas",
    "matplotlib",
    "cv2",
    "jieba",
    "playwright",
)

FAKE_HARNESS_FLAG = "--fake-harness"


def _code_root() -> Path:
    return Path(os.environ.get("EASEL_CODE_ROOT") or "/opt/easel")


def _fail(message: str) -> NoReturn:
    print(f"WORKER_SMOKE_FAILED: {message}", file=sys.stderr)
    raise SystemExit(1)


def _fake_harness() -> int:
    """最小 ACP initialize 应答；只用于构建期子进程启动冒烟。"""
    line = sys.stdin.readline()
    try:
        request = json.loads(line) if line else {}
    except json.JSONDecodeError:
        request = {}
    response = {
        "jsonrpc": "2.0",
        "id": request.get("id"),
        "result": {"protocolVersion": 1, "agentCapabilities": {}},
    }
    print(json.dumps(response), flush=True)
    return 0


def _check_no_private_state(code_root: Path) -> None:
    """构建上下文排除之外的兜底：镜像内不得出现已知凭证/缓存文件名。"""
    for path in code_root.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.lower()
        if (name in FORBIDDEN_FILE_NAMES
                or name.startswith("cookies-")
                or name.endswith(FORBIDDEN_SUFFIXES)):
            _fail(f"镜像包含开发者私有文件：{path.relative_to(code_root)}")


def _check_files(code_root: Path) -> None:
    _check_no_private_state(code_root)
    for relative in REQUIRED_FILES:
        path = code_root / relative
        if not path.is_file():
            _fail(f"缺少执行文件：{relative}")
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o222:
            _fail(f"执行代码必须只读：{relative} mode={oct(mode)}")
    for name in REQUIRED_MODULES:
        try:
            importlib.import_module(name)
        except Exception as error:  # noqa: BLE001
            _fail(f"依赖不可导入：{name}: {error}")


def _check_web_and_worker_mode(code_root: Path) -> None:
    """local 导入验证 Web 代码完整；worker 参数验证沙箱过滤生效。"""
    os.environ["EASEL_SERVICE_MODE"] = "local"
    if str(code_root) not in sys.path:
        sys.path.insert(0, str(code_root))
    importlib.import_module("web.app")

    os.environ["EASEL_SERVICE_MODE"] = "worker"
    shared = code_root / "skills" / "shared" / "scripts"
    if str(shared) not in sys.path:
        sys.path.insert(0, str(shared))
    runtime_paths = importlib.import_module("runtime_paths")
    filtered = runtime_paths.chromium_launch_args([
        "--disable-gpu", "--no-sandbox", "--disable-setuid-sandbox"])
    if filtered != ["--disable-gpu"]:
        _fail("worker 模式未移除 Chromium 沙箱禁用参数")


def _check_fake_harness() -> None:
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), FAKE_HARNESS_FLAG],
        input='{"jsonrpc":"2.0","id":1,"method":"initialize"}\n',
        capture_output=True, text=True, timeout=15, check=False)
    if result.returncode != 0:
        _fail(f"假 Harness 启动失败：{result.stderr[-500:]}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        _fail("假 Harness 未返回 JSON")
    if (payload.get("result") or {}).get("protocolVersion") != 1:
        _fail("假 Harness initialize 响应异常")


def _check_dsh() -> None:
    code_root = _code_root()
    dsh = Path("/opt/dsh")
    if not dsh.is_dir() or not (dsh / ".git").exists():
        _fail("缺少已固定的 /opt/dsh checkout")
    profile = code_root / "managed" / "acp-profile.json"
    if not profile.is_file():
        _fail("缺少 managed/acp-profile.json")
    for path in (dsh / ".git" / "HEAD", profile):
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o222:
            _fail(f"受管 Harness 配置必须只读：{path} mode={oct(mode)}")


def main(argv: list[str] | None = None) -> int:
    if FAKE_HARNESS_FLAG in (argv or sys.argv[1:]):
        return _fake_harness()
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-files", action="store_true")
    parser.add_argument("--require-dsh", action="store_true")
    args = parser.parse_args(argv)

    code_root = _code_root()
    _check_files(code_root)
    _check_web_and_worker_mode(code_root)
    _check_fake_harness()
    if args.require_dsh:
        _check_dsh()
    print("WORKER_SMOKE_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
