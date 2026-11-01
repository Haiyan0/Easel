"""Task 5A：原工作区 status/画像/内容/媒体/附件接线。"""
from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from easel.control.app import ControlConfig, create_app
from easel.control.auth import AuthService
from easel.control.store import Store
from easel.control.types import Identity, WorkerHandle
from easel.control.workspace_api import (
    WorkspaceApi,
    canonical_output_path,
    canonical_persona_name,
)

ORIGIN = "https://easel.test"
COOKIE = "__Host-easel_session"
REVISION = 0


# ---------------------------------------------------------------------------
# 真实 worker 应用（复用 web.app 的现有 handler，只替换临时数据根）
# ---------------------------------------------------------------------------

def _write_worker_persona(root: Path, name: str, text: str) -> None:
    directory = root / "profiles" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "identity.md").write_text(text, encoding="utf-8")
    (directory / "style.md").write_text(f"风格-{text}", encoding="utf-8")


def _write_output(root: Path, relative: str, data: bytes) -> Path:
    path = root / "outputs" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class WorkerTransport(httpx.AsyncBaseTransport):
    """按上游端口选择 A/B 数据根，再交给真实 web.app ASGI 应用。"""

    def __init__(self, roots: dict[int, Path]):
        self._roots = roots
        from web import app as worker_app

        self._worker = worker_app
        self._inner = httpx.ASGITransport(app=worker_app.app)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        port = request.url.port
        root = self._roots.get(port)
        if root is None:
            return httpx.Response(503, content=b'{"detail":"unknown worker"}')
        self._worker.OUTPUTS_DIR = root / "outputs"
        self._worker.PROFILES_DIR = root / "profiles"
        from easel import persona

        persona.PROFILES_DIR = root / "profiles"
        return await self._inner.handle_async_request(request)


@dataclass
class Fixture:
    store: Store
    api: WorkspaceApi
    a: Identity
    b: Identity
    root_a: Path
    root_b: Path


