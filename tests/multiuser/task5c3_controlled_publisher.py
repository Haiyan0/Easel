"""5C3 隔离容器内的固定受控发布 runner。

不是生产平台脚本，不访问任何真实平台。它被 worker 的受管发布执行器以真实
子进程启动，并用镜像内 Playwright 启动 Chromium、使用本人 HOME 下的持久
profile、读取已快照的媒体、写状态/证明文件和一次性提交计数。

原平台脚本（xhs_publish.py / douyin_publish.py / web_publisher.py /
weixin_mp_stats.py / biliup）在容器验收中不执行；它们需要的真实平台站点、
账号和网络由本 runner 替代。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path


def _profile(name: str) -> Path:
    root = Path(os.environ["HOME"]) / ".easel-browser-profiles" / name / "default"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _count(path: Path, run: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(run + "\n")
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines()
               if line.strip())


def _publish(args: argparse.Namespace) -> int:
    from playwright.sync_api import sync_playwright

    media = [Path(item) for item in args.media]
    for path in media:
        if not path.is_file():
            print(f"missing media: {path}", file=sys.stderr)
            return 2
    status_file = Path(args.status_file)
    code_file = Path(args.code_file)
    proof_file = Path(args.proof_file)
    counter_file = Path(args.counter_file)
    count = _count(counter_file, proof_file.stem)
    profile = _profile(args.profile_name)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile),
            headless=True,
            chromium_sandbox=True,
            viewport={"width": 360, "height": 240},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_content(
                "<!doctype html><meta charset='utf-8'>"
                "<title>controlled publish</title><div id='status'>ready</div>")
            ready = page.locator("#status").inner_text()
            _write_json(proof_file, {
                "platform": args.platform,
                "title": args.title,
                "count": count,
                "media": [{
                    "name": path.name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                } for path in media],
                "profile": str(profile),
                "profile_used": ready == "ready",
                "published": False,
                "run": proof_file.stem,
            })
            if args.platform == "douyin":
                _write_json(status_file, {
                    "state": "sms_required",
                    "message": "请输入容器受控短信验证码（123456）",
                })
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    if code_file.is_file():
                        code = code_file.read_text(encoding="utf-8").strip()
                        _write_json(status_file, {
                            "state": "verifying",
                            "message": "正在验证验证码…",
                        })
                        if code == "123456":
                            _write_json(status_file, {
                                "state": "success",
                                "message": "容器受控发布成功",
                            })
                            return 0
                        _write_json(status_file, {
                            "state": "sms_required",
                            "message": "验证码不正确",
                        })
                    time.sleep(0.2)
                _write_json(status_file, {
                    "state": "error",
                    "message": "短信验证超时",
                })
                return 1
            time.sleep(args.sleep)
            _write_json(status_file, {
                "state": "success", "message": "容器受控发布成功",
            })
            return 0
        finally:
            context.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)
    publish = sub.add_parser("publish")
    publish.add_argument("--platform", required=True)
    publish.add_argument("--title", required=True)
    publish.add_argument("--profile-name", default="")
    publish.add_argument("--media", action="append", default=[])
    publish.add_argument("--status-file", required=True)
    publish.add_argument("--code-file", required=True)
    publish.add_argument("--proof-file", required=True)
    publish.add_argument("--counter-file", required=True)
    publish.add_argument("--sleep", type=float, default=0.2)
    args = parser.parse_args()
    if not args.profile_name:
        args.profile_name = args.platform
    if args.mode == "publish":
        return _publish(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
