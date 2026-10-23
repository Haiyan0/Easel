"""Task 11 真实 HTTPS + Chromium 验收脚本（独立运行，不进入 pytest 收集）。

隔离临时目录、临时 CA/自签证书、真实 control(uvicorn TLS)、真实 worker
(uvicorn + ACPAdapter 夹具) 与临时浏览器上下文；不读取用户 HOME 配置，
不改系统信任库、不关闭 Chromium sandbox。

运行：
  python tests/frontend/browser_task11.py
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "multiuser"))
import test_tcp_integration as tcp  # noqa: E402

from easel.control.app import ControlConfig, create_app  # noqa: E402
from easel.control.auth import AuthService  # noqa: E402
from easel.control.proxy import WorkerRelay  # noqa: E402
from easel.control.store import Store  # noqa: E402
from easel.control.types import WorkerHandle  # noqa: E402

from playwright.async_api import async_playwright  # noqa: E402

DIST = ROOT / "web" / "frontend" / "dist"
SCREENSHOT_DIR = (ROOT / ".superpowers" / "sdd"
                  / "2026-10-23-task-11-identity-ui" / "screenshots")
REVISION = tcp.REVISION


def make_certificate(tmp_path: Path) -> tuple[Path, Path, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
    ])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(hours=4))
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost"),
            x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1")),
        ]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path = tmp_path / "control-key.pem"
    cert_path = tmp_path / "control-cert.pem"
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    spki = certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    pin = base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")
    return key_path, cert_path, pin


class ControlHTTPSHost:
    def __init__(self, db_path: Path, key: Ed25519PrivateKey, runtime,
                 provider, cert_path: Path, key_path: Path,
                 public_origin: str, port: int) -> None:
        self.db_path = db_path
        self.key = key
        self.runtime = runtime
        self.provider = provider
        self.cert_path = cert_path
        self.key_path = key_path
        self.public_origin = public_origin
        self.port = port
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None

    def _factory(self, runtime, sessions, auth, store, http_client, broker,
                 db_runner):
        return WorkerRelay(
            runtime, sessions, auth, store=store, signing_key=self.key,
            http_client=http_client, config_provider=self.provider,
            db_runner=db_runner)

    def start(self) -> "ControlHTTPSHost":
        setup = Store.connect(self.db_path)
        try:
            app = create_app(
                store=setup, auth=AuthService(setup),
                config=ControlConfig(public_origin=self.public_origin),
                runtime=self.runtime, frontend_dir=DIST,
                relay_factory=self._factory)
        finally:
            setup.close()
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning",
            log_config=None, access_log=False, proxy_headers=False,
            ssl_certfile=str(self.cert_path),
            ssl_keyfile=str(self.key_path))
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(
            target=self.server.run, name="control-browser", daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if not self.thread.is_alive():
                raise AssertionError("control 启动失败（线程退出）")
            if time.monotonic() > deadline:
                raise AssertionError("control 启动超时")
            time.sleep(0.05)
        return self

    def stop(self) -> None:
        if self.server is None:
            return
        self.server.should_exit = True
        assert self.thread is not None
        self.thread.join(timeout=30)
        assert not self.thread.is_alive(), "control 未退出"
        self.server = None


async def wait_assistant(page, text: str, timeout: float = 60.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        articles = page.locator("article").filter(has_text="助手")
        count = await articles.count()
        matched = 0
        for index in range(count):
            content = await articles.nth(index).inner_text()
            if text in content:
                matched += 1
        if matched:
            return matched
        await asyncio.sleep(0.2)
    raise AssertionError(f"未看到助手消息：{text}")


async def login(page, invite: str) -> None:
    await page.wait_for_selector("#invite", timeout=15000)
    await page.fill("#invite", invite)
    await page.get_by_role("button", name="进入工作区").click()
    await page.get_by_role("button", name=re.compile("新建会话")).wait_for(
        timeout=15000)


async def main() -> int:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    # 必须放在仓库之外：代码根（只读共享）不能包含可写 workspace/state/HOME
    tmp_path = Path(tempfile.mkdtemp(prefix="easel-task11-browser-"))
    db_path = tmp_path / "control.sqlite3"
    identities, invites = tcp.seed_control(db_path)

    # A 的第二台设备邀请
    store = Store.connect(db_path)
    try:
        admin_row = store._connection.execute(
            "SELECT id FROM users WHERE role = 'admin' LIMIT 1").fetchone()
        admin = store.get_identity(admin_row["id"])
        invite_a2 = AuthService(store).create_invite(
            admin, identities["A"].user_id)
    finally:
        store.close()

    key = Ed25519PrivateKey.generate()
    public_key_path = tmp_path / "worker-public.pem"
    public_key_path.write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo))
    launcher_path = tmp_path / "launcher.py"
    launcher_path.write_text(tcp.LAUNCHER_SOURCE, encoding="utf-8")

    worker = tcp.Worker(
        tmp_path, "a", identities["A"].workspace_id, "normal", "a",
        "service-a-browser", public_key_path, launcher_path).start()
    runtime = tcp.StaticRuntime({
        identities["A"].workspace_id: WorkerHandle(
            identities["A"].workspace_id, "container-a", worker.base,
            "service-a-browser", REVISION),
    })
    provider = tcp.make_provider(
        {identities["A"].workspace_id: tcp.TokenMint()},
        {identities["A"].workspace_id: worker.command})
    key_path, cert_path, pin = make_certificate(tmp_path)
    port = tcp.free_port()
    origin = f"https://localhost:{port}"
    host = ControlHTTPSHost(
        db_path, key, runtime, provider, cert_path, key_path, origin,
        port).start()

    requests: list[str] = []
    screenshot_paths: list[str] = []
    browser_version = ""
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=[f"--ignore-certificate-errors-spki-list={pin}"])
            browser_version = browser.version
            context = await browser.new_context(
                base_url=origin, ignore_https_errors=False)
            page = await context.new_page()
            page.on("request", lambda request: requests.append(request.url))
            await page.goto("/", wait_until="domcontentloaded")
            await page.wait_for_selector("#invite", timeout=20000)
            # 登录前不得调用任何私人业务 API 或渲染旧内容
            assert not any(any(prefix in url for prefix in (
                "/api/sessions", "/api/chat", "/api/settings",
                "/api/skills", "/api/accounts")) for url in requests), requests
            assert "你好" not in await page.locator("body").inner_text()
            path = SCREENSHOT_DIR / "01-login.png"
            await page.screenshot(path=str(path))
            screenshot_paths.append(str(path))

            await login(page, invites["a"])
            await page.get_by_role("button", name=re.compile("新建会话")).click()
            await page.locator("textarea").wait_for(timeout=15000)
            await page.locator("textarea").fill("你好")
            await page.get_by_role("button", name="发送").click()
            assert await wait_assistant(page, "你好") == 1
            path = SCREENSHOT_DIR / "02-chat-a.png"
            await page.screenshot(path=str(path))
            screenshot_paths.append(str(path))

            # 刷新只回放服务端历史，不重复 prompt
            prompt_posts_before = sum(
                1 for url in requests if url.endswith("/api/chat"))
            await page.reload(wait_until="domcontentloaded")
            assert await wait_assistant(page, "你好") == 1
            prompt_posts_after = sum(
                1 for url in requests if url.endswith("/api/chat"))
            assert prompt_posts_after == prompt_posts_before == 1, (
                prompt_posts_before, prompt_posts_after)
            path = SCREENSHOT_DIR / "03-refresh-replay.png"
            await page.screenshot(path=str(path))
            screenshot_paths.append(str(path))

            # 同浏览器第二标签：共享 Cookie，退出后必须立即卸载旧身份
            page2 = await context.new_page()
            page2.on("request", lambda request: requests.append(request.url))
            await page2.goto("/", wait_until="domcontentloaded")
            await page2.get_by_role(
                "button", name=re.compile("新建会话")).wait_for(timeout=15000)
            await page.get_by_role("button", name="退出登录").click()
            await page.wait_for_selector("#invite", timeout=15000)
            await page2.wait_for_selector("#invite", timeout=15000)
            body2 = await page2.locator("body").inner_text()
            assert "你好" not in body2, "其他标签仍显示旧身份内容"
            path = SCREENSHOT_DIR / "04-cross-tab-logout.png"
            await page2.screenshot(path=str(path))
            screenshot_paths.append(str(path))

            # B 登录：不得看到 A 的会话或消息
            await login(page, invites["b1"])
            body = await page.locator("body").inner_text()
            assert "你好" not in body
            assert "新对话" not in body
            path = SCREENSHOT_DIR / "05-login-b.png"
            await page.screenshot(path=str(path))
            screenshot_paths.append(str(path))

            # 新浏览器上下文模拟换设备：服务端目录/消息一致
            context2 = await browser.new_context(
                base_url=origin, ignore_https_errors=False)
            device = await context2.new_page()
            device.on("request", lambda request: requests.append(request.url))
            await device.goto("/", wait_until="domcontentloaded")
            await login(device, invite_a2)
            await device.get_by_role(
                "button", name=re.compile("新对话")).first.click()
            assert await wait_assistant(device, "你好") == 1
            path = SCREENSHOT_DIR / "06-new-device.png"
            await device.screenshot(path=str(path))
            screenshot_paths.append(str(path))

            # 503：身份确认失败时显示错误，不显示旧历史
            await device.route(
                "**/api/auth/me",
                lambda route: route.fulfill(
                    status=503, content_type="application/json",
                    body=json.dumps({"code": "unavailable"})))
            await device.reload(wait_until="domcontentloaded")
            await device.get_by_text(
                re.compile("服务暂不可用"), exact=False).wait_for(timeout=15000)
            body503 = await device.locator("body").inner_text()
            assert "你好" not in body503
            path = SCREENSHOT_DIR / "07-auth-503.png"
            await device.screenshot(path=str(path))
            screenshot_paths.append(str(path))
            await device.unroute("**/api/auth/me")
            await device.reload(wait_until="domcontentloaded")
            assert await wait_assistant(device, "你好") == 1

            # 受控延迟保留 A 的 SSE，再在同一页面切 B；旧响应不得回填 B
            stream_started = asyncio.Event()

            async def delay_stream(route):
                stream_started.set()
                await asyncio.sleep(2.5)
                try:
                    response = await route.fetch()
                    await route.fulfill(response=response)
                except Exception:
                    pass

            await device.route("**/api/chat/jobs/*/stream*", delay_stream)
            await device.locator("textarea").fill("延迟任务")
            await device.get_by_role("button", name="发送").click()
            await asyncio.wait_for(stream_started.wait(), timeout=15)
            await device.get_by_role("button", name="退出登录").click()
            await device.wait_for_selector("#invite", timeout=15000)
            await login(device, invites["b2"])
            await asyncio.sleep(3.0)
            delayed_body = await device.locator("body").inner_text()
            assert "延迟任务" not in delayed_body
            assert "你好" not in delayed_body
            path = SCREENSHOT_DIR / "08-stale-sse.png"
            await device.screenshot(path=str(path))
            screenshot_paths.append(str(path))
            await device.unroute("**/api/chat/jobs/*/stream*")

            # auth/me 未完成前不挂载私人页面，也不调用私人 API
            gate_requests: list[str] = []
            context3 = await browser.new_context(
                base_url=origin, ignore_https_errors=False)
            gate = await context3.new_page()
            gate.on("request",
                    lambda request: gate_requests.append(request.url))

            async def delay_auth(route):
                await asyncio.sleep(1.5)
                await route.continue_()

            await gate.route("**/api/auth/me", delay_auth)
            await gate.goto("/", wait_until="domcontentloaded")
            await gate.get_by_text(
                re.compile("正在确认登录状态"), exact=False).wait_for(
                    timeout=5000)
            await asyncio.sleep(0.4)
            assert not any("/api/sessions" in url for url in gate_requests)
            assert "你好" not in await gate.locator("body").inner_text()
            path = SCREENSHOT_DIR / "10-auth-checking.png"
            await gate.screenshot(path=str(path))
            screenshot_paths.append(str(path))
            await gate.wait_for_selector("#invite", timeout=10000)
            await context3.close()

            # 401：退出后的登录页不含旧内容
            body_login = await page.locator("body").inner_text()
            assert "你好" not in body_login

            # 远端 logout 失败：本地立即退出并明确报告，不伪造 Cookie 已清除
            await page.route(
                "**/api/auth/logout",
                lambda route: route.fulfill(
                    status=500, content_type="application/json",
                    body=json.dumps({"code": "server_error"})))
            await page.get_by_role("button", name="退出登录").click()
            await page.wait_for_selector("#invite", timeout=15000)
            await page.get_by_text(
                re.compile("服务端撤销失败"), exact=False).wait_for(
                    timeout=5000)
            assert "你好" not in await page.locator("body").inner_text()
            path = SCREENSHOT_DIR / "09-logout-remote-failure.png"
            await page.screenshot(path=str(path))
            screenshot_paths.append(str(path))
            await page.unroute("**/api/auth/logout")

            await context2.close()
            await context.close()
            await browser.close()
    finally:
        host.stop()
        worker.stop()

    exchanges = sum(1 for url in requests if url.endswith("/api/auth/exchange"))
    prompts = sum(1 for url in requests if url.endswith("/api/chat"))
    streams = sum(1 for url in requests if "/api/chat/jobs/" in url)
    print("BROWSER_VERSION", browser_version)
    print("ORIGIN", origin)
    print("LOGIN_EXCHANGES", exchanges)
    print("PROMPT_POSTS", prompts)
    print("SSE_CONNECTS", streams)
    print("SCREENSHOTS", json.dumps(screenshot_paths, ensure_ascii=False))
    print("TMP", tmp_path)
    shutil.rmtree(tmp_path, ignore_errors=True)
    print("TMP_CLEANED", True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
