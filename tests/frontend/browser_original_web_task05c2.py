"""Task 5C2 浏览器矩阵：真实 control/gateway/worker + 受控平台替身。

真实层：原 React 构建、HTTPS control、gateway、WorkspaceApi、WorkerRelay、
worker RunManager（默认 browser executor）。
替身层：原登录/分析/whoami/凭证入口只写本人临时产物，不访问真实平台、
不启动真实浏览器；页面必须从这些产物/状态读取结果。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import ipaddress
import json
import shutil
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

import httpx
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DIST = ROOT / "web" / "frontend" / "dist"
# R1 的浏览器证据写入本组子目录，不覆盖首轮已提交的历史截图。
EVIDENCE = (ROOT / "docs" / "evidence" / "original-web-task-05c2"
            / "r1" / "browser")
SHOTS: list[Path] = []
FAILURES: list[str] = []
RESULTS: dict[str, bool] = {}

PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8"
    b"\xff\xff?\x00\x05\xfe\x02\xfe\xa75\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82")
AVATAR = ("data:image/png;base64,"
          "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
          "/x8AAusB9Y9Z3ZkAAAAASUVORK5CYII=")


def _tls_files(root: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(issuer)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256()))
    key_path = root / "tls-key.pem"
    cert_path = root / "tls-cert.pem"
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


class FakePlatform:
    def __init__(self, login_dir: Path, analytics_dir: Path) -> None:
        self.login_dir = login_dir
        self.analytics_dir = analytics_dir
        self.scanned = False
        self.code = ""
        self.mp_scanned = False
        self.login_starts: list[str] = []
        self.mp_login_starts: list[str] = []
        self.whoami_calls: list[str] = []
        self.analytics_calls: list[str] = []
        self.control_status_503_once = False
        self.analytics_fail = False
        self.name = "受控测试昵称"
        self.avatar = AVATAR

    def _write(self, platform: str, state: str, message: str = "",
               qr: str = "") -> dict:
        self.login_dir.mkdir(parents=True, exist_ok=True)
        data = {"state": state, "message": message, "qr": qr,
                "ts": int(time.time())}
        (self.login_dir / f"{platform}.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return {"state": state, "message": message, "qr": qr,
                "qrTs": int(time.time() * 1000)}

    async def start_login(self, platform: str, restart: bool = False) -> dict:
        self.login_starts.append(platform)
        # 每个新登录从干净扫码状态开始；测试再显式置 scanned/短信。
        self.scanned = False
        self.code = ""
        (self.login_dir / f"{platform}.png").parent.mkdir(
            parents=True, exist_ok=True)
        (self.login_dir / f"{platform}.png").write_bytes(PNG_1PX)
        self._write(platform, "qr_ready", "请扫码",
                    f"_login/{platform}.png")
        return {"mode": "qr", "state": "starting"}

    def login_status(self, platform: str) -> dict:
        path = self.login_dir / f"{platform}.json"
        data = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                data = {}
        state = data.get("state", "unknown")
        if state in ("success", "expired", "error"):
            return self._view(platform, data)
        code_file = self.login_dir / f"{platform}.code"
        if code_file.is_file():
            try:
                self.code = code_file.read_text(encoding="utf-8").strip()
            except OSError:
                pass
        if self.scanned:
            if not self.code:
                return self._write(platform, "sms_required",
                                   "请输入短信验证码")
            if self.code != "123456":
                return self._write(platform, "sms_required",
                                   "验证码不正确")
            from web import app as worker
            worker._write_login_marker(platform, "success", self.name)
            (self.login_dir / f"{platform}.png").unlink(missing_ok=True)
            return self._write(platform, "success", self.name)
        return self._view(platform, data)

    def _view(self, platform: str, data: dict) -> dict:
        state = data.get("state", "unknown")
        qr = ""
        qr_ts = 0
        path = self.login_dir / f"{platform}.png"
        if state in ("qr_ready", "window_login") and path.is_file():
            qr = f"_login/{platform}.png"
            qr_ts = int(path.stat().st_mtime_ns // 1_000_000)
        return {"state": state, "message": data.get("message", ""),
                "qr": qr, "qrTs": qr_ts}

    async def mp_start(self, platform: str) -> dict:
        self.mp_login_starts.append(platform)
        self.login_dir.mkdir(parents=True, exist_ok=True)
        (self.login_dir / "wechat-oa-mp.png").write_bytes(PNG_1PX)
        (self.login_dir / "wechat-oa-mp.json").write_text(
            json.dumps({"state": "qr_ready", "message": "请扫码"},
                       ensure_ascii=False), encoding="utf-8")
        return {"mode": "qr", "state": "starting"}

    def mp_status(self) -> dict:
        if self.mp_scanned:
            from web import app as worker
            worker._write_login_marker("wechat-oa-mp", "success", self.name)
            (self.login_dir / "wechat-oa-mp.png").unlink(missing_ok=True)
            (self.login_dir / "wechat-oa-mp.json").write_text(
                json.dumps({"state": "success", "message": "登录成功"},
                           ensure_ascii=False), encoding="utf-8")
        path = self.login_dir / "wechat-oa-mp.json"
        data = {"state": "unknown", "message": ""}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                pass
        state = data.get("state", "unknown")
        qr = ""
        qr_ts = 0
        qr_path = self.login_dir / "wechat-oa-mp.png"
        if state in ("qr_ready", "window_login") and qr_path.is_file():
            qr = "_login/wechat-oa-mp.png"
            qr_ts = int(qr_path.stat().st_mtime_ns // 1_000_000)
        return {"state": state, "message": data.get("message", ""),
                "qr": qr, "qrTs": qr_ts}

    async def whoami(self, platform: str, force: bool = False) -> dict:
        self.whoami_calls.append(platform)
        from web import app as worker
        logged_in = worker._account_logged_in(
            platform, worker.LOGIN_RUNNERS[platform])
        result = {
            "loggedIn": logged_in,
            "name": self.name if logged_in else "",
            "avatar": self.avatar if logged_in else "",
        }
        (self.login_dir / f"{platform}-whoami.json").write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return result

    async def analytics(self, platform: str) -> dict:
        self.analytics_calls.append(platform)
        if self.analytics_fail:
            raise __import__("fastapi").HTTPException(502, "平台维护")
        payload = {
            "platform": platform, "name": "受控账号",
            "nickname": self.name, "loggedIn": True,
            "followers": 12, "likes": 34, "following": None, "posts": None,
            "metrics": [{"label": "近 7 日", "value": 5, "vs": "+1"}],
            "notes": [{"title": "受控笔记", "url": "https://example.test/1"}],
            "growth": {"week": {"followers": 2, "likes": 3, "posts": None,
                                "since_days": 7}},
            "fetched_at": 1700000000,
        }
        self.analytics_dir.mkdir(parents=True, exist_ok=True)
        (self.analytics_dir / f"{platform}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return payload


class Server:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.workspace_root = root / "workspace"
        for name in ("outputs", "profiles", "state", "home"):
            (self.workspace_root / name).mkdir(parents=True)
        from web import app as worker
        worker.OUTPUTS_DIR = self.workspace_root / "outputs"
        worker.PROFILES_DIR = self.workspace_root / "profiles"
        worker.LOGIN_DIR = worker.OUTPUTS_DIR / "_login"
        worker._SERVICE_MODE = "worker"
        from easel import persona
        persona.PROFILES_DIR = worker.PROFILES_DIR
        self.fake = FakePlatform(
            worker.LOGIN_DIR, worker.OUTPUTS_DIR / "_analytics")
        worker._start_login = self.fake.start_login
        worker._login_status = self.fake.login_status
        worker.api_mp_login_start = self.fake.mp_start
        worker._mp_login_status = self.fake.mp_status
        worker._check_account_whoami = self.fake.whoami
        worker._fetch_analytics = self.fake.analytics
        worker._wechat_verify_credentials = lambda app_id, secret: (True, "")
        worker._wechat_config_path = lambda: root / "wechat.yaml"
        self.worker_app = worker

        from easel.control.app import ControlConfig, create_app
        from easel.control.auth import AuthService
        from easel.control.gateway_client import GatewayClient
        from easel.control.proxy import PinnedRunConfig, WorkerRelay
        from easel.control.sessions import SessionService
        from easel.control.store import Store
        from easel.control.types import WorkerHandle, WorkspacePaths
        from easel.control.workspace_api import WorkspaceApi
        from easel.gateway.app import GatewayConfig, create_gateway_app
        from easel.worker.runs import RunManager
        from easel.worker.sessions import WorkspaceSessionStore
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        self.store = Store.connect(root / "control.sqlite3")
        self.admin = self.store.create_user("BrowserOwner", "admin")
        self.member = self.store.create_user("BrowserMember")
        self.auth = AuthService(self.store)
        self.invites = [
            self.auth.create_invite(self.admin, self.member.user_id)
            for _ in range(8)]
        self.key = Ed25519PrivateKey.generate()
        self.handle = WorkerHandle(
            self.member.workspace_id, f"{0x5C2:064x}",
            "http://127.0.0.1:7860", "service-browser-5c2", 0,
            gateway_instance_id="gateway-browser-5c2",
            gateway_generation="boot-browser-5c2",
            gateway_upstream="http://127.0.0.1:8899",
            gateway_state="available")
        now = int(time.time())
        outcome, lease = self.store.claim_worker_start(
            self.member.workspace_id, 0, "browser-owner", now, 3600)
        assert outcome == "started", outcome
        assert self.store.complete_worker_start(
            self.member.workspace_id, "browser-owner", lease.generation,
            self.handle, now, 3600)
        self.paths = WorkspacePaths(
            code_root=ROOT, workspace_root=self.workspace_root,
            state_root=self.workspace_root / "state",
            home=self.workspace_root / "home")
        from easel.worker.guard import WorkerIdentity
        worker._WORKER_IDENTITY = WorkerIdentity(
            self.member.workspace_id, self.handle.service_token)
        self.manager = RunManager(
            workspace_id=self.member.workspace_id, paths=self.paths,
            public_key=self.key.public_key(),
            sessions=WorkspaceSessionStore(
                self.paths.state_root, self.member.workspace_id),
            base_env={"PATH": "", "HOME": str(self.paths.home)},
            recover=False)
        worker._RUNS = self.manager
        self.worker_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=worker.app), trust_env=False)
        self.gateway_app = create_gateway_app(
            GatewayConfig(
                workspace_id=self.member.workspace_id,
                gateway_instance_id=self.handle.gateway_instance_id,
                boot_generation=self.handle.gateway_generation,
                worker_container_id=self.handle.container_id,
                config_version=0,
                worker_origin=self.handle.upstream,
                worker_service_token=self.handle.service_token),
            self.key.public_key(), activated=True,
            http_client=self.worker_client)
        self.gateway_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.gateway_app),
            trust_env=False)

        class Runtime:
            def __init__(self, handle):
                self.handle = handle

            async def ensure_ready(self, workspace_id, config_version):
                assert workspace_id == self.handle.workspace_id
                return self.handle

            async def bound_handle(self, workspace_id, container, revision):
                return self.handle

        self.runtime = Runtime(self.handle)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(128)
        self.port = self.sock.getsockname()[1]
        self.origin = f"https://127.0.0.1:{self.port}"
        self.app = create_app(
            store=self.store, auth=self.auth,
            config=ControlConfig(public_origin=self.origin),
            runtime=self.runtime, frontend_dir=DIST,
            workspace_factory=lambda runtime, store, client, db_runner:
                WorkspaceApi(runtime, store,
                             GatewayClient(self.key, self.gateway_client),
                             revision=0, db_runner=db_runner,
                             gateway_required=True),
            relay_factory=lambda runtime, sessions, auth, store, client,
                broker, db_runner: WorkerRelay(
                    runtime, sessions, auth, store=store,
                    signing_key=self.key,
                    config_provider=lambda actor: PinnedRunConfig(
                        revision=0, routes={}, agent={
                            "runtime": "acp",
                            "command": ["fixture-acp"], "timeout": 60,
                        }, broker=None),
                    http_client=self.gateway_client,
                    gateway_client=GatewayClient(
                        self.key, self.gateway_client),
                    db_runner=db_runner, gateway_required=True))
        cert, key = _tls_files(root)
        config = uvicorn.Config(
            self.app, host="127.0.0.1", port=0, log_level="warning",
            ssl_certfile=str(cert), ssl_keyfile=str(key),
            proxy_headers=False)
        self.uvicorn = uvicorn.Server(config)
        self.thread = threading.Thread(
            target=self.uvicorn.run, kwargs={"sockets": [self.sock]},
            daemon=True)

    def start(self) -> "Server":
        self.thread.start()
        deadline = time.monotonic() + 20
        while not self.uvicorn.started:
            if time.monotonic() > deadline:
                raise RuntimeError("control server did not start")
            time.sleep(0.02)
        return self

    async def stop(self) -> None:
        self.uvicorn.should_exit = True
        self.thread.join(timeout=20)
        try:
            self.sock.close()
        except OSError:
            pass
        try:
            await self.manager.aclose()
        except Exception:
            pass
        for client in (self.gateway_client, self.worker_client):
            try:
                await client.aclose()
            except Exception:
                pass
        try:
            self.store.close()
        except Exception:
            pass


async def wait_for(predicate, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if inspect.isawaitable(value):
            value = await value
        if value:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(message)


def active_runs(server: "Server") -> int:
    import sqlite3

    connection = sqlite3.connect(server.root / "control.sqlite3")
    try:
        return int(connection.execute(
            "SELECT COUNT(*) FROM runs WHERE workspace_id = ? AND status NOT IN"
            " ('done', 'failed', 'interrupted', 'cancelled')",
            (server.member.workspace_id,)).fetchone()[0])
    finally:
        connection.close()


async def text_contains(locator, needle: str) -> bool:
    return needle in (await locator.inner_text())


async def text_missing(locator, needle: str) -> bool:
    return needle not in (await locator.inner_text())


async def screenshot(page, name: str) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / name
    await page.screenshot(path=str(path), full_page=False)
    SHOTS.append(path)


async def login(page, server: Server) -> None:
    print("[5C2] login: goto", flush=True)
    invite = server.invites.pop(0)
    await page.goto(server.origin, wait_until="domcontentloaded", timeout=20000)
    print("[5C2] login: fill invite", flush=True)
    await page.fill("#invite", invite)
    await page.get_by_role("button", name="进入工作区").click()
    print("[5C2] login: wait sidebar", flush=True)
    await page.locator(".sidebar").wait_for(timeout=20000)
    await page.locator(".persona-select").wait_for(timeout=20000)
    print("[5C2] login: ready", flush=True)


async def open_accounts(page) -> None:
    print("[5C2] open accounts", flush=True)
    await page.locator(".nav-item", has_text="账号").click()
    await page.locator(".accounts-page").wait_for(timeout=20000)
    print("[5C2] accounts open", flush=True)


async def check(name: str, fn) -> None:
    print(f"[5C2] scenario start: {name}", flush=True)
    try:
        await asyncio.wait_for(fn(), timeout=150)
        RESULTS[name] = True
        print(f"[5C2] scenario ok: {name}", flush=True)
    except Exception:  # noqa: BLE001 - 汇总所有场景
        RESULTS[name] = False
        FAILURES.append(f"{name}: {traceback.format_exc()}")
        print(f"[5C2] scenario FAILED: {name}", flush=True)
        traceback.print_exc()


async def scenario_login_flow(browser, server: Server) -> None:
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
    page = await context.new_page()
    page.set_default_timeout(15000)
    await login(page, server)
    await open_accounts(page)
    await page.locator(".account-card", has_text="小红书").wait_for()
    await screenshot(page, "01_accounts_light.png")

    card = page.locator(".account-card", has_text="小红书")
    await card.get_by_role("button", name="登录").click()
    await page.locator(".modal .qr-img").wait_for(timeout=20000)
    assert server.fake.login_starts == ["xiaohongshu"]
    await screenshot(page, "02_login_qr.png")

    server.fake.scanned = True
    await page.locator(".modal input[inputmode='numeric']").wait_for(
        timeout=20000)
    await screenshot(page, "03_login_sms.png")
    await page.locator(".modal input[inputmode='numeric']").fill("000000")
    await page.get_by_role("button", name="提交验证码").click()
    await wait_for(
        lambda: text_contains(page.locator(".modal"), "验证码不正确"),
        10000, "wrong SMS should stay pending")
    await page.locator(".modal input[inputmode='numeric']").fill("123456")
    await page.get_by_role("button", name="提交验证码").click()
    await page.get_by_role("button", name="完成").wait_for(timeout=20000)
    await page.get_by_role("button", name="完成").click()
    await wait_for(
        lambda: text_contains(
            card.locator(".account-nick"), "受控测试昵称"), 20000,
        "nickname from controlled runner")
    assert await card.locator("img.account-avatar").count() == 1
    assert server.fake.login_starts == ["xiaohongshu"]
    assert "xiaohongshu" in server.fake.whoami_calls

    keys = await page.evaluate("Object.keys(localStorage)")
    assert "easel_whoami_v4" not in keys
    assert "easel_analytics" not in keys

    await page.reload(wait_until="domcontentloaded")
    await page.locator(".sidebar").wait_for(timeout=20000)
    await open_accounts(page)
    await wait_for(
        lambda: text_contains(
            page.locator(".account-card", has_text="小红书")
            .locator(".account-nick"), "受控测试昵称"), 20000,
        "nickname restored after reload")
    assert server.fake.login_starts == ["xiaohongshu"]

    await screenshot(page, "04_accounts_restored.png")
    await page.locator(".theme-toggle").click()
    await wait_for(
        lambda: page.evaluate(
            "document.documentElement.dataset.theme === 'dark'"),
        5000, "dark theme")
    await screenshot(page, "05_accounts_dark.png")
    await page.locator(".theme-toggle").click()
    await page.set_viewport_size({"width": 390, "height": 844})
    await screenshot(page, "06_accounts_mobile.png")
    await context.close()


async def scenario_wechat_states(browser, server: Server) -> None:
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
    page = await context.new_page()
    page.set_default_timeout(15000)
    await login(page, server)
    await open_accounts(page)
    card = page.locator(".account-card", has_text="微信公众号")
    await card.wait_for()
    text = await card.inner_text()
    assert "后台扫码：未登录" in text
    assert "AppID 凭证：未配置" in text
    await screenshot(page, "07_wechat_two_states.png")

    await card.get_by_role("button", name="凭证配置").click()
    modal = page.locator(".modal", has_text="配置 微信公众号")
    await modal.wait_for()
    await modal.locator("input").nth(0).fill("wx1234567890")
    await modal.locator("input").nth(1).fill("secret-value")
    await modal.get_by_role("button", name="保存并验证").click()
    await modal.wait_for(state="hidden", timeout=20000)
    await wait_for(
        lambda: text_contains(card, "AppID 凭证：已配置"), 20000,
        "credential configured state")
    assert "secret-value" not in await page.content()
    assert server.fake.mp_login_starts == []

    # 后台扫码登录与凭证是两种状态：未扫码时后台扫码仍未登录。
    text = await card.inner_text()
    assert "后台扫码：未登录" in text
    await card.get_by_role("button", name="后台扫码").click()
    await page.locator(".modal .qr-img").wait_for(timeout=20000)
    assert server.fake.mp_login_starts == ["wechat-oa"]
    server.fake.mp_scanned = True
    await page.get_by_role("button", name="完成").wait_for(timeout=20000)
    await page.get_by_role("button", name="完成").click()
    await wait_for(
        lambda: text_contains(card, "后台扫码：已登录"), 20000,
        "mp logged in state")
    await screenshot(page, "08_wechat_mp_success.png")
    await context.close()


async def scenario_cross_site_analytics_get(browser, server: Server) -> None:
    """SameSite=Lax 真实 Chromium：外站顶层 GET 可携 Cookie，但不得起任务。"""
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
    page = await context.new_page()
    page.set_default_timeout(15000)
    await login(page, server)
    before = list(server.fake.analytics_calls)

    attacker = await context.new_page()
    target = server.origin + "/api/analytics/xiaohongshu"
    await attacker.goto(
        "data:text/html,<a id='go' href='" + target + "'>open</a>")
    async with attacker.expect_response(
            lambda response: "/api/analytics/xiaohongshu" in response.url
    ) as info:
        await attacker.click("#go")
    response = await info.value
    # Cookie 必须仍随顶层 GET 发送（否则是 401）；但只能读缓存并 404。
    assert response.status == 404, await response.text()
    body = await response.json()
    assert body.get("code") == "analytics_cache_missing", body
    assert server.fake.analytics_calls == before, (
        "cross-site navigation must not execute analytics")
    await context.close()


async def scenario_late_stop_modal(browser, server: Server) -> None:
    """旧停止回包不能覆盖随后打开的新登录弹窗。"""
    import sqlite3

    context = await browser.new_context(
        viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
    page = await context.new_page()
    page.set_default_timeout(15000)
    await login(page, server)
    await open_accounts(page)
    # 前一场景可能还有 whoami 短任务在收尾；等容量释放后再开新登录。
    await wait_for(lambda: active_runs(server) == 0, 20,
                   "capacity was not released before late-stop scenario")

    first = page.locator(".account-card", has_text="快手")
    await first.get_by_role("button", name="登录", exact=True).click()
    await page.locator(".modal .qr-img").wait_for(timeout=20000)

    service = server.app.state.workspace_accounts
    original_stop = service.stop_login
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def delayed_stop(actor, platform, *, mp=False, run_id=None):
        result = await original_stop(
            actor, platform, mp=mp, run_id=run_id)
        entered.set()
        await gate.wait()
        return result

    service.stop_login = delayed_stop
    try:
        await page.get_by_role("button", name="停止登录").click()
        await wait_for(lambda: entered.is_set(), 15,
                       "stop did not reach worker terminal state")
        await wait_for(lambda: active_runs(server) == 0, 10,
                       "stop did not release capacity before opening B")
        await page.get_by_role("button", name="关闭").click()

        douyin = page.locator(".account-card", has_text="抖音")
        for _attempt in range(3):
            # 关闭 A 触发的后台 whoami 可能短暂占用容量；等待空位再开 B。
            await wait_for(lambda: active_runs(server) < 2, 20,
                           "no capacity slot for the new login")
            print("[5C2] late-stop attempt", _attempt,
                  "active", active_runs(server),
                  "starts", server.fake.login_starts, flush=True)
            try:
                async with page.expect_response(
                        lambda response: "/api/login/douyin" in response.url,
                        timeout=10000) as info:
                    await douyin.get_by_role(
                        "button", name="登录", exact=True).click()
                response = await info.value
                print("[5C2] douyin login HTTP", response.status,
                      (await response.text())[:300], flush=True)
            except PlaywrightTimeoutError:
                print("[5C2] douyin login request did not fire", flush=True)
            try:
                await page.locator(".modal .qr-img").wait_for(timeout=10000)
                break
            except PlaywrightTimeoutError:
                if await page.locator(".modal").count():
                    print("[5C2] modal after failed B:",
                          await page.locator(".modal").inner_text(),
                          flush=True)
                    await page.get_by_role("button", name="关闭").click()
                await asyncio.sleep(1.0)
        else:
            raise AssertionError("could not open the new login modal")
        assert await page.locator(".modal .qr-img").count() == 1
        gate.set()
        await asyncio.sleep(1.2)
        text = await page.locator(".modal").inner_text()
        assert "抖音" in text, text
        assert "已停止登录" not in text, text
        assert await page.locator(".modal .qr-img").count() == 1, text

        connection = sqlite3.connect(server.root / "control.sqlite3")
        try:
            latest = connection.execute(
                "SELECT status FROM runs ORDER BY created_at DESC, rowid DESC"
                " LIMIT 1").fetchone()
        finally:
            connection.close()
        assert latest is not None and latest[0] == "running", latest
    finally:
        service.stop_login = original_stop
        if not gate.is_set():
            gate.set()

    await page.get_by_role("button", name="停止登录").click()
    await wait_for(
        lambda: page.evaluate(
            "() => (document.querySelector('.modal')?.innerText || '')"
            ".includes('已停止登录')"), 20000, "new modal stop should succeed")
    await context.close()


async def scenario_analytics(browser, server: Server) -> None:
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
    page = await context.new_page()
    page.set_default_timeout(15000)
    await login(page, server)
    print("[5C2] analytics: dashboard", flush=True)
    await page.locator(".nav-item", has_text="工作台").click()
    await page.locator(".dash-page").wait_for(timeout=20000)
    print("[5C2] analytics: click platform", flush=True)
    await page.locator(".ana-plats .chip", has_text="小红书").click()
    print("[5C2] analytics: wait numbers", flush=True)
    async def analytics_numbers_ready() -> bool:
        text = await page.locator(".ana-stat-val").first.inner_text()
        return text not in ("—", "")

    await wait_for(
        analytics_numbers_ready, 20000, "analytics numbers")
    print("[5C2] analytics: numbers ready", flush=True)
    assert server.fake.analytics_calls == ["xiaohongshu"]
    await screenshot(page, "09_analytics_success.png")

    server.fake.analytics_fail = True
    await page.get_by_role("button", name="刷新 →").click()
    await wait_for(
        lambda: page.locator("text=刷新失败，显示旧数据").count(), 20000,
        "stale cache marker")
    stale = await page.locator("text=刷新失败，显示旧数据").first.inner_text()
    assert "真实取数时间" in stale

    async def stale_gone() -> bool:
        return await page.locator("text=刷新失败，显示旧数据").count() == 0

    server.fake.analytics_fail = False
    await page.get_by_role("button", name="刷新 →").click()
    await wait_for(
        lambda: len(server.fake.analytics_calls) >= 3, 20000,
        "retry execution started")
    await wait_for(stale_gone, 20000, "stale marker cleared after retry")
    assert server.fake.analytics_calls == [
        "xiaohongshu", "xiaohongshu", "xiaohongshu"], (
        f"analytics execution count: {server.fake.analytics_calls!r}")
    await context.close()


async def scenario_stop_503_401(browser, server: Server) -> None:
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
    page = await context.new_page()
    page.set_default_timeout(15000)
    page.on("response", lambda response: print(
        "[5C2] HTTP", response.status, response.url, flush=True)
        if "/stop" in response.url else None)
    await login(page, server)
    await open_accounts(page)
    card = page.locator(".account-card", has_text="知乎")
    print("[5C2] wait zhihu card", flush=True)
    await card.wait_for(timeout=15000)
    print("[5C2] click zhihu login", flush=True)
    await card.locator("button", has_text="登录").click(timeout=8000)
    print("[5C2] zhihu login clicked", flush=True)
    try:
        await page.locator(".modal .qr-img").wait_for(timeout=20000)
    except Exception:
        print("[5C2] first login modal:",
              await page.locator(".modal").inner_text(), flush=True)
        print("[5C2] first login starts:", server.fake.login_starts,
              flush=True)
        raise
    starts = server.fake.login_starts.count("zhihu")
    print("[5C2] clicking stop", flush=True)
    await page.get_by_role("button", name="停止登录").click(timeout=8000)
    print("[5C2] stop clicked", flush=True)
    try:
        await page.wait_for_function(
            "() => (document.querySelector('.modal')?.innerText || '')"
            ".includes('已停止登录')", timeout=20000)
    except Exception:
        print("[5C2] stop modal:",
              await page.locator(".modal").inner_text(), flush=True)
        await screenshot(page, "stop_modal_failure.png")
        import sqlite3
        connection = sqlite3.connect(server.root / "control.sqlite3")
        try:
            print("[5C2] runs after stop:",
                  connection.execute(
                      "select id,status from runs order by created_at desc"
                      " limit 3").fetchall(), flush=True)
        finally:
            connection.close()
        raise
    print("[5C2] stop state observed", flush=True)
    assert server.fake.login_starts.count("zhihu") == starts
    await page.get_by_role("button", name="关闭").click()

    # 重新登录；二维码出来后只在 control 适配层注入一次 503，轮询应恢复。
    await card.get_by_role("button", name="登录").click()
    try:
        await page.locator(".modal .qr-img").wait_for(timeout=20000)
    except Exception:
        print("[5C2] relogin modal:", await page.locator(".modal").inner_text(),
              flush=True)
        print("[5C2] relogin starts:", server.fake.login_starts, flush=True)
        raise
    from easel.control.types import DomainError
    service = server.app.state.workspace_accounts
    original_status = service.login_status

    async def flaky_status(actor, platform):
        if server.fake.control_status_503_once:
            server.fake.control_status_503_once = False
            raise DomainError("upstream_unavailable", 503)
        return await original_status(actor, platform)

    service.login_status = flaky_status
    server.fake.control_status_503_once = True
    await asyncio.sleep(2.2)
    assert await page.locator(".modal .qr-img").count() == 1
    assert "连接中断" not in await page.locator(".modal").inner_text()

    # 401 撤销会话：私人页面卸载回登录页。
    server.auth.revoke_all(server.member.user_id)
    await page.locator("#invite").wait_for(timeout=20000)
    assert await page.locator(".accounts-page").count() == 0
    await context.close()


async def main() -> int:
    root = ROOT / ".task5c2-browser"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir()
    print("[5C2] starting server", flush=True)
    server = Server(root).start()
    print("[5C2] server started", server.origin, flush=True)
    try:
        async with async_playwright() as playwright:
            print("[5C2] launching chromium", flush=True)
            browser = await playwright.chromium.launch(headless=True)
            print("[5C2] chromium launched", flush=True)
            only = sys.argv[1] if len(sys.argv) > 1 else ""
            if not only or only == "login_flow":
                await check("login_flow", lambda: scenario_login_flow(
                    browser, server))
            if not only or only == "wechat_states":
                await check("wechat_states", lambda: scenario_wechat_states(
                    browser, server))
            if not only or only == "cross_site_analytics":
                await check(
                    "cross_site_analytics",
                    lambda: scenario_cross_site_analytics_get(
                        browser, server))
            if not only or only == "late_stop_modal":
                await check("late_stop_modal", lambda: scenario_late_stop_modal(
                    browser, server))
            if not only or only == "analytics":
                await check("analytics", lambda: scenario_analytics(
                    browser, server))
            if not only or only == "stop_503_401":
                await check("stop_503_401", lambda: scenario_stop_503_401(
                    browser, server))
            await browser.close()
    finally:
        await server.stop()
    summary = {
        "results": RESULTS,
        "failures": FAILURES,
        "screenshots": [str(path.relative_to(ROOT)) for path in SHOTS],
        "login_starts": server.fake.login_starts,
        "mp_login_starts": server.fake.mp_login_starts,
        "whoami_calls": server.fake.whoami_calls,
        "analytics_calls": server.fake.analytics_calls,
    }
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "browser_results.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    shutil.rmtree(root, ignore_errors=True)
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
