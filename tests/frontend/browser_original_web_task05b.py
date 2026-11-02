"""Task 5B 浏览器矩阵：热点/选题/日历/概览的真实 control 链路。

不启动 Docker，不使用用户 Cookie/Key/容器。worker 复用 web.app 的真实
handler，A/B 数据根是临时 workspace；control 使用真实 Auth/Store/WorkspaceApi，
gateway 使用真实 Ed25519 验签。热点来源在夹具内固定为受控数据，不能据此
宣称公网来源已通过。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import json
import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

import httpx
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DIST = ROOT / "web" / "frontend" / "dist"
EVIDENCE = Path(os.environ.get(
    "EASEL_BROWSER_EVIDENCE",
    str(ROOT / "docs" / "evidence" / "original-web-task-05b" / "browser")))
SCREENSHOTS: list[Path] = []
IDEA_ID = "a1b2c3d4e5f6"
SCHEDULE_ID = "b1b2c3d4e5f6"


def _tls_files(root: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1"),
    ])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            x509.DNSName("localhost"),
        ]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path = root / "tls-key.pem"
    cert_path = root / "tls-cert.pem"
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class WorkerTransport(httpx.AsyncBaseTransport):
    def __init__(self, roots: dict[int, Path]):
        from web import app as worker_app

        self._worker = worker_app
        self._roots = roots
        self._inner = httpx.ASGITransport(app=worker_app.app)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        root = self._roots.get(request.url.port)
        if root is None:
            return httpx.Response(503, content=b'{"detail":"unknown worker"}')
        self._worker.OUTPUTS_DIR = root / "outputs"
        self._worker.PROFILES_DIR = root / "profiles"
        from easel import persona

        persona.PROFILES_DIR = root / "profiles"
        return await self._inner.handle_async_request(request)


class GatewayTransport(httpx.AsyncBaseTransport):
    def __init__(self, apps: dict[int, object]):
        self._apps = apps
        self._inner = {
            port: httpx.ASGITransport(app=app)
            for port, app in apps.items()
        }

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        inner = self._inner.get(request.url.port)
        if inner is None:
            return httpx.Response(503, content=b'{"error":"gateway_unavailable"}')
        return await inner.handle_async_request(request)


class Server:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.roots = {"a": root / "a", "b": root / "b"}
        for label, workspace in self.roots.items():
            (workspace / "outputs").mkdir(parents=True)
            (workspace / "profiles").mkdir(parents=True)
            self._seed_workspace(workspace, label.upper())
        self._seed_control()
        self._build_apps()
        self._build_server()

    def _seed_workspace(self, workspace: Path, label: str) -> None:
        outputs = workspace / "outputs"
        project = outputs / "项目"
        project.mkdir()
        (project / "文案.txt").write_text(
            f"{label} 私有正文", encoding="utf-8")
        persona = workspace / "profiles" / "测试画像"
        persona.mkdir()
        (persona / "identity.md").write_text(
            f"# {label} 测试画像\n\n{label} 定位", encoding="utf-8")
        for name in ("style.md", "audience.md", "platforms.md",
                     "preferences.md", "memory.md"):
            (persona / name).write_text(f"{label}-{name}", encoding="utf-8")
        target = dt.date.today() + dt.timedelta(days=5)
        _write_json(outputs / "_ideas.json", [{
            "id": IDEA_ID, "title": f"{label} 初始选题", "note": "初始备注",
            "source": "manual", "status": "pending", "created": 1,
        }])
        _write_json(outputs / "_schedule.json", [{
            "id": SCHEDULE_ID, "title": f"{label} 初始排期",
            "date": target.isoformat(), "platform": "微博", "time": "10:00",
            "status": "scheduled", "note": "", "kind": "content",
            "url": "", "source": "manual", "event_type": "", "end_date": "",
        }])

    def _seed_control(self) -> None:
        from easel.control.auth import AuthService
        from easel.control.store import Store
        from easel.control.types import WorkerHandle

        self.store = Store.connect(self.root / "control.sqlite3")
        self.admin = self.store.create_user("BrowserOwner", "admin")
        self.user_a = self.store.create_user("BrowserA")
        self.user_b = self.store.create_user("BrowserB")
        self.auth = AuthService(self.store)
        self.invites = {
            "a": [self.auth.create_invite(self.admin, self.user_a.user_id)
                  for _ in range(8)],
            "b": [self.auth.create_invite(self.admin, self.user_b.user_id)
                  for _ in range(8)],
        }
        self.handles = {}
        for label, user, port in (("a", self.user_a, 7861),
                                  ("b", self.user_b, 7862)):
            workspace = user.workspace_id
            handle = WorkerHandle(
                workspace, f"{port:064x}", f"http://127.0.0.1:{port}",
                f"service-{label}", 0,
                gateway_instance_id=f"gateway-{label}",
                gateway_generation=f"boot-{label}",
                gateway_upstream=f"http://127.0.0.1:{port}",
                gateway_state="available")
            now = int(time.time())
            outcome, lease = self.store.claim_worker_start(
                workspace, 0, f"browser-{label}", now, 3600)
            assert outcome == "started", outcome
            assert self.store.complete_worker_start(
                workspace, f"browser-{label}", lease.generation,
                handle, now, 3600)
            self.handles[label] = handle
        self.workspace_label = {
            user.workspace_id: label
            for label, user in (("a", self.user_a), ("b", self.user_b))
        }

    def _build_apps(self) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        from easel.control.app import ControlConfig, create_app
        from easel.control.gateway_client import GatewayClient
        from easel.control.workspace_api import WorkspaceApi
        from easel.gateway.app import GatewayConfig, create_gateway_app
        from web import app as worker_app

        worker_app.OUTPUTS_DIR = self.roots["a"] / "outputs"
        worker_app.PROFILES_DIR = self.roots["a"] / "profiles"
        worker_app.LOGIN_DIR = worker_app.OUTPUTS_DIR / "_login"
        worker_app.PUBLISH_DIR = worker_app.OUTPUTS_DIR / "_publish"
        worker_app.PROFILE_BUILD_DIR = worker_app.OUTPUTS_DIR / "_profile_build"
        worker_app.DEBUG_DIR = worker_app.OUTPUTS_DIR / "_debug"
        worker_app.SESSIONS_DIR = worker_app.OUTPUTS_DIR / "_sessions"
        worker_app._harness_settings = lambda: {"runtime": "api"}
        worker_app._direct_api_enabled = lambda: False
        worker_app._fetch_platform = lambda platform: [{
            "title": f"{platform}测试热点", "hot": "100",
            "url": f"https://example.test/{platform}",
        }]
        from easel import persona

        persona.PROFILES_DIR = worker_app.PROFILES_DIR
        self.worker_app = worker_app

        self.key = Ed25519PrivateKey.generate()
        self.worker_client = httpx.AsyncClient(
            transport=WorkerTransport({
                7861: self.roots["a"], 7862: self.roots["b"],
            }), trust_env=False)
        gateway_apps = {}
        for port, handle in ((7861, self.handles["a"]),
                             (7862, self.handles["b"])):
            config = GatewayConfig(
                workspace_id=handle.workspace_id,
                gateway_instance_id=handle.gateway_instance_id,
                boot_generation=handle.gateway_generation,
                worker_container_id=handle.container_id,
                config_version=0,
                worker_origin=handle.upstream,
                worker_service_token=handle.service_token,
            )
            gateway_apps[port] = create_gateway_app(
                config, self.key.public_key(), activated=True,
                http_client=self.worker_client)
        self.gateway_client = httpx.AsyncClient(
            transport=GatewayTransport(gateway_apps), trust_env=False)

        class Runtime:
            def __init__(self, handles):
                self.handles = handles

            async def ensure_ready(self, workspace_id, config_version):
                for handle in self.handles.values():
                    if handle.workspace_id == workspace_id:
                        assert config_version == handle.config_version
                        return handle
                raise AssertionError(f"unknown workspace {workspace_id}")

        self.runtime = Runtime(self.handles)

        def workspace_factory(runtime, store, _http_client, db_runner):
            return WorkspaceApi(
                runtime, store,
                GatewayClient(self.key, self.gateway_client),
                revision=0, db_runner=db_runner, gateway_required=True)

        self.origin = ""
        self.app = None

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(128)
        port = self.sock.getsockname()[1]
        self.origin = f"https://127.0.0.1:{port}"
        self.app = create_app(
            store=self.store, auth=self.auth,
            config=ControlConfig(public_origin=self.origin),
            runtime=self.runtime, frontend_dir=DIST,
            workspace_factory=workspace_factory)

    def _build_server(self) -> None:
        cert, key = _tls_files(self.root)
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
            await self.gateway_client.aclose()
            await self.worker_client.aclose()
        except Exception:
            pass
        try:
            self.store.close()
        except Exception:
            pass


async def wait_until(predicate, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(message)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


async def login(page, label: str) -> None:
    invite = SERVER.invites[label].pop(0)
    await page.goto(SERVER.origin, wait_until="domcontentloaded")
    await page.fill("#invite", invite)
    await page.get_by_role("button", name="进入工作区").click()
    await page.locator(".sidebar").wait_for(timeout=20000)
    await page.locator(".persona-select").wait_for(timeout=20000)


async def open_dashboard(page) -> None:
    await page.locator(".sidebar").get_by_role(
        "button", name="工作台", exact=True).click()
    await page.locator(".dash-page").wait_for(timeout=20000)


async def screenshot(page, name: str) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / name
    await page.screenshot(path=str(path), full_page=False)
    SCREENSHOTS.append(path)


async def scenario_workbench_flow(browser, results, failures):
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900},
        ignore_https_errors=True, color_scheme="light")
    page = await context.new_page()
    requests: list[str] = []
    responses: list[str] = []
    console: list[str] = []
    page.on("request", lambda request: requests.append(request.url))
    page.on("response", lambda response: responses.append(
        f"{response.status} {response.url}"))
    page.on("console", lambda message: console.append(message.text))
    try:
        await login(page, "a")
        await open_dashboard(page)
        await page.get_by_text("weibo测试热点").first.wait_for(timeout=20000)
        await page.get_by_text("A 初始选题").first.wait_for(timeout=20000)
        await page.get_by_text("A 初始排期").first.wait_for(timeout=20000)
        assert "已登录账号" in (await page.text_content("body") or "")
        await screenshot(page, "01-dashboard-light.png")

        await page.get_by_role("button", name="看热点", exact=True).click()
        await page.locator(".trends-page").wait_for(timeout=20000)
        await page.get_by_text("weibo测试热点").first.wait_for(timeout=20000)
        await screenshot(page, "02-trends-light.png")
        await page.locator(".trend-col").filter(
            has_text="微博").locator(".trend-save").first.click()
        await wait_until(
            lambda: any(item["title"] == "weibo测试热点"
                        for item in read_json(
                            SERVER.roots["a"] / "outputs" / "_ideas.json")),
            10, "热点收藏未写入选题")

        await page.get_by_role("button", name="选题库", exact=True).click()
        await page.locator(".ideas-page").wait_for(timeout=20000)
        card = page.locator(".idea-card").filter(
            has_text="weibo测试热点").first
        await card.wait_for(timeout=20000)
        await card.hover()
        await card.get_by_title("编辑").click()
        modal = page.locator(".modal")
        await modal.wait_for(timeout=10000)
        failed = {"armed": True}

        async def fail_once(route):
            if failed["armed"] and route.request.method == "PUT":
                failed["armed"] = False
                await route.fulfill(
                    status=502, content_type="application/json",
                    body='{"code":"upstream_invalid"}')
                return
            await route.continue_()

        await page.route("**/api/ideas/*", fail_once)
        await modal.locator("input.field").first.fill("浏览器改选题")
        await modal.get_by_role("button", name="进行中").click()
        await modal.get_by_role("button", name="保存").click()
        await modal.get_by_text("保存失败").wait_for(timeout=10000)
        assert await modal.locator("input.field").first.input_value() == "浏览器改选题"
        await modal.get_by_role("button", name="保存").click()
        await page.get_by_text("浏览器改选题").first.wait_for(timeout=20000)
        await page.unroute("**/api/ideas/*", fail_once)
        await wait_until(
            lambda: any(item["title"] == "浏览器改选题"
                        and item["status"] == "doing"
                        for item in read_json(
                            SERVER.roots["a"] / "outputs" / "_ideas.json")),
            10, "选题编辑未持久化")
        await screenshot(page, "03-ideas-light.png")

        edited = page.locator(".idea-card").filter(
            has_text="浏览器改选题").first
        await edited.get_by_role("button", name="排期").click()
        await page.get_by_text("已加入日历（今天）").wait_for(timeout=10000)
        await wait_until(
            lambda: any(item["title"] == "浏览器改选题"
                        for item in read_json(
                            SERVER.roots["a"] / "outputs" / "_schedule.json")),
            10, "选题加入日历未持久化")

        await page.get_by_role("button", name="内容日历", exact=True).click()
        await page.locator(".calendar-page").wait_for(timeout=20000)
        await page.get_by_text("浏览器改选题").first.wait_for(timeout=20000)
        await page.get_by_text("浏览器改选题").first.click()
        modal = page.locator(".modal")
        await modal.wait_for(timeout=10000)
        await modal.locator("input.field").first.fill("浏览器改排期")
        await modal.get_by_role("button", name="保存").click()
        await page.get_by_text("浏览器改排期").first.wait_for(timeout=20000)
        await wait_until(
            lambda: any(item["title"] == "浏览器改排期"
                        for item in read_json(
                            SERVER.roots["a"] / "outputs" / "_schedule.json")),
            10, "日历编辑未持久化")
        await screenshot(page, "04-calendar-light.png")

        seeded = page.locator(".cal-event").filter(
            has_text="A 初始排期").first
        await seeded.click()
        modal = page.locator(".modal")
        await modal.wait_for(timeout=10000)
        await modal.get_by_role("button", name="删除").click()
        await wait_until(
            lambda: not any(item["id"] == SCHEDULE_ID
                            for item in read_json(
                                SERVER.roots["a"] / "outputs"
                                / "_schedule.json")),
            10, "日历删除未持久化")

        await open_dashboard(page)
        await page.get_by_text("浏览器改排期").first.wait_for(timeout=20000)

        await page.reload(wait_until="domcontentloaded")
        await page.locator(".sidebar").wait_for(timeout=20000)
        await open_dashboard(page)
        await page.get_by_role("button", name="记选题", exact=True).click()
        await page.get_by_text("浏览器改选题").first.wait_for(timeout=20000)
        await page.get_by_role("button", name="内容日历", exact=True).click()
        await page.get_by_text("浏览器改排期").first.wait_for(timeout=20000)

        await page.get_by_role("button", name="热点雷达", exact=True).click()
        await page.locator(".trends-page").wait_for(timeout=20000)
        before_chat = sum(
            1 for url in requests
            if url.endswith("/api/chat") or "/api/chat/jobs/" in url)
        await page.locator(".trend-use").first.click()
        await page.locator(".chat-page").wait_for(timeout=20000)
        textarea = page.locator(".chat-input")
        await textarea.wait_for(timeout=10000)
        value = await textarea.input_value()
        assert "围绕当前热点" in value and "weibo测试热点" in value, value
        await page.wait_for_timeout(1000)
        after_chat = sum(
            1 for url in requests
            if url.endswith("/api/chat") or "/api/chat/jobs/" in url)
        assert after_chat == before_chat, (before_chat, after_chat)
        assert "weibo测试热点" not in (
            await page.locator(".messages").text_content()
            if await page.locator(".messages").count() else "")
        await screenshot(page, "05-topic-draft-light.png")
        results["workbench_flow"] = True
    except Exception as error:  # noqa: BLE001
        body = ""
        try:
            body = (await page.text_content("body") or "")[:1200]
        except Exception:
            pass
        failures.append(
            f"workbench-flow: {error!r} body={body!r} "
            f"responses={responses[-30:]!r} console={console[-20:]!r}")
    finally:
        await context.close()


async def scenario_identity_isolation(browser, results, failures):
    context = await browser.new_context(
        viewport={"width": 1280, "height": 800}, ignore_https_errors=True)
    page = await context.new_page()
    delayed = {"armed": False}
    responses: list[str] = []
    console: list[str] = []
    page.on("response", lambda response: responses.append(
        f"{response.status} {response.url}"))
    page.on("console", lambda message: console.append(message.text))
    try:
        await login(page, "a")
        await open_dashboard(page)
        await page.get_by_text("A 初始选题").first.wait_for(timeout=20000)

        async def slow_ideas(route):
            if delayed["armed"]:
                delayed["armed"] = False
                await asyncio.sleep(1.5)
            try:
                await route.continue_()
            except Exception:
                pass

        await page.route("**/api/ideas", slow_ideas)
        delayed["armed"] = True
        await page.get_by_role("button", name="记选题", exact=True).click()
        await page.wait_for_timeout(150)
        await page.get_by_role("button", name="退出").click()
        await page.locator("#invite").wait_for(timeout=15000)
        await login(page, "b")
        await open_dashboard(page)
        await page.get_by_role("button", name="记选题", exact=True).click()
        await page.get_by_text("B 初始选题").first.wait_for(timeout=20000)
        await page.wait_for_timeout(1800)
        body = await page.text_content("body") or ""
        assert "A 初始选题" not in body, body
        assert "B 初始选题" in body
        await screenshot(page, "06-identity-b.png")
        results["identity_isolation"] = True
    except Exception as error:  # noqa: BLE001
        body = ""
        try:
            body = (await page.text_content("body") or "")[:1200]
        except Exception:
            pass
        failures.append(
            f"identity-isolation: {error!r} body={body!r} "
            f"responses={responses[-30:]!r} console={console[-20:]!r}")
    finally:
        await context.close()


async def scenario_member_accounts_connected(browser, results, failures):
    """5C2 后成员账号页已接通：允许账号/分析读，仍不得触管理员/发布/登录写。"""
    context = await browser.new_context(
        viewport={"width": 1280, "height": 800}, ignore_https_errors=True)
    page = await context.new_page()
    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url))
    try:
        await login(page, "a")
        await open_dashboard(page)
        await page.get_by_role("button", name="账号", exact=True).click()
        await page.locator(".accounts-page").wait_for(timeout=20000)
        forbidden = ("/api/admin", "/api/skills", "/api/profile/build",
                     "/api/publish", "/api/login/", "/api/logout/")
        assert not [url for url in requests
                    if any(part in url for part in forbidden)], requests
        assert any("/api/accounts" in url for url in requests), requests
        await screenshot(page, "07-accounts-connected.png")
        assert not [url for url in requests
                    if any(part in url for part in forbidden)], requests
        results["member_accounts_connected"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(f"member-accounts-connected: {error!r}")
    finally:
        await context.close()


async def scenario_stale_trends(browser, results, failures):
    context = await browser.new_context(
        viewport={"width": 1280, "height": 800}, ignore_https_errors=True)
    page = await context.new_page()
    worker = SERVER.worker_app
    cache = dict(worker._TREND_CACHE)
    fetcher = worker._fetch_platform
    try:
        worker._TREND_CACHE.clear()
        worker._TREND_CACHE["weibo"] = (
            time.time() - 3600,
            [{"title": "ONE-HOUR-OLD-SENTINEL", "hot": "1", "url": ""}])
        worker._fetch_platform = lambda platform: []
        await login(page, "a")
        await open_dashboard(page)
        await page.get_by_text("热点刷新失败，显示缓存数据").first.wait_for(
            timeout=20000)
        await page.get_by_role("button", name="看热点", exact=True).click()
        await page.locator(".trends-page").wait_for(timeout=20000)
        await page.get_by_text("ONE-HOUR-OLD-SENTINEL").first.wait_for(
            timeout=20000)
        body = await page.text_content("body") or ""
        assert "ONE-HOUR-OLD-SENTINEL" in body, "缺旧缓存条目"
        assert "刷新失败，显示缓存" in body, "缺刷新失败说明"
        assert "旧数据" in body, "缺旧数据标记"
        assert " 更新" not in body, f"仍显示当前更新时间：{body!r}"
        await screenshot(page, "10-trends-stale-light.png")
        results["stale_trends"] = True
    except Exception as error:  # noqa: BLE001
        body = ""
        try:
            body = (await page.text_content("body") or "")[:1500]
        except Exception:
            pass
        failures.append(f"stale-trends: {error!r} body={body!r}")
    finally:
        worker._TREND_CACHE.clear()
        worker._TREND_CACHE.update(cache)
        worker._fetch_platform = fetcher
        await context.close()


async def scenario_dark_and_mobile(browser, results, failures):
    try:
        dark = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=True, color_scheme="dark")
        page = await dark.new_page()
        await login(page, "a")
        await open_dashboard(page)
        await page.get_by_text("weibo测试热点").first.wait_for(timeout=20000)
        tokens = await page.evaluate("""() => {
          const style = getComputedStyle(document.documentElement);
          return [style.getPropertyValue('--bg').trim(),
                  style.getPropertyValue('--surface').trim(),
                  style.getPropertyValue('--text').trim()];
        }""")
        assert tokens[0] not in ("#fff", "#ffffff", ""), tokens
        await screenshot(page, "08-dashboard-dark.png")
        await dark.close()

        mobile = await browser.new_context(
            viewport={"width": 390, "height": 844},
            ignore_https_errors=True, color_scheme="light")
        page = await mobile.new_page()
        await login(page, "a")
        await open_dashboard(page)
        await page.get_by_text("weibo测试热点").first.wait_for(timeout=20000)
        overflow = await page.evaluate(
            "() => document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 1, overflow
        await screenshot(page, "09-dashboard-mobile.png")
        await mobile.close()
        results["dark_mobile"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(f"dark-mobile: {error!r}")


async def main() -> int:
    if not DIST.is_dir() or not (DIST / "index.html").is_file():
        print("缺少 dist，请先 npm --prefix web/frontend run build")
        return 2
    global SERVER
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="easel-task5b-browser-"))
    try:
        SERVER = Server(root).start()
        results: dict[str, bool] = {}
        failures: list[str] = []
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            print("BROWSER_VERSION", browser.version)
            await scenario_workbench_flow(browser, results, failures)
            await scenario_identity_isolation(browser, results, failures)
            await scenario_member_accounts_connected(browser, results, failures)
            await scenario_stale_trends(browser, results, failures)
            await scenario_dark_and_mobile(browser, results, failures)
            await browser.close()
        print("TASK5B_BROWSER_RESULTS", results)
        print("FAILURES", failures)
        print("SCREENSHOTS", [str(path.relative_to(ROOT)) for path in SCREENSHOTS])
        ok = not failures and len(results) >= 5
        print("TASK5B_BROWSER_OK", ok)
        return 0 if ok else 1
    finally:
        try:
            await SERVER.stop()
        except Exception:
            pass
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
