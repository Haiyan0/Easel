"""Task 5C3 浏览器矩阵：真实 control/gateway/worker + 受控发布执行器。

真实层：原 React 构建、HTTPS control、gateway、WorkspaceApi、WorkerRelay、
worker RunManager、WorkspacePublishing、browser_tasks 发布执行器、ZIP 导出。
替身层：平台发布子进程只写本人临时状态，技能任务返回固定稿件；不访问真实
平台、不启动真实浏览器、不读取真实凭证。
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
import zipfile
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
EVIDENCE = ROOT / "docs" / "evidence" / "original-web-task-05c3" / "browser"
SHOTS: list[Path] = []
FAILURES: list[str] = []
RESULTS: dict[str, bool] = {}

PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8"
    b"\xff\xff?\x00\x05\xfe\x02\xfe\xa75\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82")
VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"fixture-video"


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


class FakePublisher:
    """受控发布子进程：写本人状态文件，等待当前 Run 的验证码文件。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.cancelled = 0

    async def __call__(self, platform, cmd, env, status_file, code_file,
                       timeout, emit):
        self.calls.append({
            "platform": platform, "cmd": list(cmd),
            "status_file": status_file, "code_file": code_file,
        })
        try:
            if platform == "douyin" and status_file is not None:
                status_file.parent.mkdir(parents=True, exist_ok=True)
                self._status(status_file, emit, "sms_required",
                             "请输入受控短信验证码")
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    if code_file is not None and code_file.is_file():
                        code = code_file.read_text(encoding="utf-8").strip()
                        self._status(status_file, emit, "verifying",
                                     "正在验证验证码…")
                        if code == "123456":
                            self._status(status_file, emit, "success",
                                         "发布成功（受控读回）")
                            return {"returncode": 0, "status": {
                                "state": "success",
                                "message": "发布成功（受控读回）"}}
                        self._status(status_file, emit, "sms_required",
                                     "验证码不正确")
                    await asyncio.sleep(0.05)
                return {"returncode": 1, "status": {
                    "state": "error", "message": "短信验证超时"}}
            return {"returncode": 0, "status": None}
        except asyncio.CancelledError:
            self.cancelled += 1
            raise

    @staticmethod
    def _status(path: Path, emit, state: str, message: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"state": state, "message": message}), encoding="utf-8")
        emit("activity", json.dumps(
            {"event": "browser_status", "state": state,
             "message": message}, ensure_ascii=False))


async def fake_skill_executor(request, context, bind, emit, ask):
    """受控技能执行器：只用于排版/检查，不访问模型。"""
    del bind, ask
    prompt = request.prompt or ""
    if "===小红书===" in prompt or "跨平台内容改编" in prompt:
        return "===小红书===\n受控排版正文\n\n===抖音===\n受控抖音正文"
    if "人设" in prompt or "persona" in prompt:
        return "人设一致性：受控通过"
    return "发布前检查：未见明显风险"


class Server:
    def __init__(self, root: Path, *, publish_mode: str) -> None:
        self.root = root
        self.publish_mode = publish_mode
        self.workspace_root = root / "workspace"
        for name in ("outputs", "profiles", "state", "home"):
            (self.workspace_root / name).mkdir(parents=True)
        self.outputs = self.workspace_root / "outputs"

        from web import app as worker
        worker.OUTPUTS_DIR = self.outputs
        worker.PUBLISH_DIR = self.outputs / "_publish"
        worker.PROFILES_DIR = self.workspace_root / "profiles"
        worker.LOGIN_DIR = self.outputs / "_login"
        worker._SERVICE_MODE = "worker"
        from easel import persona
        persona.PROFILES_DIR = worker.PROFILES_DIR
        self.fake = FakePublisher()
        worker._execute_managed_publish = self.fake
        self.worker_app = worker

        from easel.control.app import ControlConfig, create_app
        from easel.control.auth import AuthService
        from easel.control.gateway_client import GatewayClient
        from easel.control.proxy import PinnedRunConfig, WorkerRelay
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
            self.member.workspace_id, f"{0x5C3:064x}",
            "http://127.0.0.1:7860", "service-browser-5c3", 0,
            gateway_instance_id="gateway-browser-5c3",
            gateway_generation="boot-browser-5c3",
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
            executor=fake_skill_executor, recover=False)
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
            config=ControlConfig(public_origin=self.origin,
                                 publish_mode=publish_mode),
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
        self.seed()

    def seed(self) -> None:
        project = self.outputs / "project"
        project.mkdir(parents=True, exist_ok=True)
        (project / "cover.png").write_bytes(PNG_1PX)
        (project / "clip.mp4").write_bytes(VIDEO_BYTES)
        login = self.outputs / "_login"
        login.mkdir(parents=True, exist_ok=True)
        for platform in ("xiaohongshu", "douyin"):
            (login / f"{platform}.json").write_text(json.dumps(
                {"state": "success", "message": "受控已登录",
                 "qr": "", "ts": int(time.time())}, ensure_ascii=False),
                encoding="utf-8")

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


