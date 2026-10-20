"""Task 7 真实 Chromium：临时 context 隔离、持久 profile 保留与锁释放。"""
from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills" / "shared" / "scripts"))

from easel.control.types import WorkspacePaths  # noqa: E402
from easel.worker.browser import BrowserProfiles, ProfileLock  # noqa: E402

playwright = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright.sync_playwright


def _launch_or_skip(p):
    try:
        return p.chromium.launch(headless=True)
    except Exception as error:  # noqa: BLE001
        pytest.skip(f"Chromium 不可用：{error}")


def _profiles(tmp_path: Path) -> BrowserProfiles:
    root = tmp_path / "workspace"
    return BrowserProfiles(WorkspacePaths(
        code_root=tmp_path / "code", workspace_root=root,
        state_root=root / "state", home=tmp_path / "home"))


def test_two_temporary_contexts_do_not_share_cookies():
    with sync_playwright() as p:
        browser = _launch_or_skip(p)
        try:
            first = browser.new_context()
            second = browser.new_context()
            try:
                first.add_cookies([{"name": "session", "value": "a",
                                    "domain": "example.test", "path": "/"}])
                second.add_cookies([{"name": "session", "value": "b",
                                     "domain": "example.test", "path": "/"}])
                assert first.cookies()[0]["value"] == "a"
                assert second.cookies()[0]["value"] == "b"
            finally:
                first.close()
                second.close()
        finally:
            browser.close()


def test_persistent_profile_keeps_cookie_across_restart(tmp_path):
    manager = _profiles(tmp_path)
    with sync_playwright() as p:
        browser = _launch_or_skip(p)
        browser.close()
        with manager.persistent_profile("xiaohongshu", "default",
                                        timeout=10) as profile:
            context = p.chromium.launch_persistent_context(
                str(profile), headless=True)
            try:
                context.add_cookies([{
                    "name": "persisted", "value": "yes",
                    "domain": "example.test", "path": "/",
                    "expires": 4102444800}])
            finally:
                context.close()
        with manager.persistent_profile("xiaohongshu", "default",
                                        timeout=10) as profile:
            context = p.chromium.launch_persistent_context(
                str(profile), headless=True)
            try:
                cookies = {cookie["name"]: cookie["value"]
                           for cookie in context.cookies()}
                assert cookies.get("persisted") == "yes"
            finally:
                context.close()


def test_actual_skill_navigation_error_closes_browser_and_releases_lock(
        tmp_path, monkeypatch):
    import zhihu_comments_fetch
    import zhihu_answer

    profile = tmp_path / "home" / ".easel-browser-profiles" / "zhihu" / "default"
    for module in (zhihu_comments_fetch, zhihu_answer):
        monkeypatch.setattr(module, "PROFILE_DIR", profile, raising=False)
        with pytest.raises(Exception):
            if module is zhihu_comments_fetch:
                module.fetch_comments("bad://invalid")
            else:
                module.publish_answer("bad://invalid", "正文", dry_run=True)
        lock = ProfileLock(profile)
        lock.acquire(0.5)
        lock.release()


def test_xhtml_output_is_sandboxed_in_real_chromium(tmp_path, monkeypatch):
    import uvicorn
    import web.app as web

    outputs = tmp_path / "outputs"
    (outputs / "project").mkdir(parents=True)
    (outputs / "project" / "poc.xhtml").write_text(
        "<?xml version='1.0' encoding='utf-8'?>"
        "<html xmlns='http://www.w3.org/1999/xhtml'><head><title>SAFE</title>"
        "</head><body><script>"
        "document.title='EXECUTED_SENTINEL';window.__executed=true;"
        "</script></body></html>", encoding="utf-8")
    monkeypatch.setattr(web, "OUTPUTS_DIR", outputs)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        web.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "本地 ASGI 服务未启动"
    try:
        with sync_playwright() as p:
            browser = _launch_or_skip(p)
            try:
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{port}/api/media/project/poc.xhtml",
                          wait_until="load")
                assert page.title() == "SAFE"
                assert page.evaluate("window.__executed") is None
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_exception_while_browser_open_releases_profile_lock(tmp_path):
    manager = _profiles(tmp_path)
    with sync_playwright() as p:
        browser = _launch_or_skip(p)
        browser.close()
        with pytest.raises(RuntimeError):
            with manager.persistent_profile("xiaohongshu", "default",
                                            timeout=10) as profile:
                context = p.chromium.launch_persistent_context(
                    str(profile), headless=True)
                try:
                    raise RuntimeError("boom")
                finally:
                    context.close()
        with manager.persistent_profile("xiaohongshu", "default", timeout=2):
            pass