@pytest.fixture
def worker_fixture(tmp_path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    for root in (root_a, root_b):
        (root / "outputs").mkdir(parents=True)
        (root / "profiles").mkdir(parents=True)
    _write_worker_persona(root_a, "同名画像", "A-定位")
    _write_worker_persona(root_b, "同名画像", "B-定位")
    _write_output(root_a, "项目/甲.txt", "A 的正文".encode("utf-8"))
    _write_output(root_b, "项目/甲.txt", "B 的正文".encode("utf-8"))
    _write_output(root_a, "项目/图.png", b"A-IMAGE")
    _write_output(root_b, "项目/图.png", b"B-IMAGE")

    store = Store.connect(tmp_path / "control.sqlite3")
    user_a = store.create_user("A")
    user_b = store.create_user("B")
    a = Identity(user_a.user_id, user_a.workspace_id, "member")
    b = Identity(user_b.user_id, user_b.workspace_id, "member")

    class Runtime:
        def __init__(self):
            self.ensure_calls: list[tuple[str, int]] = []

        async def ensure_ready(self, workspace_id, config_version):
            self.ensure_calls.append((workspace_id, config_version))
            port = 7861 if workspace_id == a.workspace_id else 7862
            return WorkerHandle(
                workspace_id, f"{port:064x}",
                f"http://127.0.0.1:{port}", f"token-{port}", config_version,
                gateway_instance_id=f"gateway-{port}",
                gateway_generation=f"gen-{port}",
                gateway_upstream=f"http://127.0.0.1:{port}",
                gateway_state="available")

    runtime = Runtime()
    client = httpx.AsyncClient(
        transport=WorkerTransport({7861: root_a, 7862: root_b}),
        trust_env=False)
    from easel.control.gateway_client import GatewayClient

    gateway = GatewayClient(Ed25519PrivateKey.generate(), client)
    api = WorkspaceApi(
        runtime, store, gateway, revision=REVISION, gateway_required=True,
        max_upload_bytes=1024 * 1024)
    fixture = Fixture(store, api, a, b, root_a, root_b)
    fixture.runtime = runtime
    fixture.client = client
    try:
        yield fixture
    finally:
        store.close()
        asyncio.run(client.aclose())


def _read_stream(stream) -> tuple[int, dict, bytes]:
    async def collect():
        chunks = []
        async for chunk in stream.chunks():
            chunks.append(chunk)
        return b"".join(chunks)
    body = asyncio.run(collect())
    return stream.status_code, stream.headers, body


def test_status_does_not_start_worker_and_reports_not_started(worker_fixture):
    async def scenario():
        return await worker_fixture.api.status(worker_fixture.a)

    payload = asyncio.run(scenario())
    assert payload["state"] == "not_started"
    assert payload["gateway"] is False
    assert payload["skills"] == []
    assert payload["personas"] == []
    assert worker_fixture.runtime.ensure_calls == []


def _ready_lease(fixture, actor, port=7861):
    handle = WorkerHandle(
        actor.workspace_id, f"{port:064x}", f"http://127.0.0.1:{port}",
        f"token-{port}", REVISION,
        gateway_instance_id=f"gateway-{port}",
        gateway_generation=f"gen-{port}",
        gateway_upstream=f"http://127.0.0.1:{port}",
        gateway_state="available")
    now = int(time.time())
    outcome, lease = fixture.store.claim_worker_start(
        actor.workspace_id, REVISION, "task5a-owner", now, 600)
    assert outcome == "started"
    assert fixture.store.complete_worker_start(
        actor.workspace_id, "task5a-owner", lease.generation, handle,
        now, 600) is True
    return handle


def test_status_queries_ready_worker_without_starting(worker_fixture):
    _ready_lease(worker_fixture, worker_fixture.a)

    async def scenario():
        return await worker_fixture.api.status(worker_fixture.a)

    payload = asyncio.run(scenario())
    assert payload["state"] == "ready"
    assert [item["name"] for item in payload["personas"]] == ["同名画像"]
    assert isinstance(payload["skills"], list)
    assert payload["transport"] == payload["runtime"] == "control"
    assert worker_fixture.runtime.ensure_calls == []


def test_status_reports_unavailable_when_ready_worker_cannot_answer(worker_fixture):
    _ready_lease(worker_fixture, worker_fixture.a, port=7999)

    async def scenario():
        return await worker_fixture.api.status(worker_fixture.a)

    payload = asyncio.run(scenario())
    assert payload["state"] == "unavailable"
    assert payload["gateway"] is False
    assert "失败" in payload["detail"]


def test_personas_are_isolated_between_workspaces_with_same_name(worker_fixture):
    async def scenario():
        a_list = await worker_fixture.api.request(
            worker_fixture.a, "GET", "/api/personas")
        b_list = await worker_fixture.api.request(
            worker_fixture.b, "GET", "/api/personas")
        a_files = await worker_fixture.api.request(
            worker_fixture.a, "GET",
            f"/api/persona/{canonical_persona_name('同名画像')}/files")
        b_files = await worker_fixture.api.request(
            worker_fixture.b, "GET",
            f"/api/persona/{canonical_persona_name('同名画像')}/files")
        return a_list.json(), b_list.json(), a_files.json(), b_files.json()

    a_list, b_list, a_files, b_files = asyncio.run(scenario())
    assert [item["name"] for item in a_list] == ["同名画像"]
    assert a_list[0]["description"] == "A-定位"
    assert b_list[0]["description"] == "B-定位"
    a_text = {item["filename"]: item["content"] for item in a_files["files"]}
    b_text = {item["filename"]: item["content"] for item in b_files["files"]}
    assert a_text["identity.md"] == "A-定位"
    assert b_text["identity.md"] == "B-定位"


def test_persona_write_and_delete_only_touch_own_workspace(worker_fixture):
    name = canonical_persona_name("同名画像")

    async def scenario():
        saved = await worker_fixture.api.request(
            worker_fixture.a, "PUT", f"/api/persona/{name}/file",
            payload={"filename": "style.md", "content": "A 新风格"})
        deleted = await worker_fixture.api.request(
            worker_fixture.a, "DELETE", f"/api/persona/{name}")
        b_files = await worker_fixture.api.request(
            worker_fixture.b, "GET", f"/api/persona/{name}/files")
        return saved.json(), deleted.json(), b_files.json()

    saved, deleted, b_files = asyncio.run(scenario())
    assert saved == {"ok": True, "filename": "style.md"}
    assert deleted["ok"] is True
    b_text = {item["filename"]: item["content"] for item in b_files["files"]}
    assert b_text["style.md"] == "风格-B-定位"
    assert (worker_fixture.root_a / "profiles" / "同名画像").exists() is False


def test_outputs_content_media_head_and_range_are_isolated(worker_fixture):
    output = canonical_output_path("项目/甲.txt")
    image = canonical_output_path("项目/图.png")

    async def scenario():
        tree_a = await worker_fixture.api.request(
            worker_fixture.a, "GET", "/api/outputs")
        tree_b = await worker_fixture.api.request(
            worker_fixture.b, "GET", "/api/outputs")
        content_a = await worker_fixture.api.request(
            worker_fixture.a, "GET", f"/api/output/{output}")
        content_b = await worker_fixture.api.request(
            worker_fixture.b, "GET", f"/api/output/{output}")
        head = await worker_fixture.api.open_stream(
            worker_fixture.a, "HEAD", f"/api/media/{image}")
        ranged = await worker_fixture.api.open_stream(
            worker_fixture.a, "GET", f"/api/media/{image}",
            headers={"Range": "bytes=0-2"})
        return tree_a.json(), tree_b.json(), content_a.json(), content_b.json(), head, ranged

    tree_a, tree_b, content_a, content_b, head, ranged = asyncio.run(scenario())
    assert content_a["content"] == "A 的正文"
    assert content_b["content"] == "B 的正文"
    assert tree_a[0]["name"] == tree_b[0]["name"] == "项目"
    assert _read_stream(head)[2] == b""
    assert head.headers["content-length"] == "7"
    status, headers, body = _read_stream(ranged)
    assert status == 206
    assert body == b"A-I"
    assert headers["content-range"] == "bytes 0-2/7"


def test_archive_index_is_scoped_to_own_zip(worker_fixture):
    archive_dir = worker_fixture.root_a / "outputs" / "项目" / "素材"
    archive_dir.mkdir(parents=True)
    (archive_dir / "图.png").write_bytes(b"IMAGE")
    (archive_dir / "文档.html").write_text("<html>ok</html>", encoding="utf-8")
    with zipfile.ZipFile(archive_dir / "素材.zip", "w") as archive:
        archive.writestr("图.png", b"IMAGE")
        archive.writestr("文档.html", "<html>ok</html>".encode("utf-8"))

    async def scenario():
        return await worker_fixture.api.request(
            worker_fixture.a, "GET", "/api/artifacts/archive",
            query={"path": "项目/素材/素材.zip"})

    payload = asyncio.run(scenario())
    assert set(payload.json()["paths"]) == {
        "项目/素材/文档.html", "项目/素材/图.png",
    }


def test_other_workspace_paths_and_personas_are_not_found(worker_fixture):
    _write_output(worker_fixture.root_a, "仅A/私密.txt", b"A only")
    only_a = canonical_output_path("仅A/私密.txt")
    only_persona = canonical_persona_name("仅A画像")
    (worker_fixture.root_a / "profiles" / "仅A画像").mkdir()
    (worker_fixture.root_a / "profiles" / "仅A画像" / "identity.md").write_text(
        "A only", encoding="utf-8")

    async def scenario():
        output = None
        persona = None
        try:
            await worker_fixture.api.request(
                worker_fixture.b, "GET", f"/api/output/{only_a}")
        except Exception as error:
            output = error
        try:
            await worker_fixture.api.request(
                worker_fixture.b, "GET", f"/api/persona/{only_persona}")
        except Exception as error:
            persona = error
        return output, persona

    output, persona = asyncio.run(scenario())
    assert getattr(output, "status", None) == 404
    assert getattr(persona, "status", None) == 404
    assert not (worker_fixture.root_b / "outputs" / "仅A").exists()
    assert not (worker_fixture.root_b / "profiles" / "仅A画像").exists()


def test_output_delete_only_removes_own_file(worker_fixture):
    output = canonical_output_path("项目/甲.txt")

    async def scenario():
        deleted = await worker_fixture.api.request(
            worker_fixture.a, "DELETE", f"/api/output/{output}")
        b_content = await worker_fixture.api.request(
            worker_fixture.b, "GET", f"/api/output/{output}")
        return deleted.json(), b_content.json()

    deleted, b_content = asyncio.run(scenario())
    assert deleted["ok"] is True
    assert b_content["content"] == "B 的正文"
    assert not (worker_fixture.root_a / "outputs" / "项目" / "甲.txt").exists()
    assert (worker_fixture.root_b / "outputs" / "项目" / "甲.txt").exists()


def test_upload_returns_ref_owned_by_same_session_and_workspace(worker_fixture):
    boundary = "----easel-test"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="sessionId"\r\n\r\n'
        "session-a\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="files"; filename="素材.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
        "附件正文\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")

    async def scenario():
        response = await worker_fixture.api.request(
            worker_fixture.a, "POST", "/api/upload", body=body,
            content_type=f"multipart/form-data; boundary={boundary}")
        return response.json()

    payload = asyncio.run(scenario())
    assert payload["ok"] is True
    saved = payload["files"][0]
    assert saved["path"].startswith("_inbox/")
    file_path = worker_fixture.root_a / "outputs" / saved["path"]
    assert file_path.read_bytes() == "附件正文".encode("utf-8")

    from web import app as worker_app

    worker_app.OUTPUTS_DIR = worker_fixture.root_a / "outputs"
    request = worker_app.ChatRequest(
        message="看附件", sessionId="session-a",
        attachments=[worker_app.AttachmentRef(**saved)])
    assert "附件正文" not in worker_app._attachment_context(request)
    assert saved["path"] in worker_app._attachment_context(request)
    wrong_session = worker_app.ChatRequest(
        message="看附件", sessionId="session-b",
        attachments=[worker_app.AttachmentRef(**saved)])
    with pytest.raises(worker_app.HTTPException) as error:
        worker_app._attachment_context(wrong_session)
    assert error.value.status_code == 403