def schedule_items(server: "Server") -> list[dict]:
    path = server.outputs / "_schedule.json"
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


async def screenshot(page, name: str) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / name
    await page.screenshot(path=str(path), full_page=False)
    SHOTS.append(path)


async def login(page, server: Server) -> None:
    invite = server.invites.pop(0)
    await page.goto(server.origin, wait_until="domcontentloaded", timeout=20000)
    await page.fill("#invite", invite)
    await page.get_by_role("button", name="进入工作区").click()
    await page.locator(".sidebar").wait_for(timeout=20000)


async def open_publish(page) -> None:
    tab = page.locator(".subnav-tab", has_text="发布中心")
    if await tab.count():
        await tab.click()
    else:
        await page.locator(".nav-item", has_text="工作台").click()
        await page.get_by_role("button", name="去发布").click()
    await page.locator(".publishing-workspace").wait_for(timeout=20000)


async def check(name: str, fn) -> None:
    print(f"[5C3] scenario start: {name}", flush=True)
    try:
        await asyncio.wait_for(fn(), timeout=180)
        RESULTS[name] = True
        print(f"[5C3] scenario ok: {name}", flush=True)
    except Exception:  # noqa: BLE001 - 汇总所有场景
        RESULTS[name] = False
        FAILURES.append(f"{name}: {traceback.format_exc()}")
        print(f"[5C3] scenario FAILED: {name}", flush=True)
        traceback.print_exc()


