"""真实部署双用户测试的显式开关。

常规 pytest 只跳过本目录中的 ``multiuser_integration`` 测试；显式传入
``--multiuser-live`` 后，缺少控制面 URL 或一次性邀请等环境会直接失败，
不会 skip、xfail 或以本机假 Harness 冒充正式部署。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import httpx
import pytest

SESSION_COOKIE = "__Host-easel_session"
BASE_ENV = (
    "EASEL_TEST_CONTROL_URL",
    "EASEL_TEST_INVITE_A",
    "EASEL_TEST_INVITE_B",
)
RESTORE_ENV = (
    "EASEL_TEST_BACKUP_SOURCE",
    "EASEL_TEST_RESTORE_DEST",
)


def pytest_addoption(parser):
    parser.addoption(
        "--multiuser-live",
        action="store_true",
        default=False,
        help="运行需要真实控制面、邀请、Docker 与备份的发布门槛测试",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "multiuser_integration: 需要 --multiuser-live 与真实部署环境",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--multiuser-live"):
        return
    skip = pytest.mark.skip(
        reason="需要显式 --multiuser-live 与真实部署环境")
    for item in items:
        if "multiuser_integration" in item.keywords:
            item.add_marker(skip)


def _require_live(config, names: tuple[str, ...]) -> dict[str, str]:
    if not config.getoption("--multiuser-live"):
        pytest.skip("需要显式 --multiuser-live")
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        pytest.fail(
            "缺少 --multiuser-live 所需环境变量：" + ", ".join(missing),
            pytrace=False)
    return {name: os.environ[name] for name in names}


@dataclass(frozen=True)
class LiveEnv:
    control_url: str
    origin: str
    invite_a: str
    invite_b: str


@dataclass
class LiveUser:
    name: str
    client: httpx.Client
    cookie: str
    csrf: str
    user_id: str
    workspace_id: str
    origin: str

    def read_headers(self) -> dict[str, str]:
        return {"Cookie": self.cookie, "Origin": self.origin}

    def write_headers(self) -> dict[str, str]:
        return {**self.read_headers(), "X-CSRF-Token": self.csrf}

    @property
    def cookie_value(self) -> str:
        return self.cookie.split("=", 1)[1]


@pytest.fixture(scope="session")
def live_env(request) -> LiveEnv:
    values = _require_live(request.config, BASE_ENV)
    control_url = values["EASEL_TEST_CONTROL_URL"].rstrip("/")
    return LiveEnv(
        control_url=control_url,
        origin=control_url,
        invite_a=values["EASEL_TEST_INVITE_A"],
        invite_b=values["EASEL_TEST_INVITE_B"],
    )


def _login(env: LiveEnv, name: str, invite: str) -> LiveUser:
    client = httpx.Client(
        base_url=env.control_url, trust_env=False,
        timeout=httpx.Timeout(60.0, connect=10.0, read=300.0),
        follow_redirects=False)
    response = client.post(
        "/api/auth/exchange",
        headers={"Origin": env.origin},
        json={"token": invite})
    if response.status_code != 200:
        client.close()
        pytest.fail(
            f"{name} 邀请兑换失败（HTTP {response.status_code}）；"
            "邀请必须一次性且未过期", pytrace=False)
    set_cookie = response.headers.get("set-cookie", "")
    cookie = set_cookie.split(";", 1)[0]
    if not cookie.startswith(SESSION_COOKIE + "="):
        client.close()
        pytest.fail(f"{name} 未收到受保护会话 Cookie", pytrace=False)
    body = response.json()
    csrf = body.get("csrfToken")
    if not isinstance(csrf, str) or not csrf:
        client.close()
        pytest.fail(f"{name} 未收到 CSRF token", pytrace=False)
    me = client.get("/api/auth/me",
                    headers={"Cookie": cookie, "Origin": env.origin})
    if me.status_code != 200:
        client.close()
        pytest.fail(
            f"{name} /api/auth/me 失败（HTTP {me.status_code}）",
            pytrace=False)
    identity = me.json()
    return LiveUser(
        name=name, client=client, cookie=cookie, csrf=csrf,
        user_id=str(identity.get("userId", "")),
        workspace_id=str(identity.get("workspaceId", "")),
        origin=env.origin,
    )


@pytest.fixture(scope="session")
def live_users(live_env) -> tuple[LiveUser, LiveUser]:
    """每用户只兑换一次邀请；当前门槛串行运行，不跨进程复用。"""
    users: list[LiveUser] = []
    try:
        users.append(_login(live_env, "A", live_env.invite_a))
        users.append(_login(live_env, "B", live_env.invite_b))
        yield users[0], users[1]
    finally:
        for user in users:
            user.client.close()


@pytest.fixture(scope="session")
def live_browser_env(request) -> dict[str, str]:
    _require_live(request.config, BASE_ENV)
    preview = os.environ.get("EASEL_TEST_PREVIEW_URL")
    if not preview:
        pytest.fail(
            "缺少浏览器预览门槛环境变量：EASEL_TEST_PREVIEW_URL",
            pytrace=False)
    return {"preview_url": preview}


@pytest.fixture(scope="session")
def live_restore_env(request) -> dict[str, str]:
    values = _require_live(request.config, RESTORE_ENV)
    result = {
        "source": values["EASEL_TEST_BACKUP_SOURCE"],
        "destination": values["EASEL_TEST_RESTORE_DEST"],
    }
    managed = os.environ.get("EASEL_TEST_RESTORE_MANAGED_ROOT")
    if managed:
        result["managed_root"] = managed
    return result
