"""Task 5C1 浏览器矩阵：真实 control/gateway/worker Run 链路。

worker 使用真实 web.app 内部 Run 路由与真实 RunManager；执行器是夹具内的
受控 ACP 替身，只返回固定文本，不调用真实供应商或平台。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import os
import re
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

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
    str(ROOT / "docs" / "evidence" / "original-web-task-05c1" / "R2" / "browser")))
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
        self.workspace_root = root / "workspace"
        (self.workspace_root / "outputs").mkdir(parents=True)
        (self.workspace_root / "profiles").mkdir(parents=True)
        (self.workspace_root / "state").mkdir()
        (self.workspace_root / "home").mkdir()
        self.executions: list[tuple[str, str]] = []
        self.delay_skill = 0.0
        self._seed_workspace()
        self._seed_control()
        self._build_apps()
        self._build_server()

    def _seed_workspace(self) -> None:
        (self.workspace_root / "outputs" / "项目").mkdir()
        (self.workspace_root / "outputs" / "项目" / "文案.txt").write_text(
            "浏览器技能测试项目", encoding="utf-8")
        persona = self.workspace_root / "profiles" / "已有画像"
        persona.mkdir()
        (persona / "identity.md").write_text(
            "# 已有画像\n\n已有定位", encoding="utf-8")
        for name in ("style.md", "audience.md", "platforms.md",
                     "preferences.md", "memory.md"):
            (persona / name).write_text(f"{name} 内容", encoding="utf-8")

    def _seed_control(self) -> None:
        from easel.control.auth import AuthService
        from easel.control.store import Store
        from easel.control.types import WorkerHandle

        self.store = Store.connect(self.root / "control.sqlite3")
        self.admin = self.store.create_user("BrowserOwner", "admin")
        self.member = self.store.create_user("BrowserMember")
        self.auth = AuthService(self.store)
        self.invites = [
            self.auth.create_invite(self.admin, self.member.user_id)
            for _ in range(8)
        ]
        self.workspace_id = self.member.workspace_id
        self.handle = WorkerHandle(
            self.workspace_id, f"{0x5C1:064x}",
            "http://127.0.0.1:7860", "service-browser", 0,
            gateway_instance_id="gateway-browser",
            gateway_generation="boot-browser",
            gateway_upstream="http://gateway.internal:8897",
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
        from easel.control.proxy import PinnedRunConfig, WorkerRelay
        from easel.control.sessions import SessionService
        from easel.control.types import WorkspacePaths
        from easel.control.workspace_api import WorkspaceApi
        from easel.gateway.app import GatewayConfig, create_gateway_app
        from easel.worker.runs import RunManager
        from easel.worker.sessions import WorkspaceSessionStore
        from web import app as worker_app

        worker_app.OUTPUTS_DIR = self.workspace_root / "outputs"
        worker_app.PROFILES_DIR = self.workspace_root / "profiles"
        worker_app.SKILLS_DIR = ROOT / "skills"
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
            gateway_instance_id=self.handle.gateway_instance_id,
            boot_generation=self.handle.gateway_generation,
            worker_container_id=self.handle.container_id,
            config_version=0,
            worker_origin=self.handle.upstream,
            worker_service_token=self.handle.service_token,
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

            async def bound_handle(self, workspace_id, container, config_version):
                if (workspace_id != self.handle.workspace_id
                        or container != self.handle.container_id
                        or config_version != self.handle.config_version):
                    return None
                return self.handle

        self.runtime = Runtime(self.handle)
        self.paths = WorkspacePaths(
            code_root=ROOT,
            workspace_root=self.workspace_root,
            state_root=self.workspace_root / "state",
            home=self.workspace_root / "home",
        )

        async def executor(request, context, bind, emit, ask):
            self.executions.append((context.task_type, request.prompt))
            if context.task_type == "skill" and self.delay_skill:
                await asyncio.sleep(self.delay_skill)
            if context.task_type == "skill":
                if "ASK-SKILL" in request.prompt:
                    answers = await ask([{
                        "questionId": "permission",
                        "header": "技能授权",
                        "question": "允许执行这个技能？",
                        "options": ["允许", "拒绝"],
                    }])
                    if answers.get("permission") == ["拒绝"]:
                        emit("token", "SKILL-DENIED")
                        return "SKILL-DENIED"
                    emit("token", "SKILL-ANSWERED")
                    return "SKILL-ANSWERED"
                emit("token", "SKILL-RESULT：受控技能结果")
                return "SKILL-RESULT：受控技能结果"
            if context.task_type == "profile":
                match = re.search(r"画像「([^」]+)」", request.prompt)
                if (match is None or "/skill-profile-builder" not in request.prompt
                        or f"profiles/{match.group(1)}/" not in request.prompt):
                    raise RuntimeError("profile instruction/target missing")
                target = self.workspace_root / "profiles" / match.group(1)
                if not target.is_dir():
                    raise RuntimeError("profile target missing")
                (target / "memory.md").write_text(
                    (target / "memory.md").read_text(encoding="utf-8")
                    + "\n\nEXECUTOR-ENHANCED：按真实任务指令更新\n",
                    encoding="utf-8")
                if "ASK-PROFILE" in request.prompt:
                    first = await ask([{
                        "questionId": "analysis",
                        "header": "画像授权",
                        "question": "允许分析这个画像？",
                        "options": ["允许", "拒绝"],
                    }])
                    if first.get("analysis") == ["拒绝"]:
                        emit("token", "PROFILE-DENIED")
                        return "PROFILE-DENIED"
                    second = await ask([{
                        "questionId": "write",
                        "header": "写入授权",
                        "question": "允许写入画像文件？",
                        "options": ["允许", "拒绝"],
                    }])
                    if second.get("write") == ["拒绝"]:
                        emit("token", "PROFILE-WRITE-DENIED")
                        return "PROFILE-WRITE-DENIED"
                emit("token", "PROFILE-RESULT：受控画像增强")
                return "PROFILE-RESULT：受控画像增强"
            return "CHAT-RESULT"

        self.manager = RunManager(
            workspace_id=self.workspace_id, paths=self.paths,
            public_key=self.key.public_key(),
            sessions=WorkspaceSessionStore(
                self.paths.state_root, self.workspace_id),
            executor=executor,
            base_env={"PATH": "", "HOME": str(self.paths.home)},
            recover=False)
        worker_app._RUNS = self.manager

        def workspace_factory(runtime, store, _http_client, db_runner):
            return WorkspaceApi(
                runtime, store,
                GatewayClient(self.key, self.gateway_client),
                revision=0, db_runner=db_runner, gateway_required=True)

        def relay_factory(runtime, sessions, auth, store, _http_client,
                          broker, db_runner):
            return WorkerRelay(
                runtime, sessions, auth, store=store,
                signing_key=self.key,
                config_provider=lambda actor: PinnedRunConfig(
                    revision=0, routes={}, agent={
                        "runtime": "acp", "command": ["fixture-acp"],
                        "sandbox": "read-only", "timeout": 60,
                    }, broker=None),
                http_client=self.gateway_client,
                gateway_client=GatewayClient(
                    self.key, self.gateway_client),
                db_runner=db_runner, gateway_required=True)

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
            workspace_factory=workspace_factory, relay_factory=relay_factory)

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
            await self.manager.aclose()
        except Exception:
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


async def login(page) -> None:
    invite = SERVER.invites.pop(0)
    await page.goto(SERVER.origin, wait_until="domcontentloaded")
    await page.fill("#invite", invite)
    await page.get_by_role("button", name="进入工作区").click()
    await page.locator(".sidebar").wait_for(timeout=20000)
    await page.locator(".persona-select").wait_for(timeout=20000)


async def screenshot(page, name: str) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / name
    await page.screenshot(path=str(path), full_page=False)
    SCREENSHOTS.append(path)


async def scenario_skill_run(browser, results, failures):
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
    page = await context.new_page()
    responses: list[str] = []
    console: list[str] = []
    page.on("response", lambda response: responses.append(
        f"{response.status} {response.url}"))
    page.on("console", lambda message: console.append(message.text))
    try:
        await login(page)
        await page.get_by_role("button", name="技能库").click()
        await page.locator(".skills-page").wait_for(timeout=20000)
        card = page.locator(".skill-card-rawname").filter(
            has_text="copywriting").first
        await card.wait_for(timeout=20000)
        await card.click()
        await page.locator(".drawer").wait_for(timeout=20000)
        await page.locator(".drawer textarea").fill("写一条受控技能测试文案")
        await page.get_by_role("button", name="执行").click()
        await page.get_by_text("SKILL-RESULT", exact=False).wait_for(
            timeout=30000)
        body = await page.text_content("body") or ""
        assert "受管 API" not in body or "未配置" not in body
        assert any(task == "skill" and "copywriting" in prompt
                   for task, prompt in SERVER.executions), SERVER.executions
        await screenshot(page, "01-skill-run-light.png")

        # 刷新后重新打开同一技能，状态路由应找回上次结果，不重新派发 prompt。
        before = len(SERVER.executions)
        await page.reload(wait_until="domcontentloaded")
        await page.locator(".sidebar").wait_for(timeout=20000)
        await page.get_by_role("button", name="技能库").click()
        await page.locator(".skill-card-rawname").filter(
            has_text="copywriting").first.click()
        await page.locator(".drawer").wait_for(timeout=20000)
        await page.get_by_text("SKILL-RESULT", exact=False).wait_for(
            timeout=20000)
        assert len(SERVER.executions) == before, SERVER.executions
        await screenshot(page, "02-skill-result-refresh.png")
        results["skill_run"] = True
    except Exception as error:  # noqa: BLE001
        body = ""
        try:
            body = (await page.text_content("body") or "")[:1800]
        except Exception:
            pass
        failures.append(
            f"skill-run: {error!r} body={body!r} "
            f"responses={responses[-30:]!r} console={console[-10:]!r}")
    finally:
        await context.close()


async def scenario_profile_wizard(browser, results, failures):
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
    page = await context.new_page()
    responses: list[str] = []
    console: list[str] = []
    page.on("response", lambda response: responses.append(
        f"{response.status} {response.url}"))
    page.on("console", lambda message: console.append(message.text))
    try:
        await login(page)
        await page.get_by_role("button", name="画像").click()
        await page.locator(".profile-page").wait_for(timeout=20000)
        await page.get_by_role("button", name="新建画像").first.click()
        modal = page.locator(".modal").filter(has_text="配置账号画像")
        await modal.wait_for(timeout=10000)
        await modal.locator("input").first.fill("浏览器新画像")
        await modal.get_by_role("button", name="下一步").click()
        await modal.get_by_role("button", name="下一步").click()
        await modal.locator("input").nth(0).fill("做受控测试内容")
        await modal.get_by_role("button", name="下一步").click()
        await modal.get_by_role("button", name="生成画像").click()
        await page.get_by_text("画像已创建", exact=False).wait_for(
            timeout=30000)
        await page.get_by_role("button", name="先进去用").click()
        await page.get_by_role(
            "heading", name="浏览器新画像", exact=True).wait_for(
                timeout=20000)
        target = SERVER.workspace_root / "profiles" / "浏览器新画像"
        assert target.is_dir()
        assert len(list(target.glob("*.md"))) == 6
        await wait_for(
            lambda: (target / "memory.md").is_file()
            and "EXECUTOR-ENHANCED" in (target / "memory.md").read_text(
                encoding="utf-8"), 20000,
            "受控执行器没有按完整画像任务指令更新本人画像文件")
        assert any(task == "profile" and "做受控测试内容" in prompt
                   for task, prompt in SERVER.executions), SERVER.executions
        # 原 ProfilePage 必须读回执行器更新的文件，而不是只显示表单摘要。
        await page.reload(wait_until="domcontentloaded")
        await page.locator(".sidebar").wait_for(timeout=20000)
        await page.get_by_role("button", name="画像").click()
        card = page.locator(".profile-summary").filter(
            has_text="浏览器新画像")
        await card.get_by_role("button", name="查看 / 编辑").click()
        await page.get_by_text("EXECUTOR-ENHANCED", exact=False).wait_for(
            timeout=20000)
        await screenshot(page, "03-profile-created.png")
        results["profile_wizard"] = True
    except Exception as error:  # noqa: BLE001
        body = ""
        try:
            body = (await page.text_content("body") or "")[:1800]
        except Exception:
            pass
        failures.append(
            f"profile-wizard: {error!r} body={body!r} "
            f"responses={responses[-30:]!r} console={console[-10:]!r}")
    finally:
        await context.close()


async def scenario_skill_question(browser, results, failures):
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
    page = await context.new_page()
    try:
        await login(page)
        await page.get_by_role("button", name="技能库").click()
        await page.locator(".skill-card-rawname").filter(
            has_text="copywriting").first.click()
        await page.locator(".drawer").wait_for(timeout=20000)
        await page.locator(".drawer textarea").fill("ASK-SKILL 需要授权")
        await page.get_by_role("button", name="执行").click()
        card = page.locator(".question-card")
        await card.wait_for(timeout=30000)
        assert "允许执行这个技能" in (await card.text_content() or "")
        await screenshot(page, "07-skill-question.png")
        await card.locator(".question-card__option").filter(
            has_text="允许").click()
        await card.locator(".question-card__submit").click()
        await page.get_by_text("SKILL-ANSWERED", exact=False).wait_for(
            timeout=30000)
        await screenshot(page, "08-skill-question-answered.png")
        results["skill_question"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(f"skill-question: {error!r}")
    finally:
        await context.close()


async def scenario_profile_question_stop(browser, results, failures):
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
    page = await context.new_page()
    try:
        await login(page)
        await page.get_by_role("button", name="画像").click()
        await page.get_by_role("button", name="新建画像").first.click()
        modal = page.locator(".modal").filter(has_text="配置账号画像")
        await modal.wait_for(timeout=10000)
        await modal.locator("input").first.fill("问题增强画像")
        await modal.get_by_role("button", name="下一步").click()
        await modal.get_by_role("button", name="下一步").click()
        await modal.locator("input").nth(0).fill("ASK-PROFILE 方向")
        await modal.get_by_role("button", name="下一步").click()
        await modal.get_by_role("button", name="生成画像").click()
        card = page.locator(".question-card")
        await card.wait_for(timeout=30000)
        assert "允许分析这个画像" in (await card.text_content() or "")
        await screenshot(page, "09-profile-question.png")
        await card.locator(".question-card__option").filter(
            has_text="允许").click()
        await card.locator(".question-card__submit").click()
        await page.get_by_text("允许写入画像文件", exact=False).wait_for(
            timeout=30000)
        await screenshot(page, "10-profile-question-second.png")
        await page.get_by_role("button", name="停止", exact=True).click()
        await page.get_by_role(
            "heading", name="问题增强画像", exact=True).wait_for(
                timeout=30000)
        target = SERVER.workspace_root / "profiles" / "问题增强画像"
        assert target.is_dir()
        assert len(list(target.glob("*.md"))) == 6
        await screenshot(page, "11-profile-question-stopped.png")
        results["profile_question_stop"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(f"profile-question-stop: {error!r}")
    finally:
        await context.close()


async def scenario_skill_status_recovery(browser, results, failures):
    """一次技能状态 503 后继续轮询，恢复题卡并完成回答。"""
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
    page = await context.new_page()
    before = len(SERVER.executions)
    try:
        await login(page)
        await page.get_by_role("button", name="技能库").click()
        await page.locator(".skill-card-rawname").filter(
            has_text="copywriting").first.click()
        await page.locator(".drawer").wait_for(timeout=20000)
        await page.locator(".drawer textarea").fill("ASK-SKILL 状态恢复")
        await page.get_by_role("button", name="执行").click()
        card = page.locator(".question-card[data-pending=\"false\"]")
        await card.wait_for(timeout=30000)
        await card.locator(".question-card__option").filter(
            has_text="允许").click()
        assert not await page.get_by_role(
            "button", name="提交", exact=True).is_disabled()
        calls: list[str] = []
        injected = False

        async def once_503(route):
            nonlocal injected
            calls.append(route.request.url)
            if not injected:
                injected = True
                await route.fulfill(
                    status=503, content_type="application/json",
                    body='{"code":"temporary_status_unavailable"}')
            else:
                await route.continue_()

        await page.route("**/api/skill/status/copywriting", once_503)
        await page.get_by_text("工作区服务暂不可用（503）", exact=False).wait_for(
            timeout=12000)
        await page.locator(".question-card[data-pending=\"true\"]").wait_for(
            timeout=5000)
        await page.wait_for_timeout(4200)
        assert len(calls) >= 2, calls
        assert injected
        await page.locator(".question-card[data-pending=\"false\"]").wait_for(
            timeout=10000)
        assert not await page.get_by_role(
            "button", name="提交", exact=True).is_disabled()
        await page.locator(".question-card__option").filter(
            has_text="允许").click()
        await page.get_by_role("button", name="提交", exact=True).click()
        await page.get_by_text("SKILL-ANSWERED", exact=False).wait_for(
            timeout=30000)
        assert len(SERVER.executions) == before + 1, SERVER.executions
        await screenshot(page, "12-skill-status-recovered.png")
        results["skill_status_recovery"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(f"skill-status-recovery: {error!r}")
    finally:
        await context.close()


async def scenario_skill_stop(browser, results, failures):
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
    page = await context.new_page()
    SERVER.delay_skill = 3.0
    try:
        await login(page)
        await page.get_by_role("button", name="技能库").click()
        await page.locator(".skill-card-rawname").filter(
            has_text="copywriting").first.click()
        await page.locator(".drawer").wait_for(timeout=20000)
        await page.locator(".drawer textarea").fill("停止中的技能任务")
        await page.get_by_role("button", name="执行").click()
        await page.wait_for_timeout(500)
        await page.get_by_role("button", name="停止", exact=True).click()
        await page.get_by_text("cancelled", exact=False).wait_for(
            timeout=30000)
        assert any(prompt for task, prompt in SERVER.executions
                   if task == "skill" and "停止中的技能任务" in prompt)
        await screenshot(page, "06-skill-stop.png")
        results["skill_stop"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(f"skill-stop: {error!r}")
    finally:
        SERVER.delay_skill = 0.0
        await context.close()


async def scenario_dark_mobile(browser, results, failures):
    try:
        dark = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=True, color_scheme="dark")
        page = await dark.new_page()
        await login(page)
        await page.get_by_role("button", name="技能库").click()
        await page.locator(".skills-page").wait_for(timeout=20000)
        await screenshot(page, "04-skills-dark.png")
        await dark.close()

        mobile = await browser.new_context(
            viewport={"width": 390, "height": 844},
            ignore_https_errors=True, color_scheme="light")
        page = await mobile.new_page()
        await login(page)
        await page.get_by_role("button", name="技能库").click()
        await page.locator(".skills-page").wait_for(timeout=20000)
        overflow = await page.evaluate(
            "() => document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 1, overflow
        await screenshot(page, "05-skills-mobile.png")
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

    root = Path(tempfile.mkdtemp(prefix="easel-task5c1-browser-"))
    try:
        SERVER = Server(root).start()
        results: dict[str, bool] = {}
        failures: list[str] = []
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            print("BROWSER_VERSION", browser.version)
            await scenario_skill_run(browser, results, failures)
            await scenario_profile_wizard(browser, results, failures)
            await scenario_skill_question(browser, results, failures)
            await scenario_skill_status_recovery(browser, results, failures)
            await scenario_profile_question_stop(browser, results, failures)
            await scenario_skill_stop(browser, results, failures)
            await scenario_dark_mobile(browser, results, failures)
            await browser.close()
        print("TASK5C1_BROWSER_RESULTS", results)
        print("TASK5C1_EXECUTIONS", SERVER.executions)
        print("FAILURES", failures)
        print("SCREENSHOTS", [str(path.relative_to(ROOT)) for path in SCREENSHOTS])
        ok = not failures and len(results) >= 4
        print("TASK5C1_BROWSER_OK", ok)
        return 0 if ok else 1
    finally:
        try:
            await SERVER.stop()
        except Exception:
            pass
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
