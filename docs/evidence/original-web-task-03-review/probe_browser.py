"""Task 3 主审：真实构建 + 隔离假 API 的三条交互反例。

不读用户 Cookie/Key，不连接 Docker/供应商；所有浏览器 context 与服务器关闭。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[3]
WORKTREE = ROOT / ".claude/worktrees/original-web-task1-diagnostics"
PATH = WORKTREE / "tests/frontend/browser_original_web_task03.py"
spec = importlib.util.spec_from_file_location("task3_audit_fixture", PATH)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


async def flush(page):
    await page.evaluate("() => new Promise(resolve => requestAnimationFrame("
                        "() => requestAnimationFrame(resolve)))")


async def open_second(browser, server):
    context = await browser.new_context()
    page = await context.new_page()
    await fixture.login(page, server.origin, "invite-a")
    async with page.expect_response(lambda response:
                                   "/api/sessions/s-a2/messages?" in response.url):
        await fixture.select_session(page, "A 第二会话")
    await flush(page)
    return context, page


def cleanup(server):
    for run in list(server.state.runs.values()):
        with run["condition"]:
            run["stopped"] = True
            run["release"].set()
            run["condition"].notify_all()
    server.release_admission()
    server.release_sessions()
    server.release_a_messages()
    server.release_a_settings()
    server.stop()


async def submit_unauthorized(browser):
    server = fixture.Server("control").start()
    context = None
    try:
        context, page = await open_second(browser, server)
        await page.route("**/api/chat", lambda route: route.fulfill(
            status=401, content_type="application/json",
            body=json.dumps({"code": "unauthorized"})))
        await page.locator(".chat-input").fill("fake revoked session")
        async with page.expect_response(lambda response:
                                        urlparse(response.url).path == "/api/chat"):
            await page.locator(".send-btn").click()
        await flush(page)
        print("SUBMIT_401=" + json.dumps({
            "sidebar_visible": await page.locator(".sidebar").count() > 0,
            "login_visible": await page.locator("#invite").count() > 0,
            "pending_visible": await page.locator(".msg-pending").count() > 0,
        }))
    finally:
        if context:
            await context.close()
        cleanup(server)


async def answer_unavailable(browser):
    server = fixture.Server("control").start()
    context = None
    try:
        context, page = await open_second(browser, server)
        await page.route("**/api/chat/question/answer", lambda route:
                         route.fulfill(status=503,
                                       content_type="application/json",
                                       body=json.dumps({"code": "unavailable"})))
        await page.locator(".chat-input").fill("需要问题的问题")
        await page.locator(".send-btn").click()
        await page.get_by_role("button", name="允许本次", exact=True).click()
        async with page.expect_response(lambda response:
                                        "/api/chat/question/answer" in response.url):
            await page.get_by_role("button", name="提交", exact=True).click()
        await flush(page)
        print("ANSWER_503=" + json.dumps({
            "question_cards": await page.locator(".question-card").count(),
            "backend_answered": any(r["answered"] for r in server.state.runs.values()),
            "backend_waiting": any(r["asked"] and not r["terminal"]
                                   for r in server.state.runs.values()),
        }))
    finally:
        if context:
            await context.close()
        cleanup(server)


async def thinking_replay(browser):
    server = fixture.Server("control").start()
    context = None
    after_values = []
    try:
        context, page = await open_second(browser, server)

        async def stream(route):
            after = int(parse_qs(urlparse(route.request.url).query)
                        .get("after", ["0"])[0])
            after_values.append(after)
            frames = []
            if after < 1:
                frames.append('id: 1\nevent: state\ndata: {"state":"running"}\n\n')
            if after < 2:
                frames.append('id: 2\nevent: thinking\ndata: "REVIEW_THOUGHT"\n\n')
            await route.fulfill(status=200, content_type="text/event-stream",
                                body="".join(frames))

        await page.route("**/api/chat/jobs/*/stream?*", stream)
        await page.locator(".chat-input").fill("停止本轮")
        await page.locator(".send-btn").click()
        reconnect = page.get_by_role("button", name="重连（只回放）", exact=True)
        await reconnect.wait_for()
        before = (await page.locator(".chat-thread").text_content()).count("REVIEW_THOUGHT")
        async with page.expect_response(lambda response:
                                        "/stream?after=" in response.url):
            await reconnect.click()
        await flush(page)
        after = (await page.locator(".chat-thread").text_content()).count("REVIEW_THOUGHT")
        print("THINKING_REPLAY=" + json.dumps({
            "before_count": before, "after_count": after, "after_values": after_values,
        }))
    finally:
        if context:
            await context.close()
        cleanup(server)


async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            await submit_unauthorized(browser)
            await answer_unavailable(browser)
            await thinking_replay(browser)
            server = fixture.Server("control").start()
            cache = WORKTREE / ".pytest_cache"
            cache.mkdir(exist_ok=True)
            try:
                with tempfile.TemporaryDirectory(prefix="task3-admission-review-", dir=cache) as d:
                    fixture.EVIDENCE = Path(d)
                    results, failures = {}, []
                    await fixture.scenario_admission_ordering(browser, server, results, failures, [])
                    print("ISOLATED_ADMISSION=" + json.dumps({"results": results,
                                                            "failure_count": len(failures)}))
            finally:
                cleanup(server)
        finally:
            await browser.close()
    print("FIXTURE_CLEANUP=ok")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
