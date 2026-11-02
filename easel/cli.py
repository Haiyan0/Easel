"""Easel CLI — 社媒内容工作流整合层。

用法：
    easel chat                              # 新会话（选画像后进入）
    easel doctor                            # 检查环境
    easel gateway {start|stop|status}       # 管理 gateway
    easel ping                              # 连通性测试
    easel skill <name> -i "..." -p <画像>   # 运行 SKILL
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

from easel.commands.doctor import cmd_doctor
from easel.commands.gateway import cmd_gateway
from easel.commands.ping import cmd_ping
from easel.commands.skill import cmd_skill
from easel.control.cli import (cmd_admin_backup, cmd_admin_barrier_status,
                               cmd_admin_bootstrap, cmd_admin_drain,
                               cmd_admin_invite, cmd_admin_materialize_restore,
                               cmd_admin_migrate_local, cmd_admin_prepare_restore,
                               cmd_admin_reopen, cmd_admin_restore,
                               cmd_admin_resume, cmd_admin_stop_complete,
                               cmd_admin_stop_status, cmd_admin_update_status,
                               cmd_admin_upgrade_complete,
                               cmd_admin_verify_backup, cmd_serve_control)
from easel.openclaw_cmd import openclaw_base_cmd
from easel.persona import list_personas as _list_personas
from easel.persona import persona_prefix
from easel.persona import chat_turn_message
from easel.persona import load_profile_text
from easel.direct_api import (DirectAPIError, SYSTEM_PROMPT, history_path,
                              read_settings, stream_chat)
from easel.timeouts import TIMEOUT_CHAT
from easel import harnesses
from easel.harnesses.base import HarnessError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILES_DIR = PROJECT_ROOT / "profiles"
PROFILE = "easel"


def _proxy_env() -> dict[str, str]:
    """返回带外网代理的环境变量（保护内网直连）。"""
    env = os.environ.copy()
    env.setdefault("EASEL_ROOT", str(PROJECT_ROOT))
    env.setdefault("http_proxy", os.environ.get("EASEL_PROXY", ""))
    env.setdefault("https_proxy", os.environ.get("EASEL_PROXY", ""))
    env.setdefault("no_proxy", "localhost,127.0.0.1,*.xiaohongshu.com,*.devops.xiaohongshu.com,10.*")
    return env

CYAN = "\033[0;36m"
GREEN = "\033[0;32m"
YELLOW = "\033[0;33m"
RED = "\033[0;31m"
DIM = "\033[0;90m"
NC = "\033[0m"


def _build_chat_cmd(session_key: str, message_prefix: str = "") -> list[str]:
    """组装 `easel chat` 的 openclaw tui 命令行。

    必须用 openclaw_base_cmd() 解析启动方式，不能写死字面量 "openclaw"：Windows 上
    PATH 里的 openclaw 是 .cmd shim，CreateProcess 跑不了（见 easel/openclaw_cmd.py）。
    必须用 tui 子命令——`chat`/`terminal` 是 tui 的别名，会强制本地模式
    （openclaw dist/tui-cli-*.js: invokedSubcommand === "chat" → isLocal = true），
    而 --local 要求独占 state 目录，与 Easel 自启的 gateway 冲突，只会打印
    "A Gateway is running for this state directory" 后退出。
    """
    cmd = openclaw_base_cmd() + [
        "--profile", PROFILE,
        "tui",
        "--session", session_key,
        # chat 里可能直接发起制作层/跨层编排，给足制作层预算，避免长任务被 turn 超时掐断（O2）。
        # 超时统一走 easel/timeouts.py（三入口单一真相源），毫秒 = TIMEOUT_CHAT * 1000。
        "--timeout-ms", str(TIMEOUT_CHAT * 1000),
    ]
    # 画像作为初始消息内联注入（无全局 USER.md，避免并发竞态；与 web/skill 同源）。
    # 说明：注入随 session 历史留存，超长会话被压缩后可能丢画像——换取「每个请求自包含」，
    # 与 docs/prompt-stack.md 声明的架构一致。
    if message_prefix:
        cmd += ["--message", message_prefix]
    return cmd


def cmd_chat(_args) -> int:
    """启动 Easel 交互对话（每次新会话）。"""

    print()
    print(f"  {CYAN}Easel{NC} — 社媒内容工作流")
    print()

    # ---- 选择画像 ----
    personas = _list_personas()
    selected_persona = None

    if personas:
        print("  选择用户画像：")
        for i, name in enumerate(personas, 1):
            identity = PROFILES_DIR / name / "identity.md"
            desc = ""
            if identity.is_file():
                for line in identity.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and not line.startswith("<!--"):
                        desc = f"  — {line[:50]}"
                        break
            print(f"    {i}) {name}{desc}")
        print(f"    0) 不使用画像（通用模式）")
        print()

        try:
            choice = input("  请选择 [0]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if choice and choice != "0":
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(personas):
                    selected_persona = personas[idx]
            except ValueError:
                if choice in personas:
                    selected_persona = choice

    # ---- 每次新会话 ----
    if selected_persona:
        print(f"\n  {GREEN}✓{NC} 画像: {selected_persona}")
    else:
        print(f"\n  {YELLOW}→{NC} 通用模式")

    session_key = f"easel-{time.strftime('%m%d-%H%M%S')}"

    settings = read_settings()
    runtime = harnesses.settings(PROJECT_ROOT / "outputs", settings)["runtime"]
    if runtime not in ("api", "openclaw"):
        print(f"  Agent 底座：{runtime}（/exit 退出）")
        native_id = ""

        def remember(value):
            nonlocal native_id
            native_id = value

        while True:
            try:
                message = input("你：").strip()
                if message in ("/exit", "/quit"):
                    return 0
                if message:
                    # timeout 与同命令的 openclaw TUI / web 路径对齐（TIMEOUT_CHAT=7200）：
                    # chat 可能中途触发制作层任务，console_turn 默认 1800s 会把它掐断。
                    _, native_id = asyncio.run(harnesses.console_turn(
                        chat_turn_message(message, selected_persona), PROJECT_ROOT,
                        {**settings, **_proxy_env()}, native_id, on_bind=remember,
                        timeout=TIMEOUT_CHAT))
                    print()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            except (HarnessError, asyncio.TimeoutError) as error:
                print(f"\nAgent 执行失败：{error or '请求超时'}")
    if runtime == "api":
        print("  API 直连模式（文字问答；/exit 退出）\n")
        system = SYSTEM_PROMPT
        if selected_persona:
            system += "\n\n当前画像：\n" + load_profile_text(selected_persona)
        async def reply(message):
            async for kind, text in stream_chat(
                    read_settings(), message, system=system,
                    history_file=history_path(PROJECT_ROOT / "outputs" / "_sessions", session_key),
                    timeout=TIMEOUT_CHAT):
                if kind == "token":
                    print(text, end="", flush=True)
        while True:
            try:
                message = input("你：").strip()
                if message in ("/exit", "/quit"):
                    return 0
                if message:
                    asyncio.run(asyncio.wait_for(reply(message), TIMEOUT_CHAT))
                    print()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            except (DirectAPIError, asyncio.TimeoutError) as error:
                print(f"\n❌ {error or 'API 请求超时'}")

    print(f"  {DIM}会话: {session_key}{NC}")
    print(f"  {DIM}切换历史会话: 对话中输入 /session{NC}")
    print(f"  {CYAN}Ctrl+C{NC} 退出")
    print()

    prefix = persona_prefix(selected_persona)
    try:
        # openclaw_base_cmd() 在 openclaw 全装不上时抛 FileNotFoundError；
        # subprocess.run 在可执行文件缺失时同样抛 FileNotFoundError。两者都提示安装。
        cmd = _build_chat_cmd(session_key, prefix)
        result = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=_proxy_env())
    except FileNotFoundError:
        print(f"{RED}未找到 openclaw{NC} — 请先安装：npm i -g openclaw，"
              f"或运行 `python -m easel doctor` 检查环境。", file=sys.stderr)
        return 1

    return result.returncode


def cmd_web(args) -> int:
    """启动 Web 工作台（FastAPI + React），默认 http://localhost:7860。"""
    from easel.runtime_env import web_environment

    port = getattr(args, "port", 7860)
    env = web_environment(_proxy_env())
    env["EASEL_PORT"] = str(port)
    script = PROJECT_ROOT / "web" / "app.py"
    if not script.is_file():
        print(f"{RED}未找到 web/app.py{NC} — 安装可能不完整，请重跑 `bash setup.sh`。",
              file=sys.stderr)
        return 1
    # 透传子进程返回码：端口占用/依赖缺失导致 app.py 立刻退出时，调用方
    # （自启/CI/脚本）能据此判断启动是否成功，而非恒拿到 0。
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(PROJECT_ROOT),
        env=env,
    )
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="easel",
        description="Easel — 社媒内容工作流整合层 CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # chat
    p_chat = sub.add_parser("chat", help="交互对话（新会话）")
    p_chat.set_defaults(func=cmd_chat)

    # doctor
    p_doctor = sub.add_parser("doctor", help="检查环境")
    p_doctor.set_defaults(func=cmd_doctor)

    # gateway
    p_gw = sub.add_parser("gateway", help="管理 OpenClaw gateway")
    p_gw.add_argument("action", choices=["start", "stop", "restart", "status", "logs"],
                       default="status", nargs="?")
    p_gw.set_defaults(func=cmd_gateway)

    # ping
    p_ping = sub.add_parser("ping", help="连通性测试")
    p_ping.set_defaults(func=cmd_ping)

    # skill
    p_skill = sub.add_parser("skill", help="运行 SKILL（自动路由）")
    p_skill.add_argument("name", help="SKILL 名称")
    p_skill.add_argument("--input", "-i", required=True, help="输入内容")
    p_skill.add_argument("--profile", "-p", default=None, help="用户画像名称")
    p_skill.set_defaults(func=cmd_skill)

    # web
    p_web = sub.add_parser("web", help="启动 Web UI")
    p_web.add_argument("--port", type=int, default=7860, help="端口（默认 7860）")
    p_web.set_defaults(func=cmd_web)

    # serve-control
    p_serve = sub.add_parser("serve-control", help="启动多人控制面 HTTP 服务")
    p_serve.add_argument("--control-dir", required=True,
                         help="控制目录（存放 control.sqlite3）")
    p_serve.add_argument("--public-origin", required=True,
                         help="浏览器公共 Origin，例如 https://easel.example.com")
    p_serve.add_argument("--master-key-file", required=True,
                         help="受保护的 32 字节 AES-GCM master key 文件")
    p_serve.add_argument("--host", default="127.0.0.1", help="监听地址")
    p_serve.add_argument("--port", type=int, default=7861, help="端口（默认 7861）")
    p_serve.add_argument(
        "--publish-mode", choices=("automated", "manual"),
        default="automated",
        help="受管发布策略（默认 automated，保留原自动发布路径）")
    p_serve.add_argument("--egress-proxy", default=None,
                         help="受控出口代理 URL，例如 http://127.0.0.1:8899")
    p_serve.add_argument("--egress-policy", default=None,
                         help="管理员出口策略 JSON（approved_hosts/approved_cidrs）")
    p_serve.add_argument("--egress-ca-file", default=None,
                         help="可选：受控出口额外信任的 PEM CA 文件")
    p_serve.add_argument("--provisioner-socket", default=None,
                         help="受限 provisioner 的本地 Unix socket；缺失时失败关闭")
    p_serve.add_argument(
        "--gateway-required", action="store_true",
        help="要求 v2 gateway 元数据并在 CAS 后激活；未提供 provisioner 时失败")
    p_serve.add_argument("--snapshot-key-file", default=None,
                         help="受保护的 Ed25519 私钥文件；与受管 Agent 配置成对使用")
    p_serve.add_argument("--managed-agent-config", default=None,
                         help="受保护的受管 Agent JSON（revision/agent）；不从用户目录推断")
    p_serve.add_argument(
        "--managed-profile", default=None,
        help="受管 ACP manifest（默认控制镜像内 /opt/easel/managed/acp-profile.json）")
    p_serve.add_argument(
        "--expected-harness-commit", default=None,
        help="可选：目标 worker 镜像的 Harness commit；与 manifest 不符时失败")
    p_serve.add_argument(
        "--frontend-dir",
        default=str(PROJECT_ROOT / "web" / "frontend" / "dist"),
        help="固定前端构建目录（含 index.html）；缺失时明确失败")
    p_serve.set_defaults(func=cmd_serve_control)

    # admin：仅本地/服务器执行，不提供远程无认证等价接口
    p_admin = sub.add_parser("admin", help="本地管理员操作")
    admin_sub = p_admin.add_subparsers(dest="admin_command", required=True)
    p_bootstrap = admin_sub.add_parser("bootstrap", help="引导首个管理员")
    p_bootstrap.add_argument("--control-dir", required=True)
    p_bootstrap.add_argument("--name", required=True, help="管理员显示名")
    p_bootstrap.set_defaults(func=cmd_admin_bootstrap)
    p_invite = admin_sub.add_parser("invite", help="为现有用户签发一次性邀请")
    p_invite.add_argument("--control-dir", required=True)
    p_invite.add_argument("--admin-user-id", required=True, help="执行邀请的管理员用户 ID")
    p_invite.add_argument("--user-id", required=True, help="被邀请用户 ID")
    p_invite.add_argument("--ttl-seconds", type=int, default=604800,
                          help="邀请有效期秒数（默认 604800）")
    p_invite.set_defaults(func=cmd_admin_invite)
    p_drain = admin_sub.add_parser(
        "drain", help="drain 所有活动 workspace（停止/备份前）")
    p_drain.add_argument("--control-dir", required=True)
    p_drain.add_argument("--provisioner-socket", required=True)
    p_drain.add_argument("--gateway-required", action="store_true")
    p_drain.add_argument(
        "--barrier", action="store_true",
        help="drain 前建立跨进程停止接纳屏障（Desktop Stop 使用）")
    p_drain.set_defaults(func=cmd_admin_drain)
    p_resume = admin_sub.add_parser(
        "resume", help="清除停止接纳屏障，恢复新任务接纳")
    p_resume.add_argument("--control-dir", required=True)
    p_resume.add_argument("--barrier-id", default=None,
                          help="只清除指定屏障，避免误放行")
    p_resume.set_defaults(func=cmd_admin_resume)
    p_stop_complete = admin_sub.add_parser(
        "stop-complete", help="记录一次成功 Stop 的完成 fencing")
    p_stop_complete.add_argument("--control-dir", required=True)
    p_stop_complete.add_argument("--barrier-id", required=True)
    p_stop_complete.add_argument("--project", required=True)
    p_stop_complete.add_argument("--config-sha256", required=True)
    p_stop_complete.set_defaults(func=cmd_admin_stop_complete)
    p_reopen = admin_sub.add_parser(
        "reopen", help="仅按匹配的成功 Stop 记录恢复接纳")
    p_reopen.add_argument("--control-dir", required=True)
    p_reopen.add_argument("--barrier-id", required=True)
    p_reopen.add_argument("--project", required=True)
    p_reopen.add_argument("--config-sha256", required=True)
    p_reopen.set_defaults(func=cmd_admin_reopen)
    p_barrier = admin_sub.add_parser(
        "barrier-status", help="只读打印当前停止接纳屏障")
    p_barrier.add_argument("--control-dir", required=True)
    p_barrier.set_defaults(func=cmd_admin_barrier_status)
    p_update_status = admin_sub.add_parser(
        "update-status", help="只读输出更新所需的活动工作与 workspace 清单")
    p_update_status.add_argument("--control-dir", required=True)
    p_update_status.set_defaults(func=cmd_admin_update_status)
    p_backup = admin_sub.add_parser(
        "backup", help="备份控制库、master key 与 workspace 持久区")
    p_backup.add_argument("--control-dir", required=True)
    p_backup.add_argument("--destination", required=True,
                          help="受管根内的新备份目录")
    p_backup.add_argument("--master-key-file", required=True)
    p_backup.add_argument("--workspace-root", default=None,
                          help="workspace 持久区根；省略时只备份控制库与 key")
    p_backup.add_argument("--image-digest", default="",
                          help="记录到清单的 worker 镜像摘要")
    p_backup.add_argument("--harness-revision", default="",
                          help="记录到清单的 Harness 修订")
    p_backup.add_argument("--provisioner-socket", default=None,
                          help="有活动 worker 时必须提供，用于先 drain")
    p_backup.add_argument("--signing-key-file", default=None,
                          help="Ed25519 签名私钥；与受管配置/策略成组给出")
    p_backup.add_argument("--managed-agent-config", default=None,
                          help="受管 Agent 配置 JSON（不得含密钥）")
    p_backup.add_argument("--egress-policy-file", default=None,
                          help="管理员出口策略 JSON")
    p_backup.add_argument("--egress-ca-file", default=None,
                          help="可选；出口 TLS 校验 CA（公开证书文件）")
    p_backup.add_argument("--deployment-json", default=None,
                          help="非秘密部署身份 JSON（镜像/配置摘要/项目）")
    p_backup.add_argument("--require-clean-stop", action="store_true",
                          help="只允许备份已完成干净 Stop 的实例")
    p_backup.set_defaults(func=cmd_admin_backup)

    p_verify_backup = admin_sub.add_parser(
        "verify-backup", help="只读校验完整备份")
    p_verify_backup.add_argument("--source", required=True)
    p_verify_backup.set_defaults(func=cmd_admin_verify_backup)

    p_prepare_restore = admin_sub.add_parser(
        "prepare-restore", help="准备恢复副本并签发一次性管理员恢复邀请")
    p_prepare_restore.add_argument("--destination", required=True)
    p_prepare_restore.add_argument(
        "--invite-ttl-seconds", type=int, default=3600,
        help="恢复邀请有效期秒数（默认 3600）")
    p_prepare_restore.set_defaults(func=cmd_admin_prepare_restore)

    p_materialize = admin_sub.add_parser(
        "materialize-restore", help="把恢复包落到新部署目标目录")
    p_materialize.add_argument("--source", required=True)
    p_materialize.add_argument("--control-dir", required=True)
    p_materialize.add_argument("--keys-dir", required=True)
    p_materialize.add_argument("--egress-dir", required=True)
    p_materialize.add_argument("--workspace-root", required=True)
    p_materialize.set_defaults(func=cmd_admin_materialize_restore)

    p_stop_status = admin_sub.add_parser(
        "stop-status", help="只读打印 Stop 完成记录")
    p_stop_status.add_argument("--control-dir", required=True)
    p_stop_status.set_defaults(func=cmd_admin_stop_status)

    p_upgrade = admin_sub.add_parser(
        "upgrade-complete", help="受控升级：匹配旧配置后改写 Stop 完成记录")
    p_upgrade.add_argument("--control-dir", required=True)
    p_upgrade.add_argument("--barrier-id", required=True)
    p_upgrade.add_argument("--project", required=True)
    p_upgrade.add_argument("--from-config-sha256", required=True)
    p_upgrade.add_argument("--to-config-sha256", required=True)
    p_upgrade.add_argument("--backup-manifest-sha256", required=True)
    p_upgrade.set_defaults(func=cmd_admin_upgrade_complete)

    p_restore = admin_sub.add_parser(
        "restore", help="把备份恢复到全新目录")
    p_restore.add_argument("--source", required=True)
    p_restore.add_argument("--destination", required=True,
                           help="不存在或为空的新恢复根")
    p_restore.add_argument("--managed-root", default=None,
                           help="可选；恢复目标必须位于该受管根内")
    p_restore.set_defaults(func=cmd_admin_restore)

    p_migrate = admin_sub.add_parser(
        "migrate-local", help="显式迁移本机 outputs 与旧会话")
    p_migrate.add_argument("--source", required=True,
                           help="本机 outputs 目录或含 outputs 的安装根")
    p_migrate.add_argument("--workspace", required=True,
                           help="目标 workspace ID（32 位小写十六进制）")
    p_migrate.add_argument("--workspace-root", required=True,
                           help="目标 workspace 持久区根")
    p_migrate.add_argument("--dry-run", action="store_true",
                           help="只输出清单，不写备份或目标")
    p_migrate.add_argument("--backup-destination", default=None,
                           help="可选；迁移前完整备份目录（默认写入 workspace 根）")
    p_migrate.set_defaults(func=cmd_admin_migrate_local)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except HarnessError as error:
        print(f"Agent 设置错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
