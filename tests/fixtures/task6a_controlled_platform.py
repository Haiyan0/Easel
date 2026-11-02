"""Task 6A 受控平台脚本替身（仅隔离演练使用）。

本文件不访问任何真实平台，也不读取真实凭证。它被复制到受控 worker 镜像的
``/opt/easel/skills/shared/scripts/`` 下，替换原平台脚本的文件名，由正式
worker 的受管执行器以真实子进程启动。用途是演练账号登录、分析、自动发布、
短信和停止链路；原平台脚本在本轮不执行。

行为由脚本文件名和参数决定：

- ``xhs_publish.py`` / ``douyin_publish.py`` / ``web_publisher.py`` /
  ``weixin_mp_stats.py`` / ``bili_login.py`` / ``account_stats.py``
- 标题/正文含 ``TASK6A-HANG``：保持运行等待停止（模拟平台未确认）；
- 标题/正文含 ``TASK6A-UNKNOWN``：写出未确认错误并退出 3；
- 其余正常路径：启动真实 Chromium、使用本人 HOME 的持久 profile、写证明
  与提交计数，不访问网络。

证明文件位于本人 ``outputs/_publish/controlled-proofs/<run>.json``，
提交计数位于 ``outputs/_publish/controlled-count.txt``。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8"
    b"\xff\xff?\x00\x05\xfe\x02\xfe\xa75\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82")

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


def _launch(playwright, name: str):
    return playwright.chromium.launch_persistent_context(
        str(_profile(name)), headless=True, chromium_sandbox=True,
        viewport={"width": 320, "height": 320})


def _qr_login(platform: str) -> int:
    from playwright.sync_api import sync_playwright

    qr_out = Path(_arg("--qr-out"))
    status_file = Path(_arg("--status-file"))
    code_file = status_file.with_suffix(".code")
    timeout = float(_arg("--timeout", "60"))
    qr_out.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = _launch(playwright, platform)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_content(QR_HTML)
            page.locator("#qr").screenshot(path=str(qr_out))
            _write_json(status_file, {
                "state": "qr_ready", "message": "受控二维码已就绪",
                "qr": f"_login/{qr_out.name}", "ts": int(time.time()),
            })
            time.sleep(0.8)
            _write_json(status_file, {
                "state": "sms_required",
                "message": "请输入容器受控短信验证码（123456）",
                "qr": f"_login/{qr_out.name}", "ts": int(time.time()),
            })
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if code_file.is_file():
                    code = code_file.read_text(
                        encoding="utf-8", errors="replace").strip()
                    if code == "123456":
                        _write_json(status_file, {
                            "state": "success", "message": "受控登录成功",
                            "qr": "", "ts": int(time.time()),
                        })
                        return 0
                time.sleep(0.2)
            _write_json(status_file, {
                "state": "expired", "message": "受控登录等待超时",
                "qr": "", "ts": int(time.time()),
            })
            return 0
        finally:
            context.close()


def _whoami(platform: str) -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = _launch(playwright, platform)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_content(
                "<!doctype html><title>whoami</title>"
                "<div id='n'>受控账号</div>")
            identity = {
                "loggedIn": True,
                "name": page.locator("#n").inner_text(),
                "avatar": "",
            }
        finally:
            context.close()
    print(json.dumps(identity, ensure_ascii=False), flush=True)
    return 0


def _stats(platform: str) -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = _launch(playwright, platform)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_content(
                "<!doctype html><script>window.stats="
                "{followers:12,likes:34}</script>")
            stats = page.evaluate("() => window.stats")
        finally:
            context.close()
    payload = {
        "platform": platform, "name": "受控账号",
        "nickname": "受控账号", "loggedIn": True,
        "followers": stats["followers"], "likes": stats["likes"],
        "following": None, "posts": None,
        "metrics": [{"label": "近7日", "value": 5, "vs": "+1"}],
        "notes": [{"title": "受控笔记", "url": "https://example.test/1"}],
        "growth": {"week": {"followers": 2, "likes": 3, "posts": None,
                            "since_days": 7}},
        "fetched_at": int(time.time()),
    }
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return 0


def _arg(name: str, default: str | None = None) -> str:
    try:
        index = sys.argv.index(name)
    except ValueError:
        if default is None:
            raise SystemExit(f"缺少参数 {name}")
        return default
    return sys.argv[index + 1]


def _optional(name: str) -> str | None:
    try:
        return _arg(name)
    except SystemExit:
        return None


def _media_paths() -> list[Path]:
    paths: list[Path] = []
    for flag in ("--images", "--media", "--video"):
        try:
            value = _arg(flag)
        except SystemExit:
            continue
        if not value:
            continue
        for item in value.split(",") if flag == "--images" else [value]:
            item = item.strip()
            if item:
                paths.append(Path(item))
    return paths


def _outputs() -> Path:
    return Path(os.environ.get(
        "EASEL_WORKSPACE_ROOT", str(Path.cwd()))) / "outputs"


def _count(path: Path, run: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(run + "\n")
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines()
               if line.strip())


def _proof(platform: str, title: str, body: str, media: list[Path]) -> Path:
    outputs = _outputs()
    run = os.environ.get("EASEL_RUN_ID", "unknown")
    proof_file = outputs / "_publish" / "controlled-proofs" / f"{run}.json"
    counter_file = outputs / "_publish" / "controlled-count.txt"
    count = _count(counter_file, run)
    _write_json(proof_file, {
        "platform": platform,
        "title": title,
        "body": body,
        "count": count,
        "media": [{
            "name": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        } for path in media],
        "home": os.environ.get("HOME", ""),
        "profile": str(_profile(platform)),
        "run": run,
        "ts": int(time.time()),
    })
    return proof_file


def _publish(platform: str) -> int:
    from playwright.sync_api import sync_playwright

    title = _arg("--title", "")
    body = _optional("--content") or _optional("--desc") or ""
    media = _media_paths()
    marker = f"{title}\n{body}"
    status_value = _optional("--status-file")
    code_value = _optional("--sms-code-file")
    status_file = Path(status_value) if status_value else None
    code_file = Path(code_value) if code_value else None
    if not all(path.is_file() for path in media):
        print("missing media", file=sys.stderr)
        return 2
    # 真实子进程 + 真实 Chromium + 本人 HOME 持久 profile。
    with sync_playwright() as playwright:
        context = _launch(playwright, platform)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_content(
                "<!doctype html><title>controlled publish</title>"
                "<div id='status'>ready</div>")
            if page.locator("#status").inner_text() != "ready":
                return 2
            _proof(platform, title, body, media)
            if status_file is not None:
                _write_json(status_file, {
                    "state": "starting", "message": "受控发布启动"})
                if "TASK6A-UNKNOWN" in marker:
                    _write_json(status_file, {
                        "state": "error",
                        "message": "平台结果未确认，请核对"})
                    return 3
                _write_json(status_file, {
                    "state": "sms_required",
                    "message": "请输入容器受控短信验证码（123456）"})
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    if "TASK6A-HANG" in marker:
                        time.sleep(0.5)
                        continue
                    if code_file is not None and code_file.is_file():
                        code = code_file.read_text(
                            encoding="utf-8", errors="replace").strip()
                        _write_json(status_file, {
                            "state": "verifying",
                            "message": "正在验证验证码…"})
                        if code == "123456":
                            _write_json(status_file, {
                                "state": "success",
                                "message": "受控发布成功"})
                            return 0
                        _write_json(status_file, {
                            "state": "sms_required",
                            "message": "验证码不正确"})
                    time.sleep(0.2)
                _write_json(status_file, {
                    "state": "error", "message": "短信验证超时"})
                return 1
            if "TASK6A-UNKNOWN" in marker:
                return 3
            if "TASK6A-HANG" in marker:
                deadline = time.monotonic() + 300
                while time.monotonic() < deadline:
                    time.sleep(0.5)
                return 3
            time.sleep(0.3)
            return 0
        finally:
            context.close()


def main() -> int:
    script = Path(sys.argv[0]).name
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if script == "account_stats.py":
        return _stats(_arg("--platform", "xiaohongshu"))
    platform = {
        "xhs_publish.py": "xiaohongshu",
        "douyin_publish.py": "douyin",
        "web_publisher.py": _arg("--platform", "web"),
        "weixin_mp_stats.py": "wechat-oa",
        "bili_login.py": "bilibili",
    }.get(script, script)
    if mode in ("login", "login-qr"):
        return _qr_login(platform)
    if mode == "whoami":
        return _whoami(platform)
    if mode == "stats":
        return _stats(platform)
    if mode == "publish":
        return _publish(platform)
    print(f"unsupported controlled mode: {script} {mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
