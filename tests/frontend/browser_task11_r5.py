"""Task 11 R5 真实 HTTPS/Chromium 初次历史交错验收（R5-01）。

旧初次 GET messages 响应在“新 Run 真实完成、终态快照已提交、LocalSend
已移除”之后才交付，不得清空/回退已提交的较新历史。

全程使用真实 control/worker/SSE 与 ACP 夹具，不使用合成终态；
响应交付顺序由 asyncio.Event 门禁控制。
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


def canonical(texts: list[str]) -> list[str]:
    """只保留服务端消息正文；排除流式助手文章与本地待确认卡片。"""
    return [text for text in texts
            if text.startswith("你\n") or text.startswith("助手\n")]


def has_local_card(texts: list[str]) -> bool:
    return any("发送中" in text or "待确认（未完成）" in text
               for text in texts)


async def wait_canonical(page, expected: list[str],
                         timeout: float = 60.0) -> None:
    await r3.wait_until(
        lambda: _canonical_is(page, expected), timeout,
        f"未等待到服务端消息快照：{expected}")


async def _canonical_is(page, expected: list[str]) -> bool:
    texts = await r3.article_texts(page)
    if has_local_card(texts):
        return False
    return canonical(texts) == expected


async def assert_stable(page, expected: list[str],
                        duration: float = 1.5) -> None:
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        texts = await r3.article_texts(page)
        assert not has_local_card(texts), texts
        assert canonical(texts) == expected, texts
        await asyncio.sleep(0.1)


async def install_initial_hold(page, capture, context, origin: str,
                               session_id: str, hold: r3.Hold,
                               state: dict) -> None:
    """只捕获该会话第一次 messages GET；后续终态/恢复请求直接放行。"""
    async def handler(route):
        if state["used"]:
            await route.continue_()
            return
        state["used"] = True
        response = await capture.get(
            route.request.url,
            headers={"Cookie": await r3.cookie_header(context, origin)})
        body = await response.aread()
        hold.payload = r3._json_or_empty(body)
        hold.status = response.status_code
        hold.captured.set()
        await hold.gate.wait()
        await route.fulfill(status=response.status_code,
                            headers=r3.response_headers(response),
                            body=body)
        hold.delivered.set()
    await page.route(
        re.compile(rf"/api/sessions/{session_id}/messages"), handler)


async def scenario_empty_initial(browser, origin, invite, capture, session,
                                 results, failures, screenshots) -> None:
    """空初次快照在新终态提交后晚到：不得把两条消息清成零条。"""
    context = page = None
    hold = r3.Hold("空初次快照")
    state = {"used": False}
    expected = ["你\nnew-prompt", "助手\n你好"]
    try:
        context, page = await r1.open_app(browser, origin, invite)
        await install_initial_hold(page, capture, context, origin,
                                   session.id, hold, state)
        await r1.select_session(page, WARMUP_SESSION)
        await r1.select_session(page, session.title)
        await hold.wait_captured(30)
        assert not hold.payload.get("messages"), hold.payload
        await r3.wait_ready(page)

        await r3.send_text(page, "new-prompt")
        # 终态快照提交且对应 LocalSend 已移除，才允许旧初次响应交付。
        await wait_canonical(page, expected)

        hold.release()
        await hold.wait_delivered(30)
        await assert_stable(page, expected)
        results["r5_old_empty_initial_does_not_clear_new_run"] = True
        path = SCREENSHOTS / "r5-empty-initial-after-terminal.png"
        await page.screenshot(path=str(path))
        screenshots.append(str(path))
    except Exception as error:  # noqa: BLE001
        failures.append(f"R5-空初次: {error!r}")
    finally:
        hold.gate.set()
        if context is not None:
            await context.close()


async def scenario_old_history_initial(browser, origin, invite, capture,
                                       session, results, failures,
                                       screenshots) -> None:
    """含旧历史的初次快照晚到：不得把已提交的四条回退为两条。"""
    context = page = None
    hold = r3.Hold("旧历史初次快照")
    state = {"used": False}
    old_pair = ["你\nold-prompt", "助手\n你好"]
    expected = [
        "你\nold-prompt", "助手\n你好",
        "你\nnew-prompt", "助手\n你好",
    ]
    try:
        context, page = await r1.open_app(browser, origin, invite)
        await r1.select_session(page, WARMUP_SESSION)
        await r1.select_session(page, session.title)
        await r3.wait_ready(page)
        await r3.send_text(page, "old-prompt")
        await wait_canonical(page, old_pair)

        # 切走再返回触发同一会话的初次历史读取，并暂停这份含旧历史的响应。
        await r1.select_session(page, WARMUP_SESSION)
        await install_initial_hold(page, capture, context, origin,
                                   session.id, hold, state)
        await r1.select_session(page, session.title)
        await hold.wait_captured(30)
        old_messages = hold.payload.get("messages") or []
        assert [item.get("role") for item in old_messages] == [
            "user", "assistant"], old_messages
        await r3.wait_ready(page)

        await r3.send_text(page, "new-prompt")
        await wait_canonical(page, expected)
        assert await page.get_by_role(
            "button", name="加载更早历史").count() == 0

        hold.release()
        await hold.wait_delivered(30)
        await assert_stable(page, expected)
        assert await page.get_by_role(
            "button", name="加载更早历史").count() == 0
        results["r5_old_history_initial_does_not_rollback_new_run"] = True
        path = SCREENSHOTS / "r5-old-history-initial-after-terminal.png"
        await page.screenshot(path=str(path))
        screenshots.append(str(path))
    except Exception as error:  # noqa: BLE001
        failures.append(f"R5-旧历史初次: {error!r}")
    finally:
        hold.gate.set()
        if context is not None:
            await context.close()


async def main() -> int:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    tmp_path = Path(tempfile.mkdtemp(prefix="easel-task11-r5-"))
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
        tmp_path, "a", ws_a, "normal", "a", "service-r5-browser",
        public_key_path, launcher_path).start()
    handles = {ws_a: WorkerHandle(
        ws_a, "container-r5", worker.base, "service-r5-browser", REVISION)}
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
            await scenario_empty_initial(
                browser, origin, invites["A"].pop(), capture,
                sessions["s023"], results, failures, screenshots)
            await scenario_old_history_initial(
                browser, origin, invites["A"].pop(), capture,
                sessions["s024"], results, failures, screenshots)
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
    if failures or len(results) != 2 or not all(results.values()):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
