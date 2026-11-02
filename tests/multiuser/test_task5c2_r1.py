"""Task 5C2 R1：旧状态归属、凭证提交栅栏与迟到回包的回归测试。

真实层：control FastAPI、GatewayClient/gateway、WorkspaceApi、WorkerRelay、
worker RunManager（默认 browser executor）与原始受管入口。
替身层：平台脚本只写本人临时产物，不访问真实平台/浏览器。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from easel.control.store import Store
from web import app as worker

from tests.multiuser.test_task5c2_chain import ORIGIN, PLATFORM, Chain


@asynccontextmanager
async def _live_chain(root: Path, monkeypatch):
    chain = Chain(root, monkeypatch)
    try:
        async with chain.app.router.lifespan_context(chain.app):
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=chain.app),
                    base_url=ORIGIN, timeout=20.0) as client:
                invite = chain.auth.create_invite(
                    chain.admin, chain.member.user_id)
                exchanged = await client.post(
                    "/api/auth/exchange", json={"token": invite},
                    headers={"Origin": ORIGIN})
                assert exchanged.status_code == 200, exchanged.text
                headers = {
                    "Origin": ORIGIN,
                    "X-CSRF-Token": exchanged.json()["csrfToken"],
                }
                yield chain, client, headers
    finally:
        await chain.close()
        chain.store.close()
        monkeypatch.undo()


def _status(chain: Chain, run_id: str) -> str:
    with Store.open(chain.store.path) as store:
        return store.get_run(chain.member, run_id).status


def _active(chain: Chain) -> int:
    with Store.open(chain.store.path) as store:
        return store.count_active_runs_for_workspace(
            chain.member.workspace_id)


async def _wait_until(predicate, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(message)


def test_stale_platform_success_cannot_settle_new_login_run(
        tmp_path, monkeypatch):
    """旧 success 只能视为历史状态；新 Run 未开始前必须保持 starting。"""

    async def scenario():
        gate = asyncio.Event()
        entered = asyncio.Event()
        async with _live_chain(tmp_path, monkeypatch) as (chain, client,
                                                          headers):
            original = chain.manager._browser_executor

            async def paused(*arguments):
                entered.set()
                await gate.wait()
                return await original(*arguments)

            chain.manager._browser_executor = paused
            chain.fake._write_status(
                PLATFORM, "success", "PREVIOUS_LOGIN_SUCCESS")
            response = await client.post(
                f"/api/login/{PLATFORM}", json={"requestId": "r1-stale"},
                headers=headers)
            assert response.status_code == 200, response.text
            body = response.json()
            run_id = body["runId"]
            await asyncio.wait_for(entered.wait(), 3)
            assert body["state"] == "starting", body
            assert _status(chain, run_id) == "running"
            assert _active(chain) == 1
            assert chain.manager.state(run_id) == "running"
            assert chain.fake.login_starts == []

            # 放行后新执行器必须真正启动，旧 success 不能替代它的结果。
            gate.set()
            await _wait_until(
                lambda: chain.fake.login_starts == [PLATFORM], 5,
                "new login executor did not start")
            chain.fake.scanned = True
            chain.fake.code = "123456"
            deadline = time.monotonic() + 8
            final = None
            while time.monotonic() < deadline:
                current = await client.get(
                    f"/api/login/{PLATFORM}/status")
                assert current.status_code == 200, current.text
                final = current.json()
                if final["state"] in ("success", "error", "expired"):
                    break
                await asyncio.sleep(0.05)
            assert final is not None and final["state"] == "success", final
            assert _status(chain, run_id) == "done"
            assert _active(chain) == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("stale", ["expired", "error"])
def test_stale_terminal_status_cannot_fail_new_login_run(
        tmp_path, monkeypatch, stale):
    """旧 expired/error 不能把尚未执行的新 Run 直接 failed 或释放容量。"""

    async def scenario():
        gate = asyncio.Event()
        entered = asyncio.Event()
        async with _live_chain(tmp_path, monkeypatch) as (chain, client,
                                                          headers):
            original = chain.manager._browser_executor

            async def paused(*arguments):
                entered.set()
                await gate.wait()
                return await original(*arguments)

            chain.manager._browser_executor = paused
            chain.fake._write_status(PLATFORM, stale, "PREVIOUS")
            response = await client.post(
                f"/api/login/{PLATFORM}", json={"requestId": "r1-old-" + stale},
                headers=headers)
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["state"] == "starting", body
            await asyncio.wait_for(entered.wait(), 3)
            assert _status(chain, body["runId"]) == "running"
            assert _active(chain) == 1
            side = await client.get(f"/api/login/{PLATFORM}/status")
            assert side.status_code == 200, side.text
            assert side.json()["state"] not in ("expired", "error"), side.text
            gate.set()
            stopped = await client.post(
                f"/api/login/{PLATFORM}/stop",
                json={"runId": body["runId"]}, headers=headers)
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["state"] in ("cancelled", "interrupted")

    asyncio.run(scenario())


def test_stale_mp_status_cannot_settle_new_mp_login_run(
        tmp_path, monkeypatch):
    async def scenario():
        gate = asyncio.Event()
        entered = asyncio.Event()
        async with _live_chain(tmp_path, monkeypatch) as (chain, client,
                                                          headers):
            original = chain.manager._browser_executor
            mp_starts: list[str] = []

            async def mp_start(platform):
                mp_starts.append(platform)
                worker.LOGIN_DIR.mkdir(parents=True, exist_ok=True)
                (worker.LOGIN_DIR / "wechat-oa-mp.png").write_bytes(b"\x89PNG")
                (worker.LOGIN_DIR / "wechat-oa-mp.json").write_text(
                    json.dumps({"state": "qr_ready", "message": "请扫码"}),
                    encoding="utf-8")
                return {"mode": "qr", "state": "starting"}

            def mp_status():
                path = worker.LOGIN_DIR / "wechat-oa-mp.json"
                data = json.loads(path.read_text(encoding="utf-8"))
                return {"state": data.get("state", "unknown"),
                        "message": data.get("message", ""),
                        "qr": "_login/wechat-oa-mp.png",
                        "qrTs": 1}

            monkeypatch.setattr(worker, "api_mp_login_start", mp_start)
            monkeypatch.setattr(worker, "_mp_login_status", mp_status)

            async def paused(*arguments):
                entered.set()
                await gate.wait()
                return await original(*arguments)

            chain.manager._browser_executor = paused
            login_dir = worker.LOGIN_DIR
            login_dir.mkdir(parents=True, exist_ok=True)
            (login_dir / "wechat-oa-mp.json").write_text(
                json.dumps({"state": "success", "message": "OLD"}),
                encoding="utf-8")
            response = await client.post(
                "/api/accounts/wechat-oa/mp-login",
                json={"requestId": "r1-mp-stale"}, headers=headers)
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["state"] == "starting", body
            await asyncio.wait_for(entered.wait(), 3)
            assert _status(chain, body["runId"]) == "running"
            assert _active(chain) == 1
            assert mp_starts == []
            gate.set()
            stopped = await client.post(
                "/api/accounts/wechat-oa/mp-login/stop",
                json={"runId": body["runId"]}, headers=headers)
            assert stopped.status_code == 200, stopped.text

    asyncio.run(scenario())


def test_login_sms_requires_current_run_status_not_shared_file(
        tmp_path, monkeypatch):
    """共享 status 里的 sms_required 不能代替当前 Run 的扫码事件。"""

    async def scenario():
        async with _live_chain(tmp_path, monkeypatch) as (chain, client,
                                                          headers):
            started = await client.post(
                f"/api/login/{PLATFORM}",
                json={"requestId": "r1-sms"}, headers=headers)
            assert started.status_code == 200, started.text
            run_id = started.json()["runId"]
            await _wait_until(
                lambda: chain.fake.login_starts == [PLATFORM], 5,
                "login executor did not start")

            # 直接写共享状态文件：当前 Run 尚未报告 sms_required，拒绝。
            chain.fake._write_status(
                PLATFORM, "sms_required", "OLD SMS")
            rejected = await client.post(
                f"/api/login/{PLATFORM}/sms",
                json={"code": "123456", "runId": run_id}, headers=headers)
            assert rejected.status_code == 409, rejected.text

            # 当前 Run 自己进入 sms_required 后才接受验证码。
            chain.fake.scanned = True
            await _wait_until(
                lambda: _sms_state(chain, run_id), 5,
                "run-attributed sms status missing")
            accepted = await client.post(
                f"/api/login/{PLATFORM}/sms",
                json={"code": "123456", "runId": run_id}, headers=headers)
            assert accepted.status_code == 200, accepted.text
            stopped = await client.post(
                f"/api/login/{PLATFORM}/stop",
                json={"runId": run_id}, headers=headers)
            assert stopped.status_code == 200, stopped.text

    asyncio.run(scenario())


def _sms_state(chain: Chain, run_id: str) -> bool:
    page = chain.manager.events(run_id, after=0)
    for event in reversed(page.get("events") or []):
        if event.get("event") != "activity":
            continue
        try:
            data = json.loads(event.get("data") or "")
        except (TypeError, ValueError):
            continue
        if data.get("event") == "browser_status" \
                and data.get("state") == "sms_required":
            return True
    return False


def test_explicit_restart_uses_new_run_not_stale_success(
        tmp_path, monkeypatch):
    async def scenario():
        gate = asyncio.Event()
        entered = asyncio.Event()
        async with _live_chain(tmp_path, monkeypatch) as (chain, client,
                                                          headers):
            first = await client.post(
                f"/api/login/{PLATFORM}",
                json={"requestId": "r1-restart-old"}, headers=headers)
            assert first.status_code == 200, first.text
            first_id = first.json()["runId"]
            await _wait_until(
                lambda: chain.fake.login_starts == [PLATFORM], 5,
                "first executor did not start")
            chain.fake._write_status(PLATFORM, "success", "OLD SUCCESS")

            original = chain.manager._browser_executor

            async def paused(*arguments):
                entered.set()
                await gate.wait()
                return await original(*arguments)

            chain.manager._browser_executor = paused
            restarted = await client.post(
                f"/api/login/{PLATFORM}",
                json={"requestId": "r1-restart-new", "restart": True},
                headers=headers)
            assert restarted.status_code == 200, restarted.text
            body = restarted.json()
            assert body["runId"] != first_id
            assert body["state"] == "starting", body
            await asyncio.wait_for(entered.wait(), 3)
            assert _status(chain, first_id) == "cancelled"
            assert _status(chain, body["runId"]) == "running"
            gate.set()
            stopped = await client.post(
                f"/api/login/{PLATFORM}/stop",
                json={"runId": body["runId"]}, headers=headers)
            assert stopped.status_code == 200, stopped.text

    asyncio.run(scenario())


def test_credentials_stop_prevents_late_commit(tmp_path, monkeypatch):
    """暂停验证 → 正式 stop 终态 → 放行后不得写配置/登录标记。"""

    async def scenario():
        gate = threading.Event()
        entered = threading.Event()

        def verify(app_id, secret):
            entered.set()
            if not gate.wait(8):
                raise AssertionError("verification gate timed out")
            return True, ""

        config_path = tmp_path / "wechat.yaml"
        async with _live_chain(tmp_path, monkeypatch) as (chain, client,
                                                          headers):
            monkeypatch.setattr(
                worker, "_wechat_verify_credentials", verify)
            pending = asyncio.create_task(client.post(
                "/api/accounts/wechat-oa/credentials",
                json={"appId": "old-app", "appSecret": "old-secret",
                      "name": "old", "author": ""},
                headers=headers))
            assert await asyncio.to_thread(entered.wait, 5)
            with Store.open(chain.store.path) as store:
                run_id = store._connection.execute(
                    "SELECT id FROM runs ORDER BY created_at DESC LIMIT 1"
                ).fetchone()[0]
            stopped = await client.post(
                "/api/chat/stop", json={"runId": run_id}, headers=headers)
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["state"] in ("cancelled", "interrupted")
            gate.set()
            response = await asyncio.wait_for(pending, 8)
            assert response.status_code == 409, response.text
            assert not config_path.exists()
            assert worker._login_status("wechat-oa")["state"] != "success"

    asyncio.run(scenario())


def test_credentials_cancelled_run_does_not_block_new_verification(
        tmp_path, monkeypatch):
    """取消后的迟到验证不能覆盖随后新校验成功保存的配置。"""

    async def scenario():
        first_gate = threading.Event()
        first_entered = threading.Event()
        calls = {"total": 0}

        def verify(app_id, secret):
            calls["total"] += 1
            if app_id == "old-app":
                first_entered.set()
                if not first_gate.wait(8):
                    raise AssertionError("verification gate timed out")
                return True, ""
            return True, ""

        config_path = tmp_path / "wechat.yaml"
        async with _live_chain(tmp_path, monkeypatch) as (chain, client,
                                                          headers):
            monkeypatch.setattr(
                worker, "_wechat_verify_credentials", verify)
            old = asyncio.create_task(client.post(
                "/api/accounts/wechat-oa/credentials",
                json={"appId": "old-app", "appSecret": "old-secret",
                      "name": "old", "author": ""}, headers=headers))
            assert await asyncio.to_thread(first_entered.wait, 5)
            with Store.open(chain.store.path) as store:
                old_run = store._connection.execute(
                    "SELECT id FROM runs ORDER BY created_at DESC LIMIT 1"
                ).fetchone()[0]
            stopped = await client.post(
                "/api/chat/stop", json={"runId": old_run}, headers=headers)
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["state"] in ("cancelled", "interrupted")

            fresh = await client.post(
                "/api/accounts/wechat-oa/credentials",
                json={"appId": "new-app", "appSecret": "new-secret",
                      "name": "new", "author": ""}, headers=headers)
            assert fresh.status_code == 200, fresh.text
            assert fresh.json()["ok"] is True
            first_gate.set()
            late = await asyncio.wait_for(old, 8)
            assert late.status_code == 409, late.text
            text = config_path.read_text(encoding="utf-8")
            assert "new-app" in text and "new-secret" in text, text
            assert "old-app" not in text and "old-secret" not in text, text
            assert calls["total"] == 2

    asyncio.run(scenario())


def test_logout_stops_credentials_run_and_prevents_late_commit(
        tmp_path, monkeypatch):
    async def scenario():
        gate = threading.Event()
        entered = threading.Event()

        def verify(app_id, secret):
            entered.set()
            if not gate.wait(8):
                raise AssertionError("verification gate timed out")
            return True, ""

        async with _live_chain(tmp_path, monkeypatch) as (chain, client,
                                                          headers):
            monkeypatch.setattr(
                worker, "_wechat_verify_credentials", verify)
            pending = asyncio.create_task(client.post(
                "/api/accounts/wechat-oa/credentials",
                json={"appId": "wx-old", "appSecret": "old-secret",
                      "name": "old", "author": ""}, headers=headers))
            assert await asyncio.to_thread(entered.wait, 5)
            logged_out = await client.post(
                "/api/logout/wechat-oa", headers=headers)
            assert logged_out.status_code == 200, logged_out.text
            gate.set()
            response = await asyncio.wait_for(pending, 8)
            assert response.status_code == 409, response.text
            assert not (tmp_path / "wechat.yaml").exists()
            assert worker._login_status("wechat-oa")["state"] != "success"

    asyncio.run(scenario())


def test_credentials_commit_then_stop_reports_actual_terminal_state(
        tmp_path, monkeypatch):
    """提交先完成时 stop 必须等提交结束并返回真实 done，而不是 cancelled。"""

    async def scenario():
        saved = threading.Event()
        release = threading.Event()
        original = worker._wechat_save_credentials

        def save(*args, **kwargs):
            original(*args, **kwargs)
            saved.set()
            release.wait(5)

        monkeypatch.setattr(worker, "_wechat_save_credentials", save)
        monkeypatch.setattr(
            worker, "_wechat_verify_credentials", lambda app_id, secret: (True, ""))
        async with _live_chain(tmp_path, monkeypatch) as (chain, client,
                                                          headers):
            pending = asyncio.create_task(client.post(
                "/api/accounts/wechat-oa/credentials",
                json={"appId": "wx-commit", "appSecret": "secret",
                      "name": "commit", "author": ""}, headers=headers))
            assert await asyncio.to_thread(saved.wait, 5)
            with Store.open(chain.store.path) as store:
                run_id = store._connection.execute(
                    "SELECT id FROM runs ORDER BY created_at DESC LIMIT 1"
                ).fetchone()[0]
            stopped = asyncio.create_task(client.post(
                "/api/chat/stop", json={"runId": run_id}, headers=headers))
            await asyncio.sleep(0.05)
            assert not stopped.done(), "stop must wait for the commit fence"
            release.set()
            response = await asyncio.wait_for(pending, 8)
            assert response.status_code == 200, response.text
            assert response.json()["ok"] is True
            result = await asyncio.wait_for(stopped, 8)
            assert result.status_code == 200, result.text
            assert result.json()["state"] == "done", result.text
            assert (tmp_path / "wechat.yaml").exists()

    asyncio.run(scenario())
