"""Task 10 Step 5：真实 TCP 的 control ↔ 两个独立 worker 授权链路。

覆盖：

* control 的 relay 必须直连本人 worker，不读取宿主环境代理；
* A/B 两个独立 uvicorn worker（私有 code/workspace/state/HOME）与真实
  control app：登录 → 新建会话 → 接纳 → SSE → 断线重连 → 权限回答/停止
  → 读取持久消息快照；
* 至少一条链使用实际 ``ACPAdapter`` 与 ``tests/fixtures/harness_rpc.py``
  子进程，control 与 worker 之间是真实 TCP，而不是 ``MockTransport``；
* 并发容量、同会话互斥、跨租户拒绝、登录撤销窗口、502 不自动重发；
* 服务凭证/管理员 Key/私钥/broker token 不出现在公开响应、SSE、日志与
  用户可读消息快照中；逐 Run 签名包只把必要的最小作用域 broker token
  交给本人 worker。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from easel.control.app import ControlConfig, create_app
from easel.control.auth import AuthService
from easel.control.proxy import PinnedRunConfig, WorkerRelay
from easel.control.store import Store
from easel.control.types import DomainError, WorkerHandle
from easel.worker.config import run_input_digest, verify_run_snapshot
from easel.worker.sessions import WorkspaceSessionStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "harness_rpc.py"
ORIGIN = "https://easel.test"
SESSION_COOKIE = "__Host-easel_session"
REVISION = 7
TERMINAL = frozenset({"done", "failed", "interrupted", "cancelled"})
ADMIN_SENTINEL = "admin-key-" + uuid.uuid4().hex
TIMEOUT = httpx.Timeout(30.0, connect=5.0, read=15.0)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def install_dead_proxy(monkeypatch, port: int = 9) -> None:
    """让默认 httpx 客户端看到的代理必然失败；测试客户端自身显式绕开。

    注意 Windows 的 ``os.environ`` 大小写不敏感：清理 ``http_proxy`` 等小写
    别名会一并删除刚设置的 ``HTTP_PROXY``，所以只清理 ``NO_PROXY`` 后再
    设置大写代理变量，避免回落到注册表系统代理造成假绿。
    """
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    proxy = f"http://127.0.0.1:{port}"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, proxy)


def make_client(host) -> httpx.Client:
    # 测试进程本身也必须绕开死代理，否则连 control 的请求都会被代理截走。
    return httpx.Client(base_url=host.base, trust_env=False, timeout=TIMEOUT,
                        follow_redirects=False)


@dataclass(frozen=True)
class Auth:
    cookie: str
    csrf: str


def read_headers(auth: Auth) -> dict[str, str]:
    return {"Cookie": auth.cookie, "Origin": ORIGIN}


def write_headers(auth: Auth) -> dict[str, str]:
    return {**read_headers(auth), "X-CSRF-Token": auth.csrf}


def call(client: httpx.Client, public_texts: list[str], method: str,
         url: str, *, headers: dict | None = None,
         json_body: object = None) -> httpx.Response:
    response = client.request(method, url, headers=headers, json=json_body)
    public_texts.append(response.text)
    return response


def login(client: httpx.Client, invite: str,
          public_texts: list[str]) -> Auth:
    response = call(client, public_texts, "POST", "/api/auth/exchange",
                    headers={"Origin": ORIGIN}, json_body={"token": invite})
    assert response.status_code == 200, response.text
    cookie = response.headers["set-cookie"].split(";", 1)[0]
    assert cookie.startswith(SESSION_COOKIE + "="), cookie
    return Auth(cookie, response.json()["csrfToken"])


def create_session(client: httpx.Client, auth: Auth, public_texts: list[str],
                   title: str) -> str:
    response = call(client, public_texts, "POST", "/api/sessions",
                    headers=write_headers(auth), json_body={"title": title})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def submit(client: httpx.Client, auth: Auth, public_texts: list[str],
           session_id: str, turn_id: str, message: str) -> httpx.Response:
    return call(client, public_texts, "POST", "/api/chat",
                headers=write_headers(auth),
                json_body={"sessionId": session_id, "turnId": turn_id,
                           "message": message})


def iter_sse(response: httpx.Response):
    frame: dict[str, str] = {}
    data_lines: list[str] = []
    for line in response.iter_lines():
        if line == "":
            if frame or data_lines:
                yield {"id": frame.get("id"), "event": frame.get("event"),
                       "data": "\n".join(data_lines)}
            frame = {}
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value.lstrip(" ")
        if name == "data":
            data_lines.append(value)
        else:
            frame[name] = value
    if frame or data_lines:
        yield {"id": frame.get("id"), "event": frame.get("event"),
               "data": "\n".join(data_lines)}


def state_of(frame: dict) -> str | None:
    if frame.get("event") != "state":
        return None
    return json.loads(frame["data"]).get("state")


def question_payload(frame: dict) -> dict:
    # worker 把问题 payload 作为 JSON 字符串存入事件，SSE 再编码一次。
    return json.loads(json.loads(frame["data"]))


def collect_stream(client: httpx.Client, url: str, auth: Auth,
                   public_texts: list[str], *, headers: dict | None = None,
                   stop=None, on_frame=None) -> list[dict]:
    merged = {**read_headers(auth), **(headers or {})}
    frames: list[dict] = []
    with client.stream("GET", url, headers=merged) as response:
        if response.status_code != 200:
            body = response.read().decode("utf-8", "replace")
            public_texts.append(body)
            raise AssertionError(f"SSE {response.status_code}: {body}")
        for frame in iter_sse(response):
            frames.append(frame)
            public_texts.append(
                f"{frame.get('event')}:{frame.get('data')}")
            if on_frame is not None:
                on_frame(frame)
            if stop is not None and stop(frame):
                break
    return frames


def wait_until(predicate, *, timeout: float = 15.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"等待条件超时，最后观察值：{last!r}")


def connect_db(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def run_row(db_path: Path, run_id: str) -> dict:
    with closing(connect_db(db_path)) as connection:
        row = connection.execute(
            "SELECT id, conversation_id, client_request_id, status,"
            " dispatch_state, config_version FROM runs WHERE id = ?",
            (run_id,)).fetchone()
    assert row is not None, f"Run 不存在：{run_id}"
    return dict(row)


def active_runs(db_path: Path, workspace_id: str) -> int:
    with closing(connect_db(db_path)) as connection:
        return int(connection.execute(
            "SELECT COUNT(*) FROM runs WHERE workspace_id = ?"
            " AND status NOT IN ('done', 'failed', 'interrupted', 'cancelled')",
            (workspace_id,)).fetchone()[0])


def latest_run(db_path: Path, workspace_id: str) -> str:
    with closing(connect_db(db_path)) as connection:
        row = connection.execute(
            "SELECT id FROM runs WHERE workspace_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (workspace_id,)).fetchone()
    assert row is not None
    return str(row["id"])


def seed_control(db_path: Path):
    store = Store.connect(db_path)
    try:
        auth = AuthService(store)
        admin, _admin_invite = auth.bootstrap_admin("Admin")
        identities = {
            name: store.create_user(name) for name in ("A", "B", "C")}
        invites = {
            "a": auth.create_invite(admin, identities["A"].user_id),
            "b1": auth.create_invite(admin, identities["B"].user_id),
            "b2": auth.create_invite(admin, identities["B"].user_id),
            "c": auth.create_invite(admin, identities["C"].user_id),
        }
    finally:
        store.close()
    return identities, invites


# ---------------------------------------------------------------------------
# 受控 runtime / provider
# ---------------------------------------------------------------------------


class TokenMint:
    """逐 Run broker token 生成器；每次读取固定配置都会铸新 token。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0
        self.tokens: list[str] = []

    def __call__(self) -> str:
        with self._lock:
            self._count += 1
            token = f"broker-{self._count}-{uuid.uuid4().hex}"
            self.tokens.append(token)
            return token