def test_upload_rejects_oversize_and_bad_multipart_without_partial_file(worker_fixture):
    boundary = "----easel-test"
    oversize = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="sessionId"\r\n\r\n'
        "session-a\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="files"; filename="big.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
        + "x" * (1024 * 1024 + 1)
        + f"\r\n--{boundary}--\r\n"
    ).encode("utf-8")

    async def scenario():
        try:
            await worker_fixture.api.upload(
                worker_fixture.a, oversize,
                f"multipart/form-data; boundary={boundary}")
        except Exception as error:  # DomainError 由调用方映射
            return error
        return None

    error = asyncio.run(scenario())
    assert error is not None
    assert getattr(error, "status", None) == 413
    inbox = worker_fixture.root_a / "outputs" / "_inbox"
    assert not inbox.exists() or not any(inbox.rglob("big.txt"))


def test_upload_batch_rejects_all_or_nothing(worker_fixture):
    boundary = "----easel-test"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="sessionId"\r\n\r\n'
        "session-a\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="files"; filename="good.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
        "good\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="files"; filename="bad.exe"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
        "bad\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")

    async def scenario():
        try:
            await worker_fixture.api.request(
                worker_fixture.a, "POST", "/api/upload", body=body,
                content_type=f"multipart/form-data; boundary={boundary}")
        except Exception as error:
            return error
        return None

    error = asyncio.run(scenario())
    assert error is not None
    assert getattr(error, "status", None) == 400
    inbox = worker_fixture.root_a / "outputs" / "_inbox"
    assert not inbox.exists() or not any(inbox.rglob("good.txt"))


def _link_directory(link: Path, target: Path) -> str | None:
    """创建真实 junction/symlink；失败返回 None（不冒充已验证）。"""
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True)
        return "junction" if result.returncode == 0 else None
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        return None
    return "symlink"


def test_persona_links_cannot_read_or_mutate_outside_profiles(worker_fixture):
    from easel import persona
    from web import app as worker_app

    private = worker_fixture.root_a / "private-state"
    private.mkdir()
    (private / "identity.md").write_text(
        "PRIVATE-PERSONA-SENTINEL", encoding="utf-8")
    link = worker_fixture.root_a / "profiles" / "外部画像"
    kind = _link_directory(link, private)
    if kind is None:
        pytest.skip("当前环境不允许创建目录 junction/symlink")

    worker_app.PROFILES_DIR = worker_fixture.root_a / "profiles"
    persona.PROFILES_DIR = worker_app.PROFILES_DIR

    names = [item["name"] for item in worker_app.list_personas()]
    assert "外部画像" not in names
    with pytest.raises(worker_app.HTTPException) as detail_error:
        asyncio.run(worker_app.api_persona("外部画像"))
    assert detail_error.value.status_code == 404
    with pytest.raises(worker_app.HTTPException) as files_error:
        asyncio.run(worker_app.api_persona_files("外部画像"))
    assert files_error.value.status_code == 404

    save_error = None
    try:
        asyncio.run(worker_app.api_persona_file_save(
            "外部画像",
            worker_app.PersonaFileRequest(
                filename="identity.md", content="overwrite")))
    except worker_app.HTTPException as error:
        save_error = error
    assert save_error is not None and save_error.status_code == 404
    assert (private / "identity.md").read_text(
        encoding="utf-8") == "PRIVATE-PERSONA-SENTINEL"

    delete_error = None
    try:
        asyncio.run(worker_app.api_persona_delete("外部画像"))
    except worker_app.HTTPException as error:
        delete_error = error
    assert delete_error is not None and delete_error.status_code == 404
    assert (private / "identity.md").exists()

    # 文件 symlink 在 Windows 可能无权限；能创建时必须同样拒绝。
    normal = worker_fixture.root_a / "profiles" / "同名画像"
    file_link = normal / "style.md"
    if file_link.exists() or file_link.is_symlink():
        file_link.unlink()
    try:
        file_link.symlink_to(private / "identity.md")
    except OSError as error:
        file_kind = f"NOT_RUN:{getattr(error, 'winerror', error.errno)}"
    else:
        file_kind = "symlink"
        detail = asyncio.run(worker_app.api_persona("同名画像"))
        files = asyncio.run(worker_app.api_persona_files("同名画像"))
        assert "PRIVATE-PERSONA-SENTINEL" not in detail["content"]
        assert all(
            "PRIVATE-PERSONA-SENTINEL" not in item["content"]
            for item in files["files"])
    assert file_kind.startswith(("symlink", "NOT_RUN:"))


def test_path_helpers_reject_traversal_absolute_and_internal():
    for bad in ("../x", "/abs", "a/../b", "a//b", "a\\b", "_login/x",
                "analytics/x", ".easel.json", "_ideas.json",
                "_schedule.json", "a\x00b",
                "_publish/manual/easel-xiaohongshu-0123456789ab.zip"):
        with pytest.raises(Exception):
            canonical_output_path(bad)
    export = "_publish/manual/easel-xiaohongshu-0123456789ab.zip"
    assert canonical_output_path(export, allow_export=True) == export
    for bad in ("_publish/manual/other.zip",
                "_publish/manual/easel-evil-0123456789ab.zip",
                "_publish/manual/easel-xiaohongshu-0123.zip",
                "_publish/manual/sub/easel-xiaohongshu-0123456789ab.zip"):
        with pytest.raises(Exception):
            canonical_output_path(bad, allow_export=True)
    for bad in ("..", "../x", "a/b", "a\\b", ".hidden", "_internal", ""):
        with pytest.raises(Exception):
            canonical_persona_name(bad)


