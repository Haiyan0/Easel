"""Task 11 R2 真实 HTTPS/Chromium 边界验收（R2-01/02/04）。

复用 R1 的隔离数据（101 会话 + 2500 条 big 历史）与真实 worker/control。
不访问真实账号；临时目录运行后清理。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "multiuser"))
sys.path.insert(0, str(ROOT / "tests" / "frontend"))

import test_tcp_integration as tcp  # noqa: E402
import browser_task11 as base  # noqa: E402
import browser_task11_r1 as r1  # noqa: E402
from easel.control.store import Store  # noqa: E402
from easel.control.types import WorkerHandle  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

REVISION = tcp.REVISION
SCREENSHOTS = base.SCREENSHOT_DIR


def frame(event_id: int, event: str, data: object) -> str:
    return (f"id: {event_id}\nevent: {event}\n"
            f"data: {json.dumps(data, ensure_ascii=False)}\n\n")


def question_event(event_id: int, qid: str, label: str,
                   value: str) -> str:
    payload = {
        "id": qid,
        "questions": [{
            "questionId": "permission", "header": "执行授权",
            "question": "允许？",
            "options": [{"label": label, "value": value}],
        }],
    }
    return (f"id: {event_id}\nevent: question\ndata: "
            f"{json.dumps(json.dumps(payload, ensure_ascii=False), ensure_ascii=False)}"
            "\n\n")


async def main() -> int:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    tmp_path = Path(tempfile.mkdtemp(prefix="easel-task11-r2-"))
    db_path = tmp_path / "control.sqlite3"
    tcp.seed_control(db_path)
    identities, invites = r1.make_invites(db_path)

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey)
    key = Ed25519PrivateKey.generate()
    public_key_path = tmp_path / "worker-public.pem"
    public_key_path.write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo))
    launcher_path = tmp_path / "launcher.py"
    launcher_path.write_text(tcp.LAUNCHER_SOURCE, encoding="utf-8")
    ws_a = identities["A"].workspace_id
    worker = tcp.Worker(
        tmp_path, "a", ws_a, "normal", "a", "service-r2-browser",
        public_key_path, launcher_path).start()
    handles = {ws_a: WorkerHandle(
        ws_a, "container-r2", worker.base, "service-r2-browser", REVISION)}
    commands = {ws_a: worker.command}
    runtime = tcp.StaticRuntime(handles)
    provider = tcp.make_provider({ws_a: tcp.TokenMint()}, commands)
    key_path, cert_path, pin = base.make_certificate(tmp_path)
    port = tcp.free_port()
    origin = f"https://localhost:{port}"
    host = base.ControlHTTPSHost(
        db_path, key, runtime, provider, cert_path, key_path, origin,
        port).start()
    big, oldest = r1.seed_ui_data(db_path, worker, identities)
    store = Store.connect(db_path)
    try:
        by_title = {item.title: item for item in store.list_conversations(
            identities["A"], limit=200, include_archived=True)}
    finally:
        store.close()
    screenshots: list[str] = []
    results: dict[str, bool] = {}

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=[f"--ignore-certificate-errors-spki-list={pin}"])
            version = browser.version

            # R2-01a：旧终态历史晚于同会话新 admission/问题选择
            context, page = await r1.open_app(
                browser, origin, invites["A"].pop())
            await r1.select_session(page, "s010")
            await page.wait_for_timeout(300)
            sse_calls = {"n": 0}
            sse_first = (
                frame(1, "state", {"state": "running"})
                + frame(2, "state", {"state": "done"}))
            sse_second = (
                frame(1, "state", {"state": "running"})
                + frame(2, "token", "新进行中")
                + frame(3, "state", {"state": "awaiting_permission"})
                + question_event(4, "q2", "2. Allow", "qopt_2"))

            async def sse_two(route):
                sse_calls["n"] += 1
                body = sse_first if sse_calls["n"] == 1 else sse_second
                await route.fulfill(
                    status=200, headers={"content-type": "text/event-stream"},
                    body=body)

            await page.route(
                "**/api/chat/question/status",
                lambda route: route.fulfill(
                    status=200, content_type="application/json",
                    body=json.dumps({"questions": {
                        "q1": {"status": "pending"},
                        "q2": {"status": "pending"}}})))
            await page.route("**/api/chat/jobs/*/stream*", sse_two)
            held = {"n": 0}

            async def hold_history(route):
                held["n"] += 1
                await asyncio.sleep(4.0)
                await route.continue_()

            await page.route(
                f"**/api/sessions/{by_title['s010'].id}/messages*",
                hold_history)
            await page.locator("textarea").fill("old-prompt")
            await page.get_by_role("button", name="发送").click()
            await page.wait_for_timeout(700)  # 真 Run1 完成，合成 done 已到
            await page.locator("textarea").fill("new-prompt")
            await page.get_by_role("button", name="发送").click()
            await page.get_by_text("新进行中", exact=False).wait_for(
                timeout=15000)
            radio = page.locator("input[type=radio]").first
            await radio.wait_for(timeout=10000)
            await radio.check()
            await page.wait_for_timeout(4200)  # 旧历史响应晚到
            body = await page.locator("body").inner_text()
            assert "new-prompt" in body
            assert "新进行中" in body
            assert await radio.is_checked()
            assert await page.get_by_text("2. Allow", exact=False).count() >= 1
            results["old_terminal_does_not_overwrite_new_run"] = True
            path = SCREENSHOTS / "r2-01-old-terminal.png"
            await page.screenshot(path=str(path))
            screenshots.append(str(path))
            await page.unroute("**/api/chat/jobs/*/stream*")
            await page.unroute(
                f"**/api/sessions/{by_title['s010'].id}/messages*")
            await page.unroute("**/api/chat/question/status")
            await context.close()

            # R2-02：连续三次相同 503 仍可重连，随后恢复
            context, page = await r1.open_app(
                browser, origin, invites["A"].pop())
            await r1.select_session(page, "s013")
            fail_calls = {"n": 0, "urls": []}

            async def repeated_fail(route):
                fail_calls["n"] += 1
                fail_calls["urls"].append(route.request.url)
                if fail_calls["n"] <= 3:
                    await asyncio.sleep(0.6)
                    await route.fulfill(status=503, body="unavailable")
                else:
                    await route.continue_()

            await page.route("**/api/chat/jobs/*/stream*", repeated_fail)
            await page.locator("textarea").fill("你好")
            await page.get_by_role("button", name="发送").click()
            reconnect = page.get_by_role("button", name="重连（只回放）")
            for expected in (1, 2, 3):
                await reconnect.wait_for(timeout=15000)
                assert fail_calls["n"] == expected, fail_calls
                await reconnect.click()
                await page.wait_for_timeout(800)
            await base.wait_assistant(page, "你好")
            assert fail_calls["n"] >= 4
            results["repeated_sse_failures_reconnectable"] = True
            path = SCREENSHOTS / "r2-02-repeated-503.png"
            await page.screenshot(path=str(path))
            screenshots.append(str(path))
            await page.unroute("**/api/chat/jobs/*/stream*")
            await context.close()

            # R2-04：第二页当前会话在目录广播后保持
            context, page = await r1.open_app(
                browser, origin, invites["A"].pop())
            await r1.select_session(page, oldest.title)
            await page.wait_for_timeout(300)
            dir_calls = {"n": 0}

            async def question_stream(route):
                dir_calls["n"] += 1
                await route.fulfill(
                    status=200, headers={"content-type": "text/event-stream"},
                    body=(frame(1, "state", {"state": "running"})
                          + question_event(2, "q-dir", "2. Allow", "qopt_dir")))
            await page.route(
                "**/api/chat/question/status",
                lambda route: route.fulfill(
                    status=200, content_type="application/json",
                    body=json.dumps({"questions": {
                        "q-dir": {"status": "pending"}}})))
            await page.route(
                "**/api/chat/jobs/*/stream*", question_stream)
            await page.locator("textarea").fill("目录保持")
            await page.get_by_role("button", name="发送").click()
            await page.get_by_text("2. Allow", exact=False).wait_for(
                timeout=15000)
            calls_before = dir_calls["n"]
            dir_store = Store.connect(db_path)
            try:
                dir_store.create_conversation(identities["A"], "broadcast-new")
            finally:
                dir_store.close()
            page2 = await context.new_page()
            await page2.goto("/", wait_until="domcontentloaded")
            await page2.get_by_role(
                "button", name="新建会话").wait_for(timeout=15000)
            channel = (f"easel-control:{identities['A'].user_id}:"
                       f"{identities['A'].workspace_id}")
            await page2.evaluate(
                "name => { const ch = new BroadcastChannel(name);"
                " ch.postMessage({type: 'sessions-changed'}); ch.close(); }",
                channel)
            await page.wait_for_timeout(1200)
            header = await page.locator("header").inner_text()
            assert oldest.title in header, header
            assert await page.get_by_text("2. Allow",
                                          exact=False).count() >= 1
            assert dir_calls["n"] == calls_before
            active_key = (
                f"easel:control:v1:{identities['A'].user_id}:"
                f"{identities['A'].workspace_id}:active_session")
            stored = await page.evaluate(
                "key => localStorage.getItem(key)", active_key)
            assert stored == oldest.id, (stored, oldest.id)
            results["directory_refresh_keeps_deep_selection"] = True
            path = SCREENSHOTS / "r2-04-directory-refresh.png"
            await page.screenshot(path=str(path))
            screenshots.append(path.as_posix())

            # 另一标签删除当前会话：明确回落（真实 Store 软删除 + 目录广播）
            await page.wait_for_timeout(600)
            dir_store = Store.connect(db_path)
            try:
                dir_store.delete_conversation(identities["A"], oldest.id)
            finally:
                dir_store.close()
            await page2.evaluate(
                "name => { const ch = new BroadcastChannel(name);"
                " ch.postMessage({type: 'sessions-changed'}); ch.close(); }",
                channel)
            await page.wait_for_timeout(1500)
            header_after = await page.locator("header").inner_text()
            assert oldest.title not in header_after, header_after
            stored_after = await page.evaluate(
                "key => localStorage.getItem(key)", active_key)
            assert stored_after != oldest.id
            results["directory_refresh_deleted_falls_back"] = True
            await page.unroute("**/api/chat/jobs/*/stream*")
            await page.unroute("**/api/chat/question/status")
            await context.close()

            # R2-01b：旧连接 q1/status 晚于同 Run 新连接 q2
            worker.stop()
            hang = tcp.Worker(
                tmp_path, "hang", ws_a, "hang", "h", "service-r2-hang",
                public_key_path, launcher_path).start()
            handles[ws_a] = WorkerHandle(
                ws_a, "container-r2-hang", hang.base, "service-r2-hang",
                REVISION)
            commands[ws_a] = hang.command
            context, page = await r1.open_app(
                browser, origin, invites["A"].pop())
            await r1.select_session(page, "s011")
            await page.wait_for_timeout(300)
            conn = {"n": 0}

            async def q_stream(route):
                conn["n"] += 1
                qid = "q1" if conn["n"] == 1 else "q2"
                await route.fulfill(
                    status=200, headers={"content-type": "text/event-stream"},
                    body=(frame(1, "state", {"state": "running"})
                          + question_event(2, qid, "2. Allow",
                                           f"qopt_{qid}")))
            status_calls = {"n": 0}

            async def status_route(route):
                status_calls["n"] += 1
                if status_calls["n"] == 1:
                    await asyncio.sleep(3.5)
                await route.fulfill(
                    status=200, content_type="application/json",
                    body=json.dumps({"questions": {
                        "q1": {"status": "pending"},
                        "q2": {"status": "pending"}}}))
            await page.route(
                "**/api/chat/question/status", status_route)
            await page.route("**/api/chat/jobs/*/stream*", q_stream)
            await page.locator("textarea").fill("切换问题")
            await page.get_by_role("button", name="发送").click()
            await page.wait_for_timeout(500)
            await r1.select_session(page, "s012")
            await page.wait_for_timeout(200)
            await r1.select_session(page, "s011")
            await page.get_by_text("2. Allow", exact=False).wait_for(
                timeout=15000)
            radio = page.locator("input[type=radio]").first
            await radio.wait_for(timeout=10000)
            await radio.check()
            await page.wait_for_timeout(3800)  # 旧 q1 status 迟到
            body = await page.locator("body").inner_text()
            assert "2. Allow" in body
            assert await radio.is_checked()
            results["old_connection_question_cannot_replace_new"] = True
            path = SCREENSHOTS / "r2-01-old-question.png"
            await page.screenshot(path=str(path))
            screenshots.append(str(path))
            await page.unroute("**/api/chat/jobs/*/stream*")
            await page.unroute("**/api/chat/question/status")
            await context.close()
            hang.stop()
            await browser.close()
    finally:
        host.stop()
        try:
            worker.stop()
        except Exception:
            pass

    print("BROWSER_VERSION", version)
    print("ORIGIN", origin)
    print("RESULTS", json.dumps(results, ensure_ascii=False))
    print("SCREENSHOTS", json.dumps(screenshots, ensure_ascii=False))
    print("TMP_CLEANED", True)
    import shutil
    shutil.rmtree(tmp_path, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
