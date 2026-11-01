"""Task 5C2：真实 control/gateway/worker Run 链路（受控平台替身）。

真实层：control FastAPI、GatewayClient/gateway、WorkspaceApi、WorkerRelay、
worker RunManager（默认 browser executor）、WorkspaceAccounts。
替身层：平台脚本入口（登录/扫码/短信/分析/凭证/whoami）只写本人临时产物，
不访问真实平台、不启动真实浏览器。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from fastapi.testclient import TestClient

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
from web import app as worker

ORIGIN = "https://easel.test"
PLATFORM = "xiaohongshu"
PNG = b"\x89PNG\r\n\x1a\nfixture"


class FakePlatform:
    """受控平台状态机：二维码→扫码/短信→成功，所有状态写本人临时目录。"""

    def __init__(self, login_dir: Path, analytics_dir: Path) -> None:
        self.login_dir = login_dir
        self.analytics_dir = analytics_dir
        self.scanned = False
        self.code = ""
        self.login_starts: list[str] = []
        self.whoami_calls: list[str] = []
        self.analytics_calls: list[str] = []
        self.fail_status_once = False
        self.analytics_fail = False
        self.identity_name = "受控测试昵称"
        self.avatar = "data:image/png;base64,iVBORw0KGgo="

    def _write_status(self, platform: str, state: str, message: str = "",
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
        qr = self.login_dir / f"{platform}.png"
        qr.parent.mkdir(parents=True, exist_ok=True)
        qr.write_bytes(PNG)
        self._write_status(platform, "qr_ready", "请扫码",
                           f"_login/{platform}.png")
        return {"mode": "qr", "state": "starting"}

    def login_status(self, platform: str) -> dict:
        if self.fail_status_once:
            self.fail_status_once = False
            raise HTTPException(503, "暂时不可用")
        status_path = self.login_dir / f"{platform}.json"
        current = {}
        if status_path.is_file():
            try:
                current = json.loads(status_path.read_text(encoding="utf-8"))
            except ValueError:
                current = {}
        state = current.get("state", "unknown")
        if state in ("success", "expired", "error"):
            return self._status_view(platform, current)
        code_file = self.login_dir / f"{platform}.code"
        if code_file.is_file():
            try:
                self.code = code_file.read_text(encoding="utf-8").strip()
            except OSError:
                pass
        if self.scanned:
            if not self.code:
                return self._write_status(
                    platform, "sms_required", "请输入短信验证码")
            if self.code != "123456":
                return self._write_status(
                    platform, "sms_required", "验证码不正确")
            worker._write_login_marker(platform, "success", self.identity_name)
            (self.login_dir / f"{platform}.png").unlink(missing_ok=True)
            return self._write_status(
                platform, "success", self.identity_name)
        return self._status_view(platform, current)

    def _status_view(self, platform: str, data: dict) -> dict:
        state = data.get("state", "unknown")
        qr = ""
        qr_ts = 0
        qr_path = self.login_dir / f"{platform}.png"
        if state in ("qr_ready", "window_login") and qr_path.is_file():
            qr = f"_login/{platform}.png"
            qr_ts = int(qr_path.stat().st_mtime_ns // 1_000_000)
        return {"state": state, "message": data.get("message", ""),
                "qr": qr, "qrTs": qr_ts}

    async def whoami(self, platform: str, force: bool = False) -> dict:
        self.whoami_calls.append(platform)
        status_path = self.login_dir / f"{platform}.json"
        logged_in = False
        if status_path.is_file():
            try:
                logged_in = json.loads(
                    status_path.read_text(encoding="utf-8")).get(
                        "state") == "success"
            except ValueError:
                logged_in = False
        identity = {
            "loggedIn": logged_in,
            "name": self.identity_name if logged_in else "",
            "avatar": self.avatar if logged_in else "",
        }
        artifact = self.login_dir / f"{platform}-whoami.json"
        artifact.write_text(json.dumps(identity, ensure_ascii=False),
                            encoding="utf-8")
        return identity

    async def analytics(self, platform: str) -> dict:
        self.analytics_calls.append(platform)
        if self.analytics_fail:
            raise HTTPException(502, "平台维护")
        payload = {
            "platform": platform, "name": "受控账号",
            "nickname": self.identity_name, "loggedIn": True,
            "followers": 12, "likes": 34, "following": None,
            "posts": None,
            "metrics": [{"label": "近7日", "value": 5, "vs": "+1"}],
            "notes": [{"title": "受控笔记", "url": "https://example.test/1"}],
            "growth": {"week": {"followers": 2, "likes": 3, "posts": None,
                                "since_days": 7}},
            "fetched_at": int(time.time()),
        }
        self.analytics_dir.mkdir(parents=True, exist_ok=True)
        (self.analytics_dir / f"{platform}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return payload


class Chain:
    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.root = tmp_path
        self.workspace_root = tmp_path / "workspace"
        for name in ("outputs", "profiles", "state", "home"):
            (self.workspace_root / name).mkdir(parents=True)
        monkeypatch.setattr(worker, "OUTPUTS_DIR",
                            self.workspace_root / "outputs")
        monkeypatch.setattr(worker, "PROFILES_DIR",
                            self.workspace_root / "profiles")
        monkeypatch.setattr(worker, "LOGIN_DIR",
                            self.workspace_root / "outputs" / "_login")
        monkeypatch.setattr(worker, "_SERVICE_MODE", "worker")
        monkeypatch.setattr(worker, "_RUNS", None)
        from easel import persona
        monkeypatch.setattr(persona, "PROFILES_DIR", worker.PROFILES_DIR)
        self.fake = FakePlatform(
            worker.LOGIN_DIR, worker.OUTPUTS_DIR / "_analytics")
        monkeypatch.setattr(worker, "_start_login", self.fake.start_login)
        monkeypatch.setattr(worker, "_login_status", self.fake.login_status)
        monkeypatch.setattr(
            worker, "_check_account_whoami", self.fake.whoami)
        monkeypatch.setattr(worker, "_fetch_analytics", self.fake.analytics)
        monkeypatch.setattr(
            worker, "_wechat_verify_credentials",
            lambda app_id, secret: (True, ""))
        monkeypatch.setattr(
            worker, "_wechat_config_path", lambda: tmp_path / "wechat.yaml")
        self.store = Store.connect(tmp_path / "control.sqlite3")
        self.admin = self.store.create_user("Owner", "admin")
        self.member = self.store.create_user("Member")
        self.auth = AuthService(self.store)
        self.key = Ed25519PrivateKey.generate()
        self.handle = WorkerHandle(
            self.member.workspace_id, f"{0x5C2:064x}",
            "http://127.0.0.1:7860", "service-5c2", 0,
            gateway_instance_id="gateway-5c2",
            gateway_generation="boot-5c2",
            gateway_upstream="http://127.0.0.1:8898",
            gateway_state="available")
        now = int(time.time())
        outcome, lease = self.store.claim_worker_start(
            self.member.workspace_id, 0, "owner-5c2", now, 3600)
        assert outcome == "started", outcome
        assert self.store.complete_worker_start(
            self.member.workspace_id, "owner-5c2", lease.generation,
            self.handle, now, 3600)
        self.paths = WorkspacePaths(
            code_root=Path(__file__).resolve().parents[2],
            workspace_root=self.workspace_root,
            state_root=self.workspace_root / "state",
            home=self.workspace_root / "home")
        from easel.worker.guard import WorkerIdentity
        monkeypatch.setattr(
            worker, "_WORKER_IDENTITY",
            WorkerIdentity(self.member.workspace_id,
                           self.handle.service_token))
        self.manager = RunManager(
            workspace_id=self.member.workspace_id, paths=self.paths,
            public_key=self.key.public_key(),
            sessions=WorkspaceSessionStore(
                self.paths.state_root, self.member.workspace_id),
            base_env={"PATH": "", "HOME": str(self.paths.home)},
            recover=False)
        worker._RUNS = self.manager
        monkeypatch.setattr(worker, "_RUNS", self.manager)
        self.worker_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=worker.app),
            trust_env=False)
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
        self.app = create_app(
            store=self.store, auth=self.auth,
            config=ControlConfig(public_origin=ORIGIN),
            runtime=self.runtime,
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
                            "command": ["fixture-acp"],
                            "timeout": 60,
                        }, broker=None),
                    http_client=self.gateway_client,
                    gateway_client=GatewayClient(
                        self.key, self.gateway_client),
                    db_runner=db_runner, gateway_required=True))

    def login(self, client: TestClient) -> str:
        invite = self.auth.create_invite(self.admin, self.member.user_id)
        exchanged = client.post(
            "/api/auth/exchange", json={"token": invite},
            headers={"Origin": ORIGIN})
        assert exchanged.status_code == 200, exchanged.text
        return exchanged.json()["csrfToken"]

    async def close(self) -> None:
        await self.manager.aclose()
        await self.gateway_client.aclose()
        await self.worker_client.aclose()


@pytest.fixture
def chain(tmp_path, monkeypatch):
    instance = Chain(tmp_path, monkeypatch)
    yield instance
    asyncio.run(instance.close())


def _headers(csrf: str) -> dict:
    return {"Origin": ORIGIN, "X-CSRF-Token": csrf}


def test_chain_capacity_restart_and_stop(chain):
    with TestClient(chain.app, base_url=ORIGIN) as client:
        csrf = chain.login(client)
        first = client.post(
            f"/api/login/{PLATFORM}", json={"requestId": "cap-1"},
            headers=_headers(csrf))
        assert first.status_code == 200, first.text
        second = client.post(
            "/api/login/zhihu", json={"requestId": "cap-2"},
            headers=_headers(csrf))
        assert second.status_code == 200, second.text
        third = client.post(
            "/api/login/douyin", json={"requestId": "cap-3"},
            headers=_headers(csrf))
        assert third.status_code == 429, third.text
        assert third.json()["code"] == "workspace_capacity"

        # 显式 restart：先停旧 Run 等终态，再派发新 Run
        restarted = client.post(
            f"/api/login/{PLATFORM}",
            json={"requestId": "cap-4", "restart": True},
            headers=_headers(csrf))
        assert restarted.status_code == 200, restarted.text
        assert restarted.json()["runId"] != first.json()["runId"]
        old = chain.store.get_run(chain.member, first.json()["runId"])
        assert old.status == "cancelled"
        assert chain.fake.login_starts.count(PLATFORM) == 2

        # 停止当前登录并释放容量
        stopped = client.post(
            f"/api/login/{PLATFORM}/stop",
            json={"runId": restarted.json()["runId"]},
            headers=_headers(csrf))
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["state"] in ("cancelled", "interrupted")
        current = chain.store.get_run(
            chain.member, restarted.json()["runId"])
        assert current.status == "cancelled"
        after_stop = client.post(
            "/api/login/douyin", json={"requestId": "cap-5"},
            headers=_headers(csrf))
        assert after_stop.status_code == 200, after_stop.text


def test_real_chain_login_sms_whoami_analytics_credentials_logout(chain):
    with TestClient(chain.app, base_url=ORIGIN) as client:
        csrf = chain.login(client)
        started = client.post(
            f"/api/login/{PLATFORM}", json={"requestId": "chain-login"},
            headers=_headers(csrf))
        assert started.status_code == 200, started.text
        run_id = started.json()["runId"]
        assert run_id
        assert chain.fake.login_starts == [PLATFORM]
        # 等待 worker executor 写入二维码
        deadline = time.monotonic() + 5
        status = None
        while time.monotonic() < deadline:
            status = client.get(f"/api/login/{PLATFORM}/status")
            if status.status_code == 200 and status.json()["qr"]:
                break
            time.sleep(0.02)
        assert status is not None and status.status_code == 200, status.text
        assert status.json()["state"] == "qr_ready"
        assert status.json()["qrUrl"] == f"/api/accounts/{PLATFORM}/qr"
        qr = client.get(f"/api/accounts/{PLATFORM}/qr?v=1")
        assert qr.status_code == 200, qr.text
        assert qr.headers["content-type"].startswith("image/png")
        assert qr.content == PNG

        # 一次临时 503 不应终止任务
        chain.fake.fail_status_once = True
        transient = client.get(f"/api/login/{PLATFORM}/status")
        assert transient.status_code == 503, transient.text
        recovered = client.get(f"/api/login/{PLATFORM}/status")
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["state"] == "qr_ready"

        chain.fake.scanned = True
        sms = client.get(f"/api/login/{PLATFORM}/status")
        assert sms.status_code == 200
        assert sms.json()["state"] == "sms_required"
        wrong = client.post(
            f"/api/login/{PLATFORM}/sms",
            json={"code": "000000", "runId": run_id},
            headers=_headers(csrf))
        assert wrong.status_code == 200, wrong.text
        assert client.get(
            f"/api/login/{PLATFORM}/status").json()["state"] == "sms_required"
        right = client.post(
            f"/api/login/{PLATFORM}/sms",
            json={"code": "123456", "runId": run_id},
            headers=_headers(csrf))
        assert right.status_code == 200, right.text
        success = client.get(f"/api/login/{PLATFORM}/status")
        assert success.status_code == 200, success.text
        assert success.json()["state"] == "success"
        assert chain.fake.login_starts == [PLATFORM]
        assert chain.fake.whoami_calls == []
        # 控制库 Run 已终态，容量释放
        run = chain.store.get_run(chain.member, run_id)
        assert run.status == "done"

        # 显式 whoami：受控 runner 写本人产物，页面读取昵称/头像
        who = client.post(
            f"/api/accounts/{PLATFORM}/whoami",
            json={"force": True, "requestId": "chain-whoami"},
            headers=_headers(csrf))
        assert who.status_code == 200, who.text
        assert who.json()["loggedIn"] is True
        assert who.json()["name"] == "受控测试昵称"
        assert chain.fake.whoami_calls == [PLATFORM]

        # 分析：受控 runner 写产物，返回原值（未知数字保持 null）
        analytics = client.get(f"/api/analytics/{PLATFORM}")
        assert analytics.status_code == 200, analytics.text
        payload = analytics.json()
        assert payload["followers"] == 12
        assert payload["following"] is None
        assert payload["posts"] is None
        assert payload["fetched_at"] > 0
        assert chain.fake.analytics_calls == [PLATFORM]

        # 凭证：验证后保存，结果不回传 AppSecret
        credentials = client.post(
            "/api/accounts/wechat-oa/credentials",
            json={"appId": "wx1234567890", "appSecret": "secret-value",
                  "name": "公众号", "author": "", "requestId": "chain-cred"},
            headers=_headers(csrf))
        assert credentials.status_code == 200, credentials.text
        assert credentials.json()["ok"] is True
        assert "secret-value" not in credentials.text
        listed = client.get("/api/accounts")
        assert listed.status_code == 200
        wechat = [item for item in listed.json()
                  if item["platform"] == "wechat-oa"][0]
        assert wechat["credentialConfigured"] is True

        # 注销：停任务/清状态，注销后状态不能复活为 success
        logged_out = client.post(
            f"/api/logout/{PLATFORM}", headers=_headers(csrf))
        assert logged_out.status_code == 200, logged_out.text
        assert not (worker.LOGIN_DIR / f"{PLATFORM}.json").exists()
        after = client.get("/api/accounts")
        assert after.status_code == 200
        xhs = [item for item in after.json()
               if item["platform"] == PLATFORM][0]
        assert xhs["loggedIn"] is False