async def scenario_manual_flow(browser, server: Server) -> None:
    context = await browser.new_context(
        viewport={"width": 1360, "height": 900}, ignore_https_errors=True)
    page = await context.new_page()
    page.set_default_timeout(20000)
    await login(page, server)
    await open_publish(page)
    await page.get_by_role("button", name="人工审核发布").click()
    await page.locator("#manual-title").wait_for(timeout=20000)

    # 只保留小红书，避免默认抖音卡片干扰。
    await page.get_by_role("button", name="抖音", exact=True).click()
    await page.fill("#manual-title", "浏览器人工稿件")
    await page.fill("#manual-body", "第一段\n\n第二段")
    await page.fill("#manual-tags", "工具,日常")
    await page.get_by_role("button", name="AI 排版 / 适配").click()
    await wait_for(
        lambda: preview_contains(page, "受控排版正文"), 40,
        "manual AI adaptation did not render")
    card = page.locator(".pv-card", has_text="小红书")
    assert "待人工审核" in await card.inner_text()
    export = card.get_by_role("button", name="导出稿件和素材 ZIP")
    assert await export.is_disabled()

    await card.get_by_label("小红书人工审核").check()
    await wait_for(
        lambda: card_contains(card, "已人工审核"), 10, "review did not apply")
    assert not await export.is_disabled()

    async with page.expect_download() as download_info:
        await export.click()
    download = await download_info.value
    package = await download.path()
    assert package is not None
    with zipfile.ZipFile(package) as archive:
        names = set(archive.namelist())
        assert "title.txt" in names and "manifest.json" in names
        assert archive.read("title.txt").decode() == "浏览器人工稿件"
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["status"] == "ready_for_manual_submission"
        assert manifest["platform"] == "xiaohongshu"

    # 编辑标题后审核失效。
    await page.fill("#manual-title", "浏览器人工稿件 v2")
    await wait_for(
        lambda: card_contains(card, "待人工审核"), 10,
        "editing did not invalidate review")
    assert await export.is_disabled()
    await card.get_by_label("小红书人工审核").check()
    await wait_for(
        lambda: card_contains(card, "已人工审核"), 10, "re-review failed")

    await page.get_by_role("button", name="存草稿并排期").click()
    await wait_for(lambda: len(schedule_items(server)) == 1, 10,
                   "calendar draft was not saved")
    assert schedule_items(server)[0]["status"] == "draft"

    await page.locator("summary", has_text="发布后，回填作品链接").click()
    await page.get_by_label("小红书作品链接").fill(
        "https://example.test/works/1")
    await page.get_by_role(
        "button", name="我已在官方平台发布，记录到日历").click()
    await wait_for(lambda: len(schedule_items(server)) == 2, 10,
                   "manual published record was not saved")
    published = schedule_items(server)[1]
    assert published["status"] == "published"
    assert "人工确认已发布" in published["note"]
    assert "未自动验证平台状态" in published["note"]

    await screenshot(page, "01-manual-export-review.png")
    # 移动端无横向滚动，并保留同一页面。
    await page.set_viewport_size({"width": 390, "height": 844})
    await asyncio.sleep(0.4)
    overflow = await page.evaluate(
        "() => document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 1, overflow
    await screenshot(page, "02-manual-mobile.png")
    # 深色主题沿用原 toggle。
    await page.set_viewport_size({"width": 1360, "height": 900})
    await page.locator(".theme-toggle").click()
    await wait_for(
        lambda: page.evaluate(
            "() => document.documentElement.dataset.theme === 'dark'"), 5,
        "theme did not switch")
    await screenshot(page, "03-manual-dark.png")
    await context.close()


async def card_contains(card, needle: str) -> bool:
    return needle in await card.inner_text()


async def preview_contains(page, needle: str) -> bool:
    texts = await page.locator(".pv-text").all_inner_texts()
    return any(needle in text for text in texts)


async def scenario_policy_automated(browser, server: Server) -> None:
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
    page = await context.new_page()
    page.set_default_timeout(20000)
    await login(page, server)
    await open_publish(page)
    await page.get_by_role("button", name="自动发布").wait_for()
    assert await page.get_by_role("button", name="自动发布").is_enabled()
    assert "发布平台" in await page.locator(".publish-editor").inner_text()
    await screenshot(page, "04-policy-automated.png")
    await context.close()


async def scenario_policy_manual(browser, server: Server) -> None:
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
    page = await context.new_page()
    page.set_default_timeout(20000)
    await login(page, server)
    await open_publish(page)
    await wait_for(
        lambda: page.get_by_role("button", name="自动发布").is_disabled(), 10,
        "manual policy did not disable automatic tab")
    hint = await page.locator(".pv-hint").first.inner_text()
    assert "管理员已禁用自动发布" in hint, hint
    assert await page.locator("#manual-title").count() == 1
    await screenshot(page, "05-policy-manual.png")
    await context.close()


async def _chip_active(page, label: str) -> bool:
    return await page.get_by_role(
        "button", name=label, exact=True).evaluate(
            "el => el.classList.contains('active')")


async def _fill_auto_draft(page, title: str, media_path: str | None) -> None:
    await page.locator(".publish-editor input.field").first.fill(title)
    await page.locator(".publish-editor textarea.field").first.fill("自动发布正文")
    # 只保留抖音；重复调用时不要反向切换。
    if await _chip_active(page, "小红书"):
        await page.get_by_role("button", name="小红书", exact=True).click()
    if not await _chip_active(page, "抖音"):
        await page.get_by_role("button", name="抖音", exact=True).click()
    if await page.locator(".media-grid").count() == 0:
        await page.get_by_role("button", name="选择媒体").click()
    await page.locator(".media-grid").wait_for(timeout=20000)
    try:
        await wait_for(
            lambda: media_cell_ready(page, media_path), 20,
            f"media cell missing: {media_path}")
    except AssertionError:
        print("AUTO EDITOR", (await page.locator(
            ".publish-editor").inner_text())[:1200], flush=True)
        print("AUTO CELLS", await page.locator(".media-cell").evaluate_all(
            "els => els.map(e => e.title)"), flush=True)
        raise
    index = await media_cell_index(page, media_path)
    cell = page.locator(".media-cell").nth(index)
    selected = "sel" in ((await cell.get_attribute("class")) or "")
    if not selected:
        await cell.click()


async def media_cell_index(page, path: str) -> int | None:
    titles = await page.locator(".media-cell").evaluate_all(
        "els => els.map(e => e.title)")
    try:
        return titles.index(path)
    except ValueError:
        return None


async def media_cell_ready(page, path: str) -> bool:
    return await media_cell_index(page, path) is not None


async def unknown_state_count(page) -> int:
    return await page.locator(".pv-pubstate.unknown").count()


async def modal_open(page) -> int:
    return await page.locator(".modal").count()


async def _submit_publish(page) -> None:
    page.on("dialog", lambda dialog: asyncio.ensure_future(dialog.accept()))
    await page.get_by_role("button", name="一键发布").click()


async def _wait_sms(page) -> None:
    await page.locator(".modal").wait_for(timeout=30000)
    text = await page.locator(".modal").inner_text()
    assert "抖音" in text, text


async def _submit_code(page, code: str) -> None:
    await page.get_by_placeholder("请输入手机收到的验证码").fill(code)
    await page.get_by_role("button", name="提交验证码").click()


async def modal_contains(page, needle: str) -> bool:
    if await page.locator(".modal").count() == 0:
        return False
    return needle in await page.locator(".modal").inner_text()


async def close_modal_if_open(page) -> None:
    button = page.get_by_role("button", name="关闭")
    if await button.count():
        await button.first.click()


async def scenario_auto_publish(browser, server: Server) -> None:
    context = await browser.new_context(
        viewport={"width": 1360, "height": 900}, ignore_https_errors=True)
    page = await context.new_page()
    page.set_default_timeout(20000)
    await login(page, server)
    await open_publish(page)
    await page.get_by_role("button", name="自动发布").click()
    await page.locator(".publish-editor").wait_for(timeout=20000)

    await _fill_auto_draft(page, "自动稿件 1", "project/clip.mp4")
    await _submit_publish(page)
    await _wait_sms(page)
    await _submit_code(page, "000000")
    await wait_for(
        lambda: modal_contains(page, "验证码不正确"), 20,
        "wrong code feedback missing")
    await _submit_code(page, "123456")
    await page.locator(".pv-pubstate.ok").wait_for(timeout=30000)
    assert server.fake.calls and server.fake.calls[0]["platform"] == "douyin"
    staged = [str(part) for part in server.fake.calls[0]["cmd"]]
    assert any("_publish" in part and "runs" in part for part in staged)
    await screenshot(page, "06-auto-publish-success.png")
    assert len(server.fake.calls) == 1
    await close_modal_if_open(page)

    # 查询状态暂时失败一次：继续轮询当前 Run，不自动补发。
    calls_before = len(server.fake.calls)
    failures = {"count": 1}

    async def flaky_status(route):
        if failures["count"] > 0:
            failures["count"] -= 1
            await route.abort("failed")
            return
        await route.continue_()

    await page.route("**/api/publish/douyin/status", flaky_status)
    await _fill_auto_draft(page, "自动稿件 2", "project/clip.mp4")
    await _submit_publish(page)
    await _wait_sms(page)
    await _submit_code(page, "123456")
    await page.locator(".pv-pubstate.ok").wait_for(timeout=30000)
    await page.unroute("**/api/publish/douyin/status", flaky_status)
    assert len(server.fake.calls) == calls_before + 1
    await close_modal_if_open(page)

    # 停止本地执行：平台结果明确标为未知，不补发。
    calls_before = len(server.fake.calls)
    await _fill_auto_draft(page, "自动稿件 3", "project/clip.mp4")
    await _submit_publish(page)
    await _wait_sms(page)
    await close_modal_if_open(page)
    stop_started = time.monotonic()
    async with page.expect_response(
            lambda response: "/api/chat/stop" in response.url) as stop_info:
        await page.get_by_role("button", name="停止本地执行").click()
    stop_response = await stop_info.value
    print("STOP RESP", stop_response.status,
          round(time.monotonic() - stop_started, 2), flush=True)
    try:
        await wait_for(
            lambda: unknown_state_count(page), 20,
            "stopped run did not become unknown")
    except AssertionError:
        print("STOP STATES", await page.locator(
            ".pv-pubstate").all_inner_texts(), flush=True)
        print("STOP CLASSES", await page.locator(
            ".pv-pubstate").evaluate_all(
                "els => els.map(e => e.className)"), flush=True)
        print("STOP UNKNOWN COUNT", await page.locator(
            ".pv-pubstate.unknown").count(), flush=True)
        print("STOP TOASTS", await page.locator(
            ".toast").all_inner_texts(), flush=True)
        print("STOP CALLS", len(server.fake.calls),
              "cancelled", server.fake.cancelled, flush=True)
        raise
    text = await page.locator(".pv-pubstate.unknown").inner_text()
    assert "平台结果未知" in text, text
    assert len(server.fake.calls) == calls_before + 1

    # 刷新后找回原任务：不创建新 Run，继续当前短信。
    calls_before = len(server.fake.calls)
    await _fill_auto_draft(page, "自动稿件 4", "project/clip.mp4")
    await _submit_publish(page)
    await _wait_sms(page)
    await page.reload(wait_until="domcontentloaded")
    await page.locator(".sidebar").wait_for(timeout=20000)
    await open_publish(page)
    await wait_for(
        lambda: modal_open(page), 20,
        "refresh did not recover the pending publish run")
    await _wait_sms(page)
    await _submit_code(page, "123456")
    await page.locator(".pv-pubstate.ok").wait_for(timeout=30000)
    assert len(server.fake.calls) == calls_before + 1
    await close_modal_if_open(page)
    await screenshot(page, "07-auto-refresh-recovered.png")
    await context.close()


async def scenario_duplicate_submit(browser, server: Server) -> None:
    context = await browser.new_context(
        viewport={"width": 1360, "height": 900}, ignore_https_errors=True)
    page = await context.new_page()
    page.set_default_timeout(20000)
    await login(page, server)
    await open_publish(page)
    await page.get_by_role("button", name="自动发布").click()
    await page.locator(".publish-editor").wait_for(timeout=20000)
    await _fill_auto_draft(page, "重复提交稿件", "project/clip.mp4")
    calls_before = len(server.fake.calls)
    page.on("dialog", lambda dialog: asyncio.ensure_future(dialog.accept()))
    # 同一按钮连续点击两次：服务端只允许一个在途 Run。
    await page.evaluate("""
      () => {
        const buttons = [...document.querySelectorAll('button')];
        const target = buttons.find(b => b.textContent.includes('一键发布'));
        if (!target) throw new Error('publish button missing');
        target.click();
        target.click();
      }
    """)
    await _wait_sms(page)
    await asyncio.sleep(0.5)
    assert len(server.fake.calls) == calls_before + 1, server.fake.calls
    await _submit_code(page, "123456")
    await page.locator(".pv-pubstate.ok").wait_for(timeout=30000)
    await close_modal_if_open(page)
    await context.close()


async def main() -> int:
    if not DIST.is_dir():
        print("frontend build missing", flush=True)
        return 1
    work = ROOT / ".test-tmp" / f"browser-5c3-{int(time.time())}"
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                # 同一 worker 模块只能绑定一个 Server；自动/手动策略分两轮启动。
                automated = Server(
                    work / "automated", publish_mode="automated").start()
                try:
                    await check(
                        "manual-export-review-calendar",
                        lambda: scenario_manual_flow(browser, automated))
                    await check(
                        "policy-automated",
                        lambda: scenario_policy_automated(browser, automated))
                    await check(
                        "auto-publish-sms-stop-refresh",
                        lambda: scenario_auto_publish(browser, automated))
                    await check(
                        "duplicate-submit-once",
                        lambda: scenario_duplicate_submit(browser, automated))
                finally:
                    await automated.stop()
                manual = Server(
                    work / "manual", publish_mode="manual").start()
                try:
                    await check(
                        "policy-manual",
                        lambda: scenario_policy_manual(browser, manual))
                finally:
                    await manual.stop()
            finally:
                await browser.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(json.dumps({
        "results": RESULTS,
        "failures": FAILURES,
        "screenshots": [str(path) for path in SHOTS],
    }, ensure_ascii=False, indent=2), flush=True)
    print("TASK5C3_BROWSER_OK", not FAILURES, flush=True)
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
