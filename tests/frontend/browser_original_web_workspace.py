"""Task 5A 浏览器矩阵：真实 control/gateway/worker 进程内链路 + 真实 dist。

不启动 Docker，不使用用户 Cookie/Key/容器。worker 复用 web.app 的真实
handler，数据根是临时 workspace；control 使用真实 Auth/Store/WorkspaceApi，
gateway 使用真实 Ed25519 验签。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import ipaddress
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
BRAND_SOURCE = ROOT / "web" / "static" / "easel-icon-transparent.png"
EVIDENCE = ROOT / "docs" / "evidence" / "original-web-task-05a" / "browser"
BRAND_SHA256 = "71c260f5a28e82aced0a513186ebdcf43782b068633e3a7ca5f159d3e3606132"
SCREENSHOTS: list[Path] = []


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


class Server:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.workspace = root / "workspace"
        (self.workspace / "outputs").mkdir(parents=True)
        (self.workspace / "profiles").mkdir(parents=True)
        self._seed_workspace()
        self._seed_control()
        self._build_apps()
        self._build_server()

    def _seed_workspace(self) -> None:
        outputs = self.workspace / "outputs"
        project = outputs / "项目"
        project.mkdir()
        (project / "文案.txt").write_text("A 私有正文", encoding="utf-8")
        (project / "卡片.html").write_text(
            "<!doctype html><html><body><h1 id='card'>卡片</h1>"
            "<script>document.title='卡片标题'</script></body></html>",
            encoding="utf-8")
        shutil.copyfile(BRAND_SOURCE, project / "封面.png")
        self.brand_bytes = (project / "封面.png").read_bytes()
        (project / "素材").mkdir()
        (project / "素材" / "图.png").write_bytes(self.brand_bytes[:2048])
        (project / "素材" / "文档.html").write_text(
            "<html>zip</html>", encoding="utf-8")
        import zipfile

        with zipfile.ZipFile(project / "素材.zip", "w") as archive:
            archive.writestr("图.png", self.brand_bytes[:2048])
            archive.writestr("文档.html", "<html>zip</html>")
        persona = self.workspace / "profiles" / "测试画像"
        persona.mkdir()
        (persona / "identity.md").write_text(
            "# 测试画像\n\n原页面浏览器测试定位", encoding="utf-8")
        for name in ("style.md", "audience.md", "platforms.md",
                     "preferences.md", "memory.md"):
            (persona / name).write_text(f"{name} 初始内容", encoding="utf-8")

    def _seed_control(self) -> None:
        from easel.control.auth import AuthService
        from easel.control.store import Store
        from easel.control.types import WorkerHandle

        self.store = Store.connect(self.root / "control.sqlite3")
        self.admin = self.store.create_user("BrowserAdmin", "admin")
        self.member = self.store.create_user("BrowserMember")
        self.auth = AuthService(self.store)
        self.invites = [
            self.auth.create_invite(self.admin, self.member.user_id)
            for _ in range(12)
        ]
        self.workspace_id = self.member.workspace_id
        self.container_id = f"{0xB5A:064x}"
        self.handle = WorkerHandle(
            self.workspace_id, self.container_id,
            "http://127.0.0.1:7860", "service-browser", 0,
            gateway_instance_id="gateway-browser",
            gateway_generation="boot-browser",
            gateway_upstream="http://gateway-browser:7860",
            gateway_state="available")
        now = int(time.time())
        outcome, lease = self.store.claim_worker_start(
            self.workspace_id, 0, "browser-owner", now, 3600)
        assert outcome == "started", outcome
        assert self.store.complete_worker_start(
            self.workspace_id, "browser-owner", lease.generation,
            self.handle, now, 3600)

    def _build_apps(self) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        from easel.control.app import ControlConfig, create_app
        from easel.control.gateway_client import GatewayClient
        from easel.control.workspace_api import WorkspaceApi
        from easel.gateway.app import GatewayConfig, create_gateway_app
        from web import app as worker_app

        # 固定临时数据根；本夹具只服务一个 workspace。
        worker_app.OUTPUTS_DIR = self.workspace / "outputs"
        worker_app.PROFILES_DIR = self.workspace / "profiles"
        worker_app.LOGIN_DIR = worker_app.OUTPUTS_DIR / "_login"
        worker_app.PUBLISH_DIR = worker_app.OUTPUTS_DIR / "_publish"
        worker_app.PROFILE_BUILD_DIR = worker_app.OUTPUTS_DIR / "_profile_build"
        worker_app.DEBUG_DIR = worker_app.OUTPUTS_DIR / "_debug"
        worker_app.SESSIONS_DIR = worker_app.OUTPUTS_DIR / "_sessions"
        worker_app._harness_settings = lambda: {"runtime": "api"}
        worker_app._direct_api_enabled = lambda: False
        from easel import persona

        persona.PROFILES_DIR = worker_app.PROFILES_DIR
        self.worker_app = worker_app

        self.key = Ed25519PrivateKey.generate()
        self.worker_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=worker_app.app),
            trust_env=False)
        gateway_config = GatewayConfig(
            workspace_id=self.workspace_id,
            gateway_instance_id="gateway-browser",
            boot_generation="boot-browser",
            worker_container_id=self.container_id,
            config_version=0,
            worker_origin="http://127.0.0.1:7860",
            worker_service_token="service-browser",
        )
        self.gateway_app = create_gateway_app(
            gateway_config, self.key.public_key(), activated=True,
            http_client=self.worker_client)
        self.gateway_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.gateway_app),
            trust_env=False)

        class Runtime:
            def __init__(self, handle):
                self.handle = handle

            async def ensure_ready(self, workspace_id, config_version):
                assert workspace_id == self.handle.workspace_id
                assert config_version == self.handle.config_version
                return self.handle

        self.runtime = Runtime(self.handle)

        def workspace_factory(runtime, store, _http_client, db_runner):
            return WorkspaceApi(
                runtime, store,
                GatewayClient(self.key, self.gateway_client),
                revision=0, db_runner=db_runner, gateway_required=True)

        self.origin = ""
        self.app = None

        # 端口先绑定，再用真实 https Origin 构建 control。
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
        self.store.close()
        return self

    def stop(self) -> None:
        self.uvicorn.should_exit = True
        self.thread.join(timeout=20)
        try:
            self.sock.close()
        except OSError:
            pass
        async def close_clients():
            for client in (self.gateway_client, self.worker_client):
                try:
                    await client.aclose()
                except Exception:
                    pass

        try:
            asyncio.run(close_clients())
        except Exception:
            pass

    def paths(self) -> list[str]:
        return []


async def wait_for(predicate, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(message)


def quote_path(path: str) -> str:
    return "/".join(quote(part, safe="-._~") for part in path.split("/"))


async def login(page) -> None:
    await page.goto(SERVER.origin, wait_until="domcontentloaded")
    await page.fill("#invite", SERVER.invites.pop(0))
    await page.get_by_role("button", name="进入工作区").click()
    await page.locator(".sidebar").wait_for(timeout=20000)
    await page.locator(
        ".chat-page, .unavailable-page, .gallery-page, .profile-page"
    ).first.wait_for(timeout=20000)
    await page.locator(".persona-select").wait_for(timeout=20000)
    await page.locator(
        '.persona-select option', has_text="测试画像").wait_for(
            state="attached", timeout=20000)


async def open_outputs(page) -> None:
    await page.get_by_role("button", name="内容库").click()
    await page.locator(".gcard", has_text="项目").first.wait_for(
        timeout=20000)
    await page.locator(".gcard", has_text="项目").first.click()
    await page.get_by_text("封面.png").first.wait_for(timeout=20000)


async def screenshot(page, name: str) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / name
    await page.screenshot(path=str(path), full_page=False)
    SCREENSHOTS.append(path)


async def scenario_desktop_light(browser, results, failures):
    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        ignore_https_errors=True, color_scheme="light")
    page = await context.new_page()
    console: list[str] = []
    api_requests: list[str] = []
    page.on("console", lambda message: console.append(message.text))
    page.on("request", lambda request: api_requests.append(request.url))
    try:
        await login(page)
        await open_outputs(page)
        await page.get_by_text("文案.txt").first.click()
        await page.get_by_text("A 私有正文").first.wait_for(timeout=10000)
        await screenshot(page, "01-outputs-light.png")
        await page.locator(".drawer .icon-btn").click()
        await page.get_by_text("封面.png").first.click()
        image = page.locator(".drawer img")
        await image.wait_for(timeout=10000)
        natural = await image.evaluate(
            "(img) => [img.naturalWidth, img.naturalHeight]")
        assert natural == [800, 800], natural
        await screenshot(page, "02-media-light.png")
        await page.locator(".drawer .icon-btn").click()

        # 真实 control → gateway → worker 的 HEAD/Range/304。
        media = f"{SERVER.origin}/api/media/{quote_path('项目/封面.png')}"
        ranged = await context.request.get(
            media, headers={"Range": "bytes=0-9"})
        assert ranged.status == 206, (ranged.status, dict(ranged.headers))
        assert (await ranged.body()) == SERVER.brand_bytes[:10]
        assert ranged.headers["content-range"] == "bytes 0-9/398752"
        assert ranged.headers["content-type"].startswith("image/png")
        assert ranged.headers["cache-control"].startswith("private")
        head = await context.request.head(media)
        assert head.status == 200
        assert head.headers["content-length"] == "398752"
        assert (await head.body()) == b""
        etag = ranged.headers["etag"]
        assert etag.startswith('"easel-')
        # 原 worker FileResponse 只实现 Range，不实现 If-None-Match；保留其
        # 200 合约，同时确认 control 的作用域 ETag 不会被跨身份复用。
        conditional = await context.request.get(
            media, headers={"If-None-Match": etag})
        assert conditional.status in (200, 304), conditional.status
        assert conditional.headers["etag"] == etag
        if conditional.status == 200:
            assert (await conditional.body()) == SERVER.brand_bytes

        # HTML 预览：iframe 必须是最小 sandbox，不含 allow-same-origin。
        await page.get_by_text("卡片.html").first.click()
        frame = page.locator(".drawer iframe")
        await frame.wait_for(timeout=10000)
        sandbox = await frame.get_attribute("sandbox")
        assert sandbox == "allow-scripts", sandbox
        assert "allow-same-origin" not in (sandbox or "")
        html = await context.request.get(
            f"{SERVER.origin}/api/media/{quote_path('项目/卡片.html')}")
        assert html.status == 200
        assert html.headers["x-content-type-options"] == "nosniff"
        assert "sandbox" in html.headers["content-security-policy"]
        await screenshot(page, "03-html-preview-light.png")
        await page.locator(".drawer .icon-btn").click()

        # 删除只影响本 workspace。
        page.on("dialog", lambda dialog: asyncio.ensure_future(
            dialog.accept()))
        card = page.locator(".gcard", has_text="文案.txt")
        await card.hover()
        await card.locator(".gcard-del").click()
        await page.get_by_text("文案.txt").first.wait_for(
            state="detached", timeout=10000)
        assert not (SERVER.workspace / "outputs" / "项目" / "文案.txt").exists()
        assert not any("/api/upload/local" in url for url in api_requests)
        assert not any("/api/skills" in url for url in api_requests)
        assert not any("/api/admin" in url for url in api_requests)
        results["outputs_media_delete_light"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(f"desktop-light: {error!r}")
    finally:
        await context.close()


async def scenario_profile_and_attachment(browser, results, failures):
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900},
        ignore_https_errors=True, color_scheme="light")
    page = await context.new_page()
    try:
        await login(page)
        await page.get_by_role("button", name="画像").click()
        await page.get_by_role("heading", name="测试画像").wait_for(
            timeout=20000)
        await page.get_by_role("button", name="查看 / 编辑").click()
        await page.get_by_role("button", name="✏️ 编辑资料").click()
        style = page.locator("textarea").nth(1)
        await style.fill("浏览器保存的新风格")
        await page.get_by_role("button", name="保存", exact=True).click()
        await page.get_by_text("已保存 内容风格").wait_for(timeout=10000)
        saved = (SERVER.workspace / "profiles" / "测试画像" / "style.md")
        assert saved.read_text(encoding="utf-8") == "浏览器保存的新风格"
        await screenshot(page, "04-profile-edit-light.png")
        await page.get_by_role("button", name="← 全部画像").click()
        await page.locator(".section-title", has_text="我的画像").wait_for(
            timeout=10000)
        new_profile = page.get_by_role("button", name="新建画像")
        assert not await new_profile.is_disabled()
        assert "后续版本接入" not in (await page.text_content("body") or "")
        assert "抓取公众号数据" not in (await page.text_content("body") or "")

        await page.get_by_role("button", name="对话", exact=True).click()
        await page.get_by_role("button", name="新建对话").click()
        await page.locator(".composer").wait_for(timeout=10000)
        upload = SERVER.root / "附件.txt"
        upload.write_text("浏览器附件正文", encoding="utf-8")
        await page.locator('input[type="file"]').set_input_files(str(upload))
        await page.locator(".attach-chip").wait_for(timeout=15000)
        inbox = list((SERVER.workspace / "outputs" / "_inbox").rglob(
            "附件.txt"))
        assert len(inbox) == 1, inbox
        assert inbox[0].read_text(encoding="utf-8") == "浏览器附件正文"
        await page.locator(".send-btn").click()
        await page.get_by_text("请先输入文字，再发送附件。").wait_for(
            timeout=5000)
        assert "浏览器附件正文" not in (await page.text_content("body") or "")
        await screenshot(page, "05-attachment-upload-light.png")
        results["profile_and_attachment"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(f"profile-attachment: {error!r}")
    finally:
        await context.close()


async def scenario_dark_and_mobile(browser, results, failures):
    try:
        dark = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            ignore_https_errors=True, color_scheme="dark")
        page = await dark.new_page()
        await login(page)
        await open_outputs(page)
        tokens = await page.evaluate("""() => {
          const style = getComputedStyle(document.documentElement);
          return [style.getPropertyValue('--bg').trim(),
                  style.getPropertyValue('--surface').trim(),
                  style.getPropertyValue('--text').trim()];
        }""")
        assert tokens[0] not in ("#fff", "#ffffff", ""), tokens
        await screenshot(page, "06-outputs-dark.png")
        await dark.close()

        mobile = await browser.new_context(
            viewport={"width": 390, "height": 844},
            ignore_https_errors=True, color_scheme="light")
        page = await mobile.new_page()
        await login(page)
        await open_outputs(page)
        overflow = await page.evaluate(
            "() => document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 1, overflow
        await screenshot(page, "07-outputs-mobile.png")
        await mobile.close()
        results["dark_mobile"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(f"dark-mobile: {error!r}")


async def scenario_logout_media(browser, results, failures):
    context = await browser.new_context(
        viewport={"width": 1280, "height": 800}, ignore_https_errors=True)
    page = await context.new_page()
    try:
        await login(page)
        media = f"{SERVER.origin}/api/media/{quote_path('项目/封面.png')}"
        assert (await context.request.get(media)).status == 200
        await page.get_by_role("button", name="退出").click()
        await page.locator("#invite").wait_for(timeout=15000)
        unauthenticated = await context.request.get(media)
        assert unauthenticated.status == 401, await unauthenticated.text()
        results["logout_media_401"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(f"logout-media: {error!r}")
    finally:
        await context.close()


async def main() -> int:
    if not DIST.is_dir() or not (DIST / "index.html").is_file():
        print("缺少 dist，请先 npm --prefix web/frontend run build")
        return 2
    global SERVER
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="easel-task5a-browser-"))
    try:
        SERVER = Server(root).start()
        results: dict[str, bool] = {}
        failures: list[str] = []
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            print("BROWSER_VERSION", browser.version)
            await scenario_desktop_light(browser, results, failures)
            await scenario_profile_and_attachment(browser, results, failures)
            await scenario_dark_and_mobile(browser, results, failures)
            await scenario_logout_media(browser, results, failures)
            await browser.close()
        print("TASK5A_BROWSER_RESULTS", results)
        print("FAILURES", failures)
        print("SCREENSHOTS", [str(path.relative_to(ROOT)) for path in SCREENSHOTS])
        ok = not failures and len(results) >= 4
        print("TASK5A_BROWSER_OK", ok)
        return 0 if ok else 1
    finally:
        try:
            SERVER.stop()
        except Exception:
            pass
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
