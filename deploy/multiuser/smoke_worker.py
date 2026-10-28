#!/usr/bin/env python3
"""worker 镜像构建期冒烟：文件、依赖、签名快照与真实 ACP 指令装配。

使用临时生成的合法 Ed25519 测试快照和受管假 ACP，实际经过
``web.app`` worker 入口、``ACPAdapter``、``harness_instructions`` 与
initialize/session/new/session/prompt。真实 DSH commit/profile 门槛由
``--require-dsh`` 与部署方提供，不被本冒烟替代。
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import secrets
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import NoReturn

REQUIRED_FILES = (
    "web/app.py",
    "easel/worker/server.py",
    "web/static/index.html",
    "agents/workspace/AGENTS.md",
    "agents/workspace/SOUL.md",
    "skills/shared/scripts/runtime_paths.py",
    "skills/shared/scripts/browser_state.py",
    "skills/builtin/paper-explainer/SKILL.md",
    "skills/builtin/paper-explainer/scripts/render_slides.py",
    "deploy/multiuser/worker_sidecar.py",
    "deploy/multiuser/egress_proxy.py",
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

FAKE_ACP_FLAG = "--fake-acp"
SMOKE_WORKSPACE = "a" * 32
SMOKE_SESSION_ID = "smoke-session"


def _code_root() -> Path:
    return Path(os.environ.get("EASEL_CODE_ROOT") or "/opt/easel")


def _fail(message: str) -> NoReturn:
    print(f"WORKER_SMOKE_FAILED: {message}", file=sys.stderr)
    raise SystemExit(1)


def _response(identifier, result):
    if identifier is not None:
        print(json.dumps({
            "jsonrpc": "2.0", "id": identifier, "result": result}),
            flush=True)


def _fake_acp() -> int:
    """受管假 ACP：校验共享规则、技能、路径、环境，再完成一轮 prompt。"""
    code_root = _code_root()
    shared_scripts = code_root / "skills" / "shared" / "scripts"
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            return 2
        method = message.get("method")
        params = message.get("params") or {}
        identifier = message.get("id")
        if method == "initialize":
            _response(identifier, {"protocolVersion": 1,
                                   "agentCapabilities": {}})
        elif method == "session/new":
            _response(identifier, {"sessionId": SMOKE_SESSION_ID})
        elif method == "session/prompt":
            blocks = params.get("prompt") or []
            prompt = "".join(
                block.get("text", "") for block in blocks
                if isinstance(block, dict) and block.get("type") == "text")
            cwd = str(Path.cwd().resolve())
            required = (
                "## 共享工作区：AGENTS.md" in prompt,
                "## 共享工作区：SOUL.md" in prompt,
                f"技能库：{code_root / 'skills' / 'builtin'}" in prompt,
                f"共享脚本：{shared_scripts}" in prompt,
                f"项目根绝对路径：{cwd}" in prompt,
                os.environ.get("EASEL_CODE_ROOT") == str(code_root),
                os.environ.get("EASEL_WORKSPACE_ROOT") == "/workspace",
                os.environ.get("EASEL_STATE_ROOT") == "/workspace/state",
                os.environ.get("EASEL_ROOT") == cwd,
                (code_root / "agents" / "workspace" / "AGENTS.md").is_file(),
                (shared_scripts / "runtime_paths.py").is_file(),
            )
            _response(identifier, {
                "stopReason": "end_turn" if all(required) else "refusal"})
        else:
            _response(identifier, {})
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


def _write_snapshot(code_root: Path):
    """生成只存在于冒烟进程内的合法测试签名快照。"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey)
    from easel.worker.config import load_worker_snapshot, sign_worker_snapshot

    directory = Path(tempfile.mkdtemp(prefix="easel-snapshot-"))
    run_id = "run-smoke-" + secrets.token_hex(8)
    private_key = Ed25519PrivateKey.generate()
    public_file = directory / "worker.pub"
    public_file.write_bytes(private_key.public_key().public_bytes_raw())
    snapshot_file = directory / "snapshot.json"
    command = [sys.executable, str(Path(__file__).resolve()), FAKE_ACP_FLAG]
    envelope = sign_worker_snapshot(
        private_key, workspace_id=SMOKE_WORKSPACE, run_id=run_id,
        revision=1, routes={}, agent={"command": command},
        expires_at=int(time.time()) + 600,
        nonce="smoke-" + secrets.token_hex(8))
    snapshot_file.write_text(json.dumps(envelope), encoding="utf-8")
    loaded = load_worker_snapshot(
        snapshot_file, public_file, expected_workspace_id=SMOKE_WORKSPACE,
        expected_run_id=run_id)
    if list(loaded.agent.get("command") or []) != command:
        _fail("测试签名快照的受管 ACP command 不一致")
    return snapshot_file, public_file, command, run_id