def test_symlink_or_junction_inside_outputs_is_rejected(worker_fixture):
    outside = worker_fixture.root_a.parent / "outside-secret"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = worker_fixture.root_a / "outputs" / "项目" / "link"
    created = False
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True, text=True)
        created = result.returncode == 0
    else:
        try:
            link.symlink_to(outside, target_is_directory=True)
            created = True
        except OSError:
            pass
    if not created:
        pytest.skip("当前环境不允许创建 symlink/junction")

    async def scenario():
        try:
            await worker_fixture.api.request(
                worker_fixture.a, "GET",
                f"/api/output/{canonical_output_path('项目/link/secret.txt')}")
        except Exception as error:
            return error
        return None

    error = asyncio.run(scenario())
    assert error is not None
    assert getattr(error, "status", None) in (403, 404)


def test_stream_cancel_releases_request_lease(worker_fixture):
    image = canonical_output_path("项目/图.png")

    async def scenario():
        stream = await worker_fixture.api.open_stream(
            worker_fixture.a, "GET", f"/api/media/{image}")
        assert worker_fixture.store.count_worker_request_leases(
            worker_fixture.a.workspace_id) == 1
        await stream.aclose()
        return worker_fixture.store.count_worker_request_leases(
            worker_fixture.a.workspace_id)

    assert asyncio.run(scenario()) == 0


def test_workspace_requests_pass_real_gateway_signature(tmp_path):
    """完整 control GatewayClient → gateway 验签 → worker 真实 handler。"""
    from easel.gateway.app import GatewayConfig, create_gateway_app

    from web import app as worker_app

    root_a = tmp_path / "gateway-a"
    root_b = tmp_path / "gateway-b"
    for root, label in ((root_a, "A"), (root_b, "B")):
        (root / "outputs").mkdir(parents=True)
        (root / "profiles").mkdir(parents=True)
        _write_worker_persona(root, "画像", f"{label} 定位")
        _write_output(root, "项目/图.png", f"{label}-IMAGE".encode("utf-8"))

    store = Store.connect(tmp_path / "gateway-control.sqlite3")
    user_a = store.create_user("GA")
    user_b = store.create_user("GB")
    # store 生成的是随机 workspace；测试夹具需要固定 A/B 目录映射
    workspace_a = user_a.workspace_id
    workspace_b = user_b.workspace_id
    root_by_ws = {workspace_a: root_a, workspace_b: root_b}

    class HeaderTransport2(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx.ASGITransport(app=worker_app.app)

        async def handle_async_request(self, request):
            workspace = request.headers["x-easel-workspace-id"]
            root = root_by_ws[workspace]
            worker_app.OUTPUTS_DIR = root / "outputs"
            worker_app.PROFILES_DIR = root / "profiles"
            from easel import persona

            persona.PROFILES_DIR = root / "profiles"
            return await self.inner.handle_async_request(request)

    upstream_client = httpx.AsyncClient(
        transport=HeaderTransport2(), trust_env=False)
    key = Ed25519PrivateKey.generate()
    gateway_apps = {}
    for workspace, label in ((workspace_a, "a"), (workspace_b, "b")):
        config = GatewayConfig(
            workspace_id=workspace,
            gateway_instance_id=f"gateway-{label}",
            boot_generation=f"boot-{label}",
            worker_container_id=f"{ord(label):064x}",
            config_version=REVISION,
            worker_origin="http://127.0.0.1:7860",
            worker_service_token=f"service-{label}",
        )
        gateway_apps[label] = create_gateway_app(
            config, key.public_key(), activated=True,
            http_client=upstream_client)

    class GatewayTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = {
                "gateway-a": httpx.ASGITransport(app=gateway_apps["a"]),
                "gateway-b": httpx.ASGITransport(app=gateway_apps["b"]),
            }

        async def handle_async_request(self, request):
            return await self.inner[request.url.host].handle_async_request(
                request)

    class Runtime:
        async def ensure_ready(self, workspace_id, config_version):
            label = "a" if workspace_id == workspace_a else "b"
            return WorkerHandle(
                workspace_id, f"{ord(label):064x}",
                "http://127.0.0.1:7860", f"service-{label}", config_version,
                gateway_instance_id=f"gateway-{label}",
                gateway_generation=f"boot-{label}",
                gateway_upstream=f"http://gateway-{label}:7860",
                gateway_state="available")

    client = httpx.AsyncClient(
        transport=GatewayTransport(), trust_env=False)
    from easel.control.gateway_client import GatewayClient
    from easel.control.workspace_api import WorkspaceApi

    api = WorkspaceApi(
        Runtime(), store, GatewayClient(key, client), revision=REVISION,
        gateway_required=True)
    a = Identity(user_a.user_id, workspace_a, "member")
    b = Identity(user_b.user_id, workspace_b, "member")

    async def scenario():
        personas_a = await api.request(a, "GET", "/api/personas")
        personas_b = await api.request(b, "GET", "/api/personas")
        image = canonical_output_path("项目/图.png")
        ranged = await api.open_stream(
            a, "GET", f"/api/media/{image}",
            headers={"Range": "bytes=0-6"})
        chunks = []
        async for chunk in ranged.chunks():
            chunks.append(chunk)
        head = await api.open_stream(
            b, "HEAD", f"/api/media/{image}")
        await head.aclose()
        return personas_a.json(), personas_b.json(), b"".join(chunks), head

    try:
        personas_a, personas_b, image_a, head_b = asyncio.run(scenario())
    finally:
        store.close()
        asyncio.run(client.aclose())
        asyncio.run(upstream_client.aclose())
    assert personas_a[0]["description"] == "A 定位"
    assert personas_b[0]["description"] == "B 定位"
    assert image_a == b"A-IMAGE"
    assert head_b.status_code == 200
    assert head_b.headers["content-length"] == "7"

class FakeWorkspace:
    def __init__(self):
        self.calls = []

    async def status(self, actor):
        self.calls.append(("status", actor, None))
        return {"gateway": True, "transport": "control", "runtime": "control",
                "skills": [], "personas": [{"name": "P", "description": ""}],
                "state": "ready", "detail": ""}

    async def request(self, actor, method, path, *, query=None, payload=None,
                      body=None, content_type=None, headers=None):
        self.calls.append((method, actor, path, query, payload, body,
                           content_type, headers))
        return httpx.Response(200, json={
            "actor": actor.user_id, "method": method, "path": path})

    async def upload(self, actor, body, content_type):
        self.calls.append(("upload", actor, body, content_type))
        return httpx.Response(200, json={
            "ok": True,
            "files": [{"id": "i1", "name": "素材.txt",
                       "path": "_inbox/x/y/素材.txt"}],
        })

    async def open_stream(self, actor, method, path, *, headers=None):
        self.calls.append(("stream", actor, method, path, headers))

        class Stream:
            status_code = 200
            headers = {"content-type": "image/png", "content-length": "3",
                       "etag": '"worker-etag"', "cache-control": "no-store"}

            async def chunks(self):
                yield b"abc"

            async def aclose(self):
                return None

        return Stream()


class EtagWorkspace(FakeWorkspace):
    async def open_stream(self, actor, method, path, *, headers=None):
        self.calls.append(("stream", actor, method, path, headers))
        conditional = (headers or {}).get("If-None-Match")

        class Stream:
            status_code = 304 if conditional == '"abc"' else 200
            headers = (
                {"etag": '"abc"', "cache-control": "private, no-cache"}
                if conditional == '"abc"' else
                {"content-type": "image/png", "content-length": "3",
                 "etag": '"abc"', "cache-control": "private, no-cache"}
            )

            async def chunks(self):
                if Stream.status_code == 200:
                    yield b"abc"

            async def aclose(self):
                return None

        return Stream()


@contextmanager
def control_api(tmp_path, workspace):
    store = Store.connect(tmp_path / "control.sqlite3")
    admin = store.create_user("Owner", "admin")
    member = store.create_user("Member")
    auth = AuthService(store)
    app = create_app(store=store, auth=auth,
                     config=ControlConfig(public_origin=ORIGIN),
                     workspace_api=workspace)
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            yield client, store, auth, admin, member
    finally:
        store.close()


def login(client, auth, admin, member):
    invite = auth.create_invite(admin, member.user_id)
    response = client.post("/api/auth/exchange", json={"token": invite},
                           headers={"Origin": ORIGIN})
    assert response.status_code == 200, response.text
    return response.json()


def test_control_workspace_requires_authentication_before_forwarding(tmp_path):
    fake = FakeWorkspace()
    with control_api(tmp_path, fake) as (client, store, auth, admin, member):
        for method, path in [
            ("GET", "/api/status"), ("GET", "/api/personas"),
            ("GET", "/api/outputs"), ("GET", "/api/media/a.png"),
            ("POST", "/api/upload"), ("DELETE", "/api/output/a.png"),
        ]:
            response = client.request(method, path)
            assert response.status_code == 401, (method, path)
        assert fake.calls == []


def test_control_workspace_uses_authenticated_identity_and_csrf(tmp_path):
    fake = FakeWorkspace()
    with control_api(tmp_path, fake) as (client, store, auth, admin, member):
        body = login(client, auth, admin, member)
        user_id = body["userId"]
        csrf = body["csrfToken"]
        assert client.get("/api/status").json()["state"] == "ready"
        assert client.get("/api/outputs").json()["actor"] == user_id

        missing = client.put("/api/persona/P/file", json={
            "filename": "style.md", "content": "x"})
        assert missing.status_code == 403
        wrong = client.put(
            "/api/persona/P/file", json={"filename": "style.md", "content": "x"},
            headers={"Origin": ORIGIN, "X-CSRF-Token": "wrong"})
        assert wrong.status_code == 403
        saved = client.put(
            "/api/persona/P/file", json={"filename": "style.md", "content": "x"},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf,
                     "X-Easel-Workspace-Id": "forged",
                     "X-Easel-Role": "admin"})
        assert saved.status_code == 200, saved.text
        method, actor, path = fake.calls[-1][:3]
        assert method == "PUT" and actor.user_id == user_id
        assert path == "/api/persona/P/file"

        assert client.post(
            "/api/upload/local", headers={
                "Origin": ORIGIN, "X-CSRF-Token": csrf},
            json={}).status_code == 403