def make_provider(mints: dict[str, TokenMint],
                  commands: dict[str, list[str]]):
    """provider 只按 actor 的 workspace 给出本人固定配置，不查询请求内容。"""

    def provider(actor) -> PinnedRunConfig:
        mint = mints[actor.workspace_id]
        return PinnedRunConfig(
            revision=REVISION,
            routes={"broker_token": mint()},
            agent={"runtime": "acp", "command": list(commands[actor.workspace_id]),
                   "sandbox": "read-only", "timeout": 60})

    return provider


class StaticRuntime:
    def __init__(self, handles: dict[str, WorkerHandle]) -> None:
        self._handles = handles
        self._lock = threading.Lock()
        self.calls: list[tuple[str, int]] = []

    async def ensure_ready(self, workspace_id: str,
                           config_version: int) -> WorkerHandle:
        with self._lock:
            self.calls.append((workspace_id, config_version))
        handle = self._handles.get(workspace_id)
        if handle is None or handle.config_version != config_version:
            from easel.control.runtime import RuntimeUnavailable
            raise RuntimeUnavailable("worker_config_mismatch")
        return handle

    async def bound_handle(self, workspace_id: str, container_id: str,
                           config_version: int) -> WorkerHandle | None:
        handle = self._handles.get(workspace_id)
        if (handle is None or handle.container_id != container_id
                or handle.config_version != config_version):
            return None
        return handle

    def count(self, workspace_id: str) -> int:
        with self._lock:
            return sum(1 for workspace, _ in self.calls
                       if workspace == workspace_id)


# ---------------------------------------------------------------------------
# 真实 control app（uvicorn，端口 0）
# ---------------------------------------------------------------------------


