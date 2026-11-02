"""Task 3 R1 主审：题卡状态未回时断流重连不能丢失待确认问题。

真实构建、随机 localhost 假 API、独立 Chromium；不接用户实例。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

PATH = Path(__file__).with_name("probe_browser.py")
spec = importlib.util.spec_from_file_location("task3_gate_parent", PATH)
parent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parent)


async def main():
    from playwright.async_api import async_playwright
    server = parent.fixture.Server("control").start()
    arrived, release = asyncio.Event(), asyncio.Event()
    calls = 0
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = None
            try:
                context, page = await parent.open_second(browser, server)

                async def question_status(route):
                    nonlocal calls
                    calls += 1
                    body = route.request.post_data_json
                    arrived.set()
                    await release.wait()
                    try:
                        await route.fulfill(status=200,
                                            content_type="application/json",
                                            body=json.dumps({"questions": {
                                                key: {"status": "pending"}
                                                for key in body["questionIds"]}}))
                    except Exception:
                        # 重连取消旧 HTTP 请求后，浏览器可以不再接收该旧回应。
                        pass

                await page.route("**/api/chat/question/status", question_status)
                await page.locator(".chat-input").fill("断线问题")
                await page.locator(".send-btn").click()
                await asyncio.wait_for(arrived.wait(), 10)
                reconnect = page.get_by_role("button", name="重连（只回放）")
                await reconnect.wait_for(timeout=10000)
                before = await page.locator(".question-card").count()
                await reconnect.click()
                await parent.fixture.wait_for(
                    lambda: len(server.state.stream_afters) >= 2,
                    10, "没有重连请求")
                release.set()
                await parent.flush(page)
                await parent.flush(page)
                print("QUESTION_GATE=" + json.dumps({
                    "cards_before_reconnect": before,
                    "cards_after_reconnect": await page.locator(".question-card").count(),
                    "after_values": server.state.stream_afters,
                    "question_status_calls": calls,
                    "backend_pending": any(run["asked"] and not run["terminal"]
                                           for run in server.state.runs.values()),
                    "chat_posts": server.state.post_chat_count,
                }))
            finally:
                release.set()
                if context:
                    await context.close()
                await browser.close()
    finally:
        server.abandon_runs("A")
        parent.cleanup(server)
    print("FIXTURE_CLEANUP=ok")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
