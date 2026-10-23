"""Task 11 R1 真实 HTTPS/Chromium 竞态、恢复、分页与归档验收。

独立运行；隔离临时 DB/worker/证书/浏览器上下文，不访问真实账号。
运行：python tests/frontend/browser_task11_r1.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "multiuser"))
sys.path.insert(0, str(ROOT / "tests" / "frontend"))

import test_tcp_integration as tcp  # noqa: E402
import browser_task11 as base  # noqa: E402
from easel.control.auth import AuthService  # noqa: E402
from easel.control.store import Store  # noqa: E402
from easel.control.types import WorkerHandle  # noqa: E402
from easel.worker.sessions import WorkspaceSessionStore  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

REVISION = tcp.REVISION
SCREENSHOTS = base.SCREENSHOT_DIR


def make_invites(db_path: Path, count: int = 12):
    store = Store.connect(db_path)
    try:
        admin_row = store._connection.execute(
            "SELECT id FROM users WHERE role = 'admin' LIMIT 1").fetchone()
        admin = store.get_identity(admin_row["id"])
        auth = AuthService(store)
        identities = {}
        for name in ("A", "B"):
            row = store._connection.execute(
                "SELECT users.id AS user_id, workspaces.id AS workspace_id FROM users JOIN workspaces"
                " ON workspaces.owner_user_id = users.id"
                " WHERE users.display_name = ?", (name,)).fetchone()
            identities[name] = store.get_identity(row["user_id"])
        invites = {
            name: [auth.create_invite(admin, identities[name].user_id)
                   for _ in range(count)]
            for name in ("A", "B")
        }
        return identities, invites
    finally:
        store.close()


def seed_ui_data(db_path: Path, worker, identities):
    """101 个会话（含 big）并把 big 的 2500 条消息写入 worker 私有快照。"""
    member = identities["A"]
    store = Store.connect(db_path)
    try:
        for index in range(100):
            store.create_conversation(member, f"s{index:03d}")
        big = store.create_conversation(member, "big")
        sessions = store.list_conversations(
            member, limit=200, include_archived=True)
        oldest = sessions[100]
        sessions_store = WorkspaceSessionStore(
            worker.paths["state"], member.workspace_id)
        sessions_store.bind(
            big.id, "acp", "native-big", 1,
            cwd=str(worker.paths["workspace"]))
        path = sessions_store.messages_path(big.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as stream:
            for index in range(2500):
                stream.write(json.dumps({
                    "format": "easel.session.message",
                    "version": 1,
                    "workspace_id": member.workspace_id,
                    "session_id": big.id,
                    "message": {"role": "user", "content": f"m{index}"},
                }, ensure_ascii=False) + "\n")
        return big, oldest
    finally:
        store.close()


async def select_session(page, title: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        button = page.get_by_role("button", name=title, exact=True)
        if await button.count():
            await button.first.click()
            await page.wait_for_timeout(150)
            return
        more = page.get_by_role("button", name="加载更多会话")
        if await more.count():
            await more.click()
            await page.wait_for_timeout(150)
            continue
        await page.wait_for_timeout(150)
    raise AssertionError(f"找不到会话：{title}")


async def open_app(browser, origin: str, invite: str):
    context = await browser.new_context(
        base_url=origin, ignore_https_errors=False)
    page = await context.new_page()
    await page.goto("/", wait_until="domcontentloaded")
    await base.login(page, invite)
    return context, page


async def main() -> int:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    tmp_path = Path(tempfile.mkdtemp(prefix="easel-task11-r1-"))
    db_path = tmp_path / "control.sqlite3"
    tcp.seed_control(db_path)
    identities, invites = make_invites(db_path)

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
        tmp_path, "a", ws_a, "normal", "a", "service-r1-browser",
        public_key_path, launcher_path).start()
    runtime = tcp.StaticRuntime({
        ws_a: WorkerHandle(ws_a, "container-r1", worker.base,
                           "service-r1-browser", REVISION),
    })
    provider = tcp.make_provider(
        {ws_a: tcp.TokenMint()}, {ws_a: worker.command})
    key_path, cert_path, pin = base.make_certificate(tmp_path)
    port = tcp.free_port()
    origin = f"https://localhost:{port}"
    host = base.ControlHTTPSHost(
        db_path, key, runtime, provider, cert_path, key_path, origin,
        port).start()
    big, oldest = seed_ui_data(db_path, worker, identities)
    screenshots: list[str] = []
    results: dict[str, bool] = {}

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=[f"--ignore-certificate-errors-spki-list={pin}"])
            version = browser.version

            # S1: 会话分页 + 2500 条消息最新窗口可达
            context, page = await open_app(
                browser, origin, invites["A"].pop())
            requests: list[str] = []
            page.on("request", lambda request: requests.append(request.url))
            await page.get_by_role(
                "button", name="加载更多会话").wait_for(timeout=15000)
            assert await page.get_by_role(
                "button", name=oldest.title, exact=True).count() == 0
            await page.get_by_role(
                "button", name="加载更多会话").click()
            await page.get_by_role(
                "button", name=oldest.title, exact=True).wait_for(
                    timeout=10000)
            results["sessions_load_more"] = True

            await select_session(page, "big")
            await page.locator("textarea").wait_for(timeout=15000)
            await page.locator("textarea").fill("bigtask")
            await page.get_by_role("button", name="发送").click()
            await base.wait_assistant(page, "你好")
            await page.get_by_text("m2499", exact=False).wait_for(
                timeout=30000)
            for _ in range(8):
                more = page.get_by_role("button", name="加载更早历史")
                if not await more.count():
                    break
                await more.click()
                await page.wait_for_timeout(250)
            await page.get_by_text("m0", exact=False).wait_for(timeout=30000)
            results["large_messages_reachable"] = True
            assert any("/runs?active=true" in url for url in requests)
            results["active_run_filter_used"] = True
            path = SCREENSHOTS / "r1-01-pagination.png"
            await page.screenshot(path=str(path))
            screenshots.append(str(path))

            # S2: 历史晚到不得覆盖已切换的 B 会话
            await select_session(page, "s000")
            await page.wait_for_timeout(300)

            async def hold_big_history(route):
                await _delay_continue(route, 2.5)

            await page.route(
                f"**/api/sessions/{big.id}/messages*", hold_big_history)
            await select_session(page, "big")
            await page.wait_for_timeout(100)
            await select_session(page, "s000")
            await page.wait_for_timeout(3200)
            body = await page.locator("body").inner_text()
            assert "m2499" not in body
            assert "s000" in await page.locator("header").inner_text()
            results["stale_history_does_not_commit"] = True
            await page.unroute(f"**/api/sessions/{big.id}/messages*")
            await context.close()

            # S3: admission 晚到不得抢占另一会话
            context, page = await open_app(
                browser, origin, invites["A"].pop())
            await select_session(page, "s001")
            chat_requests = []
            page.on("request", lambda request: chat_requests.append(
                request.url) if "/api/chat" in request.url else None)

            async def hold_chat(route):
                await asyncio.sleep(2.5)
                await route.continue_()

            await page.route("**/api/chat", hold_chat)
            await page.locator("textarea").fill("延迟admission")
            await page.get_by_role("button", name="发送").click()
            await page.wait_for_timeout(100)
            await select_session(page, "s002")
            await page.wait_for_timeout(3200)
            body = await page.locator("body").inner_text()
            assert "延迟admission" not in body
            assert "s002" in await page.locator("header").inner_text()
            assert not any("/api/chat/jobs/" in url for url in chat_requests)
            results["late_admission_does_not_steal_ui"] = True
            await page.unroute("**/api/chat")
            await context.close()

            # S3b: 退出切账号后旧 admission 不重建缓存、不发新 SSE
            context, page = await open_app(
                browser, origin, invites["A"].pop())
            await select_session(page, "s005")
            late_requests: list[str] = []
            page.on("request", lambda request: late_requests.append(
                request.url) if "/api/chat" in request.url else None)

            async def hold_late(route):
                await asyncio.sleep(2.5)
                await route.continue_()

            await page.route("**/api/chat", hold_late)
            await page.locator("textarea").fill("切换账号")
            await page.get_by_role("button", name="发送").click()
            await page.wait_for_timeout(100)
            await page.get_by_role("button", name="退出登录").click()
            await page.wait_for_selector("#invite", timeout=15000)
            await base.login(page, invites["B"].pop())
            await page.wait_for_timeout(3200)
            body = await page.locator("body").inner_text()
            assert "切换账号" not in body
            assert not any("/api/chat/jobs/" in url for url in late_requests),                 late_requests
            keys = await page.evaluate("Object.keys(sessionStorage)")
            ws_a = identities["A"].workspace_id
            assert not any(ws_a in key for key in keys), keys
            results["late_admission_after_account_switch_ignored"] = True
            await page.unroute("**/api/chat")
            await context.close()

            # S4: 首次 SSE 失败后重连按钮可用
            context, page = await open_app(
                browser, origin, invites["A"].pop())
            await select_session(page, "s003")
            sse_calls = {"n": 0, "urls": []}

            async def fail_first(route):
                sse_calls["n"] += 1
                sse_calls["urls"].append(route.request.url)
                if sse_calls["n"] == 1:
                    await route.fulfill(status=503, body="unavailable")
                else:
                    await route.continue_()

            await page.route("**/api/chat/jobs/*/stream*", fail_first)
            await page.locator("textarea").fill("你好")
            await page.get_by_role("button", name="发送").click()
            reconnect = page.get_by_role("button", name="重连（只回放）")
            await reconnect.wait_for(timeout=15000)
            results["first_sse_failure_reconnect_visible"] = True
            await reconnect.click()
            await base.wait_assistant(page, "你好")
            assert sse_calls["n"] >= 2
            results["first_sse_reconnect_succeeds"] = True
            await page.unroute("**/api/chat/jobs/*/stream*")
            await context.close()

            # S5: 携带游标重连保留正文/题卡（合成 SSE；真实组件）
            context, page = await open_app(
                browser, origin, invites["A"].pop())
            await select_session(page, "s004")
            synthetic = {"n": 0, "after": []}
            question = {
                "id": "runq_synthetic",
                "questions": [{
                    "questionId": "permission", "header": "执行授权",
                    "question": "允许？",
                    "options": [{"label": "1. Allow", "value": "qopt_1"}],
                }],
            }
            first_body = (
                'id: 1\nevent: state\ndata: {"state":"running"}\n\n'
                'id: 2\nevent: token\ndata: "部分"\n\n'
                'id: 3\nevent: state\ndata: {"state":"awaiting_permission"}\n\n'
                'id: 4\nevent: question\ndata: '
                + json.dumps(json.dumps(question, ensure_ascii=False),
                             ensure_ascii=False) + "\n\n"
            )

            async def synthetic_stream(route):
                synthetic["n"] += 1
                if synthetic["n"] == 1:
                    await route.fulfill(
                        status=200,
                        headers={"content-type": "text/event-stream"},
                        body=first_body)
                else:
                    synthetic["after"].append(route.request.url)
                    await route.fulfill(
                        status=200,
                        headers={"content-type": "text/event-stream"},
                        body="id: 5\nevent: state\ndata: "
                             '{"state":"awaiting_permission"}\n\n')

            await page.route(
                "**/api/chat/question/status",
                lambda route: route.fulfill(
                    status=200, content_type="application/json",
                    body=json.dumps({"questions": {
                        "runq_synthetic": {"status": "pending"}}})))
            await page.route(
                "**/api/chat/jobs/*/stream*", synthetic_stream)
            await page.locator("textarea").fill("问题")
            await page.get_by_role("button", name="发送").click()
            await page.get_by_text("1. Allow", exact=False).wait_for(
                timeout=15000)
            await page.get_by_text("部分", exact=False).wait_for(
                timeout=5000)
            reconnect = page.get_by_role("button", name="重连（只回放）")
            await reconnect.wait_for(timeout=15000)
            await reconnect.click()
            await page.wait_for_timeout(800)
            assert synthetic["after"], synthetic
            assert "after=4" in synthetic["after"][0], synthetic["after"]
            await page.get_by_text("1. Allow", exact=False).wait_for(
                timeout=5000)
            body = await page.locator("body").inner_text()
            assert "部分部分" not in body
            results["reconnect_preserves_cursor_state"] = True
            path = SCREENSHOTS / "r1-05-reconnect-question.png"
            await page.screenshot(path=str(path))
            screenshots.append(str(path))
            await page.unroute("**/api/chat/jobs/*/stream*")
            await page.unroute("**/api/chat/question/status")
            await context.close()

            # S6: 归档会话历史可读、发送只读
            context, page = await open_app(
                browser, origin, invites["A"].pop())
            await select_session(page, "big")
            await page.get_by_text("m2499", exact=False).wait_for(
                timeout=30000)
            await page.get_by_role("button", name="归档").click()
            await page.wait_for_timeout(700)
            await select_session(page, "big（已归档）")
            await page.get_by_text("m2499", exact=False).wait_for(
                timeout=30000)
            assert "已归档 · 只读" in await page.locator("header").inner_text()
            assert await page.locator("textarea").is_disabled()
            results["archived_history_readable"] = True
            path = SCREENSHOTS / "r1-07-archived-readonly.png"
            await page.screenshot(path=str(path))
            screenshots.append(str(path))
            await context.close()

            # S7: 普通业务 401 卸载私人树（big 已在 S6 归档）
            context, page = await open_app(
                browser, origin, invites["A"].pop())
            await select_session(page, "big（已归档）")
            await page.get_by_text("m2499", exact=False).wait_for(
                timeout=30000)
            await page.route(
                "**/api/sessions",
                lambda route: route.fulfill(
                    status=401, content_type="application/json",
                    body=json.dumps({"code": "unauthorized"})))
            await page.get_by_role(
                "button", name="新建会话").click()
            await page.wait_for_selector("#invite", timeout=15000)
            body = await page.locator("body").inner_text()
            assert "m2499" not in body
            assert "你好" not in body
            results["business_401_unmounts_private_tree"] = True
            path = SCREENSHOTS / "r1-01-business-401.png"
            await page.screenshot(path=str(path))
            screenshots.append(str(path))
            await page.unroute("**/api/sessions")
            await context.close()

            # S8: 迟到的 auth/me 200/401 无权覆盖新 B
            async def late_identity(status: int) -> bool:
                context = await browser.new_context(
                    base_url=origin, ignore_https_errors=False)
                page1 = await context.new_page()
                page2 = await context.new_page()
                calls = {"n": 0}
                a_ctx = {
                    "userId": identities["A"].user_id,
                    "workspaceId": identities["A"].workspace_id,
                    "role": "member", "csrfToken": "csrf-synthetic",
                    "capabilities": ["workspace"],
                }

                async def late(route):
                    calls["n"] += 1
                    if calls["n"] == 1:
                        await asyncio.sleep(3)
                        if status == 200:
                            await route.fulfill(status=200, json=a_ctx)
                        else:
                            await route.fulfill(
                                status=status, content_type="application/json",
                                body=json.dumps({"code": "unauthorized"}))
                    else:
                        await route.continue_()

                await page1.route("**/api/auth/me", late)
                await page1.goto("/", wait_until="domcontentloaded")
                await page1.get_by_text(
                    "正在确认登录状态", exact=False).wait_for(timeout=10000)
                await page2.goto("/", wait_until="domcontentloaded")
                await base.login(page2, invites["B"].pop())
                await page1.get_by_role(
                    "button", name="新建会话").wait_for(timeout=15000)
                await page1.wait_for_timeout(3500)
                prefix_b = identities["B"].workspace_id[:8]
                prefix_a = identities["A"].workspace_id[:8]
                sidebar = await page1.locator("aside").inner_text()
                assert prefix_b in sidebar
                assert prefix_a not in sidebar
                assert await page1.locator("#invite").count() == 0
                await context.close()
                return True

            results["late_auth_200_ignored"] = await late_identity(200)
            results["late_auth_401_ignored"] = await late_identity(401)

            await browser.close()
    finally:
        host.stop()
        worker.stop()

    print("BROWSER_VERSION", version)
    print("ORIGIN", origin)
    print("RESULTS", json.dumps(results, ensure_ascii=False))
    print("SCREENSHOTS", json.dumps(screenshots, ensure_ascii=False))
    print("TMP_CLEANED", True)
    import shutil
    shutil.rmtree(tmp_path, ignore_errors=True)
    return 0


async def _delay_continue(route, seconds: float) -> None:
    await asyncio.sleep(seconds)
    await route.continue_()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
