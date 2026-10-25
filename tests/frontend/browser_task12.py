"""Task 12 真实 HTTPS/Chromium 验收脚本（独立运行，不进入 pytest）。

覆盖：个人覆盖/恢复继承与 A/B 隔离、管理员默认更新/版本冲突、邀请一次性
明文/兑换/禁用与撤销、聊天中切设置页保留题卡与待确认输入、身份切换时旧
GET/保存响应不回填新身份。

使用隔离临时 DB/master key/证书/worker/浏览器上下文；不读取真实 HOME、
账号或 profile，不关闭 Chromium sandbox，不调用真实供应商。
运行：python tests/frontend/browser_task12.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "multiuser"))
sys.path.insert(0, str(ROOT / "tests" / "frontend"))

import test_tcp_integration as tcp  # noqa: E402
import browser_task11 as base  # noqa: E402
from easel.control.app import ControlConfig, create_app  # noqa: E402
from easel.control.auth import AuthService  # noqa: E402
from easel.control.config import MASTER_KEY_BYTES, ConfigService  # noqa: E402
from easel.control.store import Store  # noqa: E402
from easel.control.types import WorkerHandle  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

REVISION = tcp.REVISION
MASTER_KEY = b"t" * MASTER_KEY_BYTES
ADMIN_SECRET = "task12-admin-secret-sentinel"
PERSONAL_A_SECRET = "task12-personal-a-secret-sentinel"
B_PERSONAL_SECRET = "task12-personal-b-secret-sentinel"
CONFLICT_SECRET = "task12-conflict-secret-sentinel"
CHAT_SWITCH_SECRET = "task12-chat-switch-secret-sentinel"
SCREENSHOTS = (ROOT / ".superpowers" / "sdd"
               / "2026-10-25-task-12-control-settings" / "screenshots")


def admin_route(model: str = "admin-tts", **overrides) -> dict:
    route = {
        "provider": "openai-compatible",
        "model": model,
        "base_url": "https://provider.example/v1",
        "protocol": "openai",
        "secret": ADMIN_SECRET,
        "options": {},
        "allowed_models": ["admin-tts", "admin-v2", "admin-v3",
                           "admin-conflict"],
    }
    route.update(overrides)
    return route


def personal_route(model: str, base_url: str, secret: str) -> dict:
    return {
        "provider": "openai-compatible",
        "model": model,
        "base_url": base_url,
        "protocol": "openai",
        "secret": secret,
        "options": {},
    }


class Task12Host(base.ControlHTTPSHost):
    def __init__(self, db_path, key, runtime, provider, cert_path, key_path,
                 public_origin, port, config_service, harness_summary):
        super().__init__(db_path, key, runtime, provider, cert_path,
                         key_path, public_origin, port)
        self.config_service = config_service
        self.harness_summary = harness_summary

    def start(self) -> "Task12Host":
        setup = Store.connect(self.db_path)
        try:
            app = create_app(
                store=setup, auth=AuthService(setup),
                config=ControlConfig(public_origin=self.public_origin),
                config_service=self.config_service,
                runtime=self.runtime, frontend_dir=base.DIST,
                relay_factory=self._factory,
                harness_summary=self.harness_summary)
        finally:
            setup.close()
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning",
            log_config=None, access_log=False, proxy_headers=False,
            ssl_certfile=str(self.cert_path),
            ssl_keyfile=str(self.key_path))
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(
            target=self.server.run, name="control-task12", daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if not self.thread.is_alive():
                raise AssertionError("control 启动失败（线程退出）")
            if time.monotonic() > deadline:
                raise AssertionError("control 启动超时")
            time.sleep(0.05)
        return self


def seed_control(db_path: Path):
    store = Store.connect(db_path)
    try:
        auth = AuthService(store)
        admin, admin_invite = auth.bootstrap_admin("Admin")
        members = {"A": store.create_user("A"), "B": store.create_user("B")}
        invites = {"admin1": admin_invite,
                   "admin2": auth.create_invite(admin, admin.user_id),
                   "admin3": auth.create_invite(admin, admin.user_id),
                   "admin4": auth.create_invite(admin, admin.user_id),
                   "admin5": auth.create_invite(admin, admin.user_id)}
        for name, count in (("A", 8), ("B", 3)):
            invites[name] = [auth.create_invite(
                admin, members[name].user_id) for _ in range(count)]
        config = ConfigService(store, MASTER_KEY)
        config.publish(admin, {"defaults": {"speech": admin_route()}}, 0)
        return admin, members, invites, config
    finally:
        store.close()


def save_personal(db_path: Path, identity, route: dict) -> None:
    store = Store.connect(db_path)
    try:
        ConfigService(store, MASTER_KEY).save_override(
            identity, "speech", route)
    finally:
        store.close()


def seed_bulk(db_path: Path, admin):
    """在分页验收前写入 55 个成员与 56 个邀请（含最旧待兑换邀请）。"""
    store = Store.connect(db_path)
    try:
        auth = AuthService(store)
        members = []
        for index in range(55):
            name = "末尾成员" if index == 54 else f"分页成员{index:02d}"
            members.append(store.create_user(name))
        old_id, old_token, _expires = auth.issue_invite(
            admin, members[0].user_id)
        for _ in range(55):
            auth.create_invite(admin, members[0].user_id)
        return members[-1], old_id, old_token
    finally:
        store.close()


def read_override_blob(db_path: Path, workspace_id: str,
                       channel: str) -> bytes:
    store = Store.connect(db_path)
    try:
        row = store.get_workspace_override(workspace_id, channel)
        assert row is not None and row["encrypted_secrets"] is not None
        return bytes(row["encrypted_secrets"])
    finally:
        store.close()


def invite_user_id(db_path: Path, token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    store = Store.connect(db_path)
    try:
        row = store._connection.execute(
            "SELECT user_id FROM invites WHERE token_digest = ?",
            (digest,)).fetchone()
        assert row is not None
        return str(row["user_id"])
    finally:
        store.close()



async def login_app(browser, origin: str, invite: str):
    context = await browser.new_context(
        base_url=origin, ignore_https_errors=False)
    page = await context.new_page()
    await page.goto("/", wait_until="domcontentloaded")
    try:
        await base.login(page, invite)
    except Exception as error:  # noqa: BLE001
        body = await page_excerpt(page)
        await context.close()
        raise AssertionError(f"登录失败：{error!r} BODY={body}") from error
    return context, page


async def open_personal(page) -> None:
    await page.get_by_role("button", name="个人设置", exact=True).click()
    await page.get_by_role("heading", name="个人设置").wait_for(timeout=20000)


async def open_admin(page) -> None:
    await page.get_by_role("button", name="全体默认", exact=True).click()
    await page.get_by_role("heading", name="全体默认").wait_for(timeout=20000)


async def wait_text(page, pattern: str, timeout: float = 30000) -> None:
    await page.get_by_text(re.compile(pattern)).first.wait_for(timeout=timeout)


async def cookie_header(context, origin: str) -> str:
    cookies = await context.cookies(origin)
    return "; ".join(f"{item['name']}={item['value']}" for item in cookies)


def simple_headers(response: httpx.Response) -> dict:
    return {
        "content-type": response.headers.get(
            "content-type", "application/json"),
        "cache-control": "no-store",
    }


async def page_excerpt(page) -> str:
    if page is None:
        return ""
    try:
        return (await page.locator("body").inner_text())[:600]
    except Exception:  # noqa: BLE001
        return "<unavailable>"


async def settings_channel(capture, context, origin: str,
                           channel: str = "speech") -> dict:
    cookie = await cookie_header(context, origin)
    response = await capture.get(
        f"{origin}/api/settings/models", headers={"Cookie": cookie})
    assert response.status_code == 200, response.text
    return response.json()["channels"][channel]


async def fill(page, label: str, value: str) -> None:
    await page.get_by_label(label, exact=True).fill(value)


def field(page, label: str):
    # 精确匹配，避免“模型”命中“个人能力模型”区域
    return page.get_by_label(label, exact=True)


async def scenario_personal(browser, origin, invites, results, failures,
                            screenshots) -> None:
    context_a = page_a = context_b = page_b = None
    try:
        context_a, page_a = await login_app(browser, origin, invites["A"][0])
        await open_personal(page_a)
        await wait_text(page_a, "管理员默认（inherited · v1）")
        await fill(page_a, "供应商", "openai-compatible")
        await fill(page_a, "模型", "personal-a")
        await fill(page_a, "API 地址", "https://personal-a.example/v1")
        await fill(page_a, "协议", "openai")
        await fill(page_a, "API Key", PERSONAL_A_SECRET)
        await page_a.get_by_role(
            "button", name="保存个人配置").click()
        await wait_text(page_a, "已保存个人配置")
        await wait_text(page_a, "个人配置（complete · v1）")
        body = await page_a.locator("body").inner_text()
        assert PERSONAL_A_SECRET not in body, body
        assert ADMIN_SECRET not in body, body
        assert await field(page_a, "API 地址").input_value() == \
            "https://personal-a.example/v1"
        path = SCREENSHOTS / "t12-personal-settings.png"
        await page_a.screenshot(path=str(path))
        screenshots.append(str(path))

        context_b, page_b = await login_app(browser, origin, invites["B"][0])
        await open_personal(page_b)
        await wait_text(page_b, "管理员默认（inherited · v1）")
        assert await field(page_b, "模型").input_value() == "admin-tts"
        body_b = await page_b.locator("body").inner_text()
        assert "personal-a" not in body_b, body_b
        assert "personal-a.example" not in body_b, body_b

        await page_a.get_by_role(
            "button", name="恢复继承管理员默认").click()
        await wait_text(page_a, "已恢复继承管理员默认")
        await wait_text(page_a, "管理员默认（inherited · v1）")
        assert "personal-a.example" not in await page_a.content()
        results["t12_personal_override_restore_and_isolation"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(
            f"T12-个人覆盖: {error!r} BODY={await page_excerpt(page_a or page_b)}")
    finally:
        for context in (context_a, context_b):
            if context is not None:
                await context.close()


async def scenario_admin_defaults(browser, origin, invites, results,
                                  failures, screenshots) -> None:
    contexts: list = []
    page1 = page2 = page3 = None
    try:
        context1, page1 = await login_app(browser, origin, invites["admin1"])
        context2, page2 = await login_app(browser, origin, invites["admin2"])
        contexts.extend([context1, context2])
        await open_admin(page1)
        await open_admin(page2)
        await wait_text(page1, "受管默认模型 · 版本 1")
        await wait_text(page2, "受管默认模型 · 版本 1")

        await fill(page1, "模型", "admin-v2")
        await page1.get_by_role("button", name="保存全体默认").click()
        await wait_text(page1, "全体默认已保存为新版本")
        await wait_text(page1, "受管默认模型 · 版本 2")
        await wait_text(page1, "openai-compatible/admin-v2")
        path = SCREENSHOTS / "t12-admin-defaults.png"
        await page1.screenshot(path=str(path))
        screenshots.append(str(path))

        # 第二个管理员重新读取到版本 2，再与第一个管理员竞争
        await page2.get_by_role("button", name="重新读取").click()
        await wait_text(page2, "受管默认模型 · 版本 2")
        await fill(page1, "模型", "admin-v3")
        await page1.get_by_role("button", name="保存全体默认").click()
        await wait_text(page1, "受管默认模型 · 版本 3")

        await fill(page2, "模型", "admin-conflict")
        await fill(page2, "API Key", CONFLICT_SECRET)
        await page2.get_by_role("button", name="保存全体默认").click()
        await wait_text(page2, "版本冲突")
        assert await field(page2, "模型").input_value() == \
            "admin-conflict"
        assert await field(page2, "API Key").input_value() == \
            CONFLICT_SECRET
        path = SCREENSHOTS / "t12-admin-conflict.png"
        await page2.screenshot(path=str(path),
                              mask=[field(page2, "API Key")])
        screenshots.append(str(path))

        # 成员看到最新默认，而不是冲突者/旧版本
        context3, page3 = await login_app(browser, origin, invites["A"][1])
        contexts.append(context3)
        await open_personal(page3)
        await wait_text(page3, "openai-compatible/admin-v3")
        assert "admin-conflict" not in await page3.content()
        results["t12_admin_default_update_and_conflict"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(
            f"T12-管理员默认: {error!r} BODY={await page_excerpt(page3 or page1)}")
    finally:
        for context in contexts:
            await context.close()


async def scenario_members_and_invites(browser, origin, invites, db_path,
                                       results, failures,
                                       screenshots) -> None:
    contexts: list = []
    page_admin = member_page = invalid_page = None
    try:
        context_admin, page_admin = await login_app(
            browser, origin, invites["admin3"])
        contexts.append(context_admin)
        await open_admin(page_admin)
        await wait_text(page_admin, "成员与邀请")

        await fill(page_admin, "新成员显示名", "浏览器成员")
        await page_admin.get_by_role("button", name="创建成员").click()
        await wait_text(page_admin, "已创建成员：浏览器成员")

        async def issue_token() -> str:
            await field(page_admin, "邀请成员").select_option(
                label="浏览器成员")
            await page_admin.get_by_role("button", name="创建邀请").click()
            section = page_admin.locator(
                'section[aria-label="邀请签发结果"]')
            await section.wait_for(timeout=20000)
            token = await section.locator("code").text_content()
            assert token
            return token.strip()

        token1 = await issue_token()
        await page_admin.get_by_role(
            "button", name="关闭邀请结果").click()
        token2 = await issue_token()
        await page_admin.get_by_role(
            "button", name="关闭邀请结果").click()

        # 离开管理页再回来：邀请明文不可重建
        await page_admin.get_by_role("button", name="对话", exact=True).click()
        await open_admin(page_admin)
        await wait_text(page_admin, "浏览器成员")
        body = await page_admin.locator("body").inner_text()
        assert token1 not in body and token2 not in body, body
        assert await page_admin.locator(
            'section[aria-label="邀请签发结果"]').count() == 0
        path = SCREENSHOTS / "t12-admin-invites.png"
        await page_admin.screenshot(path=str(path))
        screenshots.append(str(path))

        member_context, member_page = await login_app(browser, origin, token1)
        contexts.append(member_context)
        await member_page.get_by_role(
            "button", name=re.compile("新建会话")).click()
        await member_page.locator("textarea").wait_for(timeout=20000)
        await member_page.get_by_role(
            "button", name="新对话", exact=True).wait_for(timeout=20000)

        await page_admin.get_by_role(
            "button", name="禁用 浏览器成员").click()
        await wait_text(page_admin, "已禁用 浏览器成员")
        await member_page.reload(wait_until="domcontentloaded")
        await member_page.wait_for_selector("#invite", timeout=20000)

        # 未兑换邀请随禁用失效，重新启用也不复活
        invalid_context = await browser.new_context(
            base_url=origin, ignore_https_errors=False)
        contexts.append(invalid_context)
        invalid_page = await invalid_context.new_page()
        await invalid_page.goto("/", wait_until="domcontentloaded")
        await invalid_page.wait_for_selector("#invite", timeout=20000)

        async def try_invite(token: str) -> None:
            await invalid_page.fill("#invite", token)
            await invalid_page.get_by_role(
                "button", name="进入工作区").click()
            await invalid_page.get_by_role("alert").wait_for(timeout=20000)
            assert await invalid_page.locator("#invite").count() == 1

        await try_invite(token2)
        await page_admin.get_by_role(
            "button", name="启用 浏览器成员").click()
        await wait_text(page_admin, "已启用 浏览器成员")
        await try_invite(token2)

        token3 = await issue_token()
        await page_admin.get_by_role(
            "button", name="关闭邀请结果").click()
        fresh_context, fresh_page = await login_app(
            browser, origin, token3)
        contexts.append(fresh_context)
        await fresh_page.get_by_role(
            "button", name="新对话", exact=True).wait_for(timeout=20000)

        await page_admin.get_by_role(
            "button", name="撤销登录 浏览器成员").click()
        await wait_text(page_admin, "已撤销")
        await fresh_page.reload(wait_until="domcontentloaded")
        await fresh_page.wait_for_selector("#invite", timeout=20000)
        results["t12_invite_once_disable_reenable_revoke"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(
            f"T12-成员邀请: {error!r} BODY="
            f"{await page_excerpt(member_page or invalid_page or page_admin)}")
    finally:
        for context in contexts:
            try:
                await context.close()
            except Exception:  # noqa: BLE001
                pass


async def scenario_chat_state(browser, origin, invites, results, failures,
                              screenshots) -> None:
    context = page = None
    try:
        context, page = await login_app(browser, origin, invites["A"][2])
        chat_posts: list[str] = []
        page.on("request", lambda request: chat_posts.append(request.url)
                if request.method == "POST"
                and request.url.rstrip("/").endswith("/api/chat") else None)
        await page.get_by_role(
            "button", name=re.compile("新建会话")).click()
        await page.locator("textarea").wait_for(timeout=20000)
        await page.locator("textarea").fill("权限检查")
        await page.get_by_role("button", name="发送").click()
        await page.get_by_role(
            "button", name="提交回答").wait_for(timeout=60000)
        assert len(chat_posts) == 1, chat_posts
        assert await page.locator(
            'article:has-text("发送中")').count() >= 1

        await open_personal(page)
        await field(page, "API Key").wait_for(timeout=20000)
        await field(page, "API Key").fill(CHAT_SWITCH_SECRET)
        await page.get_by_role("button", name="对话", exact=True).click()
        await page.get_by_role(
            "button", name="提交回答").wait_for(timeout=20000)
        assert len(chat_posts) == 1, chat_posts
        assert await page.locator(
            'article:has-text("发送中")').count() >= 1
        body = await page.locator("body").inner_text()
        assert CHAT_SWITCH_SECRET not in body, body
        path = SCREENSHOTS / "t12-chat-question-preserved.png"
        await page.screenshot(path=str(path))
        screenshots.append(str(path))

        await page.get_by_role("radio", name=re.compile("Allow")).check()
        await page.get_by_role("button", name="提交回答").click()
        await base.wait_assistant(page, "你好")
        assert len(chat_posts) == 1, chat_posts
        results["t12_chat_state_survives_settings_navigation"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(
            f"T12-聊天保持: {error!r} BODY={await page_excerpt(page)}")
    finally:
        if context is not None:
            await context.close()


async def scenario_stale_identity(browser, origin, invites, db, members,
                                  capture, results, failures) -> None:
    context_b = page_b = context_a = page_a = None
    try:
        save_personal(db, members["B"], personal_route(
            "b-personal-model", "https://b-personal.example/v1",
            B_PERSONAL_SECRET))

        # 旧 GET：B 的初次设置快照在切到 A 之后才交付
        context_b, page_b = await login_app(browser, origin, invites["B"][1])
        captured = asyncio.Event()
        gate = asyncio.Event()
        state = {"used": False, "body": b"", "status": 0, "headers": {}}

        async def hold_models(route):
            if state["used"]:
                await route.continue_()
                return
            state["used"] = True
            try:
                cookie = await cookie_header(context_b, origin)
                response = await capture.get(
                    route.request.url, headers={"Cookie": cookie})
                state["body"] = response.content
                state["status"] = response.status_code
                state["headers"] = simple_headers(response)
            except Exception as error:  # noqa: BLE001
                state["body"] = json.dumps(
                    {"error": repr(error)}).encode("utf-8")
                state["status"] = 500
                state["headers"] = {"content-type": "application/json"}
            captured.set()
            await asyncio.wait_for(gate.wait(), timeout=60)
            try:
                await route.fulfill(
                    status=state["status"], headers=state["headers"],
                    body=state["body"])
            except Exception:  # noqa: BLE001
                pass

        await page_b.route(re.compile(r"/api/settings/models$"), hold_models)
        await open_personal(page_b)
        await asyncio.wait_for(captured.wait(), timeout=30)
        await page_b.get_by_role("button", name="退出登录").click()
        await page_b.wait_for_selector("#invite", timeout=20000)
        await base.login(page_b, invites["A"][3])
        await open_personal(page_b)
        await wait_text(page_b, "管理员默认（inherited · v[0-9]+）")
        gate.set()
        await asyncio.sleep(0.5)
        body = await page_b.locator("body").inner_text()
        assert "b-personal-model" not in body, body
        assert "b-personal.example" not in body, body

        # 旧保存：B 的 save 已由服务端处理，响应在切到 A 后才交付
        context_a, page_a = await login_app(browser, origin, invites["B"][2])
        await open_personal(page_a)
        await wait_text(page_a, "个人配置（complete · v1）")
        await fill(page_a, "模型", "b-stale-save-model")
        save_captured = asyncio.Event()
        save_gate = asyncio.Event()
        save_state = {"used": False, "status": 0, "body": b"",
                      "headers": {}}

        async def hold_save(route):
            if save_state["used"]:
                await route.continue_()
                return
            save_state["used"] = True
            try:
                cookie = await cookie_header(context_a, origin)
                response = await capture.post(
                    route.request.url,
                    content=route.request.post_data.encode("utf-8"),
                    headers={
                        "Cookie": cookie,
                        "Origin": origin,
                        "Content-Type": "application/json",
                        "X-CSRF-Token": route.request.headers.get(
                            "x-csrf-token", ""),
                    })
                save_state["body"] = response.content
                save_state["status"] = response.status_code
                save_state["headers"] = simple_headers(response)
            except Exception as error:  # noqa: BLE001
                save_state["body"] = json.dumps(
                    {"error": repr(error)}).encode("utf-8")
                save_state["status"] = 500
                save_state["headers"] = {"content-type": "application/json"}
            save_captured.set()
            await asyncio.wait_for(save_gate.wait(), timeout=60)
            try:
                await route.fulfill(
                    status=save_state["status"],
                    headers=save_state["headers"],
                    body=save_state["body"])
            except Exception:  # noqa: BLE001
                pass

        await page_a.route(
            re.compile(r"/api/settings/models/save$"), hold_save)
        await page_a.get_by_role("button", name="保存个人配置").click()
        await asyncio.wait_for(save_captured.wait(), timeout=30)
        assert save_state["status"] == 200, save_state
        await page_a.get_by_role("button", name="退出登录").click()
        await page_a.wait_for_selector("#invite", timeout=20000)
        await base.login(page_a, invites["A"][4])
        await open_personal(page_a)
        await wait_text(page_a, "管理员默认（inherited · v[0-9]+）")
        save_gate.set()
        await asyncio.sleep(0.5)
        body = await page_a.locator("body").inner_text()
        assert "b-stale-save-model" not in body, body
        assert "已保存个人配置" not in body, body
        results["t12_stale_identity_settings_do_not_fill_b"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(
            f"T12-身份切换: {error!r} BODY="
            f"{await page_excerpt(page_a or page_b)}")
    finally:
        for context in (context_b, context_a):
            if context is not None:
                await context.close()


async def scenario_managed_model_precision(browser, origin, invites, capture,
                                           results, failures,
                                           screenshots) -> None:
    contexts: list = []
    page_admin = page_member = None
    try:
        context_admin, page_admin = await login_app(
            browser, origin, invites["admin4"])
        context_member, page_member = await login_app(
            browser, origin, invites["A"][5])
        contexts.extend([context_admin, context_member])
        await open_admin(page_admin)
        await wait_text(page_admin, "受管默认模型 · 版本")
        await fill(page_admin, "模型", "admin-v4")
        await fill(page_admin, "非敏感参数（JSON）",
                   '{"speed": 1.3, "voice": "admin"}')
        await fill(page_admin, "允许的模型（逗号分隔）",
                   "admin-v4, admin-v5, admin-v6")
        await page_admin.get_by_role("button", name="保存全体默认").click()
        await wait_text(page_admin, "全体默认已保存为新版本")
        await wait_text(page_admin, "openai-compatible/admin-v4")

        await open_personal(page_member)
        await wait_text(page_member, "管理员默认（inherited · v[0-9]+）")
        await field(page_member, "个人配置方式").select_option(
            "managed-model")
        tree_model = await field(page_member, "受管模型").input_value()
        assert tree_model == "", tree_model
        assert await field(
            page_member, "非敏感参数（JSON）").input_value() == ""
        await fill(page_member, "非敏感参数（JSON）", '{"speed": 1.4}')
        await page_member.get_by_role(
            "button", name="保存个人配置").click()
        await wait_text(page_member, "个人配置（managed-model · v[0-9]+）")

        await page_member.get_by_role(
            "button", name="重新读取").click()
        await wait_text(page_member, "个人配置（managed-model · v[0-9]+）")
        assert await field(page_member, "受管模型").input_value() == ""
        assert json.loads(await field(
            page_member, "非敏感参数（JSON）").input_value()) == {
                "speed": 1.4}
        speech = await settings_channel(capture, context_member, origin)
        assert speech["model"] == "admin-v4"
        assert speech["personal_model"] is None
        assert speech["personal_options"] == {"speed": 1.4}
        assert speech["options"] == {"speed": 1.4, "voice": "admin"}

        # 原样保存：删除原先会把生效 model 固化成个人值
        await page_member.get_by_role(
            "button", name="保存个人配置").click()
        await wait_text(page_member, "已保存个人配置")
        speech = await settings_channel(capture, context_member, origin)
        assert speech["personal_model"] is None
        assert speech["personal_options"] == {"speed": 1.4}

        # 管理员更新默认：跟随新 model，个人 speed 继续覆盖
        await fill(page_admin, "模型", "admin-v5")
        await fill(page_admin, "非敏感参数（JSON）",
                   '{"speed": 1.8, "voice": "nova"}')
        await page_admin.get_by_role("button", name="保存全体默认").click()
        await wait_text(page_admin, "openai-compatible/admin-v5")
        await page_member.get_by_role("button", name="重新读取").click()
        await wait_text(page_member, "openai-compatible/admin-v5")
        speech = await settings_channel(capture, context_member, origin)
        assert speech["model"] == "admin-v5"
        assert speech["personal_model"] is None
        assert speech["personal_options"] == {"speed": 1.4}
        assert speech["options"]["speed"] == 1.4
        assert speech["options"]["voice"] == "nova"

        # 显式固定 model，且显式 voice 与当时默认相同也不得视为未覆盖
        await field(page_member, "受管模型").select_option("admin-v5")
        await fill(page_member, "非敏感参数（JSON）", '{"voice": "nova"}')
        await page_member.get_by_role(
            "button", name="保存个人配置").click()
        await wait_text(page_member, "已保存个人配置")
        speech = await settings_channel(capture, context_member, origin)
        assert speech["personal_model"] == "admin-v5"
        assert speech["personal_options"] == {"voice": "nova"}

        await fill(page_admin, "模型", "admin-v6")
        await fill(page_admin, "非敏感参数（JSON）",
                   '{"voice": "other", "speed": 2.0}')
        await page_admin.get_by_role("button", name="保存全体默认").click()
        await wait_text(page_admin, "openai-compatible/admin-v6")
        await page_member.get_by_role("button", name="重新读取").click()
        await wait_text(page_member, "openai-compatible/admin-v5")
        speech = await settings_channel(capture, context_member, origin)
        assert speech["model"] == "admin-v5"
        assert speech["personal_model"] == "admin-v5"
        assert speech["personal_options"] == {"voice": "nova"}
        assert speech["options"] == {"voice": "nova", "speed": 2.0}
        path = SCREENSHOTS / "t12-managed-model-follow.png"
        await page_member.screenshot(path=str(path))
        screenshots.append(str(path))
        results["t12_managed_model_follow_precision"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(
            f"T12-受管模型原值: {error!r} BODY="
            f"{await page_excerpt(page_member or page_admin)}")
    finally:
        for context in contexts:
            await context.close()


async def scenario_multifield_credentials(browser, origin, invites, db_path,
                                          members, capture, results,
                                          failures, screenshots) -> None:
    context = page = None
    try:
        context, page = await login_app(browser, origin, invites["A"][6])
        save_posts: list[str] = []
        page.on("request", lambda request: save_posts.append(request.url)
                if request.method == "POST"
                and request.url.endswith("/api/settings/models/save") else None)
        await open_personal(page)
        await field(page, "个人配置方式").select_option("complete")
        await fill(page, "供应商", "minimax")
        await fill(page, "模型", "speech-01")
        await fill(page, "API 地址", "https://minimax.example/v1")
        await fill(page, "协议", "minimax")
        await field(page, "凭证 API Key").wait_for(timeout=20000)
        await fill(page, "凭证 API Key", "task12-minimax-key-sentinel")
        await fill(page, "凭证 Group ID", "task12-minimax-group-sentinel")
        await page.get_by_role("button", name="保存个人配置").click()
        await wait_text(page, "个人配置（complete · v[0-9]+）")
        blob = read_override_blob(
            db_path, members["A"].workspace_id, "speech")
        body = await page.locator("body").inner_text()
        assert "task12-minimax-key-sentinel" not in body, body
        assert "task12-minimax-group-sentinel" not in body, body
        speech = await settings_channel(capture, context, origin)
        assert speech["source"] == "user"
        assert speech["mode"] == "complete"
        assert speech["provider"] == "minimax"
        assert speech["broker_supported"] is False

        # 重新打开：凭证字段不得从服务端读回
        await page.get_by_role("button", name="重新读取").click()
        await wait_text(page, "个人配置（complete · v[0-9]+）")
        assert await field(page, "凭证 API Key").input_value() == ""
        assert await field(page, "凭证 Group ID").input_value() == ""

        # 全部留空：同作用域复用，密文逐字节不变
        await page.get_by_role("button", name="保存个人配置").click()
        await wait_text(page, "已保存个人配置")
        assert read_override_blob(
            db_path, members["A"].workspace_id, "speech") == blob

        # 只填一个必需字段：本地拒绝，不发请求
        before_posts = len(save_posts)
        await fill(page, "凭证 API Key", "partial-key")
        await page.get_by_role("button", name="保存个人配置").click()
        await wait_text(page, "请填写全部必需凭证")
        assert len(save_posts) == before_posts, save_posts
        assert read_override_blob(
            db_path, members["A"].workspace_id, "speech") == blob

        # 合法全量轮换
        await fill(page, "凭证 API Key", "task12-minimax-key-rotated")
        await fill(page, "凭证 Group ID", "task12-minimax-group-rotated")
        await page.get_by_role("button", name="保存个人配置").click()
        await wait_text(page, "已保存个人配置")
        rotated = read_override_blob(
            db_path, members["A"].workspace_id, "speech")
        assert rotated != blob
        path = SCREENSHOTS / "t12-multifield-credentials.png"
        await page.screenshot(path=str(path))
        screenshots.append(str(path))

        # 换目的地址但全部留空：明确拒绝，密文不变
        await fill(page, "API 地址", "https://minimax-other.example/v1")
        await page.get_by_role("button", name="保存个人配置").click()
        await wait_text(page, "保存失败")
        assert read_override_blob(
            db_path, members["A"].workspace_id, "speech") == rotated
        body = await page.locator("body").inner_text()
        assert "task12-minimax-key" not in body, body
        assert "task12-minimax-group" not in body, body
        results["t12_personal_multifield_credentials"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(
            f"T12-多字段凭证: {error!r} BODY={await page_excerpt(page)}")
    finally:
        if context is not None:
            await context.close()


async def scenario_pagination(browser, origin, invites, db_path, admin,
                              results, failures, screenshots) -> None:
    context = page = None
    try:
        last_member, old_invite_id, old_token = seed_bulk(db_path, admin)
        assert last_member.user_id
        context, page = await login_app(
            browser, origin, invites["admin5"])
        await open_admin(page)
        await wait_text(page, "成员与邀请")
        await wait_text(page, "分页成员00")
        assert await page.get_by_role(
            "button", name="加载更多成员").count() == 1
        assert await page.get_by_role(
            "button", name="加载更多邀请").count() == 1

        await page.get_by_role("button", name="加载更多成员").click()
        await wait_text(page, "末尾成员")
        await page.get_by_role("button", name="禁用 末尾成员").click()
        await wait_text(page, "已禁用 末尾成员")
        await page.get_by_role("button", name="启用 末尾成员").click()
        await wait_text(page, "已启用 末尾成员")
        await page.get_by_role("button", name="撤销登录 末尾成员").click()
        await wait_text(page, "已撤销")

        await field(page, "邀请成员").select_option(label="末尾成员")
        # 重新读取会把成员列表退回第一页；已选目标不能被悄悄替换
        await page.get_by_role("button", name="重新读取").first.click()
        await wait_text(page, "分页成员00")
        await page.get_by_role("button", name="创建邀请").click()
        section = page.locator('section[aria-label="邀请签发结果"]')
        await section.wait_for(timeout=20000)
        token = (await section.locator("code").text_content() or "").strip()
        assert token
        assert invite_user_id(db_path, token) == last_member.user_id
        await page.get_by_role("button", name="关闭邀请结果").click()

        await page.get_by_role("button", name="加载更多邀请").click()
        await page.get_by_role(
            "button", name=f"撤销邀请 {old_invite_id}").click()
        await wait_text(page, "邀请已撤销")
        body = await page.locator("body").inner_text()
        assert old_token not in body, body
        path = SCREENSHOTS / "t12-pagination.png"
        await page.screenshot(path=str(path))
        screenshots.append(str(path))
        results["t12_pagination_reaches_later_rows"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(
            f"T12-分页管理: {error!r} BODY={await page_excerpt(page)}")
    finally:
        if context is not None:
            await context.close()


async def main() -> int:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    tmp_path = Path(tempfile.mkdtemp(prefix="easel-task12-"))
    db_path = tmp_path / "control.sqlite3"
    admin, members, invites, config_service = seed_control(db_path)
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
    ws_a = members["A"].workspace_id
    worker = tcp.Worker(
        tmp_path, "a", ws_a, "permission", "a", "service-task12",
        public_key_path, launcher_path).start()
    runtime = tcp.StaticRuntime({
        ws_a: WorkerHandle(ws_a, "container-task12", worker.base,
                           "service-task12", REVISION),
    })
    provider = tcp.make_provider(
        {ws_a: tcp.TokenMint()}, {ws_a: worker.command})
    key_path, cert_path, pin = base.make_certificate(tmp_path)
    port = tcp.free_port()
    origin = f"https://localhost:{port}"
    host = Task12Host(
        db_path, key, runtime, provider, cert_path, key_path, origin, port,
        config_service,
        {"runtime": "acp", "revision": REVISION,
         "model": "managed-chat", "sandbox": "read-only", "timeout": 60,
         "codexAutoApprove": False}).start()

    results: dict[str, bool] = {}
    failures: list[str] = []
    screenshots: list[str] = []
    browser = None
    version = ""
    capture = httpx.AsyncClient(verify=False, trust_env=False, timeout=20)
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=[f"--ignore-certificate-errors-spki-list={pin}"])
            version = browser.version
            for name, runner in (
                ("personal", lambda: scenario_personal(
                    browser, origin, invites, results, failures, screenshots)),
                ("admin-defaults", lambda: scenario_admin_defaults(
                    browser, origin, invites, results, failures, screenshots)),
                ("members-invites", lambda: scenario_members_and_invites(
                    browser, origin, invites, db_path, results, failures,
                    screenshots)),
                ("chat-state", lambda: scenario_chat_state(
                    browser, origin, invites, results, failures, screenshots)),
                ("stale-identity", lambda: scenario_stale_identity(
                    browser, origin, invites, db_path, members, capture,
                    results, failures)),
                ("managed-model", lambda: scenario_managed_model_precision(
                    browser, origin, invites, capture, results, failures,
                    screenshots)),
                ("multifield", lambda: scenario_multifield_credentials(
                    browser, origin, invites, db_path, members, capture,
                    results, failures, screenshots)),
                ("pagination", lambda: scenario_pagination(
                    browser, origin, invites, db_path, admin, results,
                    failures, screenshots)),
            ):
                only = {item.strip() for item in
                        os.environ.get("EASEL_TASK12_ONLY", "").split(",")
                        if item.strip()}
                if only and name not in only:
                    continue
                print("SCENARIO_START", name, flush=True)
                await runner()
                print("SCENARIO_DONE", name,
                      json.dumps(failures, ensure_ascii=False), flush=True)
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
    import shutil as _shutil
    _shutil.rmtree(tmp_path, ignore_errors=True)
    print("TMP_CLEANED", not tmp_path.exists())
    if failures or len(results) != 8 or not all(results.values()):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
