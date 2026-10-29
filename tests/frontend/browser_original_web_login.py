"""Task 4 原主题登录、设置与品牌资源浏览器验收。

真实 ``npm run build`` 产物 + 隔离假 control API + 独立 Chromium context。
覆盖浅/深色、桌面/窄屏、回车与焦点、忙碌/重复提交、一次性邀请、
401/403/429/503 语义、身份/模式错误主题、成员/管理员设置及真实品牌图。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import re
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "web" / "frontend" / "dist"
EVIDENCE = ROOT / "docs" / "evidence" / "original-web-task-04" / "browser"
BRAND_SOURCE = ROOT / "web" / "static" / "easel-icon-transparent.png"
BRAND_DIST = DIST / "static" / "easel-icon-transparent.png"
BRAND_SHA256 = (
    "71c260f5a28e82aced0a513186ebdcf43782b068633e3a7ca5f159d3e3606132"
)
BRAND_SIZE = (800, 800)

USERS = {
    "admin": {
        "userId": "user-admin",
        "workspaceId": "workspace-admin",
        "role": "admin",
        "csrfToken": "csrf-admin",
        "capabilities": ["workspace", "admin"],
    },
    "member": {
        "userId": "user-member",
        "workspaceId": "workspace-member",
        "role": "member",
        "csrfToken": "csrf-member",
        "capabilities": ["workspace"],
    },
}

SESSIONS = {
    "admin": {
        "id": "s-admin",
        "title": "管理员会话",
        "messages": [
            {"role": "user", "content": "管理员私有问题"},
            {"role": "assistant", "content": "管理员私有回答"},
        ],
    },
    "member": {
        "id": "s-member",
        "title": "成员会话",
        "messages": [
            {"role": "user", "content": "成员私有问题"},
            {"role": "assistant", "content": "成员私有回答"},
        ],
    },
}


def identity(user: str) -> dict:
    return {**USERS[user]}


def channel(**overrides) -> dict:
    base = {
        "configured": False,
        "source": "none",
        "version": 1,
        "mode": "none",
        "provider": None,
        "model": None,
        "base_url": None,
        "protocol": None,
        "options": {},
        "broker_adapter": None,
        "broker_supported": False,
        "override_allowed": True,
    }
    base.update(overrides)
    return base


class FakeState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.requests: list[tuple[str, str]] = []
        self.mode_status = 200
        self.identity_status = 200
        self.hold_identity = False
        self.identity_waiting = threading.Event()
        self.identity_release = threading.Event()
        self.hold_exchange = False
        self.exchange_waiting = threading.Event()
        self.exchange_release = threading.Event()
        self.chat_configured = False
        self.consumed: set[str] = set()
        self.exchanges = 0

    def record(self, method: str, path: str) -> None:
        with self.lock:
            self.requests.append((method, path))

    def reset(self) -> None:
        with self.lock:
            self.mode_status = 200
            self.identity_status = 200
            self.hold_identity = False
            self.hold_exchange = False
            self.chat_configured = False
            self.consumed.clear()
        self.identity_release.set()
        self.exchange_release.set()


class QuietHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def log_message(self, *args) -> None:  # noqa: D401 - quiet fixture
        return

    def handle_error(self, request, client_address) -> None:
        return


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> FakeState:
        return self.server.state

    def log_message(self, *args) -> None:
        return

    # ---- helpers ----
    def _json(self, status: int, payload: object,
              headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, OSError):
            return

    def _empty(self, status: int = 204,
               headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _query(self) -> dict[str, list[str]]:
        return parse_qs(urlparse(self.path).query)

    def _cookie_user(self) -> str | None:
        cookie = self.headers.get("Cookie") or ""
        match = re.search(r"(?:^|;\s*)easel_login_session=([^;]+)", cookie)
        if not match:
            return None
        candidate = match.group(1)
        return candidate if candidate in USERS else None

    def _require_user(self) -> str | None:
        user = self._cookie_user()
        if user is None:
            self._json(401, {"code": "unauthorized"})
            return None
        return user

    def _require_admin(self) -> str | None:
        user = self._require_user()
        if user is None:
            return None
        if USERS[user]["role"] != "admin":
            self._json(403, {"code": "forbidden"})
            return None
        return user

    def _require_csrf(self, user: str) -> bool:
        supplied = self.headers.get("X-CSRF-Token") or ""
        if supplied != USERS[user]["csrfToken"]:
            self._json(403, {"code": "csrf_invalid"})
            return False
        return True

    def _static(self) -> bool:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            file_path = DIST / "index.html"
        else:
            relative = path.lstrip("/")
            file_path = (DIST / relative).resolve()
            if DIST.resolve() not in file_path.parents:
                return False
        if not file_path.is_file():
            return False
        body = file_path.read_bytes()
        content_type = mimetypes.guess_type(str(file_path))[0] \
            or "application/octet-stream"
        if file_path.suffix == ".js":
            content_type = "text/javascript; charset=utf-8"
        elif file_path.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, OSError):
            return True
        return True

    def _settings_models(self) -> dict:
        speech = channel(
            configured=True, source="admin", version=1, mode="inherited",
            provider="managed-provider", model="tts-default",
            base_url="https://provider.example/v1", protocol="openai",
            broker_adapter="openai", broker_supported=True,
            allowed_models=["tts-default"],
        )
        return {
            "version": 1,
            "channels": {
                "speech": speech,
                "image": channel(),
                "video": channel(),
                "music": channel(),
                "chat": channel(),
            },
            "providers": {},
        }

    def _managed(self) -> dict:
        defaults = {
            "speech": {
                "provider": "managed-provider",
                "model": "tts-default",
                "base_url": "https://provider.example/v1",
                "protocol": "openai",
                "options": {},
                "allowed_models": ["tts-default"],
                "credential_configured": True,
            },
        }
        if self.state.chat_configured:
            defaults["chat"] = {
                "provider": "managed-provider",
                "model": "managed-chat",
                "base_url": "https://provider.example/v1",
                "protocol": "openai",
                "options": {},
                "allowed_models": ["managed-chat"],
                "credential_configured": True,
            }
        return {"version": 3, "defaults": defaults}

    # ---- HTTP ----
    def do_GET(self) -> None:  # noqa: N802
        self.state.record("GET", self.path)
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            if not self._static():
                self._json(404, {"code": "not_found"})
            return
        if path == "/api/runtime":
            if self.state.mode_status != 200:
                self._json(self.state.mode_status,
                           {"code": "service_unavailable"})
                return
            self._json(200, {"mode": "control"})
            return
        if path == "/api/auth/me":
            if self.state.identity_status != 200:
                self._json(self.state.identity_status,
                           {"code": "service_unavailable"})
                return
            if self.state.hold_identity:
                with self.state.lock:
                    self.state.hold_identity = False
                    self.state.identity_waiting.set()
                self.state.identity_release.wait(30)
            user = self._cookie_user()
            if user is None:
                self._json(401, {"code": "unauthorized"})
                return
            self._json(200, identity(user))
            return
        if path == "/api/sessions":
            user = self._require_user()
            if user is None:
                return
            row = SESSIONS[user]
            self._json(200, {
                "sessions": [{
                    "id": row["id"],
                    "workspaceId": USERS[user]["workspaceId"],
                    "title": row["title"],
                    "archived": False,
                }],
                "nextOffset": None,
            })
            return
        match = re.fullmatch(r"/api/sessions/([^/]+)/messages", path)
        if match:
            user = self._require_user()
            if user is None:
                return
            row = SESSIONS[user]
            if match.group(1) != row["id"]:
                self._json(404, {"code": "not_found"})
                return
            self._json(200, {
                "messages": row["messages"],
                "total": len(row["messages"]),
            })
            return
        match = re.fullmatch(r"/api/sessions/([^/]+)/runs", path)
        if match:
            user = self._require_user()
            if user is None:
                return
            if match.group(1) != SESSIONS[user]["id"]:
                self._json(404, {"code": "not_found"})
                return
            self._json(200, {"runs": [], "nextOffset": None})
            return
        if path == "/api/settings/models":
            if self._require_user() is None:
                return
            self._json(200, self._settings_models())
            return
        if path == "/api/settings/harness":
            if self._require_user() is None:
                return
            self._json(200, {
                "locked": True, "configured": True, "runtime": "acp",
                "revision": 7, "model": "managed-chat",
                "sandbox": "workspace-write", "timeout": 60,
                "codexAutoApprove": False, "unavailableReason": None,
            })
            return
        if path == "/api/admin/managed-config":
            if self._require_admin() is None:
                return
            self._json(200, self._managed())
            return
        if path.startswith("/api/admin/users"):
            if self._require_admin() is None:
                return
            self._json(200, {
                "users": [
                    {"userId": "user-admin",
                     "workspaceId": "workspace-admin",
                     "displayName": "管理员", "role": "admin",
                     "disabled": False, "createdAt": 1},
                    {"userId": "user-member",
                     "workspaceId": "workspace-member",
                     "displayName": "成员一", "role": "member",
                     "disabled": False, "createdAt": 2},
                ],
                "nextOffset": None,
            })
            return
        if path.startswith("/api/admin/invites"):
            if self._require_admin() is None:
                return
            self._json(200, {"invites": [], "nextOffset": None})
            return
        self._json(404, {"code": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        self.state.record("POST", self.path)
        path = urlparse(self.path).path
        if path == "/api/auth/exchange":
            with self.state.lock:
                self.state.exchanges += 1
            if self.state.hold_exchange:
                with self.state.lock:
                    self.state.hold_exchange = False
                    self.state.exchange_waiting.set()
                self.state.exchange_release.wait(30)
            body = self._body()
            token = body.get("token")
            status = {
                "invite-invalid": 401,
                "invite-forbidden": 403,
                "invite-rate": 429,
                "invite-down": 503,
            }.get(token)
            if status is not None:
                self._json(status, {"code": f"exchange_{status}"})
                return
            user = {
                "invite-admin": "admin",
                "invite-member": "member",
                "invite-once": "member",
            }.get(token)
            with self.state.lock:
                if user is None or token in self.state.consumed:
                    user = None
                else:
                    self.state.consumed.add(token)
            if user is None:
                self._json(401, {"code": "invalid_invite"})
                return
            cookie = (
                f"easel_login_session={user}; Path=/; HttpOnly; SameSite=Lax"
            )
            self._json(200, identity(user), {"Set-Cookie": cookie})
            return
        if path == "/api/auth/logout":
            user = self._require_user()
            if user is None:
                return
            if not self._require_csrf(user):
                return
            self._empty(204, {
                "Set-Cookie": "easel_login_session=; Path=/; Max-Age=0",
            })
            return
        self._json(404, {"code": "not_found"})


class Server:
    def __init__(self) -> None:
        self.state = FakeState()
        self.httpd = QuietHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.state = self.state
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        host, port = self.httpd.server_address
        return f"http://127.0.0.1:{port}"

    def start(self) -> "Server":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.state.reset()
        self.httpd.shutdown()
        self.httpd.server_close()

    def hold_identity_once(self) -> None:
        with self.state.lock:
            self.state.hold_identity = True
            self.state.identity_waiting.clear()
            self.state.identity_release.clear()

    def release_identity(self) -> None:
        self.state.identity_release.set()

    def hold_exchange_once(self) -> None:
        with self.state.lock:
            self.state.hold_exchange = True
            self.state.exchange_waiting.clear()
            self.state.exchange_release.clear()

    def release_exchange(self) -> None:
        self.state.exchange_release.set()

    def set_mode_status(self, status: int) -> None:
        with self.state.lock:
            self.state.mode_status = status

    def set_identity_status(self, status: int) -> None:
        with self.state.lock:
            self.state.identity_status = status

    def set_chat_configured(self, configured: bool) -> None:
        with self.state.lock:
            self.state.chat_configured = configured

    def requests(self) -> list[tuple[str, str]]:
        with self.state.lock:
            return list(self.state.requests)


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


async def login(page, origin: str, invite: str) -> None:
    await page.goto(origin, wait_until="domcontentloaded")
    await page.fill("#invite", invite)
    await page.get_by_role("button", name="进入工作区").click()
    await page.locator(".sidebar").wait_for(timeout=15000)
    await page.locator(".chat-page").wait_for(timeout=15000)


async def logout(page) -> None:
    await page.get_by_role("button", name="退出").click()
    await page.locator("#invite").wait_for(timeout=15000)


async def open_settings(page) -> None:
    await page.get_by_role("button", name="设置").click()
    await page.locator(".settings-panel").wait_for(timeout=15000)


async def close_settings(page) -> None:
    await page.locator(".settings-close").click()
    await page.locator(".settings-overlay").wait_for(state="detached")


def check_brand_files() -> None:
    assert BRAND_SOURCE.is_file(), "缺少原品牌图 web/static/easel-icon-transparent.png"
    assert BRAND_DIST.is_file(), (
        "dist/static 未包含品牌图；请先 npm --prefix web/frontend run build")
    source = BRAND_SOURCE.read_bytes()
    published = BRAND_DIST.read_bytes()
    assert hashlib.sha256(source).hexdigest() == BRAND_SHA256
    assert hashlib.sha256(published).hexdigest() == BRAND_SHA256
    width, height = struct.unpack(">II", published[16:24])
    assert (width, height) == BRAND_SIZE


async def assert_brand_loaded(page, origin: str) -> None:
    image = page.locator(".auth-brand-icon")
    await image.wait_for(timeout=5000)
    data = await image.evaluate("""(img) => ({
        complete: img.complete,
        width: img.naturalWidth,
        height: img.naturalHeight,
        src: img.getAttribute('src') || '',
    })""")
    assert data["complete"] is True, data
    assert (data["width"], data["height"]) == BRAND_SIZE, data
    assert "static/easel-icon-transparent.png" in data["src"], data
    response = await page.request.get(
        f"{origin}/static/easel-icon-transparent.png")
    assert response.status == 200
    assert response.headers["content-type"].startswith("image/png")
    body = await response.body()
    assert hashlib.sha256(body).hexdigest() == BRAND_SHA256
    assert struct.unpack(">II", body[16:24]) == BRAND_SIZE


async def theme_snapshot(page) -> dict:
    return await page.evaluate("""() => {
        const shell = document.querySelector('.auth-shell');
        const card = document.querySelector('.auth-card');
        const root = getComputedStyle(document.documentElement);
        return {
            theme: document.documentElement.dataset.theme,
            stored: localStorage.getItem('easel_theme'),
            bg: root.getPropertyValue('--bg').trim(),
            surface: root.getPropertyValue('--surface').trim(),
            cardBg: card ? getComputedStyle(card).backgroundColor : null,
            shellBg: shell ? getComputedStyle(shell).backgroundColor : null,
            docWidth: document.documentElement.scrollWidth,
            clientWidth: document.documentElement.clientWidth,
        };
    }""")


def assert_no_technical_text(body: str) -> None:
    for forbidden in ("EASEL CONTROL", "Cookie", "Docker", "broker",
                      "/opt/easel", ".env", "HttpOnly"):
        assert forbidden not in body, (forbidden, body[:400])


async def scenario_login_desktop_theme(
    browser, server: Server, results: dict, failures: list,
    screenshots: list,
) -> None:
    context = await browser.new_context(viewport={
        "width": 1280, "height": 800}, color_scheme="light")
    await context.add_init_script(
        "try{if(!localStorage.getItem('easel_theme'))"
        "localStorage.setItem('easel_theme','light')}catch(e){}")
    page = await context.new_page()
    console: list[str] = []
    page.on("console", lambda message: console.append(message.text))
    try:
        check_brand_files()
        await page.goto(server.origin, wait_until="domcontentloaded")
        await page.locator("#invite").wait_for(timeout=15000)
        await assert_brand_loaded(page, server.origin)
        light = await theme_snapshot(page)
        assert light["theme"] == "light", light
        assert light["bg"] in ("#fff", "#ffffff"), light
        assert light["surface"] in ("#fff", "#ffffff"), light
        assert light["cardBg"] == "rgb(255, 255, 255)", light
        assert light["shellBg"] == "rgb(255, 255, 255)", light
        assert light["docWidth"] <= light["clientWidth"] + 1, light
        body = await page.text_content("body") or ""
        assert "Easel" in body
        assert "邀请仅能使用一次" in body or "一次性邀请" in body
        assert "使用后失效" in body
        assert "退出后需重新获取" in body
        assert "如何重新获取邀请" in body
        assert_no_technical_text(body)
        assert await page.evaluate(
            "document.activeElement && document.activeElement.id") == "invite"
        assert await page.get_by_role(
            "button", name="进入工作区").is_disabled()
        await page.screenshot(path=str(
            EVIDENCE / "01-login-desktop-light.png"))

        await page.get_by_role(
            "button", name="切换到夜间模式").click()
        await page.wait_for_function(
            "document.documentElement.dataset.theme === 'dark'")
        dark = await theme_snapshot(page)
        assert dark["stored"] == "dark", dark
        assert dark["bg"] == "#14171e", dark
        assert dark["surface"] == "#1e242e", dark
        assert dark["cardBg"] == "rgb(27, 32, 41)", dark
        assert dark["shellBg"] == "rgb(20, 23, 30)", dark
        await page.screenshot(path=str(
            EVIDENCE / "02-login-desktop-dark.png"))

        await login(page, server.origin, "invite-admin")
        workspace = await page.evaluate("""() => ({
            theme: document.documentElement.dataset.theme,
            stored: localStorage.getItem('easel_theme'),
            mainBg: getComputedStyle(
                document.querySelector('.main-content')).backgroundColor,
        })""")
        assert workspace["theme"] == "dark", workspace
        assert workspace["stored"] == "dark", workspace
        assert workspace["mainBg"] == "rgb(20, 23, 30)", workspace
        await page.screenshot(path=str(
            EVIDENCE / "03-workspace-desktop-dark.png"))
        await logout(page)
        assert await page.evaluate(
            "document.documentElement.dataset.theme") == "dark"
        results["login_desktop_theme_brand"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(f"login-desktop-theme: {error!r}")
    finally:
        await context.close()


async def scenario_login_mobile_theme(
    browser, server: Server, results: dict, failures: list,
    screenshots: list,
) -> None:
    for name, theme in (("light", "light"), ("dark", "dark")):
        context = await browser.new_context(
            viewport={"width": 390, "height": 844},
            color_scheme=theme)
        await context.add_init_script(
            "try{if(!localStorage.getItem('easel_theme'))"
            "localStorage.setItem('easel_theme','"
            + theme + "')}catch(e){}")
        page = await context.new_page()
        try:
            await page.goto(server.origin, wait_until="domcontentloaded")
            await page.locator("#invite").wait_for(timeout=15000)
            await assert_brand_loaded(page, server.origin)
            metrics = await theme_snapshot(page)
            assert metrics["docWidth"] <= metrics["clientWidth"] + 1, metrics
            box = await page.locator(".auth-card").bounding_box()
            assert box is not None
            assert box["x"] >= 0, box
            assert box["x"] + box["width"] <= 390.5, box
            await page.locator(".auth-help-details summary").scroll_into_view_if_needed()
            assert await page.get_by_text("如何重新获取邀请").is_visible()
            assert await page.get_by_role(
                "button", name="进入工作区").is_visible()
            await page.screenshot(path=str(
                EVIDENCE / f"04-login-mobile-{name}.png"))
        except Exception as error:  # noqa: BLE001
            failures.append(f"login-mobile-{name}: {error!r}")
        finally:
            await context.close()
    if not any(item.startswith("login-mobile-") for item in failures):
        results["login_mobile_theme_brand"] = True


async def scenario_login_system_and_cross_tab(
    browser, server: Server, results: dict, failures: list,
    screenshots: list,
) -> None:
    context = await browser.new_context(
        viewport={"width": 1280, "height": 800}, color_scheme="dark")
    page = await context.new_page()
    second = await context.new_page()
    try:
        await page.goto(server.origin, wait_until="domcontentloaded")
        await page.locator("#invite").wait_for(timeout=15000)
        first = await theme_snapshot(page)
        assert first["theme"] == "dark", first
        assert first["stored"] is None, first
        await page.get_by_role("button", name="切换到日间模式").click()
        await page.wait_for_function(
            "document.documentElement.dataset.theme === 'light'")
        light = await theme_snapshot(page)
        assert light["stored"] == "light", light
        await page.reload(wait_until="domcontentloaded")
        await page.locator("#invite").wait_for(timeout=15000)
        assert await page.evaluate(
            "document.documentElement.dataset.theme") == "light"

        await second.goto(server.origin, wait_until="domcontentloaded")
        await second.locator("#invite").wait_for(timeout=15000)
        assert await second.evaluate(
            "document.documentElement.dataset.theme") == "light"
        await second.get_by_role("button", name="切换到夜间模式").click()
        await second.wait_for_function(
            "document.documentElement.dataset.theme === 'dark'")
        await page.wait_for_function(
            "document.documentElement.dataset.theme === 'dark'")
        assert await page.evaluate(
            "localStorage.getItem('easel_theme')") == "dark"
        await page.screenshot(path=str(
            EVIDENCE / "05-login-cross-tab-dark.png"))
        results["login_system_and_cross_tab_theme"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(f"login-system-cross-tab: {error!r}")
    finally:
        await context.close()


async def scenario_login_keyboard_busy_once(
    browser, server: Server, results: dict, failures: list,
    screenshots: list,
) -> None:
    context = await browser.new_context(viewport={
        "width": 1280, "height": 800}, color_scheme="light")
    await context.add_init_script(
        "try{if(!localStorage.getItem('easel_theme'))"
        "localStorage.setItem('easel_theme','light')}catch(e){}")
    page = await context.new_page()
    console: list[str] = []
    page.on("console", lambda message: console.append(message.text))
    try:
        await page.goto(server.origin, wait_until="domcontentloaded")
        await page.locator("#invite").wait_for(timeout=15000)
        await page.fill("#invite", "invite-once")
        await page.keyboard.press("Tab")
        await page.wait_for_timeout(80)
        active = await page.evaluate(
            "document.activeElement && document.activeElement.textContent")
        assert "进入工作区" in (active or ""), active
        outline = await page.evaluate(
            "getComputedStyle(document.activeElement).outlineStyle")
        assert outline != "none", outline

        with server.state.lock:
            exchanges_before = server.state.exchanges
        server.hold_exchange_once()
        await page.keyboard.press("Enter")
        await wait_for(
            lambda: server.state.exchange_waiting.is_set(), 10,
            "登录兑换请求未挂起")
        button = page.locator(".auth-submit")
        assert await button.is_disabled()
        assert "正在验证" in (await button.text_content() or "")
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.4)
        with server.state.lock:
            assert server.state.exchanges == exchanges_before + 1, (
                server.state.exchanges, exchanges_before)
        server.release_exchange()
        await page.locator(".sidebar").wait_for(timeout=15000)
        stored = await page.evaluate("""() => JSON.stringify({
            local: Object.fromEntries(Object.entries(localStorage)),
            session: Object.fromEntries(Object.entries(sessionStorage)),
        })""")
        assert "invite-once" not in stored, stored
        assert not any("invite-once" in item for item in console), console
        await page.screenshot(path=str(
            EVIDENCE / "06-workspace-desktop-light.png"))
        await logout(page)
        await page.fill("#invite", "invite-once")
        await page.get_by_role("button", name="进入工作区").click()
        alert = page.locator('[role="alert"]')
        await alert.wait_for(timeout=10000)
        text = await alert.text_content() or ""
        assert "已使用" in text or "已过期" in text, text
        results["login_keyboard_busy_once"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(f"login-keyboard-busy: {error!r}")
    finally:
        server.release_exchange()
        await context.close()


async def scenario_login_error_semantics(
    browser, server: Server, results: dict, failures: list,
    screenshots: list,
) -> None:
    context = await browser.new_context(viewport={
        "width": 1280, "height": 800}, color_scheme="light")
    page = await context.new_page()
    cases = [
        ("invite-invalid", ("邀请无效", "已使用", "已过期"), ()),
        ("invite-forbidden", ("来源", "权限"), ("邀请无效", "已使用", "已过期")),
        ("invite-rate", ("过多",), ("邀请无效", "已过期")),
        ("invite-down", ("服务暂不可用",), ("邀请无效", "已过期")),
    ]
    try:
        await page.goto(server.origin, wait_until="domcontentloaded")
        await page.locator("#invite").wait_for(timeout=15000)
        for token, required, forbidden in cases:
            await page.fill("#invite", token)
            await page.get_by_role("button", name="进入工作区").click()
            alert = page.locator('[role="alert"]')
            await alert.wait_for(timeout=10000)
            text = await alert.text_content() or ""
            assert any(item in text for item in required), (token, text)
            for item in forbidden:
                assert item not in text, (token, item, text)
            assert await page.locator("#invite").input_value() == ""
            stored = await page.evaluate(
                "JSON.stringify(Object.entries(localStorage))")
            assert token not in stored
        await page.screenshot(path=str(
            EVIDENCE / "07-login-503-error.png"))
        results["login_error_semantics"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(f"login-error-semantics: {error!r}")
    finally:
        await context.close()


async def scenario_identity_and_mode_status_theme(
    browser, server: Server, results: dict, failures: list,
    screenshots: list,
) -> None:
    context = await browser.new_context(viewport={
        "width": 1280, "height": 800}, color_scheme="light")
    await context.add_init_script(
        "try{if(!localStorage.getItem('easel_theme'))"
        "localStorage.setItem('easel_theme','light')}catch(e){}")
    page = await context.new_page()
    try:
        server.hold_identity_once()
        await page.goto(server.origin, wait_until="domcontentloaded")
        await page.get_by_text("正在确认登录状态…").wait_for(timeout=15000)
        await assert_brand_loaded(page, server.origin)
        assert await page.locator(".theme-toggle").count() == 1
        light = await theme_snapshot(page)
        assert light["theme"] == "light", light
        await page.get_by_role("button", name="切换到夜间模式").click()
        await page.wait_for_function(
            "document.documentElement.dataset.theme === 'dark'")
        await page.screenshot(path=str(
            EVIDENCE / "08-identity-checking-dark.png"))
        server.release_identity()
        await page.locator("#invite").wait_for(timeout=15000)
        assert await page.evaluate(
            "document.documentElement.dataset.theme") == "dark"

        server.set_identity_status(503)
        await page.reload(wait_until="domcontentloaded")
        await page.get_by_text("服务暂不可用", exact=False).wait_for(
            timeout=15000)
        assert await page.get_by_role("button", name="重试").is_visible()
        await page.screenshot(path=str(
            EVIDENCE / "09-identity-error-dark.png"))
        server.set_identity_status(200)
        await page.get_by_role("button", name="重试").click()
        await page.locator("#invite").wait_for(timeout=15000)

        server.set_mode_status(503)
        await page.reload(wait_until="domcontentloaded")
        await page.get_by_text("无法确认服务模式", exact=False).wait_for(
            timeout=15000)
        mode = await theme_snapshot(page)
        assert mode["theme"] == "dark", mode
        await page.screenshot(path=str(
            EVIDENCE / "10-mode-error-dark.png"))
        server.set_mode_status(200)
        await page.get_by_role("button", name="重试").click()
        await page.locator("#invite").wait_for(timeout=15000)
        results["identity_and_mode_status_theme"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(f"identity-mode-theme: {error!r}")
    finally:
        server.release_identity()
        await context.close()


async def scenario_member_settings_theme(
    browser, server: Server, results: dict, failures: list,
    screenshots: list,
) -> None:
    context = await browser.new_context(viewport={
        "width": 1280, "height": 800}, color_scheme="light")
    await context.add_init_script(
        "try{if(!localStorage.getItem('easel_theme'))"
        "localStorage.setItem('easel_theme','light')}catch(e){}")
    page = await context.new_page()
    try:
        await login(page, server.origin, "invite-member")
        await open_settings(page)
        assert await page.locator(".settings-overlay").is_visible()
        await page.get_by_role("heading", name="个人设置").wait_for(
            timeout=15000)
        body = await page.text_content("body") or ""
        assert "全体默认" not in body
        assert "成员与邀请" not in body
        assert "保存全体默认" not in body
        assert "创建成员" not in body
        light = await page.evaluate("""() => ({
            theme: document.documentElement.dataset.theme,
            panel: getComputedStyle(
                document.querySelector('.settings-panel')).backgroundColor,
            section: getComputedStyle(
                document.querySelector('.settings-section')).backgroundColor,
            docWidth: document.documentElement.scrollWidth,
            clientWidth: document.documentElement.clientWidth,
        })""")
        assert light["theme"] == "light", light
        assert light["panel"] == "rgb(255, 255, 255)", light
        assert light["section"] == "rgb(255, 255, 255)", light
        await page.screenshot(path=str(
            EVIDENCE / "11-member-settings-light.png"))
        await close_settings(page)

        await page.evaluate("localStorage.setItem('easel_theme','dark')")
        await page.reload(wait_until="domcontentloaded")
        await page.locator(".sidebar").wait_for(timeout=15000)
        await open_settings(page)
        dark = await page.evaluate("""() => ({
            theme: document.documentElement.dataset.theme,
            panel: getComputedStyle(
                document.querySelector('.settings-panel')).backgroundColor,
            section: getComputedStyle(
                document.querySelector('.settings-section')).backgroundColor,
        })""")
        assert dark["theme"] == "dark", dark
        assert dark["panel"] == "rgb(20, 23, 30)", dark
        assert dark["section"] == "rgb(30, 36, 46)", dark
        await page.screenshot(path=str(
            EVIDENCE / "12-member-settings-dark.png"))

        await page.set_viewport_size({"width": 390, "height": 844})
        await page.wait_for_timeout(150)
        mobile = await page.evaluate("""() => {
            const panel = document.querySelector('.settings-panel');
            return {
                docWidth: document.documentElement.scrollWidth,
                clientWidth: document.documentElement.clientWidth,
                panelWidth: panel.getBoundingClientRect().width,
                panelLeft: panel.getBoundingClientRect().left,
            };
        }""")
        assert mobile["docWidth"] <= mobile["clientWidth"] + 1, mobile
        assert mobile["panelWidth"] <= 390.5, mobile
        assert mobile["panelLeft"] >= 0, mobile
        await page.screenshot(path=str(
            EVIDENCE / "13-member-settings-mobile.png"))

        paths = [path for _method, path in server.requests()]
        assert not any(path.startswith("/api/admin") for path in paths), paths
        results["member_settings_theme"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(f"member-settings: {error!r}")
    finally:
        await context.close()


async def scenario_admin_settings_theme(
    browser, server: Server, results: dict, failures: list,
    screenshots: list,
) -> None:
    context = await browser.new_context(viewport={
        "width": 1280, "height": 800}, color_scheme="light")
    await context.add_init_script(
        "try{if(!localStorage.getItem('easel_theme'))"
        "localStorage.setItem('easel_theme','light')}catch(e){}")
    page = await context.new_page()
    try:
        await login(page, server.origin, "invite-admin")
        await open_settings(page)
        for label in ("个人设置", "全体默认", "成员与邀请"):
            assert await page.get_by_role("tab", name=label).count() == 1
        await page.get_by_role("tab", name="全体默认").click()
        await page.get_by_text("主聊天尚未配置").wait_for(timeout=15000)
        assert await page.get_by_text("主聊天已配置").count() == 0
        guide = page.locator(".settings-guide")
        assert await guide.count() == 1
        assert "settings-guide--ok" not in (
            await guide.get_attribute("class") or "")
        await page.get_by_role("button", name="配置主聊天").click()
        chat = page.get_by_role("button", name="主聊天", exact=True)
        assert await chat.get_attribute("aria-pressed") == "true"
        await page.screenshot(path=str(
            EVIDENCE / "14-admin-settings-warning-light.png"))

        await page.get_by_role("tab", name="成员与邀请").click()
        await page.get_by_role(
            "heading", name="成员与邀请").first.wait_for(timeout=15000)
        assert await page.get_by_text("成员一").count() >= 1
        assert await page.get_by_role("button", name="创建邀请").count() == 1
        await page.screenshot(path=str(
            EVIDENCE / "15-admin-members-light.png"))
        await close_settings(page)

        server.set_chat_configured(True)
        await page.evaluate("localStorage.setItem('easel_theme','dark')")
        await page.reload(wait_until="domcontentloaded")
        await page.locator(".sidebar").wait_for(timeout=15000)
        await open_settings(page)
        await page.get_by_role("tab", name="全体默认").click()
        await page.get_by_text("主聊天已配置").wait_for(timeout=15000)
        assert await page.get_by_text("主聊天尚未配置").count() == 0
        dark = await page.evaluate("""() => ({
            theme: document.documentElement.dataset.theme,
            guide: document.querySelector('.settings-guide').className,
        })""")
        assert dark["theme"] == "dark", dark
        assert "settings-guide--ok" in dark["guide"], dark
        await page.screenshot(path=str(
            EVIDENCE / "16-admin-settings-ready-dark.png"))
        results["admin_settings_theme_guidance"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(f"admin-settings: {error!r}")
    finally:
        await context.close()


async def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    if not (DIST / "index.html").is_file():
        print("缺少构建产物，请先 npm --prefix web/frontend run build")
        return 2
    from playwright.async_api import async_playwright

    server = Server().start()
    results: dict[str, bool] = {}
    failures: list[str] = []
    screenshots: list[str] = []
    browser = None
    version = ""
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            version = browser.version
            scenarios = [
                scenario_login_desktop_theme,
                scenario_login_mobile_theme,
                scenario_login_system_and_cross_tab,
                scenario_login_keyboard_busy_once,
                scenario_login_error_semantics,
                scenario_identity_and_mode_status_theme,
                scenario_member_settings_theme,
                scenario_admin_settings_theme,
            ]
            for scenario in scenarios:
                await scenario(
                    browser, server, results, failures, screenshots)
                server.state.reset()
            await browser.close()
            browser = None
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass
        server.stop()

    shot_files = sorted(
        str(path.relative_to(ROOT)) for path in EVIDENCE.iterdir()
        if path.suffix == ".png")
    print("BROWSER_VERSION", version)
    print("TASK4_BROWSER_RESULTS",
          json.dumps(results, ensure_ascii=False, sort_keys=True))
    print("FAILURES", json.dumps(failures, ensure_ascii=False))
    print("SCREENSHOTS", json.dumps(shot_files, ensure_ascii=False))
    expected = {
        "login_desktop_theme_brand",
        "login_mobile_theme_brand",
        "login_system_and_cross_tab_theme",
        "login_keyboard_busy_once",
        "login_error_semantics",
        "identity_and_mode_status_theme",
        "member_settings_theme",
        "admin_settings_theme_guidance",
    }
    missing = expected - set(results)
    if missing:
        failures.append(f"missing scenarios: {sorted(missing)}")
    ok = len(failures) == 0 and set(results) == expected
    print("TASK4_BROWSER_OK", ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