def test_control_media_preserves_range_headers_and_scopes_etag(tmp_path):
    fake = FakeWorkspace()
    with control_api(tmp_path, fake) as (client, store, auth, admin, member):
        body = login(client, auth, admin, member)
        response = client.get(
            "/api/media/a.png",
            headers={"Range": "bytes=0-2", "If-None-Match": '"abc"'})
        assert response.status_code == 200
        assert response.content == b"abc"
        assert response.headers["content-length"] == "3"
        assert response.headers["cache-control"].startswith("private")
        assert response.headers["vary"] == "Cookie"
        etag = response.headers["etag"]
        assert etag != '"worker-etag"'
        scope = hashlib.sha256(
            body["workspaceId"].encode("utf-8")).hexdigest()[:12]
        assert scope in etag
        stream_call = fake.calls[-1]
        assert stream_call[0] == "stream"
        assert stream_call[4]["Range"] == "bytes=0-2"
        assert "If-None-Match" not in stream_call[4]


def test_control_media_304_only_accepts_own_scoped_etag(tmp_path):
    fake = EtagWorkspace()
    with control_api(tmp_path, fake) as (client, store, auth, admin, member):
        body = login(client, auth, admin, member)
        first = client.get("/api/media/a.png")
        assert first.status_code == 200
        etag = first.headers["etag"]

        revalidated = client.get(
            "/api/media/a.png", headers={"If-None-Match": etag})
        assert revalidated.status_code == 304
        assert revalidated.content == b""
        assert revalidated.headers["etag"] == etag
        assert fake.calls[-1][4]["If-None-Match"] == '"abc"'

        foreign = client.get(
            "/api/media/a.png",
            headers={"If-None-Match": '"other-workspace-etag"'})
        assert foreign.status_code == 200
        assert "If-None-Match" not in fake.calls[-1][4]
        assert body["workspaceId"]


