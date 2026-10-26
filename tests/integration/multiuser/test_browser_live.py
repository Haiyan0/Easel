"""Task 14：真实 Chromium 对正式控制面的 Cookie/缓存与预览 CSP 检查。

持久 profile 串行、只读技能目录和本机 sandbox 预览由
``test_browser_profiles_real.py`` 的独立门槛覆盖；本文件只验证双用户
浏览器上下文不共享登录态与身份缓存。
"""
from __future__ import annotations

import os
import uuid

import pytest

from tests.multiuser.cache_checks import assert_no_private_cache

pytestmark = pytest.mark.multiuser_integration

try:  # 缺少 Playwright 时在 live 门槛内明确失败，而不是 import error/skip
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - 本机通常已安装
    sync_playwright = None


def _add_session_cookie(context, user, base_url: str) -> None:
    context.add_cookies([{
        "name": "__Host-easel_session",
        "value": user.cookie_value,
        "url": base_url,
        "secure": True,
        "httpOnly": True,
        "sameSite": "Lax",
    }])


def _launch(playwright):
    try:
        return playwright.chromium.launch(headless=True)
    except Exception as error:  # noqa: BLE001 - 环境缺失必须失败
        pytest.fail(
            f"正式浏览器门槛无法启动 Chromium：{type(error).__name__}",
            pytrace=False)


def test_live_browser_contexts_do_not_share_login_or_cache(
        live_env, live_users):
    if sync_playwright is None:
        pytest.fail("缺少 Playwright，正式浏览器门槛失败", pytrace=False)
    a, b = live_users
    base = os.environ.get(
        "EASEL_TEST_BROWSER_URL", live_env.control_url).rstrip("/")
    with sync_playwright() as playwright:
        browser = _launch(playwright)
        try:
            context_a = browser.new_context()
            context_b = browser.new_context()
            try:
                _add_session_cookie(context_a, a, base)
                _add_session_cookie(context_b, b, base)
                page_a = context_a.new_page()
                page_b = context_b.new_page()
                page_a.goto(base, wait_until="domcontentloaded", timeout=30000)
                page_b.goto(base, wait_until="domcontentloaded", timeout=30000)

                me_a = page_a.evaluate(
                    "fetch('/api/auth/me',{credentials:'same-origin'})"
                    ".then(r=>r.json())")
                me_b = page_b.evaluate(
                    "fetch('/api/auth/me',{credentials:'same-origin'})"
                    ".then(r=>r.json())")
                assert me_a.get("userId") == a.user_id
                assert me_b.get("userId") == b.user_id
                assert me_a.get("workspaceId") == a.workspace_id
                assert me_b.get("workspaceId") == b.workspace_id

                cookies_a = {cookie["name"]: cookie["value"]
                             for cookie in context_a.cookies()}
                cookies_b = {cookie["name"]: cookie["value"]
                             for cookie in context_b.cookies()}
                assert cookies_a["__Host-easel_session"] == a.cookie_value
                assert cookies_b["__Host-easel_session"] == b.cookie_value
                assert a.cookie_value != b.cookie_value

                # A 的持久缓存写入测试 sentinel；B 的 localStorage/
                # sessionStorage 键和值都不得出现 A 的身份或数据。
                private_marker = "task14-private-" + uuid.uuid4().hex
                page_a.evaluate(
                    "v => { localStorage.setItem('task14-private', v);"
                    " sessionStorage.setItem('task14-private', v); }",
                    private_marker)
                assert_no_private_cache(
                    page_b, [a.user_id, a.workspace_id, private_marker],
                    label="B")
                assert_no_private_cache(
                    page_a, [b.user_id, b.workspace_id], label="A")
                visible_b = page_b.text_content("body") or ""
                for marker in (a.user_id, a.workspace_id, private_marker):
                    assert marker not in visible_b
            finally:
                context_a.close()
                context_b.close()
        finally:
            browser.close()


def test_live_preview_response_is_sandboxed(
        live_env, live_users, live_browser_env):
    if sync_playwright is None:
        pytest.fail("缺少 Playwright，正式预览门槛失败", pytrace=False)
    a, _b = live_users
    base = os.environ.get(
        "EASEL_TEST_BROWSER_URL", live_env.control_url).rstrip("/")
    with sync_playwright() as playwright:
        browser = _launch(playwright)
        try:
            context = browser.new_context()
            try:
                _add_session_cookie(context, a, base)
                response = context.request.get(
                    live_browser_env["preview_url"], timeout=30000)
                assert response.status == 200
                csp = response.headers.get(
                    "content-security-policy", "").lower()
                assert "sandbox" in csp
                assert "allow-scripts" not in csp
                assert "allow-same-origin" not in csp
            finally:
                context.close()
        finally:
            browser.close()
