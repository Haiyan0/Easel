"""Task 14：本机双用户端到端验收。

本文件只证明 **本机开发证据**：真实 control app（uvicorn 回环）、两个独立
worker 子进程、真实 worker HTTP/SSE 与临时假 ACP Harness。它不证明目标
Linux 部署、真实 DSH ACP、真实供应商、Docker 资源限制或正式发布通过。

关键边界：

* A/B 使用各自一次性邀请登录，私有会话、消息、原生绑定和 outputs 相互隔离；
* 静态/工作区产物通过 worker 的真实 HTTP 路由读取；control 尚未装配的
  workspace-content 路由仍明确 503，本测试不会把它伪装成已交付；
* 备份恢复只使用临时目录与本机 SQLite/文件，不读取开发者 HOME、平台凭证
  或真实浏览器 profile。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from easel.control.cli import create_backup, restore_backup
from easel.control.store import Store
from easel.control.types import DomainError, WorkerHandle
from easel.worker.sessions import WorkspaceSessionStore

# 复用 Task 10 已在真实 TCP 下验证的 control/worker 夹具，避免复制两套
# 启动逻辑；本文件只改变 Harness mode 并扩展双用户/恢复断言。
from tests.multiuser.test_tcp_integration import (
    LAUNCHER_SOURCE,
    REVISION,
    ControlHost,
    StaticRuntime,
    TokenMint,
    Worker,
    collect_stream,
    create_session,
    login,
    make_client,
    make_provider,
    read_headers,
    seed_control,
    state_of,
    submit,
    write_headers,
)

TERMINAL = frozenset({"done", "failed", "interrupted", "cancelled"})


def _tokens(frames: list[dict]) -> str:
    """拼接 SSE token 事件；夹具多用户模式的回复带 workspace 标识。"""
    parts: list[str] = []
    for frame in frames:
        if frame.get("event") != "token" or not frame.get("data"):
            continue
        value = json.loads(frame["data"])
        if isinstance(value, str):
            parts.append(value)
    return "".join(parts)


def _wait_terminal(frames: list[dict]) -> str:
    state = state_of(frames[-1]) if frames else None
    assert state in TERMINAL, f"SSE 未到达终态：{state!r}"
    return state


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest_tree(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = _sha256(path)
    return result


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    """启动真实 control + A/B 两个独立 worker，并使用临时假 ACP。"""
    root = tmp_path_factory.mktemp("task14-two-users")
    managed = root / "managed"
    control_dir = managed / "control"
    control_dir.mkdir(parents=True)
    db_path = control_dir / "control.sqlite3"
    identities, invites = seed_control(db_path)
    ws_a = identities["A"].workspace_id
    ws_b = identities["B"].workspace_id

    key = Ed25519PrivateKey.generate()
    public_key_path = root / "worker-public.pem"
    public_key_path.write_bytes(key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo))
    launcher_path = root / "run-fixture.py"
    launcher_path.write_text(LAUNCHER_SOURCE, encoding="utf-8")

    service_a = f"service-a-{uuid.uuid4().hex}"
    service_b = f"service-b-{uuid.uuid4().hex}"
    worker_a = Worker(root, "a", ws_a, "multiuser", "a", service_a,
                      public_key_path, launcher_path)
    worker_b = Worker(root, "b", ws_b, "multiuser", "b", service_b,
                      public_key_path, launcher_path)
    workers: list[Worker] = []
    clients: list[httpx.Client] = []
    host = None
    try:
        workers.extend([worker_a.start(), worker_b.start()])
        mints = {ws_a: TokenMint(), ws_b: TokenMint()}
        runtime = StaticRuntime({
            ws_a: WorkerHandle(ws_a, "container-a", worker_a.base, service_a,
                               REVISION),
            ws_b: WorkerHandle(ws_b, "container-b", worker_b.base, service_b,
                               REVISION),
        })
        host = ControlHost(
            db_path, key, runtime,
            make_provider(mints, {ws_a: worker_a.command,
                                  ws_b: worker_b.command})).start()
        clients.extend([make_client(host), make_client(host)])
        public_texts: list[str] = []
        auth_a = login(clients[0], invites["a"], public_texts)
        auth_b = login(clients[1], invites["b1"], public_texts)
        namespace = SimpleNamespace(
            root=root, managed=managed, control_dir=control_dir,
            db_path=db_path, identities=identities, invites=invites,
            ws_a=ws_a, ws_b=ws_b, worker_a=worker_a, worker_b=worker_b,
            workers=workers, host=host, runtime=runtime, mints=mints,
            client_a=clients[0], client_b=clients[1],
            auth_a=auth_a, auth_b=auth_b, public_texts=public_texts,
            service_a=service_a, service_b=service_b,
        )
        yield namespace
    finally:
        for client in clients:
            client.close()
        if host is not None:
            host.stop()
        for worker in workers:
            worker.stop()


def _prompt(cluster, client, auth, session_id: str, turn_id: str,
            message: str) -> tuple[str, list[dict]]:
    public_texts = cluster.public_texts
    accepted = submit(client, auth, public_texts, session_id, turn_id, message)
    assert accepted.status_code == 202, accepted.text
    run_id = accepted.json()["runId"]
    frames = collect_stream(
        client, f"/api/chat/jobs/{run_id}/stream", auth, public_texts,
        stop=lambda frame: state_of(frame) in TERMINAL)
    assert _wait_terminal(frames) == "done"
    return run_id, frames


def test_two_users_keep_sessions_messages_and_native_bindings_private(cluster):
    session_a = create_session(
        cluster.client_a, cluster.auth_a, cluster.public_texts, "同名会话")
    session_b = create_session(
        cluster.client_b, cluster.auth_b, cluster.public_texts, "同名会话")
    assert session_a != session_b

    run_a, frames_a = _prompt(
        cluster, cluster.client_a, cluster.auth_a, session_a,
        "turn-a-1", "A 的私密内容")
    run_b, frames_b = _prompt(
        cluster, cluster.client_b, cluster.auth_b, session_b,
        "turn-b-1", "B 的私密内容")

    # 假 Harness 在多人模式下返回固定 native ID；控制面与 state_root 必须
    # 各自按 workspace 隔离，不能因为 native ID 相同而串消息。
    assert f"{cluster.ws_a}:multiuser-session" in _tokens(frames_a)
    assert f"{cluster.ws_b}:multiuser-session" in _tokens(frames_b)
    assert cluster.ws_b not in _tokens(frames_a)
    assert cluster.ws_a not in _tokens(frames_b)

    messages_a = cluster.client_a.get(
        f"/api/sessions/{session_a}/messages",
        headers=read_headers(cluster.auth_a))
    assert messages_a.status_code == 200, messages_a.text
    assert [item["role"] for item in messages_a.json()["messages"]] == [
        "user", "assistant"]
    assert [item["content"] for item in messages_a.json()["messages"]] == [
        "A 的私密内容", f"{cluster.ws_a}:multiuser-session"]

    messages_b = cluster.client_b.get(
        f"/api/sessions/{session_b}/messages",
        headers=read_headers(cluster.auth_b))
    assert messages_b.status_code == 200, messages_b.text
    assert [item["role"] for item in messages_b.json()["messages"]] == [
        "user", "assistant"]
    assert [item["content"] for item in messages_b.json()["messages"]] == [
        "B 的私密内容", f"{cluster.ws_b}:multiuser-session"]

    store_a = WorkspaceSessionStore(cluster.worker_a.paths["state"],
                                    cluster.ws_a)
    store_b = WorkspaceSessionStore(cluster.worker_b.paths["state"],
                                    cluster.ws_b)
    binding_a = store_a.binding(session_a)
    binding_b = store_b.binding(session_b)
    assert binding_a["native_id"] == "multiuser-session"
    assert binding_b["native_id"] == "multiuser-session"
    assert binding_a["workspace_id"] == cluster.ws_a
    assert binding_b["workspace_id"] == cluster.ws_b
    assert binding_a["cwd"] == str(cluster.worker_a.paths["workspace"])
    assert binding_b["cwd"] == str(cluster.worker_b.paths["workspace"])
    assert store_a.binding(session_b) == {}
    assert store_b.binding(session_a) == {}

    records_a = cluster.worker_a.fixture_records(run_a)
    records_b = cluster.worker_b.fixture_records(run_b)
    runtime_a = [record for record in records_a
                 if record.get("method") == "fixture/runtime"]
    runtime_b = [record for record in records_b
                 if record.get("method") == "fixture/runtime"]
    assert len(runtime_a) == 1
    assert len(runtime_b) == 1
    assert runtime_a[0]["cwd"] == str(cluster.worker_a.paths["workspace"])
    assert runtime_b[0]["cwd"] == str(cluster.worker_b.paths["workspace"])
    assert runtime_a[0]["home"] == str(cluster.worker_a.paths["home"])
    assert runtime_b[0]["home"] == str(cluster.worker_b.paths["home"])
    assert runtime_a[0]["workspaceId"] == cluster.ws_a
    assert runtime_b[0]["workspaceId"] == cluster.ws_b
    assert "PATH" not in runtime_a[0]
    assert "env" not in runtime_a[0]
    assert "PATH" not in runtime_b[0]
    assert "env" not in runtime_b[0]

    # 同一会话的下一轮必须续接同一 native ID；新连接走 session/load。
    second_run, second_frames = _prompt(
        cluster, cluster.client_a, cluster.auth_a, session_a,
        "turn-a-2", "A 的续接追问")
    assert f"{cluster.ws_a}:multiuser-session" in _tokens(second_frames)
    assert store_a.binding(session_a)["native_id"] == "multiuser-session"
    second_records = cluster.worker_a.fixture_records(second_run)
    methods = [record.get("method") for record in second_records]
    assert "session/load" in methods or "session/resume" in methods
    history = cluster.client_a.get(
        f"/api/sessions/{session_a}/messages",
        headers=read_headers(cluster.auth_a)).json()["messages"]
    assert [item["content"] for item in history] == [
        "A 的私密内容", f"{cluster.ws_a}:multiuser-session",
        "A 的续接追问", f"{cluster.ws_a}:multiuser-session",
    ]
    assert cluster.worker_a.prompt_count(run_a) == 1
    assert cluster.worker_a.prompt_count(second_run) == 1
    assert cluster.worker_b.prompt_count(run_b) == 1

    corpus = "\n".join(cluster.public_texts)
    for secret in (cluster.service_a, cluster.service_b):
        assert secret not in corpus


def test_cross_tenant_requests_are_404_before_worker_forwarding(cluster):
    session_a = create_session(
        cluster.client_a, cluster.auth_a, cluster.public_texts, "A 的边界")
    run_a, _ = _prompt(
        cluster, cluster.client_a, cluster.auth_a, session_a,
        "turn-boundary", "边界内容")

    # 伪造 workspace 头不能改变身份；B 仍是 B。
    spoofed = cluster.client_b.get(
        "/api/auth/me",
        headers={**read_headers(cluster.auth_b),
                 "X-Easel-Workspace-Id": cluster.ws_a})
    assert spoofed.status_code == 200
    assert spoofed.json()["workspaceId"] == cluster.ws_b

    b_runtime_calls = cluster.runtime.count(cluster.ws_b)
    attacks = [
        ("GET", f"/api/sessions/{session_a}/messages", None,
         read_headers(cluster.auth_b)),
        ("GET", f"/api/sessions/{session_a}/runs", None,
         read_headers(cluster.auth_b)),
        ("GET", f"/api/chat/jobs/{run_a}/stream", None,
         read_headers(cluster.auth_b)),
        ("POST", "/api/chat/stop", {"runId": run_a},
         write_headers(cluster.auth_b)),
        ("POST", "/api/chat/question/status",
         {"runId": run_a, "questionIds": ["permission"]},
         write_headers(cluster.auth_b)),
        ("POST", "/api/chat",
         {"sessionId": session_a, "turnId": "turn-attack",
          "message": "越权"}, write_headers(cluster.auth_b)),
    ]
    for method, url, body, headers in attacks:
        response = cluster.client_b.request(
            method, url, headers=headers, json=body)
        assert response.status_code == 404, (url, response.text)
        assert response.json()["code"] == "not_found", (url, response.text)

    assert cluster.runtime.count(cluster.ws_b) == b_runtime_calls
    assert cluster.worker_a.prompt_count(run_a) == 1
    own = cluster.client_a.get(
        f"/api/sessions/{session_a}/messages",
        headers=read_headers(cluster.auth_a))
    assert own.status_code == 200
    assert [item["content"] for item in own.json()["messages"]] == [
        "边界内容", f"{cluster.ws_a}:multiuser-session"]


def test_worker_outputs_are_tenant_scoped_and_control_route_stays_503(cluster):
    outputs_a = cluster.worker_a.paths["workspace"] / "outputs"
    outputs_b = cluster.worker_b.paths["workspace"] / "outputs"
    project_a = outputs_a / "shared-project"
    project_b = outputs_b / "shared-project"
    project_a.mkdir(parents=True, exist_ok=True)
    project_b.mkdir(parents=True, exist_ok=True)
    (project_a / "same-name.txt").write_text("A artifact", encoding="utf-8")
    (project_b / "same-name.txt").write_text("B artifact", encoding="utf-8")

    def worker_outputs(worker, headers):
        return httpx.get(worker.base + "/api/outputs", headers=headers,
                         trust_env=False, timeout=10.0)

    def worker_output(worker, path, headers):
        return httpx.get(worker.base + f"/api/output/{path}", headers=headers,
                         trust_env=False, timeout=10.0)

    headers_a = cluster.worker_a.internal_headers
    headers_b = cluster.worker_b.internal_headers
    list_a = worker_outputs(cluster.worker_a, headers_a)
    list_b = worker_outputs(cluster.worker_b, headers_b)
    assert list_a.status_code == 200, list_a.text
    assert list_b.status_code == 200, list_b.text
    assert "shared-project" in list_a.text
    assert "shared-project" in list_b.text

    file_a = worker_output(cluster.worker_a, "shared-project/same-name.txt",
                           headers_a)
    file_b = worker_output(cluster.worker_b, "shared-project/same-name.txt",
                           headers_b)
    assert file_a.status_code == 200, file_a.text
    assert file_b.status_code == 200, file_b.text
    assert file_a.json()["content"] == "A artifact"
    assert file_b.json()["content"] == "B artifact"

    # B 的服务 token 不能读取 A 的 worker，反之亦然。
    cross_b_on_a = worker_outputs(cluster.worker_a, headers_b)
    cross_a_on_b = worker_outputs(cluster.worker_b, headers_a)
    assert cross_b_on_a.status_code == 401
    assert cross_a_on_b.status_code == 401
    assert "A artifact" not in cross_b_on_a.text
    assert "B artifact" not in cross_a_on_b.text

    # control 的 workspace-content 路由仍未装配；只有本人 worker HTTP 可达。
    control_outputs = cluster.client_a.get(
        "/api/outputs", headers=read_headers(cluster.auth_a))
    assert control_outputs.status_code == 503
    assert control_outputs.json()["code"] == "capability_unavailable"


def test_backup_restore_keeps_two_user_ownership_and_files(cluster):
    session_a = create_session(
        cluster.client_a, cluster.auth_a, cluster.public_texts, "恢复 A")
    session_b = create_session(
        cluster.client_b, cluster.auth_b, cluster.public_texts, "恢复 B")
    _prompt(cluster, cluster.client_a, cluster.auth_a, session_a,
            "turn-restore-a", "恢复 A 内容")
    _prompt(cluster, cluster.client_b, cluster.auth_b, session_b,
            "turn-restore-b", "恢复 B 内容")

    home_a = cluster.worker_a.paths["home"] / "profile.txt"
    home_b = cluster.worker_b.paths["home"] / "profile.txt"
    home_a.write_text("A profile", encoding="utf-8")
    home_b.write_text("B profile", encoding="utf-8")
    artifact_a = (cluster.worker_a.paths["workspace"] / "outputs" /
                  "restore-project" / "a.txt")
    artifact_b = (cluster.worker_b.paths["workspace"] / "outputs" /
                  "restore-project" / "b.txt")
    artifact_a.parent.mkdir(parents=True, exist_ok=True)
    artifact_b.parent.mkdir(parents=True, exist_ok=True)
    artifact_a.write_text("restore A", encoding="utf-8")
    artifact_b.write_text("restore B", encoding="utf-8")

    data_root = cluster.managed / "workspaces"
    for workspace_id, worker in ((cluster.ws_a, cluster.worker_a),
                                 (cluster.ws_b, cluster.worker_b)):
        target = data_root / workspace_id
        shutil.copytree(worker.paths["workspace"],
                        target / "workspace")
        shutil.copytree(worker.paths["state"],
                        target / "workspace" / "state")
        shutil.copytree(worker.paths["home"], target / "home")

    key_file = cluster.managed / "master.key"
    key_file.write_bytes(b"k" * 32)
    backup = cluster.managed / "backups" / "roundtrip"
    store = Store.connect(cluster.db_path)
    try:
        create_backup(
            store, backup, control_dir=cluster.control_dir,
            master_key_file=key_file, workspace_root=data_root,
            image_digest="sha256:" + "1" * 64,
            harness_revision="task14-local")
    finally:
        store.close()

    source_before = _digest_tree(backup)
    restored = cluster.managed / "restored"
    assert restore_backup(backup, restored,
                          managed_root=cluster.managed) == restored
    assert _digest_tree(backup) == source_before
    assert (restored / "manifest.json").read_bytes() == (
        backup / "manifest.json").read_bytes()
    manifest = json.loads((backup / "manifest.json").read_text(
        encoding="utf-8"))
    assert set(manifest["workspaces"]) >= {cluster.ws_a, cluster.ws_b}
    for entry in manifest["files"]:
        path = restored.joinpath(*entry["path"].split("/"))
        assert path.is_file(), entry["path"]
        assert _sha256(path) == entry["sha256"]
        assert path.stat().st_size == entry["bytes"]

    assert (restored / "workspaces" / cluster.ws_a / "workspace" /
            "outputs" / "restore-project" / "a.txt").read_text(
                encoding="utf-8") == "restore A"
    assert (restored / "workspaces" / cluster.ws_b / "workspace" /
            "outputs" / "restore-project" / "b.txt").read_text(
                encoding="utf-8") == "restore B"
    assert (restored / "workspaces" / cluster.ws_a / "home" /
            "profile.txt").read_text(encoding="utf-8") == "A profile"
    assert (restored / "workspaces" / cluster.ws_b / "home" /
            "profile.txt").read_text(encoding="utf-8") == "B profile"

    restored_db = restored / "control" / "control.sqlite3"
    with Store.open(restored_db) as restored_store:
        conversation_a = restored_store.get_conversation(
            cluster.identities["A"], session_a)
        conversation_b = restored_store.get_conversation(
            cluster.identities["B"], session_b)
        assert conversation_a.workspace_id == cluster.ws_a
        assert conversation_b.workspace_id == cluster.ws_b
        with pytest.raises(DomainError):
            restored_store.get_conversation(
                cluster.identities["A"], session_b)
        with pytest.raises(DomainError):
            restored_store.get_conversation(
                cluster.identities["B"], session_a)

    restored_a = WorkspaceSessionStore(
        restored / "workspaces" / cluster.ws_a / "workspace" / "state",
        cluster.ws_a)
    restored_b = WorkspaceSessionStore(
        restored / "workspaces" / cluster.ws_b / "workspace" / "state",
        cluster.ws_b)
    assert [item["content"] for item in restored_a.messages(session_a)] == [
        "恢复 A 内容", f"{cluster.ws_a}:multiuser-session"]
    assert [item["content"] for item in restored_b.messages(session_b)] == [
        "恢复 B 内容", f"{cluster.ws_b}:multiuser-session"]
