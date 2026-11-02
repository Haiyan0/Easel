"""真实构建反例：非空未知问题状态不能被当作已回答而移除题卡。"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

path = Path(__file__).with_name("probe_browser.py")
spec = importlib.util.spec_from_file_location("task3_unknown_parent", path)
parent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parent)


async def probe(browser, status):
    server = parent.fixture.Server("control").start()
    context = None
    try:
        context, page = await parent.open_second(browser, server)

        async def response(route):
            ids = route.request.post_data_json["questionIds"]
            values = {} if status is None else {
                key: {"status": status} for key in ids}
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps({"questions": values}))

        await page.route("**/api/chat/question/status", response)
        await page.locator(".chat-input").fill("需要问题的问题")
        async with page.expect_response(lambda r:
                                        "/api/chat/question/status" in r.url):
            await page.locator(".send-btn").click()
        await parent.flush(page)
        card = page.locator(".question-card")
        count = await card.count()
        pending = await card.first.get_attribute("data-pending") if count else None
        print("QUESTION_STATUS=" + json.dumps({
            "status": status, "cards": count, "pending": pending,
            "backend_pending": any(r["asked"] and not r["terminal"]
                                   for r in server.state.runs.values()),
        }))
    finally:
        if context:
            await context.close()
        server.abandon_runs("A")
        parent.cleanup(server)


async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            for status in (None, "unknown", "checking", "pending"):
                await probe(browser, status)
        finally:
            await browser.close()
    print("FIXTURE_CLEANUP=ok")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
