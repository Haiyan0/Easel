"""Task 5C3：真实 control/gateway/worker Run 链路（受控发布替身）。

真实层：control FastAPI、GatewayClient/gateway、WorkspaceApi、WorkerRelay、
worker RunManager、WorkspacePublishing、browser_tasks 发布执行器。
替身层：平台发布子进程只写本人临时状态，不访问真实平台、不启动浏览器。
"""
from __future__ import annotations

import asyncio
import json
import time
import zipfile
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

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
from web import app as worker

ORIGIN = "https://easel.test"
PLATFORM = "xiaohongshu"
DOUYIN = "douyin"
PNG = b"\x89PNG\r\n\x1a\nfixture"
VIDEO = b"\x00\x00\x00\x18ftypmp42fixture"


class FakePublisher:
    """受控发布进程：按模式写状态、等待当前 Run 的验证码文件。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.mode = "success"
        self.cancelled = 0

    async def __call__(self, platform, cmd, env, status_file, code_file,
                       timeout, emit):
        self.calls.append({
            "platform": platform, "cmd": list(cmd),
            "status_file": status_file, "code_file": code_file,
        })
        try:
            if status_file is not None and platform == DOUYIN:
                status_file.parent.mkdir(parents=True, exist_ok=True)
                self._status(status_file, emit, "sms_required",
                             "请输入容器受控短信验证码")
                deadline = time.monotonic() + 10
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
                    await asyncio.sleep(0.02)
                return {"returncode": 1, "status": {
                    "state": "error", "message": "短信验证超时"}}
            if self.mode == "unknown":
                return {"returncode": 5, "status": None}
            if self.mode == "error":
                return {"returncode": 1, "status": None}
            if self.mode == "slow":
                await asyncio.sleep(30)
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


class Chain:
    def __init__(self, tmp_path: Path, monkeypatch, *,
                 publish_mode: str = "automated") -> None:
        self.root = tmp_path
        self.workspace_root = tmp_path / "workspace"
        for name in ("outputs", "profiles", "state", "home"):
            (self.workspace_root / name).mkdir(parents=True)
        self.outputs = self.workspace_root / "outputs"
        monkeypatch.setattr(worker, "OUTPUTS_DIR", self.outputs)
        monkeypatch.setattr(worker, "PUBLISH_DIR", self.outputs / "_publish")
        monkeypatch.setattr(worker, "PROFILES_DIR",
                            self.workspace_root / "profiles")
        monkeypatch.setattr(worker, "LOGIN_DIR", self.outputs / "_login")
        monkeypatch.setattr(worker, "_SERVICE_MODE", "worker")
        monkeypatch.setattr(worker, "_RUNS", None)
        from easel import persona
        monkeypatch.setattr(persona, "PROFILES_DIR", worker.PROFILES_DIR)
        self.fake = FakePublisher()
        monkeypatch.setattr(worker, "_execute_managed_publish", self.fake)
        self.store = Store.connect(tmp_path / "control.sqlite3")
        self.admin = self.store.create_user("Owner", "admin")
        self.member = self.store.create_user("Member")
        self.auth = AuthService(self.store)
        self.key = Ed25519PrivateKey.generate()
        self.handle = WorkerHandle(
            self.member.workspace_id, f"{0x5C3:064x}",
            "http://127.0.0.1:7860", "service-5c3", 0,
            gateway_instance_id="gateway-5c3",
            gateway_generation="boot-5c3",
            gateway_upstream="http://127.0.0.1:8898",
            gateway_state="available")
        now = int(time.time())
        outcome, lease = self.store.claim_worker_start(
            self.member.workspace_id, 0, "owner-5c3", now, 3600)
        assert outcome == "started", outcome
        assert self.store.complete_worker_start(
            self.member.workspace_id, "owner-5c3", lease.generation,
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
            config=ControlConfig(public_origin=ORIGIN,
                                 publish_mode=publish_mode),
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

    def media(self, relative: str = "project/cover.png",
              data: bytes = PNG) -> Path:
        path = self.outputs / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

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


@pytest.fixture
def manual_chain(tmp_path, monkeypatch):
    instance = Chain(tmp_path, monkeypatch, publish_mode="manual")
    yield instance
    asyncio.run(instance.close())


def _headers(csrf: str) -> dict:
    return {"Origin": ORIGIN, "X-CSRF-Token": csrf}


def _publish_body(media: str = "project/cover.png",
                  request_id: str = "publish-1") -> dict:
    return {"title": "测试标题", "body": "测试正文", "tags": "工具",
            "media": [media], "persona": "", "requestId": request_id}


def _wait_status(client, platform: str, states: tuple[str, ...],
                 timeout: float = 10) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = client.get(f"/api/publish/{platform}/status")
        if last.status_code == 200 and last.json().get("state") in states:
            return last.json()
        time.sleep(0.02)
    assert last is not None
    raise AssertionError(last.text)


def test_chain_manual_export_downloads_and_unpacks(chain):
    chain.media()
    with TestClient(chain.app, base_url=ORIGIN) as client:
        csrf = chain.login(client)
        exported = client.post(
            "/api/manual-publish/xiaohongshu/export",
            json={"title": "人工稿件", "body": "正文", "tags": "工具",
                  "media": ["project/cover.png"], "reviewed": True,
                  "requestId": "export-1"},
            headers=_headers(csrf))
        assert exported.status_code == 200, exported.text
        payload = exported.json()
        assert payload["published"] is False
        assert payload["path"].startswith(
            "_publish/manual/easel-xiaohongshu-")
        package = client.get(f"/api/media/{payload['path']}")
        assert package.status_code == 200, package.text
        with zipfile.ZipFile(
                __import__("io").BytesIO(package.content)) as archive:
            assert archive.read("media/01_cover.png") == PNG
            assert archive.read("title.txt").decode() == "人工稿件"
        assert not (chain.outputs / "_schedule.json").exists()

        # 同 requestId 幂等，不生成第二个包。
        again = client.post(
            "/api/manual-publish/xiaohongshu/export",
            json={"title": "人工稿件", "body": "正文", "tags": "工具",
                  "media": ["project/cover.png"], "reviewed": True,
                  "requestId": "export-1"},
            headers=_headers(csrf))
        assert again.status_code == 200, again.text
        assert again.json()["path"] == payload["path"]
        packages = list(
            (chain.outputs / "_publish" / "manual").glob("*.zip"))
        assert len(packages) == 1


def test_chain_publish_success_records_calendar_once(chain):
    chain.media()
    with TestClient(chain.app, base_url=ORIGIN) as client:
        csrf = chain.login(client)
        response = client.post(
            "/api/publish/xiaohongshu", json=_publish_body(),
            headers=_headers(csrf))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["async"] is True
        assert body["state"] == "starting"
        status = _wait_status(client, PLATFORM, ("success",))
        assert status["runId"] == body["runId"]
        assert status["calendarRecorded"] is True
        schedule = json.loads(
            (chain.outputs / "_schedule.json").read_text(encoding="utf-8"))
        assert len(schedule) == 1
        assert schedule[0]["source"] == "publish-page"
        assert len(chain.fake.calls) == 1
        staged = chain.fake.calls[0]["cmd"]
        assert any("_publish" in str(part) and "runs" in str(part)
                   for part in staged)

        # 同 requestId 重试返回原 Run，不再次执行/记录。
        duplicate = client.post(
            "/api/publish/xiaohongshu", json=_publish_body(),
            headers=_headers(csrf))
        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.json()["runId"] == body["runId"]
        assert len(chain.fake.calls) == 1
        schedule = json.loads(
            (chain.outputs / "_schedule.json").read_text(encoding="utf-8"))
        assert len(schedule) == 1


def test_chain_douyin_sms_binds_current_run(chain):
    chain.media("project/clip.mp4", VIDEO)
    with TestClient(chain.app, base_url=ORIGIN) as client:
        csrf = chain.login(client)
        started = client.post(
            "/api/publish/douyin",
            json=_publish_body("project/clip.mp4", "douyin-1"),
            headers=_headers(csrf))
        assert started.status_code == 200, started.text
        run_id = started.json()["runId"]
        assert started.json()["async"] is True
        status = _wait_status(client, DOUYIN, ("sms_required",))
        assert status["runId"] == run_id

        early = client.post(
            "/api/publish/douyin/sms",
            json={"code": "123456", "runId": "unknown-run"},
            headers=_headers(csrf))
        assert early.status_code == 404, early.text
        wrong = client.post(
            "/api/publish/douyin/sms",
            json={"code": "000000", "runId": run_id},
            headers=_headers(csrf))
        assert wrong.status_code == 200, wrong.text
        _wait_status(client, DOUYIN, ("sms_required",))
        accepted = client.post(
            "/api/publish/douyin/sms",
            json={"code": "123456", "runId": run_id},
            headers=_headers(csrf))
        assert accepted.status_code == 200, accepted.text
        success = _wait_status(client, DOUYIN, ("success",), timeout=15)
        assert success["runId"] == run_id
        assert len(chain.fake.calls) == 1
        late = client.post(
            "/api/publish/douyin/sms",
            json={"code": "123456", "runId": run_id},
            headers=_headers(csrf))
        assert late.status_code == 409, late.text


def test_chain_unknown_result_is_not_success_or_calendar(chain):
    chain.media()
    chain.fake.mode = "unknown"
    with TestClient(chain.app, base_url=ORIGIN) as client:
        csrf = chain.login(client)
        response = client.post(
            "/api/publish/xiaohongshu", json=_publish_body(),
            headers=_headers(csrf))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["async"] is True
        status = _wait_status(client, PLATFORM, ("unknown",))
        assert status["runId"] == body["runId"]
        assert not (chain.outputs / "_schedule.json").exists()
        again = client.get(f"/api/publish/{PLATFORM}/status")
        assert again.status_code == 200, again.text
        assert again.json()["state"] == "unknown"


def test_chain_stop_marks_unknown_and_does_not_replay(chain):
    chain.media("project/clip.mp4", VIDEO)
    with TestClient(chain.app, base_url=ORIGIN) as client:
        csrf = chain.login(client)
        started = client.post(
            "/api/publish/douyin",
            json=_publish_body("project/clip.mp4", "stop-1"),
            headers=_headers(csrf))
        assert started.status_code == 200, started.text
        run_id = started.json()["runId"]
        _wait_status(client, DOUYIN, ("sms_required",))
        stopped = client.post(
            "/api/chat/stop", json={"runId": run_id},
            headers=_headers(csrf))
        assert stopped.status_code == 200, stopped.text
        status = _wait_status(client, DOUYIN, ("unknown",))
        assert status["runId"] == run_id
        assert status["state"] == "unknown"
        assert len(chain.fake.calls) == 1
        assert chain.fake.cancelled == 1
        assert not (chain.outputs / "_schedule.json").exists()
        # 停止后不会自动补发；重复状态查询也不创建新 Run。
        assert client.get(f"/api/publish/{DOUYIN}/status").json()[
            "state"] == "unknown"
        assert len(chain.fake.calls) == 1


def test_manual_policy_rejects_before_worker_and_run(manual_chain):
    manual_chain.media()
    with TestClient(manual_chain.app, base_url=ORIGIN) as client:
        csrf = manual_chain.login(client)
        response = client.post(
            "/api/publish/xiaohongshu", json=_publish_body(),
            headers=_headers(csrf))
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "publishing_manual_only"
        assert manual_chain.fake.calls == []
        with Store.open(manual_chain.store.path) as opened:
            assert opened.get_task_conversation(
                manual_chain.member, "publish:xiaohongshu") is None