def test_control_upload_forwards_bounded_multipart_and_requires_csrf(tmp_path):
    fake = FakeWorkspace()
    with control_api(tmp_path, fake) as (client, store, auth, admin, member):
        body = login(client, auth, admin, member)
        csrf = body["csrfToken"]
        payload = b"--x\r\ncontent\r\n--x--\r\n"
        missing = client.post(
            "/api/upload", content=payload,
            headers={"Content-Type": "multipart/form-data; boundary=x"})
        assert missing.status_code == 403
        response = client.post(
            "/api/upload", content=payload,
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf,
                     "Content-Type": "multipart/form-data; boundary=x"})
        assert response.status_code == 200, response.text
        assert response.json()["files"][0]["path"].startswith("_inbox/")
        upload_call = fake.calls[-1]
        assert upload_call[0] == "upload"
        assert upload_call[1].user_id == body["userId"]
        assert upload_call[2] == payload
        assert upload_call[3].startswith("multipart/form-data; boundary=x")

        json_upload = client.post(
            "/api/upload", json={"files": []},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
        assert json_upload.status_code == 400


def test_publishing_routes_need_relay_capability(tmp_path):
    fake = FakeWorkspace()
    with control_api(tmp_path, fake) as (client, store, auth, admin, member):
        body = login(client, auth, admin, member)
        headers = {"Origin": ORIGIN, "X-CSRF-Token": body["csrfToken"]}
        # 5C3 已接通发布/导出路由；缺少 relay 时必须是明确 503，不能假成功。
        publish = client.post(
            "/api/publish/xiaohongshu",
            json={"title": "T", "body": "B",
                  "media": ["project/cover.png"], "requestId": "r1"},
            headers=headers)
        assert publish.status_code == 503, publish.text
        assert publish.json()["code"] == "capability_unavailable"
        export = client.post(
            "/api/manual-publish/xiaohongshu/export",
            json={"title": "T", "body": "B", "media": [], "reviewed": True},
            headers=headers)
        assert export.status_code == 503, export.text
        assert export.json()["code"] == "capability_unavailable"


def _workbench_response(path: str, method: str, payload=None):
    if path == "/api/trends":
        return {"trends": [{"platform": "weibo", "label": "微博",
                            "items": [{"title": "热搜", "hot": "1", "url": ""}],
                            "ok": True}], "updated": 1}
    if path == "/api/schedule":
        if method == "GET":
            return [{"id": "a1b2c3d4e5f6", "title": "排期", "date": "2026-10-30",
                     "platform": "", "time": "", "status": "idea", "note": ""}]
        return {"id": "a1b2c3d4e5f6", **payload}
    if path == "/api/schedule/context":
        return {"window_days": 14, "suggestions": []}
    if path == "/api/ideas":
        if method == "GET":
            return [{"id": "a1b2c3d4e5f6", "title": "选题", "note": "",
                     "source": "", "status": "pending", "created": 1}]
        return {"id": "a1b2c3d4e5f6", **payload}
    return {"ok": True}


def test_control_workbench_routes_forward_exact_identity_requests(tmp_path):
    class WorkbenchWorkspace(FakeWorkspace):
        async def request(self, actor, method, path, *, query=None, payload=None,
                          body=None, content_type=None, headers=None):
            self.calls.append((method, actor, path, query, payload, body,
                               content_type, headers))
            return httpx.Response(200, json=_workbench_response(path, method,
                                                                payload))

    fake = WorkbenchWorkspace()
    with control_api(tmp_path, fake) as (client, store, auth, admin, member):
        body = login(client, auth, admin, member)
        user_id = body["userId"]
        csrf = body["csrfToken"]
        headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}

        trends = client.get("/api/trends?platforms=weibo,douyin&limit=15")
        assert trends.status_code == 200, trends.text
        assert trends.json()["trends"][0]["ok"] is True
        call = fake.calls[-1]
        assert call[0] == "GET" and call[2] == "/api/trends"
        assert call[1].user_id == user_id
        assert call[3] == {"platforms": "weibo,douyin", "limit": "15"}

        defaults = client.get("/api/trends")
        assert defaults.status_code == 200, defaults.text
        assert fake.calls[-1][3] == {
            "platforms": "weibo,douyin,zhihu", "limit": "12",
        }

        schedule = client.get("/api/schedule")
        assert schedule.status_code == 200
        assert fake.calls[-1][2] == "/api/schedule"
        created_schedule = client.post(
            "/api/schedule",
            json={"title": "新排期", "date": "2026-10-30", "platform": "微博",
                  "time": "10:00", "status": "scheduled", "note": "备注",
                  "kind": "content", "url": "", "source": "manual",
                  "event_type": "", "end_date": "", "id": "forged"},
            headers=headers)
        assert created_schedule.status_code == 200, created_schedule.text
        call = fake.calls[-1]
        assert call[0] == "POST" and call[2] == "/api/schedule"
        assert "id" not in call[4]
        assert call[4]["title"] == "新排期"
        assert set(call[4]) == {
            "title", "date", "platform", "time", "status", "note",
            "kind", "url", "source", "event_type", "end_date"}
        updated_schedule = client.put(
            "/api/schedule/a1b2c3d4e5f6",
            json={"title": "改排期", "date": "2026-10-31"}, headers=headers)
        assert updated_schedule.status_code == 200
        deleted_schedule = client.delete(
            "/api/schedule/a1b2c3d4e5f6", headers=headers)
        assert deleted_schedule.status_code == 200
        assert fake.calls[-1][0] == "DELETE"

        context = client.get("/api/schedule/context?days=14")
        assert context.status_code == 200, context.text
        assert fake.calls[-1][3] == {"days": "14"}

        ideas = client.get("/api/ideas")
        assert ideas.status_code == 200
        assert fake.calls[-1][2] == "/api/ideas"
        created_idea = client.post(
            "/api/ideas",
            json={"title": "新选题", "note": "角度", "source": "微博热搜",
                  "status": "pending", "created": 999, "id": "forged"},
            headers=headers)
        assert created_idea.status_code == 200, created_idea.text
        call = fake.calls[-1]
        assert call[2] == "/api/ideas"
        assert call[4] == {"title": "新选题", "note": "角度",
                           "source": "微博热搜", "status": "pending"}
        updated_idea = client.put(
            "/api/ideas/a1b2c3d4e5f6",
            json={"title": "改选题", "note": "", "source": "",
                  "status": "doing"}, headers=headers)
        assert updated_idea.status_code == 200
        deleted_idea = client.delete(
            "/api/ideas/a1b2c3d4e5f6", headers=headers)
        assert deleted_idea.status_code == 200
        assert fake.calls[-1][0] == "DELETE"


def test_control_workbench_rejects_malformed_targets_before_forwarding(tmp_path):
    fake = FakeWorkspace()
    with control_api(tmp_path, fake) as (client, store, auth, admin, member):
        body = login(client, auth, admin, member)
        csrf = body["csrfToken"]
        headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
        before = len(fake.calls)

        for path in [
            "/api/trends?platforms=weibo,evil",
            "/api/trends?platforms=weibo&platforms=douyin",
            "/api/trends?limit=0",
            "/api/trends?limit=31",
            "/api/trends?limit=not-a-number",
            "/api/schedule/context?days=0",
            "/api/schedule/context?days=91",
            "/api/schedule/context?days=14&days=15",
        ]:
            response = client.get(path)
            assert response.status_code == 400, (path, response.text)

        for path in [
            "/api/ideas/not-an-id", "/api/ideas/a1b2c3d4e5f",
            "/api/schedule/a1b2c3d4e5f6z", "/api/schedule/%2e%2e",
        ]:
            response = client.put(path, json={"title": "x", "date": "x"},
                                  headers=headers)
            assert response.status_code == 400, (path, response.text)

        for payload in [
            {},
            {"title": 1},
            {"title": "x" * 201},
            {"title": "x\x01"},
            {"title": "x", "status": "x" * 33},
        ]:
            response = client.post("/api/ideas", json=payload, headers=headers)
            assert response.status_code == 400, (payload, response.text)

        for payload in [
            {"title": "x"},
            {"title": "x", "date": 1},
            {"title": "x" * 201, "date": "2026-10-30"},
            {"title": "x", "date": "2026-10-30", "note": "x" * 4001},
        ]:
            response = client.post("/api/schedule", json=payload,
                                   headers=headers)
            assert response.status_code == 400, (payload, response.text)

        huge = client.post(
            "/api/ideas", content=b'{"title":"' + b"x" * (70 * 1024) + b'"}',
            headers={**headers, "Content-Type": "application/json"})
        assert huge.status_code == 413, huge.text
        assert len(fake.calls) == before

        missing_csrf = client.post(
            "/api/ideas", json={"title": "x"},
            headers={"Origin": ORIGIN})
        assert missing_csrf.status_code == 403
        assert len(fake.calls) == before