def _check_worker_entry(code_root: Path) -> None:
    """走 worker Web 入口、指令装配与完整 ACP 生命周期。"""
    workspace = Path("/workspace")
    state = workspace / "state"
    state.mkdir(parents=True, exist_ok=True)
    snapshot_file, public_file, command, run_id = _write_snapshot(code_root)
    os.environ.update({
        "EASEL_SERVICE_MODE": "worker",
        "EASEL_WORKSPACE_ID": SMOKE_WORKSPACE,
        "EASEL_SERVICE_TOKEN": "s" * 32,
        "EASEL_CONTAINER_ID": f"easel-worker-{SMOKE_WORKSPACE}",
        "EASEL_CONFIG_VERSION": "1",
        "EASEL_RUN_ID": run_id,
        "EASEL_WORKER_SNAPSHOT_FILE": str(snapshot_file),
        "EASEL_WORKER_PUBLIC_KEY_FILE": str(public_file),
        "EASEL_CODE_ROOT": str(code_root),
        "EASEL_WORKSPACE_ROOT": str(workspace),
        "EASEL_STATE_ROOT": str(state),
    })
    if str(code_root) not in sys.path:
        sys.path.insert(0, str(code_root))
    web_app = importlib.import_module("web.app")
    snapshot = getattr(web_app, "_WORKER_SNAPSHOT", None)
    if snapshot is None or snapshot.workspace_id != SMOKE_WORKSPACE:
        _fail("worker Web 入口未加载合法测试签名快照")

    shared = code_root / "skills" / "shared" / "scripts"
    if str(shared) not in sys.path:
        sys.path.insert(0, str(shared))
    runtime_paths = importlib.import_module("runtime_paths")
    if runtime_paths.chromium_launch_args(
            ["--disable-gpu", "--no-sandbox"]) != ["--disable-gpu"]:
        _fail("worker 模式未移除 Chromium 沙箱禁用参数")
    if runtime_paths.chromium_sandbox_options() != {"chromium_sandbox": True}:
        _fail("worker 模式未显式启用 Playwright 沙箱")

    from easel.harnesses.acp import ACPAdapter
    from easel.harnesses.base import RunRequest

    cwd = Path(tempfile.mkdtemp(prefix="easel-acp-smoke-"))
    env = {
        "EASEL_SERVICE_MODE": "worker",
        "EASEL_CODE_ROOT": str(code_root),
        "EASEL_WORKSPACE_ROOT": str(workspace),
        "EASEL_STATE_ROOT": str(state),
        "HOME": os.environ.get("HOME") or "/home/easel",
    }
    request = RunRequest(
        prompt="镜像构建期 ACP 冒烟", cwd=cwd, env=env,
        command=tuple(command), code_root=code_root,
        workspace_id=SMOKE_WORKSPACE, sandbox="workspace-write", timeout=30)
    bound: list[str] = []

    async def ask(_questions):
        return {}

    try:
        asyncio.run(ACPAdapter().run(
            request, lambda _kind, _text: None, ask, bound.append))
    except Exception as error:  # noqa: BLE001
        _fail(f"ACP 指令装配/会话冒烟失败：{error}")
    if bound != [SMOKE_SESSION_ID]:
        _fail(f"ACP 会话未按预期建立：{bound}")


def _check_dsh() -> None:
    """最终镜像门槛：固定源码、真实 CLI/profile、原生依赖与 ACP 握手。"""
    code_root = _code_root()
    dsh = Path("/opt/dsh")
    managed_dir = code_root / "managed"
    profile = managed_dir / "acp-profile.json"
    patch = managed_dir / "acp.patch.yml"
    if not dsh.is_dir() or not (dsh / ".git").exists():
        _fail("缺少已固定的 /opt/dsh checkout")
    for path in (dsh / ".git" / "HEAD", profile, patch):
        if not path.is_file():
            _fail(f"缺少受管 Harness 文件：{path}")
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o222:
            _fail(f"受管 Harness 配置必须只读：{path} mode={oct(mode)}")

    deploy_dir = Path(__file__).resolve().parent
    if str(deploy_dir) not in sys.path:
        sys.path.insert(0, str(deploy_dir))
    import smoke_harness  # noqa: PLC0415
    import verify_managed_profile  # noqa: PLC0415

    try:
        manifest = verify_managed_profile.validate_manifest(
            json.loads(profile.read_text(encoding="utf-8")),
            expected_commit=os.environ.get("DSH_COMMIT") or None,
            patch_root=managed_dir)
        details = verify_managed_profile.verify_installation(
            manifest, dsh_root=dsh, managed_root=managed_dir)
        acp = smoke_harness.run_smoke(profile)
    except (verify_managed_profile.ProfileError,
            smoke_harness.SmokeError) as error:
        _fail(f"真实 Harness profile/ACP 门槛失败：{error}")
    print(
        "DSH_PROFILE_OK commit={} node={} patch_sha256={} model={}".format(
            details["commit"], details["node"],
            details["patch_sha256"], acp["model"]))


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if FAKE_ACP_FLAG in arguments:
        return _fake_acp()
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-files", action="store_true")
    parser.add_argument("--require-dsh", action="store_true")
    args = parser.parse_args(arguments)

    code_root = _code_root()
    if str(code_root) not in sys.path:
        sys.path.insert(0, str(code_root))
    _check_files(code_root)
    _check_worker_entry(code_root)
    if args.require_dsh:
        _check_dsh()
    print("WORKER_SMOKE_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