class ControlHost:
    def __init__(self, db_path: Path, key: Ed25519PrivateKey, runtime,
                 provider) -> None:
        self.db_path = db_path
        self.key = key
        self.runtime = runtime
        self.provider = provider
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None
        self.base = ""

    def _relay_factory(self, runtime, sessions, auth, store, http_client,
                       broker, db_runner):
        return WorkerRelay(
            runtime, sessions, auth, store=store, signing_key=self.key,
            http_client=http_client, config_provider=self.provider,
            db_runner=db_runner)

    def start(self) -> "ControlHost":
        setup = Store.connect(self.db_path)
        try:
            app = create_app(
                store=setup, auth=AuthService(setup),
                config=ControlConfig(public_origin=ORIGIN),
                runtime=self.runtime, relay_factory=self._relay_factory)
        finally:
            # create_app 只读取路径；lifespan 在专用线程重建自己的连接。
            setup.close()
        config = uvicorn.Config(
            app, host="127.0.0.1", port=0, log_level="info",
            log_config=None, access_log=True, proxy_headers=False)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(
            target=self.server.run, name="easel-control-test", daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if not self.thread.is_alive():
                raise AssertionError("control 启动失败（线程已退出）")
            if time.monotonic() > deadline:
                raise AssertionError("control 启动超时")
            time.sleep(0.01)
        sockets = self.server.servers[0].sockets
        self.base = f"http://127.0.0.1:{sockets[0].getsockname()[1]}"
        return self

    def stop(self) -> None:
        if self.server is None:
            return
        self.server.should_exit = True
        assert self.thread is not None
        self.thread.join(timeout=30)
        assert not self.thread.is_alive(), "control 未在超时内退出"
        self.server = None


# ---------------------------------------------------------------------------
# 真实 worker 子进程（私有目录，ACPAdapter + 夹具子进程）
# ---------------------------------------------------------------------------


LAUNCHER_SOURCE = r'''
import json
import os
import runpy
import sys
from pathlib import Path

config_path, fixture, runtime = sys.argv[1:4]
config = json.loads(Path(config_path).read_text(encoding="utf-8"))
run_id = os.environ.get("EASEL_RUN_ID", "unknown")
env_dir = Path(config["env_dir"])
env_dir.mkdir(parents=True, exist_ok=True)
record = {
    "run_id": run_id,
    "workspace_id": os.environ.get("EASEL_WORKSPACE_ID"),
    "service_token": os.environ.get("EASEL_SERVICE_TOKEN"),
    "admin_key": os.environ.get("EASEL_ADMIN_KEY"),
    "public_key_file": os.environ.get("EASEL_WORKER_PUBLIC_KEY_FILE"),
    "broker_token": os.environ.get("EASEL_BROKER_TOKEN"),
    "service_mode": os.environ.get("EASEL_SERVICE_MODE"),
    "agent_runtime": os.environ.get("EASEL_AGENT_RUNTIME"),
    "code_root": os.environ.get("EASEL_CODE_ROOT"),
    "workspace_root": os.environ.get("EASEL_WORKSPACE_ROOT"),
    "home": os.environ.get("HOME"),
    "userprofile": os.environ.get("USERPROFILE"),
    "cwd": os.getcwd(),
}
(env_dir / f"{run_id}.jsonl").write_text(
    json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
os.environ["FAKE_HARNESS_MODE"] = config["mode"]
os.environ["FAKE_HARNESS_NATIVE"] = config["native"]
os.environ["FAKE_HARNESS_AUDIT"] = str(
    Path(config["fixture_dir"]) / f"{run_id}.jsonl")
sys.argv = [fixture, runtime]
runpy.run_path(fixture, run_name="__main__")
'''


class Worker:
    """一个独立 uvicorn worker；工作区/状态/HOME 私有，代码根只读共享。"""

    def __init__(self, tmp_path: Path, name: str, workspace_id: str,
                 mode: str, native: str, service_token: str,
                 public_key_path: Path, launcher_path: Path) -> None:
        self.name = name
        self.workspace_id = workspace_id
        self.service_token = service_token
        base = tmp_path / f"worker-{name}"
        # 代码根与真实部署一致：只读共享镜像代码；工作区/状态/HOME 各自私有。
        self.code_root = PROJECT_ROOT
        self.paths = {
            label: base / label
            for label in ("workspace", "state", "home", "fx", "env")}
        for path in self.paths.values():
            path.mkdir(parents=True, exist_ok=True)
        self.fixture_dir = self.paths["fx"]
        self.env_dir = self.paths["env"]
        config_path = base / "fixture-config.json"
        config_path.write_text(json.dumps({
            "mode": mode, "native": native,
            "fixture_dir": str(self.fixture_dir),
            "env_dir": str(self.env_dir),
        }), encoding="utf-8")
        self.command = [sys.executable, str(launcher_path),
                        str(config_path), str(FIXTURE), "acp"]
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.log_path = base / "worker.log"
        self.process: subprocess.Popen | None = None
        self._log = None
        env = {
            key: value for key, value in os.environ.items()
            if not key.upper().startswith(("EASEL_", "FAKE_HARNESS_"))
            and key.upper() != "PYTHONPATH"
        }
        env.update({
            "EASEL_SERVICE_MODE": "worker",
            "EASEL_WORKSPACE_ID": workspace_id,
            "EASEL_SERVICE_TOKEN": service_token,
            "EASEL_WORKER_PUBLIC_KEY_FILE": str(public_key_path),
            "EASEL_CODE_ROOT": str(self.code_root),
            "EASEL_WORKSPACE_ROOT": str(self.paths["workspace"]),
            "EASEL_STATE_ROOT": str(self.paths["state"]),
            "HOME": str(self.paths["home"]),
            "USERPROFILE": str(self.paths["home"]),
            "EASEL_AGENT_RUNTIME": "acp",
            "EASEL_PORT": str(self.port),
            # 模拟宿主误注入管理员 Key：子进程环境必须把它清掉。
            "EASEL_ADMIN_KEY": ADMIN_SENTINEL,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        })
        self._env = env

    @property
    def internal_headers(self) -> dict[str, str]:
        return {"X-Easel-Workspace-Id": self.workspace_id,
                "X-Easel-Service-Token": self.service_token}

    def start(self) -> "Worker":
        self._log = open(self.log_path, "wb")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "web.app:app",
             "--host", "127.0.0.1", "--port", str(self.port),
             "--log-level", "info"],
            cwd=str(PROJECT_ROOT), env=self._env,
            stdin=subprocess.DEVNULL, stdout=self._log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(
                    f"worker {self.name} 提前退出：\n{self.log_text()}")
            try:
                response = httpx.get(
                    self.base + "/api/personas",
                    headers=self.internal_headers, trust_env=False,
                    timeout=3.0)
            except httpx.HTTPError:
                pass
            else:
                if response.status_code == 200:
                    return self
            time.sleep(0.2)
        raise AssertionError(
            f"worker {self.name} 就绪超时：\n{self.log_text()}")

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False)
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self._log is not None:
            self._log.flush()
            self._log.close()
            self._log = None

    def log_text(self) -> str:
        if self.log_path.exists():
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        return ""

    def env_record(self, run_id: str) -> dict | None:
        path = self.env_dir / f"{run_id}.jsonl"
        if not path.exists():
            return None
        lines = [line for line in path.read_text(
            encoding="utf-8").splitlines() if line.strip()]
        return json.loads(lines[0]) if lines else None

    def fixture_records(self, run_id: str) -> list[dict]:
        path = self.fixture_dir / f"{run_id}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(
            encoding="utf-8").splitlines() if line.strip()]

    def prompt_count(self, run_id: str) -> int:
        return sum(1 for record in self.fixture_records(run_id)
                   if record.get("method") == "session/prompt")

    def total_prompts(self) -> int:
        return sum(
            sum(1 for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
                and json.loads(line).get("method") == "session/prompt")
            for path in self.fixture_dir.glob("*.jsonl"))