def test_control_workbench_requires_authentication(tmp_path):
    fake = FakeWorkspace()
    with control_api(tmp_path, fake) as (client, store, auth, admin, member):
        for method, path in [
            ("GET", "/api/trends"),
            ("GET", "/api/schedule"),
            ("GET", "/api/schedule/context"),
            ("GET", "/api/ideas"),
            ("POST", "/api/ideas"),
            ("PUT", "/api/ideas/a1b2c3d4e5f6"),
            ("DELETE", "/api/schedule/a1b2c3d4e5f6"),
        ]:
            response = client.request(method, path)
            assert response.status_code == 401, (method, path)
        assert fake.calls == []


def test_workbench_ideas_and_schedule_are_isolated_and_persisted(
        worker_fixture):
    import json as json_module

    same_idea = "a1b2c3d4e5f6"
    same_schedule = "b1b2c3d4e5f6"
    for root, label in ((worker_fixture.root_a, "A"),
                        (worker_fixture.root_b, "B")):
        (root / "outputs" / "_ideas.json").write_text(json_module.dumps([{
            "id": same_idea, "title": f"{label} 选题", "note": "",
            "source": "", "status": "pending", "created": 1,
        }]), encoding="utf-8")
        (root / "outputs" / "_schedule.json").write_text(json_module.dumps([{
            "id": same_schedule, "title": f"{label} 排期",
            "date": "2026-10-30", "platform": "", "time": "",
            "status": "idea", "note": "",
        }]), encoding="utf-8")

    async def scenario():
        a_ideas = await worker_fixture.api.request(
            worker_fixture.a, "GET", "/api/ideas")
        b_ideas = await worker_fixture.api.request(
            worker_fixture.b, "GET", "/api/ideas")
        a_schedule = await worker_fixture.api.request(
            worker_fixture.a, "GET", "/api/schedule")
        b_schedule = await worker_fixture.api.request(
            worker_fixture.b, "GET", "/api/schedule")
        created = await worker_fixture.api.request(
            worker_fixture.a, "POST", "/api/ideas",
            payload={"title": "A 新选题", "note": "角度",
                     "source": "微博热搜", "status": "pending"})
        created_schedule = await worker_fixture.api.request(
            worker_fixture.a, "POST", "/api/schedule",
            payload={"title": "A 新排期", "date": "2026-10-31",
                     "platform": "微博", "time": "10:00",
                     "status": "scheduled", "note": ""})
        updated = await worker_fixture.api.request(
            worker_fixture.a, "PUT", f"/api/ideas/{same_idea}",
            payload={"title": "A 改选题", "note": "", "source": "",
                     "status": "doing"})
        b_after = await worker_fixture.api.request(
            worker_fixture.b, "GET", "/api/ideas")
        return (a_ideas.json(), b_ideas.json(), a_schedule.json(),
                b_schedule.json(), created.json(), created_schedule.json(),
                updated.json(), b_after.json())

    (a_ideas, b_ideas, a_schedule, b_schedule, created, created_schedule,
     updated, b_after) = asyncio.run(scenario())
    assert a_ideas[0]["title"] == "A 选题"
    assert b_ideas[0]["title"] == "B 选题"
    assert a_schedule[0]["title"] == "A 排期"
    assert b_schedule[0]["title"] == "B 排期"
    assert created["id"] != same_idea
    assert created["created"] > 0
    assert created_schedule["id"] != same_schedule
    assert updated["title"] == "A 改选题"
    assert b_after[0]["title"] == "B 选题"

    a_data = json_module.loads(
        (worker_fixture.root_a / "outputs" / "_ideas.json").read_text(
            encoding="utf-8"))
    b_data = json_module.loads(
        (worker_fixture.root_b / "outputs" / "_ideas.json").read_text(
            encoding="utf-8"))
    assert {item["title"] for item in a_data} == {"A 改选题", "A 新选题"}
    assert {item["title"] for item in b_data} == {"B 选题"}

    async def delete_both():
        await worker_fixture.api.request(
            worker_fixture.a, "DELETE", "/api/ideas/a1b2c3d4e5f6")
        deleted_b = await worker_fixture.api.request(
            worker_fixture.b, "DELETE", "/api/ideas/a1b2c3d4e5f6")
        try:
            await worker_fixture.api.request(
                worker_fixture.a, "DELETE", "/api/ideas/a1b2c3d4e5f6")
        except Exception as error:
            return deleted_b.json(), error

    deleted_b, error = asyncio.run(delete_both())
    assert deleted_b["ok"] is True
    assert getattr(error, "status", None) == 404
    assert len(json_module.loads(
        (worker_fixture.root_b / "outputs" / "_ideas.json").read_text(
            encoding="utf-8"))) == 0


def test_workbench_corrupt_storage_is_error_not_empty_or_overwritten(
        worker_fixture):
    broken = "{not-json"
    for name in ("_ideas.json", "_schedule.json"):
        (worker_fixture.root_a / "outputs" / name).write_text(
            broken, encoding="utf-8")

    async def scenario():
        errors = []
        for path in ("/api/ideas", "/api/schedule", "/api/schedule/context"):
            try:
                await worker_fixture.api.request(
                    worker_fixture.a, "GET", path,
                    query={"days": "14"} if path.endswith("context") else None)
            except Exception as error:
                errors.append(error)
        return errors

    errors = asyncio.run(scenario())
    assert len(errors) == 3
    assert all(getattr(error, "status", None) == 502 for error in errors)
    for name in ("_ideas.json", "_schedule.json"):
        assert (worker_fixture.root_a / "outputs" / name).read_text(
            encoding="utf-8") == broken


