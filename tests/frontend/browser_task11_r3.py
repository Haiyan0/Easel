"""Task 11 R3 真实 HTTPS/Chromium 边界验收（R3-01/02）。

响应交付顺序全部由 asyncio.Event 门禁控制：先真实请求服务端并捕获响应，
再按测试步骤放行；不用固定 sleep 猜测前提。

R3-01：新请求等待 202 时旧终态历史/旧连接不得抹掉新输入。
R3-02：目录刷新补页/存在性查询期间用户的新选择不得被旧请求撤销。

复用 R1/R2 的隔离数据与真实 control/worker；不访问真实账号，
临时目录运行后清理。
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
from easel.control.store import Store  # noqa: E402
from easel.control.types import WorkerHandle  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

REVISION = tcp.REVISION
SCREENSHOTS = base.SCREENSHOT_DIR
SKIP_HEADERS = {
    "content-length", "content-encoding", "transfer-encoding",
    "connection", "keep-alive",
}
REPLAY_SKIP = {
    "host", "content-length", "connection", "accept-encoding",
}


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


def response_headers(response: httpx.Response) -> dict[str, str]:
    return {key: value for key, value in response.headers.items()
            if key.lower() not in SKIP_HEADERS}


def replay_headers(request) -> dict[str, str]:
    return {key: value for key, value in request.headers.items()
            if key.lower() not in REPLAY_SKIP}


async def cookie_header(context, origin: str) -> str:
    items = await context.cookies(origin)
    return "; ".join(f"{item['name']}={item['value']}" for item in items)


async def wait_event(event: asyncio.Event, label: str,
                     timeout: float = 30.0) -> None:
    try:
        await asyncio.wait_for(event.wait(), timeout)
    except asyncio.TimeoutError:
        raise AssertionError(f"等待超时：{label}") from None


async def wait_until(predicate, timeout: float = 30.0,
                     message: str = "条件未满足") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(message)


async def active_runs(capture: httpx.AsyncClient, context, origin: str,
                      session_id: str) -> list[dict]:
    response = await capture.get(
        f"{origin}/api/sessions/{session_id}/runs"
        "?active=true&limit=1&offset=0",
        headers={"Cookie": await cookie_header(context, origin)})
    payload = response.json()
    runs = payload.get("runs")
    return runs if isinstance(runs, list) else []


async def wait_runs_settled(capture: httpx.AsyncClient, context,
                            origin: str, session_id: str,
                            timeout: float = 40.0) -> None:
    await wait_until(
        lambda: _no_active_runs(capture, context, origin, session_id),
        timeout, f"会话 {session_id} 的 Run 未在时限内结束")


async def _no_active_runs(capture, context, origin, session_id) -> bool:
    return len(await active_runs(capture, context, origin, session_id)) == 0


class Hold:
    """一个响应门禁：捕获真实响应 -> 等待测试放行 -> 交付页面。"""

    def __init__(self, label: str = "响应") -> None:
        self.label = label
        self.captured = asyncio.Event()
        self.gate = asyncio.Event()
        self.delivered = asyncio.Event()
        self.status = 0
        self.payload: dict = {}

    async def wait_captured(self, timeout: float = 30.0) -> None:
        try:
            await asyncio.wait_for(self.captured.wait(), timeout)
        except asyncio.TimeoutError:
            raise AssertionError(f"等待捕获超时：{self.label}") from None

    async def wait_delivered(self, timeout: float = 30.0) -> None:
        try:
            await asyncio.wait_for(self.delivered.wait(), timeout)
        except asyncio.TimeoutError:
            raise AssertionError(f"等待交付超时：{self.label}") from None

    def release(self) -> None:
        self.gate.set()


async def hold_response(route, capture, hold: Hold, cookie: str,
                        fail_status: int = 0) -> None:
    """先真实请求服务端并捕获响应，等门禁后再交给浏览器。"""
    response = await capture.get(route.request.url,
                                 headers={"Cookie": cookie})
    body = await response.aread()
    hold.status = response.status_code
    hold.payload = _json_or_empty(body)
    headers = response_headers(response)
    hold.captured.set()
    await hold.gate.wait()
    if fail_status:
        await route.fulfill(
            status=fail_status, content_type="application/json",
            body=json.dumps({"code": "upstream_unavailable"}))
    else:
        await route.fulfill(status=response.status_code, headers=headers,
                            body=body)
    hold.delivered.set()


def _json_or_empty(body: bytes) -> dict:
    try:
        value = json.loads(body)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def install_stream(page, bodies: list[str]):
    """按调用顺序交付受控 SSE；同一 Run 的重连也走这里。"""
    state = {"n": 0}

    async def handler(route):
        index = min(state["n"], len(bodies) - 1)
        state["n"] += 1
        await route.fulfill(
            status=200, headers={"content-type": "text/event-stream"},
            body=bodies[index])
    return handler


def install_post(page, capture, context, origin: str, control: dict,
                 hold: Hold, fail_status: int = 0):
    """第 hold_index 次 /api/chat 请求由测试捕获并延迟交付。"""
    async def handler(route):
        control["count"] += 1
        control["bodies"].append(route.request.post_data or "")
        if control["mode"] == "pass" \
                or control["count"] != control["hold_index"]:
            await route.continue_()
            return
        headers = replay_headers(route.request)
        content = route.request.post_data_buffer
        if content is None:
            content = (route.request.post_data or "").encode()
        response = await capture.post(route.request.url, content=content,
                                      headers=headers)
        body = await response.aread()
        hold.status = response.status_code
        hold.payload = _json_or_empty(body)
        hold.captured.set()
        await hold.gate.wait()
        if fail_status:
            await route.fulfill(
                status=fail_status, content_type="application/json",
                body=json.dumps({"code": "upstream_unavailable"}))
        else:
            await route.fulfill(status=response.status_code,
                                headers=response_headers(response),
                                body=body)
        hold.delivered.set()
    return handler


def question_status_route(question_ids: list[str]):
    async def handler(route):
        await route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"questions": {
                qid: {"status": "pending"} for qid in question_ids}}))
    return handler


async def messages_route(route, capture, context, origin, hold: Hold,
                         phase: dict) -> None:
    cookie = await cookie_header(context, origin)
    response = await capture.get(route.request.url,
                                 headers={"Cookie": cookie})
    body = await response.aread()
    headers = response_headers(response)
    if not phase["hold"]:
        await route.fulfill(status=response.status_code, headers=headers,
                            body=body)
        phase["initial"] += 1
        phase["initial_done"].set()
        return
    hold.status = response.status_code
    hold.payload = _json_or_empty(body)
    hold.captured.set()
    await hold.gate.wait()
    await route.fulfill(status=response.status_code, headers=headers,
                        body=body)
    hold.delivered.set()


async def install_messages(page, capture, context, origin, session_id,
                           hold: Hold, phase: dict) -> None:
    async def handler(route):
        await messages_route(route, capture, context, origin, hold, phase)
    await page.route(
        re.compile(rf"/api/sessions/{session_id}/messages"), handler)


async def wait_ready(page) -> None:
    await page.wait_for_function(
        "() => { const t = document.querySelector('textarea');"
        " return !!t && !t.disabled; }", timeout=30000)


async def send_text(page, text: str) -> None:
    await page.locator("textarea").fill(text)
    await page.get_by_role("button", name="发送").click()


async def article_texts(page) -> list[str]:
    return await page.locator("article").all_inner_texts()


async def header_text(page) -> str:
    return await page.locator("header").inner_text()


def identity_prefix(identity) -> str:
    return (f"easel:control:v1:{identity.user_id}:"
            f"{identity.workspace_id}")


# ---------------------------------------------------------------------------
# R3-01 场景
# ---------------------------------------------------------------------------

async def scenario_r3_01a(browser, origin, invite, capture, sessions,
                          results, failures, screenshots) -> None:
    """旧终态历史早于新 202 交付：不得抹掉新输入，最终快照收敛。"""
    context = page = None
    history = Hold("旧终态历史")
    admission = Hold("新 admission")
    phase = {"hold": False, "initial": 0,
             "initial_done": asyncio.Event()}
    control = {"count": 0, "hold_index": 2, "mode": "hold", "bodies": []}
    session = sessions["s010"]
    try:
        context, page = await r1.open_app(browser, origin, invite)
        await install_messages(page, capture, context, origin, session.id,
                               history, phase)
        await page.route(
            re.compile(r"/api/chat$"),
            install_post(page, capture, context, origin, control,
                         admission))
        await page.route(
            re.compile(r"/api/chat/question/status$"),
            question_status_route(["q2"]))
        sse = (frame(1, "state", {"state": "running"})
               + frame(2, "state", {"state": "done"}))
        sse_new = (frame(1, "state", {"state": "running"})
                   + frame(2, "token", "新进行中")
                   + frame(3, "state", {"state": "awaiting_permission"})
                   + question_event(4, "q2", "2. Allow", "qopt_2"))
        sse_done = frame(5, "state", {"state": "done"})
        await page.route(
            re.compile(r"/api/chat/jobs/[^/]+/stream"),
            install_stream(page, [sse, sse_new, sse_done]))
        await r1.select_session(page, "s010")
        await wait_event(phase["initial_done"], "初始历史响应", 30)
        await wait_ready(page)

        phase["hold"] = True
        await send_text(page, "old-prompt")
        await history.wait_captured(30)
        await wait_runs_settled(capture, context, origin, session.id)

        await send_text(page, "new-prompt")
        await admission.wait_captured(30)

        history.release()
        await history.wait_delivered(30)
        body = await page.locator("body").inner_text()
        assert "new-prompt" in body, (
            "旧终态历史交付后新输入被抹掉", body[-300:])

        admission.release()
        await admission.wait_delivered(30)
        assert admission.status == 202, admission.status
        await page.get_by_text("新进行中", exact=False).wait_for(
            timeout=20000)
        radio = page.locator("input[type=radio]").first
        await radio.wait_for(timeout=10000)
        await radio.check()
        assert await radio.is_checked()

        # 等待服务端真实 Run2 完成，再重连取最终快照，验证顺序收敛。
        await wait_runs_settled(capture, context, origin, session.id)
        await page.get_by_role(
            "button", name="重连（只回放）").click()
        await wait_until(
            lambda: _snapshot_converged(page), 30,
            "最终快照未收敛到旧回答/新输入/新回答顺序")
        results["r3_01_old_history_before_202_keeps_new_input"] = True
        path = SCREENSHOTS / "r3-01-old-history-before-202.png"
        await page.screenshot(path=str(path))
        screenshots.append(str(path))
    except Exception as error:  # noqa: BLE001 - 汇总红绿证据
        failures.append(f"R3-01a: {error!r}")
    finally:
        history.gate.set()
        admission.gate.set()
        if context is not None:
            await context.close()


async def _snapshot_converged(page) -> bool:
    texts = await article_texts(page)
    new_positions = [index for index, text in enumerate(texts)
                     if "new-prompt" in text]
    if len(new_positions) != 1:
        return False
    new_index = new_positions[0]
    assistants = [index for index, text in enumerate(texts)
                  if text.startswith("助手")]
    return (any(index < new_index for index in assistants)
            and any(index > new_index for index in assistants))


async def scenario_r3_01b(browser, origin, invite, capture, sessions,
                          results, failures, screenshots) -> None:
    """旧终态历史晚于新 202 交付：新题卡/选择保持。"""
    context = page = None
    history = Hold("旧终态历史")
    admission = Hold("新 admission")
    phase = {"hold": False, "initial": 0,
             "initial_done": asyncio.Event()}
    control = {"count": 0, "hold_index": 2, "mode": "hold", "bodies": []}
    session = sessions["s011"]
    try:
        context, page = await r1.open_app(browser, origin, invite)
        await install_messages(page, capture, context, origin, session.id,
                               history, phase)
        await page.route(
            re.compile(r"/api/chat$"),
            install_post(page, capture, context, origin, control,
                         admission))
        await page.route(
            re.compile(r"/api/chat/question/status$"),
            question_status_route(["q2"]))
        sse = (frame(1, "state", {"state": "running"})
               + frame(2, "state", {"state": "done"}))
        sse_new = (frame(1, "state", {"state": "running"})
                   + frame(2, "token", "新进行中")
                   + frame(3, "state", {"state": "awaiting_permission"})
                   + question_event(4, "q2", "2. Allow", "qopt_2"))
        await page.route(
            re.compile(r"/api/chat/jobs/[^/]+/stream"),
            install_stream(page, [sse, sse_new]))
        await r1.select_session(page, "s011")
        await wait_event(phase["initial_done"], "初始历史响应", 30)
        await wait_ready(page)

        phase["hold"] = True
        await send_text(page, "old-prompt")
        await history.wait_captured(30)
        await wait_runs_settled(capture, context, origin, session.id)

        await send_text(page, "new-prompt")
        await admission.wait_captured(30)

        admission.release()
        await admission.wait_delivered(30)
        await page.get_by_text("新进行中", exact=False).wait_for(
            timeout=20000)
        radio = page.locator("input[type=radio]").first
        await radio.wait_for(timeout=10000)
        await radio.check()

        history.release()
        await history.wait_delivered(30)
        body = await page.locator("body").inner_text()
        assert "new-prompt" in body
        assert "新进行中" in body
        assert await radio.is_checked()
        assert await page.get_by_text(
            "2. Allow", exact=False).count() >= 1
        await wait_runs_settled(capture, context, origin, session.id)
        results["r3_01_old_history_after_202_keeps_new_input"] = True
        path = SCREENSHOTS / "r3-01-old-history-after-202.png"
        await page.screenshot(path=str(path))
        screenshots.append(str(path))
    except Exception as error:  # noqa: BLE001
        failures.append(f"R3-01b: {error!r}")
    finally:
        history.gate.set()
        admission.gate.set()
        if context is not None:
            await context.close()


async def scenario_r3_01c(browser, origin, invite, capture, sessions,
                          results, failures, screenshots) -> None:
    """新 202 结果未知：保留待审查输入，并以同一 turnId 重试。"""
    context = page = None
    history = Hold("旧终态历史")
    admission = Hold("新 admission")
    phase = {"hold": False, "initial": 0,
             "initial_done": asyncio.Event()}
    control = {"count": 0, "hold_index": 2, "mode": "hold", "bodies": []}
    session = sessions["s012"]
    try:
        context, page = await r1.open_app(browser, origin, invite)
        await install_messages(page, capture, context, origin, session.id,
                               history, phase)
        await page.route(
            re.compile(r"/api/chat$"),
            install_post(page, capture, context, origin, control,
                         admission, fail_status=502))
        await page.route(
            re.compile(r"/api/chat/question/status$"),
            question_status_route(["q3"]))
        sse_old = (frame(1, "state", {"state": "running"})
                   + frame(2, "state", {"state": "done"}))
        sse_new = (frame(1, "state", {"state": "running"})
                   + frame(2, "state", {"state": "awaiting_permission"})
                   + question_event(3, "q3", "3. Retry", "qopt_3"))
        await page.route(
            re.compile(r"/api/chat/jobs/[^/]+/stream"),
            install_stream(page, [sse_old, sse_new]))
        await r1.select_session(page, "s012")
        await wait_event(phase["initial_done"], "初始历史响应", 30)
        await wait_ready(page)

        phase["hold"] = True
        await send_text(page, "old-prompt")
        await history.wait_captured(30)
        await wait_runs_settled(capture, context, origin, session.id)

        await send_text(page, "new-prompt")
        await admission.wait_captured(30)
        first_turn = json.loads(control["bodies"][1])["turnId"]

        # 旧历史先交付，再交付“结果未知”的 502。
        history.release()
        await history.wait_delivered(30)
        admission.release()
        await admission.wait_delivered(30)
        await page.get_by_text("发送结果未知", exact=False).wait_for(
            timeout=20000)
        body = await page.locator("body").inner_text()
        assert body.count("new-prompt") == 1, body[-300:]
        retry = page.get_by_role(
            "button", name="重试同一请求（不会新造请求键）")
        await retry.wait_for(timeout=10000)

        control["mode"] = "pass"
        await retry.click()
        await page.get_by_text("3. Retry", exact=False).wait_for(
            timeout=20000)
        assert control["count"] >= 3, control["count"]
        retry_turn = json.loads(control["bodies"][2])["turnId"]
        assert retry_turn == first_turn, (first_turn, retry_turn)
        body = await page.locator("body").inner_text()
        assert body.count("new-prompt") == 1, body[-300:]
        assert await page.get_by_text(
            "重试同一请求（不会新造请求键）", exact=False).count() == 0
        await wait_runs_settled(capture, context, origin, session.id)
        results["r3_01_unknown_admission_keeps_input_same_turn"] = True
        path = SCREENSHOTS / "r3-01-unknown-admission-retry.png"
        await page.screenshot(path=str(path))
        screenshots.append(str(path))
    except Exception as error:  # noqa: BLE001
        failures.append(f"R3-01c: {error!r}")
    finally:
        history.gate.set()
        admission.gate.set()
        if context is not None:
            await context.close()


async def scenario_r3_01d(browser, origin, invite, capture, sessions,
                          identity, results, failures, screenshots) -> None:
    """等待 202 期间用户切会话：旧收尾不得抢占新会话。"""
    context = page = None
    history = Hold("旧终态历史")
    admission = Hold("新 admission")
    phase = {"hold": False, "initial": 0,
             "initial_done": asyncio.Event()}
    control = {"count": 0, "hold_index": 2, "mode": "hold", "bodies": []}
    session = sessions["s013"]
    try:
        context, page = await r1.open_app(browser, origin, invite)
        await install_messages(page, capture, context, origin, session.id,
                               history, phase)
        await page.route(
            re.compile(r"/api/chat$"),
            install_post(page, capture, context, origin, control,
                         admission))
        sse = (frame(1, "state", {"state": "running"})
               + frame(2, "state", {"state": "done"}))
        streams = {"n": 0}

        async def stream(route):
            streams["n"] += 1
            await route.fulfill(
                status=200, headers={"content-type": "text/event-stream"},
                body=sse)
        await page.route(
            re.compile(r"/api/chat/jobs/[^/]+/stream"), stream)
        await r1.select_session(page, "s013")
        await wait_event(phase["initial_done"], "初始历史响应", 30)
        await wait_ready(page)

        phase["hold"] = True
        await send_text(page, "old-prompt")
        await history.wait_captured(30)
        await wait_runs_settled(capture, context, origin, session.id)

        await send_text(page, "new-prompt")
        await admission.wait_captured(30)
        admission_run = admission.payload.get("runId")

        await r1.select_session(page, "s014")
        assert "s014" in await header_text(page)

        history.release()
        await history.wait_delivered(30)
        admission.release()
        await admission.wait_delivered(30)
        assert "s014" in await header_text(page)
        assert await page.get_by_text(
            "2. Allow", exact=False).count() == 0
        assert streams["n"] == 1, streams["n"]
        recovery_key = (
            f"{identity_prefix(identity)}:run:{session.id}")
        raw = await page.evaluate(
            "key => sessionStorage.getItem(key)", recovery_key)
        assert raw, "切会话后未保留本人恢复记录"
        value = json.loads(raw)
        assert value.get("runId") == admission_run, (value, admission_run)
        await wait_runs_settled(capture, context, origin, session.id)
        results["r3_01_switch_during_202_keeps_new_session"] = True
        path = SCREENSHOTS / "r3-01-switch-during-202.png"
        await page.screenshot(path=str(path))
        screenshots.append(str(path))
    except Exception as error:  # noqa: BLE001
        failures.append(f"R3-01d: {error!r}")
    finally:
        history.gate.set()
        admission.gate.set()
        if context is not None:
            await context.close()


# ---------------------------------------------------------------------------
# R3-02 场景
# ---------------------------------------------------------------------------

async def broadcast_refresh(page2, channel: str) -> None:
    await page2.evaluate(
        "name => { const ch = new BroadcastChannel(name);"
        " ch.postMessage({type: 'sessions-changed'}); ch.close(); }",
        channel)


async def scenario_r3_02(browser, origin, invite, capture, sessions,
                         identity, db_path: Path, first_page, oldest,
                         results, failures, screenshots) -> None:
    """目录刷新补页/存在性查询期间，用户的新选择不得被旧请求撤销。"""
    context = page = page2 = None
    results_map: dict[str, bool] = {}
    run_map: dict[str, str] = {}
    post_labels: list[str] = []
    sse_counts: dict[str, int] = {}
    active_key = f"{identity_prefix(identity)}:active_session"
    try:
        context, page = await r1.open_app(browser, origin, invite)
        page2 = await context.new_page()
        await page2.goto("/", wait_until="domcontentloaded")
        await page2.get_by_role(
            "button", name="新建会话").wait_for(timeout=30000)
        channel = (f"easel-control:{identity.user_id}:"
                   f"{identity.workspace_id}")

        async def chat_post(route):
            label = post_labels.pop(0) if post_labels else "other"
            content = route.request.post_data_buffer
            if content is None:
                content = (route.request.post_data or "").encode()
            response = await capture.post(
                route.request.url, content=content,
                headers=replay_headers(route.request))
            body = await response.aread()
            payload = _json_or_empty(body)
            run_id = payload.get("runId")
            if isinstance(run_id, str):
                run_map[run_id] = label
            await route.fulfill(status=response.status_code,
                                headers=response_headers(response),
                                body=body)

        await page.route(re.compile(r"/api/chat$"), chat_post)
        await page.route(
            re.compile(r"/api/chat/question/status$"),
            question_status_route(["q-old", "q-big"]))
        bodies = {
            "old": (frame(1, "state", {"state": "running"})
                    + question_event(2, "q-old", "Old Allow", "qopt_old")),
            "big": (frame(1, "state", {"state": "running"})
                    + question_event(2, "q-big", "Big Allow", "qopt_big")),
        }

        async def stream(route):
            match = re.search(r"/api/chat/jobs/([^/]+)/stream",
                              route.request.url)
            label = run_map.get(match.group(1), "old") if match else "old"
            sse_counts[label] = sse_counts.get(label, 0) + 1
            await route.fulfill(
                status=200,
                headers={"content-type": "text/event-stream"},
                body=bodies[label])
        await page.route(
            re.compile(r"/api/chat/jobs/[^/]+/stream"), stream)

        # 1) 第二页会话 oldest 产生 active Run 与题卡
        await r1.select_session(page, oldest.title)
        post_labels.append("old")
        await send_text(page, "旧卡")
        await page.get_by_text(
            "Old Allow", exact=False).wait_for(timeout=30000)
        # 2) 第一页会话 first_page 产生第二个 active Run 与题卡
        await r1.select_session(page, first_page.title)
        post_labels.append("big")
        await send_text(page, "big卡")
        await page.get_by_text(
            "Big Allow", exact=False).wait_for(timeout=30000)
        await page.locator("input[type=radio]").first.check()
        # 3) 正常再次切回原会话
        await r1.select_session(page, oldest.title)
        await page.get_by_text(
            "Old Allow", exact=False).wait_for(timeout=30000)
        assert oldest.title in await header_text(page)
        results_map["r3_02_switch_back_original_session"] = True

        # 主场景：oldest（第二页）为当前会话时刷新，补页响应被捕获并暂停
        second_page = Hold("目录第二页")
        remaining = {"n": 1}
        page2_pattern = re.compile(
            r"/api/sessions\?limit=100&offset=100&include_archived=true$")

        async def second_handler(route):
            if remaining["n"] > 0:
                remaining["n"] -= 1
                await hold_response(
                    route, capture, second_page,
                    await cookie_header(context, origin))
            else:
                await route.continue_()
        store = Store.connect(db_path)
        try:
            store.create_conversation(identity, "refresh-marker")
        finally:
            store.close()
        await page.route(page2_pattern, second_handler)
        await broadcast_refresh(page2, channel)
        await second_page.wait_captured(30)

        # 用户在旧页响应挂起时主动选择 big，并收到题卡/选项
        await r1.select_session(page, first_page.title)
        await page.get_by_text(
            "Big Allow", exact=False).wait_for(timeout=30000)
        radio = page.locator("input[type=radio]").first
        await radio.wait_for(timeout=10000)
        await radio.check()
        counts_before = dict(sse_counts)

        second_page.release()
        await second_page.wait_delivered(30)
        # 标记会话只可能由这次目录提交加入，出现即证明旧请求已完成提交
        await page.get_by_role(
            "button", name="refresh-marker", exact=True).wait_for(
                timeout=30000)
        assert first_page.title in await header_text(page), await header_text(page)
        assert await page.get_by_text(
            "Big Allow", exact=False).count() >= 1
        assert await radio.is_checked()
        stored = await page.evaluate(
            "key => localStorage.getItem(key)", active_key)
        assert stored == first_page.id, (stored, first_page.id)
        assert dict(sse_counts) == counts_before, (sse_counts, counts_before)
        results_map["r3_02_refresh_keeps_new_selection"] = True
        path = SCREENSHOTS / "r3-02-refresh-keeps-selection.png"
        await page.screenshot(path=str(path))
        screenshots.append(str(path))

        # 目录请求彼此交错：旧补页挂起时新的刷新完成，旧结果不得回切
        await page.unroute(page2_pattern)
        await r1.select_session(page, oldest.title)
        await page.get_by_text(
            "Old Allow", exact=False).wait_for(timeout=30000)
        store = Store.connect(db_path)
        try:
            store.create_conversation(identity, "interleave-new")
        finally:
            store.close()
        interleave = Hold("交错目录第二页")
        inter_remaining = {"n": 1}

        async def inter_handler(route):
            if inter_remaining["n"] > 0:
                inter_remaining["n"] -= 1
                await hold_response(
                    route, capture, interleave,
                    await cookie_header(context, origin))
            else:
                await route.continue_()
        await page.route(page2_pattern, inter_handler)
        await broadcast_refresh(page2, channel)
        await interleave.wait_captured(30)
        await broadcast_refresh(page2, channel)
        await page.get_by_role(
            "button", name="interleave-new", exact=True).wait_for(
                timeout=30000)
        interleave.release()
        await interleave.wait_delivered(30)
        assert oldest.title in await header_text(page), await header_text(page)
        assert await page.get_by_text(
            "Old Allow", exact=False).count() >= 1
        stored = await page.evaluate(
            "key => localStorage.getItem(key)", active_key)
        assert stored == oldest.id, (stored, oldest.id)
        results_map["r3_02_interleaved_directory_requests"] = True
        await page.unroute(page2_pattern)

        # 当前会话真实删除 + 慢存在性响应期间用户另选
        s050 = sessions["s050"]
        await r1.select_session(page, s050.title)
        assert s050.title in await header_text(page)
        store = Store.connect(db_path)
        try:
            store.delete_conversation(identity, s050.id)
        finally:
            store.close()
        existence = Hold("删除存在性")
        existence_pattern = re.compile(
            rf"/api/sessions/{s050.id}/runs\?limit=1&offset=0$")

        async def existence_handler(route):
            await hold_response(
                route, capture, existence,
                await cookie_header(context, origin))
        await page.route(existence_pattern, existence_handler)
        store = Store.connect(db_path)
        try:
            store.create_conversation(identity, "existence-marker")
        finally:
            store.close()
        await broadcast_refresh(page2, channel)
        await existence.wait_captured(30)

        await r1.select_session(page, oldest.title)
        await page.get_by_text(
            "Old Allow", exact=False).wait_for(timeout=30000)
        existence.release()
        await existence.wait_delivered(30)
        await page.get_by_role(
            "button", name="existence-marker", exact=True).wait_for(
                timeout=30000)
        assert oldest.title in await header_text(page), await header_text(page)
        assert await page.get_by_text(
            "Old Allow", exact=False).count() >= 1
        stored = await page.evaluate(
            "key => localStorage.getItem(key)", active_key)
        assert stored == oldest.id, (stored, oldest.id)
        results_map["r3_02_deleted_current_keeps_new_selection"] = True
        path = SCREENSHOTS / "r3-02-deleted-current-keeps-selection.png"
        await page.screenshot(path=str(path))
        screenshots.append(str(path))
        await page.unroute(existence_pattern)

        # 当前会话删除且用户未另选：明确回落到其它仍存在会话
        s051 = sessions["s051"]
        await r1.select_session(page, s051.title)
        store = Store.connect(db_path)
        try:
            store.delete_conversation(identity, s051.id)
        finally:
            store.close()
        await broadcast_refresh(page2, channel)

        async def header_left_051() -> bool:
            return s051.title not in await header_text(page)
        await wait_until(header_left_051, 30,
                         "删除当前会话后未明确回落")
        stored_after = await page.evaluate(
            "key => localStorage.getItem(key)", active_key)
        assert stored_after and stored_after != s051.id
        results_map["r3_02_deleted_current_falls_back"] = True

        results.update(results_map)
    except Exception as error:  # noqa: BLE001
        results.update(results_map)
        failures.append(f"R3-02: {error!r}")
    finally:
        if context is not None:
            await context.close()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

async def main() -> int:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    tmp_path = Path(tempfile.mkdtemp(prefix="easel-task11-r3-"))
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
        tmp_path, "a", ws_a, "normal", "a", "service-r3-browser",
        public_key_path, launcher_path).start()
    handles = {ws_a: WorkerHandle(
        ws_a, "container-r3", worker.base, "service-r3-browser", REVISION)}
    commands = {ws_a: worker.command}
    runtime = tcp.StaticRuntime(handles)
    provider = tcp.make_provider({ws_a: tcp.TokenMint()}, commands)
    key_path, cert_path, pin = base.make_certificate(tmp_path)
    port = tcp.free_port()
    origin = f"https://localhost:{port}"
    host = base.ControlHTTPSHost(
        db_path, key, runtime, provider, cert_path, key_path, origin,
        port).start()
    _, oldest = r1.seed_ui_data(db_path, worker, identities)
    store = Store.connect(db_path)
    try:
        rows = store.list_conversations(
            identities["A"], limit=200, include_archived=True)
        sessions = {item.title: item for item in rows}
        first_page = rows[0]
    finally:
        store.close()

    # R3_ONLY=a,b,c,d,02 仅供本地单场景诊断；交付回归不带该变量。
    import os
    only = {item for item in os.environ.get("R3_ONLY", "").split(",")
            if item}
    screenshots: list[str] = []
    results: dict[str, bool] = {}
    failures: list[str] = []
    browser = None
    hang = None
    version = ""
    capture = httpx.AsyncClient(verify=False, trust_env=False, timeout=20)
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=[f"--ignore-certificate-errors-spki-list={pin}"])
            version = browser.version

            if not only or "a" in only:
                await scenario_r3_01a(
                    browser, origin, invites["A"].pop(), capture, sessions,
                    results, failures, screenshots)
            if not only or "b" in only:
                await scenario_r3_01b(
                    browser, origin, invites["A"].pop(), capture, sessions,
                    results, failures, screenshots)
            if not only or "c" in only:
                await scenario_r3_01c(
                    browser, origin, invites["A"].pop(), capture, sessions,
                    results, failures, screenshots)
            if not only or "d" in only:
                await scenario_r3_01d(
                    browser, origin, invites["A"].pop(), capture, sessions,
                    identities["A"], results, failures, screenshots)

            # R3-02 需要不结束的 Run 来保持题卡与连接；先停 normal。
            worker.stop()
            hang = tcp.Worker(
                tmp_path, "hang", ws_a, "hang", "h", "service-r3-hang",
                public_key_path, launcher_path).start()
            handles[ws_a] = WorkerHandle(
                ws_a, "container-r3-hang", hang.base, "service-r3-hang",
                REVISION)
            commands[ws_a] = hang.command

            if not only or "02" in only:
                await scenario_r3_02(
                    browser, origin, invites["A"].pop(), capture, sessions,
                    identities["A"], db_path, first_page, oldest, results,
                    failures, screenshots)
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
        if hang is not None:
            hang.stop()
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
    if failures or len(results) != 9 or not all(results.values()):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