def worker_state(worker: Worker, run_id: str) -> str:
    response = httpx.get(
        worker.base + f"/internal/runs/{run_id}/events?after=0",
        headers=worker.internal_headers, trust_env=False, timeout=10.0)
    assert response.status_code == 200, response.text
    return response.json()["state"]


# ---------------------------------------------------------------------------
# 死代理桩 worker：只证明 relay 不走环境代理
# ---------------------------------------------------------------------------


class _StubHandler(BaseHTTPRequestHandler):
    server_version = "EaselStubWorker"

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        self.server.calls.append({
            "path": self.path,
            "headers": {key.lower(): value
                        for key, value in self.headers.items()},
        })
        body = json.dumps({"state": "accepted"}).encode("utf-8")
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 保持测试输出安静
        pass


class StubWorker:
    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self.server.calls = []
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)

    @property
    def calls(self) -> list[dict]:
        return self.server.calls

    def start(self) -> "StubWorker":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def install_control_log(path: Path):
    root = logging.getLogger()
    previous_level = root.level
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    return root, handler, previous_level


def restore_control_log(state) -> None:
    root, handler, previous_level = state
    root.removeHandler(handler)
    handler.flush()
    handler.close()
    root.setLevel(previous_level)


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


def test_control_relay_does_not_use_environment_proxies(tmp_path, monkeypatch):
    """宿主环境里的代理不得改写 control 到本人 worker 的内部请求。"""
    db_path = tmp_path / "control.sqlite3"
    identities, invites = seed_control(db_path)
    member = identities["A"]
    stub = StubWorker().start()
    install_dead_proxy(monkeypatch)
    key = Ed25519PrivateKey.generate()
    runtime = StaticRuntime({
        member.workspace_id: WorkerHandle(
            member.workspace_id, "container-stub", stub.base,
            "service-stub", REVISION),
    })
    commands = {member.workspace_id: ["stub"]}
    host = ControlHost(
        db_path, key, runtime,
        make_provider({member.workspace_id: TokenMint()}, commands)).start()
    client = make_client(host)
    public_texts: list[str] = []
    try:
        auth = login(client, invites["a"], public_texts)
        session_id = create_session(client, auth, public_texts, "代理隔离")
        response = submit(
            client, auth, public_texts, session_id, "turn-proxy", "你好")
        assert response.status_code == 202, response.text
        assert response.json()["duplicate"] is False
        assert len(stub.calls) == 1
        assert stub.calls[0]["path"].startswith("/internal/runs/")
        assert stub.calls[0]["headers"]["x-easel-service-token"] == (
            "service-stub")
    finally:
        client.close()
        host.stop()
        stub.stop()


