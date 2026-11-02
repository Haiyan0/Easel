"""5C2 R1 隔离容器内的固定受控平台 runner。

这个文件不是生产平台脚本，也不访问任何真实平台。它被挂载进 worker 容器，
由 launcher 的受管入口以**真实子进程**启动；每次运行都会用镜像内 Playwright
启动 Chromium，读取/写入本人 HOME 下的持久 profile、二维码和状态文件。

用途：证明镜像内浏览器、子进程、profile、二维码/结果文件和取消链路真实可用。
原平台脚本（xhs_publish.py / account_stats.py / weixin_mp_stats.py 等）在容器
验收中不执行；它们需要的真实平台站点、账号和网络均被本 runner 替代。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

QR_HTML = """<!doctype html>
<meta charset="utf-8">
<style>html,body{margin:0;background:#fff}canvas{display:block}</style>
<canvas id="qr" width="240" height="240"></canvas>
<script>
const c = document.getElementById('qr');
const x = c.getContext('2d');
x.fillStyle = '#fff'; x.fillRect(0, 0, 240, 240);
x.fillStyle = '#111';
for (let i = 0; i < 24; i++) {
  for (let j = 0; j < 24; j++) {
    if ((i * 7 + j * 11 + i * j) % 5 < 2) x.fillRect(10 + i * 9, 10 + j * 9, 8, 8);
  }
}
</script>
"""


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _profile(name: str) -> Path:
    root = Path(os.environ["HOME"]) / ".easel-browser-profiles" / name / "default"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _launch(playwright, profile_name: str):
    # worker 模式强制 Chromium 内置沙箱；容器测试用 seccomp=unconfined
    # 允许非特权用户命名空间，而不是关闭浏览器沙箱。
    return playwright.chromium.launch_persistent_context(
        str(_profile(profile_name)),
        headless=True,
        chromium_sandbox=True,
        viewport={"width": 280, "height": 280},
    )


def _login(args: argparse.Namespace) -> int:
    from playwright.sync_api import sync_playwright

    qr_out = Path(args.qr_out)
    status_file = Path(args.status_file)
    code_file = Path(args.code_file)
    qr_out.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = _launch(playwright, args.profile_name)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_content(QR_HTML)
            page.locator("#qr").screenshot(path=str(qr_out))
            _write_json(status_file, {
                "state": "qr_ready", "message": "容器受控二维码已就绪",
                "qr": f"_login/{qr_out.name}", "ts": int(time.time()),
            })
            # 留一个轮询窗口让执行器先上报 qr_ready，再进入固定短信墙；
            # 验证 control 只接受当前 Run 自己报告的 sms_required。
            time.sleep(1.0)
            _write_json(status_file, {
                "state": "sms_required",
                "message": "请输入容器受控短信验证码（123456）",
                "qr": f"_login/{qr_out.name}", "ts": int(time.time()),
            })
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                if code_file.is_file():
                    try:
                        code = code_file.read_text(encoding="utf-8").strip()
                    except OSError:
                        code = ""
                    if code == "123456":
                        _write_json(status_file, {
                            "state": "success", "message": "容器受控登录成功",
                            "qr": "", "ts": int(time.time()),
                        })
                        return 0
                time.sleep(0.2)
            _write_json(status_file, {
                "state": "expired", "message": "容器受控登录等待超时",
                "qr": "", "ts": int(time.time()),
            })
            return 0
        finally:
            context.close()


def _whoami(args: argparse.Namespace) -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = _launch(playwright, args.profile_name)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_content(
                "<!doctype html><title>whoami</title><div id='n'>容器受控昵称</div>")
            identity = {
                "loggedIn": True,
                "name": page.locator("#n").inner_text(),
                "avatar": "",
            }
        finally:
            context.close()
    print(json.dumps(identity, ensure_ascii=False), flush=True)
    return 0


def _analytics(args: argparse.Namespace) -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = _launch(playwright, args.profile_name)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_content(
                "<!doctype html><script>window.stats={followers:12,likes:34}"
                "</script>")
            stats = page.evaluate("() => window.stats")
        finally:
            context.close()
    payload = {
        "platform": args.platform, "name": "容器受控账号",
        "nickname": "容器受控昵称", "loggedIn": True,
        "followers": stats["followers"], "likes": stats["likes"],
        "following": None, "posts": None,
        "metrics": [{"label": "近7日", "value": 5, "vs": "+1"}],
        "notes": [{"title": "容器受控笔记", "url": "https://example.test/1"}],
        "growth": {"week": {"followers": 2, "likes": 3, "posts": None,
                            "since_days": 7}},
        "fetched_at": int(time.time()),
    }
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return 0


def _verify(_args: argparse.Namespace) -> int:
    from playwright.sync_api import sync_playwright

    # 秘密只经 stdin 进入本进程，不进入 argv/日志。
    data = json.load(sys.stdin)
    app_id = str(data.get("app_id") or "")
    secret = str(data.get("app_secret") or "")
    with sync_playwright() as playwright:
        context = _launch(playwright, "wechat-oa")
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_content("<!doctype html><title>verify</title><p>x</p>")
            ready = page.locator("p").is_visible()
        finally:
            context.close()
    if ready and app_id.startswith("wx") and len(secret) >= 6:
        print("OK", flush=True)
        return 0
    print("ERR:invalid_secret", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)
    login = sub.add_parser("login")
    login.add_argument("--platform", required=True)
    login.add_argument("--profile-name", default="")
    login.add_argument("--qr-out", required=True)
    login.add_argument("--status-file", required=True)
    login.add_argument("--code-file", required=True)
    login.add_argument("--timeout", type=float, default=60.0)
    whoami = sub.add_parser("whoami")
    whoami.add_argument("--platform", required=True)
    whoami.add_argument("--profile-name", default="")
    analytics = sub.add_parser("analytics")
    analytics.add_argument("--platform", required=True)
    analytics.add_argument("--profile-name", default="")
    sub.add_parser("verify")
    args = parser.parse_args()
    if getattr(args, "profile_name", None) == "":
        args.profile_name = getattr(args, "platform", "wechat-oa")
    if args.mode == "login":
        return _login(args)
    if args.mode == "whoami":
        return _whoami(args)
    if args.mode == "analytics":
        return _analytics(args)
    return _verify(args)


if __name__ == "__main__":
    raise SystemExit(main())
