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

from easel.control.types import WorkspacePaths
from easel.worker.browser import BrowserProfiles, ProfileLock
from tests.multiuser.cache_checks import assert_no_private_cache

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
    import zhihu_answer
    import zhihu_comments_fetch

    profile = tmp_path / "home" / ".easel-browser-profiles" / "zhihu" / "default"
    for module in (zhihu_comments_fetch, zhihu_answer):
        monkeypatch.setattr(module, "PROFILE_DIR", profile, raising=False)
        with pytest.raises(Exception):  # noqa: B017 - 只验证失败路径清理
            if module is zhihu_comments_fetch:
                module.fetch_comments("bad://invalid")
            else:
                module.publish_answer("bad://invalid", "正文", dry_run=True)
        lock = ProfileLock(profile)
        lock.acquire(0.5)
        lock.release()


def _publish_args(platform: str, keep_open: bool):
    from types import SimpleNamespace
    return SimpleNamespace(
        platform=platform, profile_base=None, media=None, title="t",
        desc="", tags="", cover="", keep_open=keep_open)


def _install_test_platform(monkeypatch, web_publisher, platform: str):
    monkeypatch.setitem(web_publisher.PLATFORMS, platform, {
        "name": "本地测试", "profile": "Task7TestProfile",
        "login_url": "data:text/html,<html><body>login</body></html>",
        "publish_url": "data:text/html,<html><body>publish</body></html>",
        "login_check": "#missing", "logged_out_selector": "#missing",
        "steps": [],
    })


@pytest.mark.parametrize("keep_open", [False, True])
def test_run_browser_precheck_failure_releases_lock_with_keep_open(
        tmp_path, monkeypatch, keep_open):
    import web_publisher

    profile = tmp_path / "home" / ".easel-browser-profiles" / "test" / "default"
    monkeypatch.setattr(web_publisher, "_profile_dir",
                        lambda platform, base: profile)
    _install_test_platform(monkeypatch, web_publisher, "task7-precheck")

    def fail(*args, **kwargs):
        raise RuntimeError("precheck failed")

    monkeypatch.setattr(web_publisher, "_resolve_steps", fail)

    with pytest.raises(RuntimeError):
        web_publisher._run_browser(
            _publish_args("task7-precheck", keep_open), headed=False,
            do_publish=True)
    lock = ProfileLock(profile)
    lock.acquire(1.0)
    lock.release()


@pytest.mark.parametrize("keep_open", [False, True])
def test_run_browser_systemexit_precheck_releases_lock_with_keep_open(
        tmp_path, monkeypatch, keep_open):
    import web_publisher

    profile = tmp_path / "home" / ".easel-browser-profiles" / "test" / "default"
    monkeypatch.setattr(web_publisher, "_profile_dir",
                        lambda platform, base: profile)
    _install_test_platform(monkeypatch, web_publisher, "task7-exit")
    monkeypatch.setattr(web_publisher, "_settle_login",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(web_publisher, "_is_logged_in",
                        lambda page, cfg: False)

    with pytest.raises(SystemExit):
        web_publisher._run_browser(
            _publish_args("task7-exit", keep_open), headed=False,
            do_publish=True)
    lock = ProfileLock(profile)
    lock.acquire(1.0)
    lock.release()


@pytest.mark.parametrize("keep_open", [False, True])
def test_run_browser_success_releases_lock_with_keep_open(
        tmp_path, monkeypatch, keep_open):
    import web_publisher

    profile = tmp_path / "home" / ".easel-browser-profiles" / "test" / "default"
    monkeypatch.setattr(web_publisher, "_profile_dir",
                        lambda platform, base: profile)
    _install_test_platform(monkeypatch, web_publisher, "task7-ok")
    monkeypatch.setattr(web_publisher, "_settle_login",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(web_publisher, "_is_logged_in",
                        lambda page, cfg: True)

    assert web_publisher._run_browser(
        _publish_args("task7-ok", keep_open), headed=False, do_publish=True) == 0
    lock = ProfileLock(profile)
    lock.acquire(1.0)
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
        with (pytest.raises(RuntimeError),
              manager.persistent_profile("xiaohongshu", "default",
                                         timeout=10) as profile):
            context = p.chromium.launch_persistent_context(
                str(profile), headless=True)
            try:
                raise RuntimeError("boom")
            finally:
                context.close()
        with manager.persistent_profile("xiaohongshu", "default", timeout=2):
            pass


def test_cache_values_with_foreign_identity_are_detected():
    """真实 Chromium：缓存值（而非仅键名）含 A 数据时必须失败。"""
    with sync_playwright() as p:
        browser = _launch_or_skip(p)
        try:
            context = browser.new_context()
            page = context.new_page()
            context.route(
                "https://cache.test/",
                lambda route: route.fulfill(
                    status=200, content_type="text/html",
                    body="<html><body>cache</body></html>"))
            page.goto("https://cache.test/", wait_until="domcontentloaded")
            page.evaluate(
                "v => { localStorage.setItem('shared-cache', v);"
                " sessionStorage.setItem('shared-cache', v); }",
                "user-A ws-A A private")
            with pytest.raises(AssertionError):
                assert_no_private_cache(
                    page, ["user-A", "ws-A", "A private"])
            page.evaluate(
                "() => { localStorage.clear(); sessionStorage.clear(); }")
            page.evaluate(
                "v => localStorage.setItem('mine', v)",
                "user-B ws-B")
            assert_no_private_cache(
                page, ["user-A", "ws-A", "A private"])
        finally:
            context.close()
            browser.close()