def test_two_worker_authorized_relay_end_to_end(tmp_path, monkeypatch):
    """A/B 两个真实 worker + 真实 control 的完整授权、回放与撤销链路。"""
    db_path = tmp_path / "control.sqlite3"
    identities, invites = seed_control(db_path)
    ws_a = identities["A"].workspace_id
    ws_b = identities["B"].workspace_id
    ws_c = identities["C"].workspace_id
    service_a = f"service-a-{uuid.uuid4().hex}"
    service_b = f"service-b-{uuid.uuid4().hex}"
    service_c = f"service-c-{uuid.uuid4().hex}"

    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    private_marker = private_pem.decode("ascii").strip().splitlines()[1]
    assert len(private_marker) >= 40
    public_key_path = tmp_path / "worker-public.pem"
    public_key_path.write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo))
    launcher_path = tmp_path / "run-fixture.py"
    launcher_path.write_text(LAUNCHER_SOURCE, encoding="utf-8")

    public_texts: list[str] = []
    clients: list[httpx.Client] = []
    hosts: list[ControlHost] = []
    workers: list[Worker] = []
    log_state = None
    closed_c_port = free_port()

    def make_worker(name: str, workspace_id: str, mode: str, native: str,
                    token: str) -> Worker:
        worker = Worker(
            tmp_path, name, workspace_id, mode, native, token,
            public_key_path, launcher_path)
        workers.append(worker)
        return worker.start()

    try:
        worker_a = make_worker("a", ws_a, "permission", "a", service_a)
        worker_b = make_worker("b", ws_b, "hang", "b", service_b)

        commands = {
            ws_a: worker_a.command,
            ws_b: worker_b.command,
            ws_c: ["stub-c"],
        }
        mints = {workspace: TokenMint()
                 for workspace in (ws_a, ws_b, ws_c)}
        runtime = StaticRuntime({
            ws_a: WorkerHandle(ws_a, "container-a", worker_a.base,
                               service_a, REVISION),
            ws_b: WorkerHandle(ws_b, "container-b", worker_b.base,
                               service_b, REVISION),
            # C 的 worker 不存在：本机回环的已关闭端口，必然 502。
            ws_c: WorkerHandle(
                ws_c, "container-c", f"http://127.0.0.1:{closed_c_port}",
                service_c, REVISION),
        })
        provider = make_provider(mints, commands)

        install_dead_proxy(monkeypatch)
        log_state = install_control_log(tmp_path / "control.log")
        host1 = ControlHost(db_path, key, runtime, provider).start()
        hosts.append(host1)
        client1 = make_client(host1)
        client1_write = make_client(host1)
        clients.extend([client1, client1_write])

        # ------------------------------------------------------------------
        # A：登录 → 接纳 → SSE → 权限问题 → 断线重连 → 回答 → 完成 → 消息
        # ------------------------------------------------------------------
        auth_a = login(client1, invites["a"], public_texts)
        session_a = create_session(client1, auth_a, public_texts, "A 会话")
        accepted = submit(
            client1, auth_a, public_texts, session_a, "turn-a-1", "你好")
        assert accepted.status_code == 202, accepted.text
        accepted_body = accepted.json()
        assert accepted_body["duplicate"] is False
        run_a = accepted_body["runId"]

        frames = collect_stream(
            client1, f"/api/chat/jobs/{run_a}/stream", auth_a, public_texts,
            stop=lambda frame: frame.get("event") == "question")
        questions = [frame for frame in frames
                     if frame.get("event") == "question"]
        assert len(questions) == 1
        payload = question_payload(questions[0])
        question_id = payload["id"]
        assert payload["questions"][0]["questionId"] == "permission"
        assert [option["label"] for option
                in payload["questions"][0]["options"]] == [
                    "1. Allow", "2. Reject"]
        question_event_id = int(questions[0]["id"])
        assert worker_state(worker_a, run_a) == "awaiting_permission"
        assert worker_a.prompt_count(run_a) == 1

        answered = {"value": False}

        def answer_on_replay(frame):
            if frame.get("event") != "question":
                return
            assert not answered["value"], "重连回放了重复问题"
            answered["value"] = True
            response = call(
                client1_write, public_texts, "POST",
                "/api/chat/question/answer", headers=write_headers(auth_a),
                json_body={"runId": run_a, "questionId": question_id,
                           "answers": {"permission": ["1. Allow"]}})
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "resolved"

        replay = collect_stream(
            client1, f"/api/chat/jobs/{run_a}/stream", auth_a, public_texts,
            headers={"Last-Event-ID": str(question_event_id - 1)},
            on_frame=answer_on_replay)
        assert answered["value"]
        assert any(frame.get("event") == "question" for frame in replay)
        assert state_of(replay[-1]) == "done"
        assert worker_a.prompt_count(run_a) == 1

        status = call(
            client1, public_texts, "POST", "/api/chat/question/status",
            headers=write_headers(auth_a),
            json_body={"runId": run_a, "questionIds": [question_id]})
        assert status.status_code == 200, status.text
        assert status.json()["questions"][question_id]["status"] == "resolved"

        messages = call(
            client1, public_texts, "GET",
            f"/api/sessions/{session_a}/messages",
            headers=read_headers(auth_a))
        assert messages.status_code == 200, messages.text
        assert messages.headers["cache-control"] == "no-store"
        assert [item["role"] for item in
                messages.json()["messages"]] == ["user", "assistant"]
        assert [item["content"] for item in
                messages.json()["messages"]] == ["你好", "你好"]
        page = call(
            client1, public_texts, "GET",
            f"/api/sessions/{session_a}/messages?limit=1&offset=1",
            headers=read_headers(auth_a))
        assert [item["content"] for item in
                page.json()["messages"]] == ["你好"]
        assert page.json()["messages"][0]["role"] == "assistant"
        beyond = call(
            client1, public_texts, "GET",
            f"/api/sessions/{session_a}/messages?limit=5&offset=10",
            headers=read_headers(auth_a))
        assert beyond.json()["messages"] == []
        assert worker_a.prompt_count(run_a) == 1

        # ------------------------------------------------------------------
        # control 重启：同一请求只认原 Run，不重新派发/执行
        # ------------------------------------------------------------------
        client1.close()
        client1_write.close()
        host1.stop()
        hosts.remove(host1)

        host2 = ControlHost(db_path, key, runtime, provider).start()
        hosts.append(host2)
        client2 = make_client(host2)
        clients.append(client2)
        auth_a2 = Auth(cookie=auth_a.cookie, csrf=auth_a.csrf)

        duplicate = submit(
            client2, auth_a2, public_texts, session_a, "turn-a-1", "你好")
        assert duplicate.status_code == 202, duplicate.text
        assert duplicate.json() == {"runId": run_a, "state": "done",
                                    "duplicate": True}
        assert worker_a.prompt_count(run_a) == 1
        conflict = submit(
            client2, auth_a2, public_texts, session_a, "turn-a-1", "不同输入")
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["code"] == "request_conflict"
        assert worker_a.prompt_count(run_a) == 1

        # ------------------------------------------------------------------
        # B：两个会话并发、第三会话 429、同会话第二个 409
        # ------------------------------------------------------------------
        auth_b1 = login(client2, invites["b1"], public_texts)
        session_b1 = create_session(client2, auth_b1, public_texts, "B 会话一")
        first_b = submit(
            client2, auth_b1, public_texts, session_b1, "turn-b1-1", "第一")
        assert first_b.status_code == 202, first_b.text
        run_b1 = first_b.json()["runId"]
        session_b2 = create_session(client2, auth_b1, public_texts, "B 会话二")
        second_b = submit(
            client2, auth_b1, public_texts, session_b2, "turn-b2-1", "并行")
        assert second_b.status_code == 202, second_b.text
        run_b2 = second_b.json()["runId"]
        wait_until(lambda: worker_state(worker_b, run_b1) == "running")
        wait_until(lambda: worker_state(worker_b, run_b2) == "running")
        assert active_runs(db_path, ws_b) == 2

        session_b3 = create_session(client2, auth_b1, public_texts, "B 会话三")
        capacity = submit(
            client2, auth_b1, public_texts, session_b3, "turn-b3-1", "第三")
        assert capacity.status_code == 429, capacity.text
        assert capacity.json()["code"] == "workspace_capacity"
        busy = submit(
            client2, auth_b1, public_texts, session_b1, "turn-b1-2", "并发")
        assert busy.status_code == 409, busy.text
        assert busy.json()["code"] == "conversation_busy"

        # ------------------------------------------------------------------
        # 跨租户：A 对 B 的 run/question/session 一律在转发前 404
        # ------------------------------------------------------------------
        b_calls_before = runtime.count(ws_b)
        attacks_a_on_b = [
            ("POST", "/api/chat/stop", {"runId": run_b1},
             write_headers(auth_a2)),
            ("GET", f"/api/chat/jobs/{run_b1}/stream", None,
             read_headers(auth_a2)),
            ("POST", "/api/chat/question/answer",
             {"runId": run_b1, "questionId": "permission",
              "answers": {"permission": ["1. Allow"]}},
             write_headers(auth_a2)),
            ("POST", "/api/chat/question/status",
             {"runId": run_b1, "questionIds": ["permission"]},
             write_headers(auth_a2)),
            ("GET", f"/api/sessions/{session_b1}/messages", None,
             read_headers(auth_a2)),
            ("POST", "/api/chat",
             {"sessionId": session_b1, "turnId": "attack-a", "message": "越权"},
             write_headers(auth_a2)),
        ]
        for method, url, body, headers in attacks_a_on_b:
            response = call(client2, public_texts, method, url,
                            headers=headers, json_body=body)
            assert response.status_code == 404, (url, response.text)
            assert response.json()["code"] == "not_found", (url, response.text)
        assert runtime.count(ws_b) == b_calls_before
        assert worker_b.prompt_count(run_b1) == 1
        assert worker_b.prompt_count(run_b2) == 1

        # B 同样不能探测 A 的已完成 Run/会话
        a_calls_before = runtime.count(ws_a)
        auth_b1_headers = write_headers(auth_b1)
        attacks_b_on_a = [
            ("POST", "/api/chat/stop", {"runId": run_a}, auth_b1_headers),
            ("GET", f"/api/chat/jobs/{run_a}/stream", None,
             read_headers(auth_b1)),
            ("GET", f"/api/sessions/{session_a}/messages", None,
             read_headers(auth_b1)),
        ]
        for method, url, body, headers in attacks_b_on_a:
            response = call(client2, public_texts, method, url,
                            headers=headers, json_body=body)
            assert response.status_code == 404, (url, response.text)
            assert response.json()["code"] == "not_found", (url, response.text)
        assert runtime.count(ws_a) == a_calls_before
        assert worker_a.prompt_count(run_a) == 1

        # ------------------------------------------------------------------
        # 停止 b1 后容量释放一次，同会话可再次接纳
        # ------------------------------------------------------------------
        stopped = call(client2, public_texts, "POST", "/api/chat/stop",
                       headers=auth_b1_headers, json_body={"runId": run_b1})
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["state"] == "cancelled"
        assert active_runs(db_path, ws_b) == 1

        run_b1b = submit(
            client2, auth_b1, public_texts, session_b1, "turn-b1-3", "第二")
        assert run_b1b.status_code == 202, run_b1b.text
        run_b1b_id = run_b1b.json()["runId"]
        wait_until(
            lambda: worker_state(worker_b, run_b1b_id) == "running")

        # 未接纳过任务的会话：空快照，不启动 worker
        session_empty = create_session(
            client2, auth_b1, public_texts, "B 空会话")
        b_calls_before_empty = runtime.count(ws_b)
        empty = call(client2, public_texts, "GET",
                     f"/api/sessions/{session_empty}/messages",
                     headers=read_headers(auth_b1))
        assert empty.status_code == 200, empty.text
        assert empty.json() == {"messages": [], "total": 0}
        assert runtime.count(ws_b) == b_calls_before_empty

        # ------------------------------------------------------------------
        # 登录撤销：静默 SSE 必须在 5 秒内关闭，后台任务继续到终态
        # ------------------------------------------------------------------
        logout_client = make_client(host2)
        clients.append(logout_client)
        revoked = {"running": False, "logout_at": 0.0}

        def logout_on_running(frame):
            if state_of(frame) != "running" or revoked["running"]:
                return
            revoked["running"] = True
            response = call(
                logout_client, public_texts, "POST", "/api/auth/logout",
                headers=write_headers(auth_b1))
            assert response.status_code == 204, response.text
            revoked["logout_at"] = time.monotonic()

        stream_frames = collect_stream(
            client2, f"/api/chat/jobs/{run_b1b_id}/stream", auth_b1,
            public_texts, on_frame=logout_on_running)
        elapsed = time.monotonic() - revoked["logout_at"]
        assert revoked["running"]
        assert elapsed <= 5.0, f"撤销后 SSE 关闭耗时 {elapsed:.2f}s"
        assert not any(state_of(frame) in TERMINAL
                       for frame in stream_frames)
        assert worker_state(worker_b, run_b1b_id) == "running"
        assert worker_b.prompt_count(run_b1b_id) == 1
        assert call(client2, public_texts, "GET", "/api/auth/me",
                    headers=read_headers(auth_b1)).status_code == 401

        # 重新登录（同用户第二个邀请）后只回放，不重新 prompt
        client_b2 = make_client(host2)
        clients.append(client_b2)
        auth_b2 = login(client_b2, invites["b2"], public_texts)
        assert auth_b2.cookie != auth_b1.cookie
        replayed = collect_stream(
            client_b2, f"/api/chat/jobs/{run_b1b_id}/stream", auth_b2,
            public_texts,
            stop=lambda frame: state_of(frame) == "running")
        assert any(state_of(frame) == "running" for frame in replayed)
        assert worker_b.prompt_count(run_b1b_id) == 1

        stopped_b1b = call(
            client_b2, public_texts, "POST", "/api/chat/stop",
            headers=write_headers(auth_b2), json_body={"runId": run_b1b_id})
        assert stopped_b1b.status_code == 200, stopped_b1b.text
        assert stopped_b1b.json()["state"] == "cancelled"
        final_b1b = collect_stream(
            client_b2, f"/api/chat/jobs/{run_b1b_id}/stream?after=0",
            auth_b2, public_texts)
        assert state_of(final_b1b[-1]) == "cancelled"

        stopped_b2 = call(
            client_b2, public_texts, "POST", "/api/chat/stop",
            headers=write_headers(auth_b2), json_body={"runId": run_b2})
        assert stopped_b2.status_code == 200, stopped_b2.text
        assert stopped_b2.json()["state"] == "cancelled"
        wait_until(lambda: active_runs(db_path, ws_b) == 0)
        assert active_runs(db_path, ws_b) == 0

        b1_messages = call(
            client_b2, public_texts, "GET",
            f"/api/sessions/{session_b1}/messages",
            headers=read_headers(auth_b2))
        assert b1_messages.status_code == 200, b1_messages.text
        assert [item["role"] for item in
                b1_messages.json()["messages"]] == ["user", "user"]
        assert [item["content"] for item in
                b1_messages.json()["messages"]] == ["第一", "第二"]
        b2_messages = call(
            client_b2, public_texts, "GET",
            f"/api/sessions/{session_b2}/messages",
            headers=read_headers(auth_b2))
        assert [item["content"] for item in
                b2_messages.json()["messages"]] == ["并行"]

        # ------------------------------------------------------------------
        # C：worker 不可达 → 502；重试只读到原 Run，不自动重发
        # ------------------------------------------------------------------
        client_c = make_client(host2)
        clients.append(client_c)
        auth_c = login(client_c, invites["c"], public_texts)
        session_c = create_session(client_c, auth_c, public_texts, "C 会话")
        failed = submit(
            client_c, auth_c, public_texts, session_c, "turn-c-1", "首次")
        assert failed.status_code == 502, failed.text
        assert failed.json()["code"] == "upstream_unavailable"
        run_c = latest_run(db_path, ws_c)
        row = run_row(db_path, run_c)
        assert row["dispatch_state"] == "dispatching"
        assert row["status"] == "reserved"

        retried = submit(
            client_c, auth_c, public_texts, session_c, "turn-c-1", "首次")
        assert retried.status_code == 202, retried.text
        assert retried.json() == {"runId": run_c, "state": "reserved",
                                  "duplicate": True}
        assert run_row(db_path, run_c)["dispatch_state"] == "dispatching"
        changed = submit(
            client_c, auth_c, public_texts, session_c, "turn-c-1", "换了输入")
        assert changed.status_code == 409, changed.text
        assert changed.json()["code"] == "request_conflict"
        assert run_row(db_path, run_c)["dispatch_state"] == "dispatching"
        with closing(connect_db(db_path)) as connection:
            total_c = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE workspace_id = ?",
                (ws_c,)).fetchone()[0]
        assert total_c == 1

        # ------------------------------------------------------------------
        # policy 默认拒绝、worker 旧入口不绕过 Run 账本
        # ------------------------------------------------------------------
        health = httpx.get(
            host2.base + "/api/health", trust_env=False, timeout=10.0)
        assert health.status_code == 200

        unknown = call(client_b2, public_texts, "GET", "/api/not-registered",
                       headers=read_headers(auth_b2))
        assert unknown.status_code == 403
        assert unknown.json()["code"] == "forbidden"

        admin_denied = call(
            client_b2, public_texts, "POST", "/api/admin/users",
            headers=write_headers(auth_b2), json_body={"displayName": "X"})
        assert admin_denied.status_code == 403
        assert admin_denied.json()["code"] == "forbidden"

        internal_denied = call(
            client_b2, public_texts, "GET", "/internal/runs/x/events",
            headers=read_headers(auth_b2))
        assert internal_denied.status_code == 403
        assert internal_denied.json()["code"] == "forbidden"

        no_headers = httpx.get(
            worker_a.base + "/api/personas", trust_env=False, timeout=10.0)
        assert no_headers.status_code == 401
        wrong_headers = httpx.get(
            worker_a.base + "/api/admin/users",
            headers=worker_a.internal_headers, trust_env=False, timeout=10.0)
        assert wrong_headers.status_code == 403
        unknown_worker = httpx.get(
            worker_a.base + "/api/not-registered",
            headers=worker_a.internal_headers, trust_env=False, timeout=10.0)
        assert unknown_worker.status_code == 403

        probe_run = uuid.uuid4().hex
        invalid_submit = httpx.post(
            worker_a.base + f"/internal/runs/{probe_run}",
            headers=worker_a.internal_headers, json={},
            trust_env=False, timeout=10.0)
        assert invalid_submit.status_code == 400, invalid_submit.text
        missing = httpx.get(
            worker_a.base + f"/internal/runs/{probe_run}/events",
            headers=worker_a.internal_headers, trust_env=False, timeout=10.0)
        assert missing.status_code == 404

        prompts_before_legacy = worker_a.total_prompts()
        legacy = httpx.post(
            worker_a.base + "/api/chat",
            headers=worker_a.internal_headers,
            json={"sessionId": "probe-session", "message": "旧入口不得执行"},
            trust_env=False, timeout=10.0)
        assert legacy.status_code == 409, legacy.text
        assert worker_a.total_prompts() == prompts_before_legacy

        # ------------------------------------------------------------------
        # 逐 Run 签名包：内容绑定 + 原始 broker token 只进入本人 worker
        # ------------------------------------------------------------------
        for client in clients:
            client.close()
        clients.clear()
        for host in list(hosts):
            host.stop()
            hosts.remove(host)
        restore_control_log(log_state)
        log_state = None
        for worker in workers:
            worker.stop()
        assert not any(host.thread is not None for host in hosts)

        expected_env_tokens = {
            run_a: mints[ws_a].tokens[0],
            run_b1: mints[ws_b].tokens[0],
            run_b2: mints[ws_b].tokens[1],
            run_b1b_id: mints[ws_b].tokens[4],
        }
        env_records: dict[str, dict] = {}
        for worker in workers:
            for path in worker.env_dir.glob("*.jsonl"):
                record = json.loads(path.read_text(encoding="utf-8"))
                env_records[record["run_id"]] = record
        assert set(env_records) == set(expected_env_tokens)
        for run_id, token in expected_env_tokens.items():
            record = env_records[run_id]
            assert record["broker_token"] == token
            assert record["workspace_id"] in (ws_a, ws_b)
            assert record["service_token"] is None
            assert record["admin_key"] is None
            assert record["public_key_file"] is None
            assert record["service_mode"] == "worker"
            assert record["agent_runtime"] == "acp"
            expected_root = (worker_a if record["workspace_id"] == ws_a
                             else worker_b)
            assert record["home"] == str(expected_root.paths["home"])
            assert record["userprofile"] == str(expected_root.paths["home"])
            assert record["cwd"] == str(expected_root.paths["workspace"])
            assert record["code_root"] == str(PROJECT_ROOT)
        assert run_c not in env_records, "502 的 Run 不得启动 worker 执行"

        assert worker_a.prompt_count(run_a) == 1
        assert worker_b.prompt_count(run_b1) == 1
        assert worker_b.prompt_count(run_b2) == 1
        assert worker_b.prompt_count(run_b1b_id) == 1

        store = Store.connect(db_path)
        try:
            raw = store.get_run_snapshot(identities["A"], run_a)
            assert raw is not None
            envelope = json.loads(raw)
            snapshot = verify_run_snapshot(
                envelope, key.public_key(), expected_workspace_id=ws_a,
                expected_run_id=run_a, expected_conversation_id=session_a,
                expected_input_digest=run_input_digest(
                    conversation_id=session_a, task_type="chat",
                    message="你好", attachments=[], persona=None),
                expected_task_type="chat", expected_revision=REVISION)
            # 同请求重试（含 control 重启）复用原签名包：不再铸造第二个 token
            assert len(mints[ws_a].tokens) == 1
            assert snapshot.routes["broker_token"] == mints[ws_a].tokens[0]
            tampered = dict(envelope, conversation_id=session_b1)
            with pytest.raises(DomainError):
                verify_run_snapshot(
                    tampered, key.public_key(), expected_workspace_id=ws_a,
                    expected_run_id=run_a,
                    expected_conversation_id=session_a,
                    expected_input_digest=envelope["input_digest"],
                    expected_task_type="chat", expected_revision=REVISION)
            raw_c = store.get_run_snapshot(identities["C"], run_c)
            assert raw_c is not None
            envelope_c = json.loads(raw_c)
            assert envelope_c["routes"]["broker_token"] == (
                mints[ws_c].tokens[0])
        finally:
            store.close()

        # 原生绑定只落在各自私有 state root
        binding_a = WorkspaceSessionStore(
            worker_a.paths["state"], ws_a).binding(session_a)
        assert binding_a["native_id"] == "a-session"
        binding_b = WorkspaceSessionStore(
            worker_b.paths["state"], ws_b).binding(session_b1)
        assert binding_b["native_id"] == "b-session"
        assert WorkspaceSessionStore(
            worker_b.paths["state"], ws_b).binding(session_a) == {}
        for first in workers:
            for second in workers:
                if first is second:
                    continue
                assert not first.paths["state"].is_relative_to(
                    second.paths["state"])
                assert not first.paths["home"].is_relative_to(
                    second.paths["home"])

        # ------------------------------------------------------------------
        # 秘密泄漏扫描：公开响应、SSE、日志、夹具审计、消息快照
        # ------------------------------------------------------------------
        corpus_parts = list(public_texts)
        corpus_parts.append(
            (tmp_path / "control.log").read_text(
                encoding="utf-8", errors="replace"))
        for worker in workers:
            corpus_parts.append(worker.log_text())
            for path in worker.fixture_dir.glob("*.jsonl"):
                corpus_parts.append(
                    path.read_text(encoding="utf-8", errors="replace"))
        corpus = "\n".join(corpus_parts)

        minted = [token for mint in mints.values() for token in mint.tokens]
        secrets_to_hide = [
            service_a, service_b, service_c, ADMIN_SENTINEL,
            private_marker, *minted,
        ]
        for secret in secrets_to_hide:
            assert secret not in corpus, f"秘密泄漏到公开/日志语料：{secret}"

        # 真实 TCP 证据：worker 访问日志必须出现内部任务与快照请求
        assert "/internal/runs/" in worker_a.log_text()
        assert "/internal/sessions/" in worker_a.log_text()
    finally:
        for client in clients:
            try:
                client.close()
            except Exception:
                pass
        for host in list(hosts):
            host.stop()
            hosts.remove(host)
        for worker in workers:
            worker.stop()
        if log_state is not None:
            restore_control_log(log_state)
