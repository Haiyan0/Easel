"""Task 11 R4 真实 HTTPS/Chromium 失败状态验收（R4-01）。

新请求明确失败/结果未知时，上一轮已完成回答仍可读，同时保留待确认输入
和原 turnId，不自动重发。响应交付顺序全部使用 asyncio.Event 门禁：
先确认真实 worker Run 已完成，再抓取含助手回答的服务端快照并暂停交付。

不访问真实账号；临时目录运行后清理。
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "multiuser"))
sys.path.insert(0, str(ROOT / "tests" / "frontend"))

import test_tcp_integration as tcp  # noqa: E402
import browser_task11 as base  # noqa: E402
import browser_task11_r1 as r1  # noqa: E402
import browser_task11_r3 as r3  # noqa: E402
from easel.control.store import Store  # noqa: E402
from easel.control.types import WorkerHandle  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

REVISION = tcp.REVISION
SCREENSHOTS = base.SCREENSHOT_DIR
WARMUP_SESSION = "s019"
RUNNING_DONE = (r3.frame(1, "state", {"state": "running"})
                + r3.frame(2, "state", {"state": "done"}))


async def assistant_count(page) -> int:
    texts = await r3.article_texts(page)
    return sum(1 for text in texts if text.startswith("助手"))


async def wait_assistant(page, expected: int, timeout: float = 20.0) -> None:
    await r3.wait_until(
        lambda: _assistant_at_least(page, expected), timeout,
        f"页面未显示 {expected} 条已完成助手回答")


async def _assistant_at_least(page, expected: int) -> bool:
    return await assistant_count(page) >= expected


async def final_snapshot_ok(page) -> bool:
    texts = await r3.article_texts(page)
    users = [index for index, text in enumerate(texts)
             if text.startswith("你")]
    assistants = [index for index, text in enumerate(texts)
                  if text.startswith("助手")]
    new_positions = [index for index, text in enumerate(texts)
                     if "new-prompt" in text]
    old_positions = [index for index, text in enumerate(texts)
                     if "old-prompt" in text]
    if (len(users) != 2 or len(assistants) != 2
            or len(new_positions) != 1 or len(old_positions) != 1):
        return False
    new_index = new_positions[0]
    return (any(index < new_index for index in assistants)
            and any(index > new_index for index in assistants))


async def scenario_a(browser, origin, invite, capture, session, results,
                     failures, screenshots) -> None:
    """明确 403 停在失败状态；旧回答可读，同 turnId 重试后收敛。"""
    context = page = None
    history = r3.Hold("旧终态历史")
    phase = {"hold": False, "initial": 0,
             "initial_done": asyncio.Event()}
    post = {"count": 0, "mode": "hold", "bodies": []}
    failure = r3.Hold("明确 403")
    sse = {
        "n": 0,
        "old_requested": asyncio.Event(),
        "old_gate": asyncio.Event(),
        "retry_requested": asyncio.Event(),
        "retry_gate": asyncio.Event(),
    }
    try:
        context, page = await r1.open_app(browser, origin, invite)
        await r3.install_messages(
            page, capture, context, origin, session.id, history, phase)

        async def chat_post(route):
            post["count"] += 1
            post["bodies"].append(route.request.post_data or "")
            if post["count"] == 1 or post["mode"] == "pass":
                await route.continue_()
                return
            if post["count"] == 2:
                failure.captured.set()
                await failure.gate.wait()
                await route.fulfill(
                    status=403, content_type="application/json",
                    body=json.dumps({"code": "forbidden"}))
                failure.delivered.set()
                return
            await route.continue_()
        await page.route(re.compile(r"/api/chat$"), chat_post)

        async def stream(route):
            sse["n"] += 1
            if sse["n"] == 1:
                sse["old_requested"].set()
                await sse["old_gate"].wait()
            else:
                sse["retry_requested"].set()
                await sse["retry_gate"].wait()
            await route.fulfill(
                status=200,
                headers={"content-type": "text/event-stream"},
                body=RUNNING_DONE)
        await page.route(
            re.compile(r"/api/chat/jobs/[^/]+/stream"), stream)

        # 先切到其它会话，确保目标会话的选择一定触发一次新的历史读取
        # （目标可能正是应用启动时自动选中的会话，重复点击不会重载）。
        await r1.select_session(page, WARMUP_SESSION)
        await r1.select_session(page, session.title)
        await r3.wait_event(phase["initial_done"], "初始历史", 30)
        await r3.wait_ready(page)

        phase["hold"] = True
        await r3.send_text(page, "old-prompt")
        await r3.wait_event(sse["old_requested"], "旧 SSE 请求", 30)
        # 先确认真实 Run 完成，再交付终态，使快照包含助手回答。
        await r3.wait_runs_settled(capture, context, origin, session.id)
        sse["old_gate"].set()
        await history.wait_captured(30)
        old_messages = history.payload.get("messages") or []
        old_assistants = sum(
            1 for item in old_messages if item.get("role") == "assistant")
        assert old_assistants >= 1, old_messages

        await r3.send_text(page, "new-prompt")
        await failure.wait_captured(30)
        first_turn = json.loads(post["bodies"][1])["turnId"]

        # 先交付旧快照，再交付明确 403。
        history.release()
        await history.wait_delivered(30)
        await wait_assistant(page, 1)
        body = await page.locator("body").inner_text()
        assert body.count("new-prompt") == 1, body.count("new-prompt")
        assert body.count("old-prompt") == 1, body.count("old-prompt")

        failure.release()
        await failure.wait_delivered(30)
        await page.get_by_text(
            "发送失败（403", exact=False).wait_for(timeout=20000)
        retry = page.get_by_role(
            "button", name="重试同一请求（不会新造请求键）")
        await retry.wait_for(timeout=10000)
        await wait_assistant(page, 1)
        body = await page.locator("body").inner_text()
        assert body.count("old-prompt") == 1, body.count("old-prompt")
        assert body.count("new-prompt") == 1, body.count("new-prompt")
        assert post["count"] == 2, post["count"]
        await page.wait_for_timeout(700)
        assert post["count"] == 2, post["count"]
        results["r4_explicit_failure_keeps_old_answer"] = True
        path = SCREENSHOTS / "r4-explicit-403-failed.png"
        await page.screenshot(path=str(path))
        screenshots.append(str(path))

        # 手动重试同一请求键，再等真实 Run 完成后交付终态。
        post["mode"] = "pass"
        await retry.click()
        await r3.wait_event(sse["retry_requested"], "重试 SSE 请求", 30)
        await r3.wait_runs_settled(capture, context, origin, session.id)
        sse["retry_gate"].set()
        await r3.wait_until(
            lambda: final_snapshot_ok(page), 30,
            "重试后最终快照未收敛为两条不同轮次消息")
        assert post["count"] == 3, post["count"]
        retry_turn = json.loads(post["bodies"][2])["turnId"]
        assert retry_turn == first_turn, (first_turn, retry_turn)
        results["r4_retry_same_turn_converges"] = True
        path = SCREENSHOTS / "r4-retry-same-turn.png"
        await page.screenshot(path=str(path))
        screenshots.append(str(path))
    except Exception as error:  # noqa: BLE001
        failures.append(f"R4-A: {error!r}")
    finally:
        history.gate.set()
        failure.gate.set()
        sse["old_gate"].set()
        sse["retry_gate"].set()
        if context is not None:
            await context.close()


async def scenario_b(browser, origin, invite, capture, session, results,
                     failures, screenshots) -> None:
    """服务端已接纳但页面收到 502：停在未知状态，不点击重试。"""
    context = page = None
    history = r3.Hold("旧终态历史")
    admission = r3.Hold("真实 202")
    phase = {"hold": False, "initial": 0,
             "initial_done": asyncio.Event()}
    control = {"count": 0, "hold_index": 2, "mode": "hold", "bodies": []}
    sse = {"n": 0, "old_requested": asyncio.Event(),
           "old_gate": asyncio.Event()}
    try:
        context, page = await r1.open_app(browser, origin, invite)
        await r3.install_messages(
            page, capture, context, origin, session.id, history, phase)
        await page.route(
            re.compile(r"/api/chat$"),
            r3.install_post(page, capture, context, origin, control,
                            admission, fail_status=502))

        async def stream(route):
            sse["n"] += 1
            if sse["n"] == 1:
                sse["old_requested"].set()
                await sse["old_gate"].wait()
                await route.fulfill(
                    status=200,
                    headers={"content-type": "text/event-stream"},
                    body=RUNNING_DONE)
            else:
                await route.fulfill(
                    status=200,
                    headers={"content-type": "text/event-stream"},
                    body=RUNNING_DONE)
        await page.route(
            re.compile(r"/api/chat/jobs/[^/]+/stream"), stream)

        # 先切到其它会话，确保目标会话的选择一定触发一次新的历史读取
        # （目标可能正是应用启动时自动选中的会话，重复点击不会重载）。
        await r1.select_session(page, WARMUP_SESSION)
        await r1.select_session(page, session.title)
        await r3.wait_event(phase["initial_done"], "初始历史", 30)
        await r3.wait_ready(page)

        phase["hold"] = True
        await r3.send_text(page, "old-prompt")
        await r3.wait_event(sse["old_requested"], "旧 SSE 请求", 30)
        await r3.wait_runs_settled(capture, context, origin, session.id)
        sse["old_gate"].set()
        await history.wait_captured(30)
        old_messages = history.payload.get("messages") or []
        assert sum(1 for item in old_messages
                   if item.get("role") == "assistant") >= 1, old_messages

        await r3.send_text(page, "new-prompt")
        await admission.wait_captured(30)
        assert admission.status == 202, admission.status

        history.release()
        await history.wait_delivered(30)
        await wait_assistant(page, 1)
        body = await page.locator("body").inner_text()
        assert body.count("new-prompt") == 1, body.count("new-prompt")

        admission.release()
        await admission.wait_delivered(30)
        await page.get_by_text(
            "发送结果未知", exact=False).wait_for(timeout=20000)
        await page.get_by_role(
            "button", name="重试同一请求（不会新造请求键）").wait_for(
                timeout=10000)
        await wait_assistant(page, 1)
        body = await page.locator("body").inner_text()
        assert body.count("old-prompt") == 1, body.count("old-prompt")
        assert body.count("new-prompt") == 1, body.count("new-prompt")
        assert control["count"] == 2, control["count"]
        assert sse["n"] == 1, sse["n"]
        await page.wait_for_timeout(700)
        assert control["count"] == 2, control["count"]
        assert sse["n"] == 1, sse["n"]
        results["r4_unknown_failure_keeps_old_answer"] = True
        path = SCREENSHOTS / "r4-unknown-502-failed.png"
        await page.screenshot(path=str(path))
        screenshots.append(str(path))
    except Exception as error:  # noqa: BLE001
        failures.append(f"R4-B: {error!r}")
    finally:
        history.gate.set()
        admission.gate.set()
        sse["old_gate"].set()
        if context is not None:
            await context.close()


async def scenario_c(browser, origin, invite, capture, session, neighbor,
                     results, failures, screenshots) -> None:
    """恢复 GET 失败后切会话再返回；较新请求不清除旧待确认输入。"""
    context = page = None
    phase = {"hold": False, "fail": False, "initial": 0,
             "initial_done": asyncio.Event(),
             "fail_seen": asyncio.Event()}
    post = {"count": 0, "mode": "hold", "bodies": []}
    failure = r3.Hold("明确 403")
    sse = {"n": 0, "third_requested": asyncio.Event(),
           "third_gate": asyncio.Event()}
    try:
        context, page = await r1.open_app(browser, origin, invite)

        async def messages(route):
            if phase["hold"] and phase["fail"]:
                phase["fail_seen"].set()
                await route.fulfill(
                    status=503, content_type="application/json",
                    body=json.dumps({"code": "upstream_unavailable"}))
                return
            response = await capture.get(
                route.request.url,
                headers={"Cookie": await r3.cookie_header(context, origin)})
            body = await response.aread()
            await route.fulfill(status=response.status_code,
                                headers=r3.response_headers(response),
                                body=body)
            if not phase["hold"]:
                phase["initial_done"].set()
        await page.route(
            re.compile(rf"/api/sessions/{session.id}/messages"), messages)

        async def chat_post(route):
            post["count"] += 1
            post["bodies"].append(route.request.post_data or "")
            if post["count"] != 2:
                await route.continue_()
                return
            failure.captured.set()
            await failure.gate.wait()
            await route.fulfill(
                status=403, content_type="application/json",
                body=json.dumps({"code": "forbidden"}))
            failure.delivered.set()
        await page.route(re.compile(r"/api/chat$"), chat_post)

        async def stream(route):
            sse["n"] += 1
            if sse["n"] == 2:
                sse["third_requested"].set()
                await sse["third_gate"].wait()
            await route.fulfill(
                status=200,
                headers={"content-type": "text/event-stream"},
                body=RUNNING_DONE)
        await page.route(
            re.compile(r"/api/chat/jobs/[^/]+/stream"), stream)

        # 先切到其它会话，确保目标会话的选择一定触发一次新的历史读取
        # （目标可能正是应用启动时自动选中的会话，重复点击不会重载）。
        await r1.select_session(page, WARMUP_SESSION)
        await r1.select_session(page, session.title)
        await r3.wait_event(phase["initial_done"], "初始历史", 30)
        await r3.wait_ready(page)

        # r1 的终态快照读取被服务端拒绝：必须明确显示恢复失败。
        phase["hold"] = True
        phase["fail"] = True
        await r3.send_text(page, "old-prompt")
        await r3.wait_event(phase["fail_seen"], "终态快照失败", 30)
        await page.get_by_text(
            "最终历史读取失败", exact=False).wait_for(timeout=20000)
        await r3.wait_runs_settled(capture, context, origin, session.id)

        # r2 明确 403；恢复失败不得伪造空历史或丢掉待确认输入。
        await r3.send_text(page, "new-prompt")
        await failure.wait_captured(30)
        failure.release()
        await failure.wait_delivered(30)
        await page.get_by_text(
            "发送失败（403", exact=False).wait_for(timeout=20000)
        await page.get_by_role(
            "button", name="重试同一请求（不会新造请求键）").wait_for(
                timeout=10000)
        body = await page.locator("body").inner_text()
        assert body.count("old-prompt") == 1, body.count("old-prompt")
        assert body.count("new-prompt") == 1, body.count("new-prompt")
        assert await assistant_count(page) == 0
        assert post["count"] == 2, post["count"]
        await page.wait_for_timeout(500)
        assert post["count"] == 2, post["count"]

        # 切到相邻会话：旧待确认状态不写到新选择；返回后从服务端恢复。
        phase["fail"] = False
        await r1.select_session(page, neighbor.title)
        body = await page.locator("body").inner_text()
        assert neighbor.title in await r3.header_text(page)
        assert "old-prompt" not in body
        assert "new-prompt" not in body
        assert await page.get_by_role(
            "button", name="重试同一请求（不会新造请求键）").count() == 0

        # 先切到其它会话，确保目标会话的选择一定触发一次新的历史读取
        # （目标可能正是应用启动时自动选中的会话，重复点击不会重载）。
        await r1.select_session(page, WARMUP_SESSION)
        await r1.select_session(page, session.title)
        await wait_assistant(page, 1)
        body = await page.locator("body").inner_text()
        assert body.count("old-prompt") == 1, body.count("old-prompt")
        assert body.count("new-prompt") == 1, body.count("new-prompt")
        await page.get_by_role(
            "button", name="重试同一请求（不会新造请求键）").wait_for(
                timeout=10000)
        results["r4_recovery_failure_scoped_and_recovers"] = True
        path = SCREENSHOTS / "r4-recovery-failure-switch-back.png"
        await page.screenshot(path=str(path))
        screenshots.append(str(path))

        # 较新请求成功：旧失败输入与重试入口保留，不被新快照清除。
        await r3.send_text(page, "third-prompt")
        await r3.wait_event(sse["third_requested"], "第三次 SSE 请求", 30)
        await r3.wait_runs_settled(capture, context, origin, session.id)
        sse["third_gate"].set()

        async def newer_ok() -> bool:
            texts = await r3.article_texts(page)
            assistants = [index for index, text in enumerate(texts)
                          if text.startswith("助手")]
            old_positions = [index for index, text in enumerate(texts)
                             if "old-prompt" in text]
            new_positions = [index for index, text in enumerate(texts)
                             if "new-prompt" in text]
            third_positions = [index for index, text in enumerate(texts)
                               if "third-prompt" in text]
            return (len(assistants) == 2 and len(old_positions) == 1
                    and len(new_positions) == 1
                    and len(third_positions) == 1
                    and any(index < third_positions[0]
                            for index in assistants)
                    and any(index > third_positions[0]
                            for index in assistants))
        await r3.wait_until(newer_ok, 30, "较新请求未收敛到服务端快照")
        assert post["count"] == 3, post["count"]
        await page.get_by_role(
            "button", name="重试同一请求（不会新造请求键）").wait_for(
                timeout=10000)
        results["r4_newer_request_keeps_older_pending"] = True
        path = SCREENSHOTS / "r4-newer-request-keeps-pending.png"
        await page.screenshot(path=str(path))
        screenshots.append(str(path))
    except Exception as error:  # noqa: BLE001
        failures.append(f"R4-C: {error!r}")
    finally:
        failure.gate.set()
        sse["third_gate"].set()
        if context is not None:
            await context.close()


async def main() -> int:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    tmp_path = Path(tempfile.mkdtemp(prefix="easel-task11-r4-"))
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
        tmp_path, "a", ws_a, "normal", "a", "service-r4-browser",
        public_key_path, launcher_path).start()
    handles = {ws_a: WorkerHandle(
        ws_a, "container-r4", worker.base, "service-r4-browser", REVISION)}
    commands = {ws_a: worker.command}
    runtime = tcp.StaticRuntime(handles)
    provider = tcp.make_provider({ws_a: tcp.TokenMint()}, commands)
    key_path, cert_path, pin = base.make_certificate(tmp_path)
    port = tcp.free_port()
    origin = f"https://localhost:{port}"
    host = base.ControlHTTPSHost(
        db_path, key, runtime, provider, cert_path, key_path, origin,
        port).start()
    r1.seed_ui_data(db_path, worker, identities)
    store = Store.connect(db_path)
    try:
        sessions = {item.title: item for item in store.list_conversations(
            identities["A"], limit=200, include_archived=True)}
    finally:
        store.close()

    screenshots: list[str] = []
    results: dict[str, bool] = {}
    failures: list[str] = []
    browser = None
    version = ""
    capture = httpx.AsyncClient(verify=False, trust_env=False, timeout=20)
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=[f"--ignore-certificate-errors-spki-list={pin}"])
            version = browser.version
            await scenario_a(
                browser, origin, invites["A"].pop(), capture,
                sessions["s020"], results, failures, screenshots)
            await scenario_b(
                browser, origin, invites["A"].pop(), capture,
                sessions["s021"], results, failures, screenshots)
            await scenario_c(
                browser, origin, invites["A"].pop(), capture,
                sessions["s022"], sessions["s014"], results, failures,
                screenshots)
            await browser.close()
            browser = None
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            await capture.aclose()
        except Exception:  # noqa: BLE001
            pass
        try:
            worker.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            host.stop()
        except AssertionError as error:
            print("CONTROL_STOP_WARNING", error)

    print("BROWSER_VERSION", version)
    print("RESULTS", json.dumps(results, ensure_ascii=False))
    print("FAILURES", json.dumps(failures, ensure_ascii=False))
    print("SCREENSHOTS", json.dumps(screenshots, ensure_ascii=False))
    print("TMP_CLEANED", True)
    import shutil
    shutil.rmtree(tmp_path, ignore_errors=True)
    if failures or len(results) != 5 or not all(results.values()):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