def test_workbench_concurrent_idea_creates_do_not_lose_records(
        worker_fixture, monkeypatch):
    from web import app as worker_app

    original_read = worker_app._read_ideas

    def slow_read():
        items = original_read()
        time.sleep(0.2)
        return items

    monkeypatch.setattr(worker_app, "_read_ideas", slow_read)

    async def scenario():
        first, second = await asyncio.gather(
            worker_fixture.api.request(
                worker_fixture.a, "POST", "/api/ideas",
                payload={"title": "并发一", "note": "", "source": "",
                         "status": "pending"}),
            worker_fixture.api.request(
                worker_fixture.a, "POST", "/api/ideas",
                payload={"title": "并发二", "note": "", "source": "",
                         "status": "pending"}),
        )
        return (await worker_fixture.api.request(
            worker_fixture.a, "GET", "/api/ideas")).json()

    ideas = asyncio.run(scenario())
    assert {item["title"] for item in ideas} == {"并发一", "并发二"}


def test_workbench_concurrent_schedule_creates_do_not_lose_records(
        worker_fixture, monkeypatch):
    from web import app as worker_app

    original_read = worker_app._read_schedule

    def slow_read():
        items = original_read()
        time.sleep(0.2)
        return items

    monkeypatch.setattr(worker_app, "_read_schedule", slow_read)

    async def scenario():
        await asyncio.gather(
            worker_fixture.api.request(
                worker_fixture.a, "POST", "/api/schedule",
                payload={"title": "并发排期一", "date": "2026-11-01",
                         "platform": "", "time": "", "status": "idea",
                         "note": ""}),
            worker_fixture.api.request(
                worker_fixture.a, "POST", "/api/schedule",
                payload={"title": "并发排期二", "date": "2026-11-02",
                         "platform": "", "time": "", "status": "idea",
                         "note": ""}),
        )
        return (await worker_fixture.api.request(
            worker_fixture.a, "GET", "/api/schedule")).json()

    items = asyncio.run(scenario())
    assert {item["title"] for item in items} == {"并发排期一", "并发排期二"}


def test_workbench_context_uses_fixed_script_and_absolute_data_path(
        worker_fixture, monkeypatch):
    from web import app as worker_app

    captured = []

    def fake_run(cmd, **kwargs):
        captured.append((list(cmd), kwargs))

        class Result:
            returncode = 0
            stdout = '{"window_days":14,"suggestions":["保持节奏"]}'

        return Result()

    monkeypatch.setattr(worker_app.subprocess, "run", fake_run)

    async def scenario():
        return await worker_fixture.api.request(
            worker_fixture.a, "GET", "/api/schedule/context",
            query={"days": "14"})

    payload = asyncio.run(scenario())
    assert payload.json()["suggestions"] == ["保持节奏"]
    cmd, kwargs = captured[0]
    script = Path(cmd[1])
    assert script.name == "calendar_ops.py"
    assert script.parent.name == "scripts"
    assert cmd[cmd.index("--data") + 1] == str(
        worker_fixture.root_a / "outputs" / "_schedule.json")
    assert cmd[cmd.index("--days") + 1] == "14"
    assert kwargs["cwd"] == str(worker_app.PROJECT_ROOT)


def test_workbench_outputs_junction_is_rejected_before_first_write(tmp_path):
    from web import app as worker_app

    private = tmp_path / "private-state"
    private.mkdir()
    outputs = tmp_path / "outputs"
    kind = _link_directory(outputs, private)
    if kind is None:
        pytest.skip("当前环境不允许创建目录 junction/symlink")
    prior = worker_app.OUTPUTS_DIR
    worker_app.OUTPUTS_DIR = outputs
    try:
        cases = [
            ("_ideas.json", worker_app.api_ideas_list,
             worker_app.api_ideas_create,
             worker_app.IdeaItem(title="越界选题")),
            ("_schedule.json", worker_app.api_schedule_list,
             worker_app.api_schedule_create,
             worker_app.ScheduleItem(title="越界排期", date="2026-11-01")),
        ]
        for name, read, create, request in cases:
            with pytest.raises(worker_app.HTTPException) as read_error:
                asyncio.run(read())
            assert read_error.value.status_code == 500
            with pytest.raises(worker_app.HTTPException) as write_error:
                asyncio.run(create(request))
            assert write_error.value.status_code == 500
            assert not (private / name).exists(), name
        with pytest.raises(worker_app.HTTPException):
            asyncio.run(worker_app.api_schedule_context(14))
        assert list(private.iterdir()) == []
    finally:
        worker_app.OUTPUTS_DIR = prior


def test_workbench_missing_outputs_directory_still_allows_first_create(
        tmp_path):
    from web import app as worker_app

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outputs = workspace / "outputs"
    prior = worker_app.OUTPUTS_DIR
    worker_app.OUTPUTS_DIR = outputs
    try:
        assert asyncio.run(worker_app.api_ideas_list()) == []
        idea = asyncio.run(worker_app.api_ideas_create(
            worker_app.IdeaItem(title="合法首次选题")))
        assert idea["title"] == "合法首次选题"
        assert (outputs / "_ideas.json").is_file()
        assert asyncio.run(worker_app.api_ideas_list())[0]["title"] == \
            "合法首次选题"

        schedule = asyncio.run(worker_app.api_schedule_create(
            worker_app.ScheduleItem(title="合法首次排期", date="2026-11-01")))
        assert schedule["title"] == "合法首次排期"
        assert (outputs / "_schedule.json").is_file()
    finally:
        worker_app.OUTPUTS_DIR = prior


def test_workbench_directory_in_place_of_storage_is_rejected(
        worker_fixture):
    target = worker_fixture.root_a / "outputs" / "_ideas.json"
    target.mkdir()

    async def scenario():
        try:
            await worker_fixture.api.request(
                worker_fixture.a, "GET", "/api/ideas")
        except Exception as error:
            return error

    error = asyncio.run(scenario())
    assert getattr(error, "status", None) == 502
    assert target.is_dir()


def test_workbench_link_storage_is_rejected_if_symlink_available(
        worker_fixture):
    private = worker_fixture.root_a / "private-schedule.json"
    private.write_text('[]', encoding="utf-8")
    link = worker_fixture.root_a / "outputs" / "_schedule.json"
    try:
        link.symlink_to(private)
    except OSError:
        pytest.skip("当前环境不允许创建文件 symlink")

    async def scenario():
        try:
            await worker_fixture.api.request(
                worker_fixture.a, "GET", "/api/schedule")
        except Exception as error:
            return error

    error = asyncio.run(scenario())
    assert getattr(error, "status", None) == 502
    assert link.is_symlink()


def test_media_after_logout_is_unauthorized(tmp_path):
    fake = FakeWorkspace()
    with control_api(tmp_path, fake) as (client, store, auth, admin, member):
        body = login(client, auth, admin, member)
        assert client.get("/api/media/a.png").status_code == 200
        logout = client.post(
            "/api/auth/logout",
            headers={"Origin": ORIGIN, "X-CSRF-Token": body["csrfToken"]})
        assert logout.status_code == 204
        assert client.get("/api/media/a.png").status_code == 401
