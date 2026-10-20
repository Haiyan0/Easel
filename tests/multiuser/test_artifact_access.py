"""Task 7：产物接口内部状态边界与 HTML/SVG 预览响应头。"""
from __future__ import annotations

import os
import subprocess
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import web.app as web

LOCAL = "http://127.0.0.1:7860"


def make_outputs(tmp_path: Path) -> Path:
    outputs = tmp_path / "outputs"
    (outputs / "成都文旅").mkdir(parents=True)
    (outputs / "成都文旅" / "成品.html").write_text(
        "<html><body>ok</body></html>", encoding="utf-8")
    (outputs / "成都文旅" / "预览.SVG").write_text(
        "<svg xmlns='http://www.w3.org/2000/svg'></svg>", encoding="utf-8")
    (outputs / "成都文旅" / "文档.xhtml").write_text(
        "<html xmlns='http://www.w3.org/1999/xhtml'><body>ok</body></html>",
        encoding="utf-8")
    (outputs / "成都文旅" / "文档大写.XHTML").write_text(
        "<html xmlns='http://www.w3.org/1999/xhtml'><body>ok</body></html>",
        encoding="utf-8")
    (outputs / "成都文旅" / "封面.png").write_bytes(b"png")
    (outputs / "_login").mkdir()
    (outputs / "_login" / "xiaohongshu.json").write_text(
        '{"state": "success", "cookie": "secret"}', encoding="utf-8")
    (outputs / "_login" / "secret.png").write_bytes(b"qr")
    (outputs / "_login" / "xiaohongshu.png").write_bytes(b"qr")
    (outputs / "_login" / "bundle.zip").write_bytes(b"zip")
    (outputs / "_sessions").mkdir()
    (outputs / "_sessions" / "events.jsonl").write_text(
        '{"token": "secret"}', encoding="utf-8")
    (outputs / "analytics").mkdir()
    (outputs / "analytics" / "data.json").write_text("{}", encoding="utf-8")
    (outputs / ".hidden").mkdir()
    (outputs / ".hidden" / "x.txt").write_text("secret", encoding="utf-8")
    (outputs / "_inbox" / "scope" / "batch").mkdir(parents=True)
    (outputs / "_inbox" / "scope" / "batch" / "a.png").write_bytes(b"img")
    return outputs


@pytest.fixture
def artifact_client(tmp_path, monkeypatch):
    outputs = make_outputs(tmp_path)
    monkeypatch.setattr(web, "OUTPUTS_DIR", outputs)
    monkeypatch.setattr(web, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web, "ENV_FILE", tmp_path / ".env")
    (tmp_path / ".env").write_text("", encoding="utf-8")
    with TestClient(web.app, base_url=LOCAL, client=("127.0.0.1", 50000)) as client:
        yield client, outputs


@pytest.mark.parametrize("path", [
    "_login/xiaohongshu.json",
    "_login/secret.png",
    "_sessions/events.jsonl",
    "_inbox/scope/batch/a.png",
    "analytics/data.json",
    ".hidden/x.txt",
])
def test_generic_text_and_media_endpoints_reject_internal_state(
        artifact_client, path):
    client, _ = artifact_client
    for url in (f"/api/output/{path}", f"/api/media/{path}"):
        response = client.get(url)
        assert response.status_code == 403, (url, response.status_code)


@pytest.mark.parametrize("path", [
    "_login/secret.png",
    "_sessions/events.jsonl",
    "analytics/data.json",
    ".hidden/x.txt",
])
def test_archive_index_cannot_read_internal_state(artifact_client, path):
    client, outputs = artifact_client
    bundle = outputs / "_login" / "index.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("a.png", b"x")
    response = client.get("/api/artifacts/archive", params={"path": path})
    assert response.status_code == 403


def test_archive_skips_internal_entries(artifact_client):
    client, outputs = artifact_client
    (outputs / "成都文旅" / "asset.png").write_bytes(b"png")
    bundle = outputs / "成都文旅" / "bundle.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("asset.png", b"png")
        archive.writestr("_secret.png", b"secret")
        archive.writestr(".hidden.png", b"secret")
    response = client.get("/api/artifacts/archive",
                          params={"path": "成都文旅/bundle.zip"})
    assert response.status_code == 200, response.text
    assert response.json()["paths"] == ["成都文旅/asset.png"]


@pytest.mark.parametrize("path", [
    "../outside.txt",
    "/etc/passwd",
    "成都文旅/../../_login/xiaohongshu.json",
])
def test_path_traversal_and_absolute_paths_are_rejected(
        artifact_client, path):
    client, _ = artifact_client
    for url in (f"/api/output/{path}", f"/api/media/{path}"):
        assert client.get(url).status_code in (403, 404)


@pytest.mark.parametrize("name", ["成品.html", "预览.SVG", "文档.xhtml",
                                   "文档大写.XHTML"])
def test_executable_document_previews_are_sandboxed(artifact_client, name):
    client, _ = artifact_client
    response = client.get(f"/api/media/成都文旅/{name}")
    assert response.status_code == 200
    policy = response.headers["content-security-policy"]
    assert "sandbox" in policy
    assert "allow-scripts" not in policy
    assert "allow-same-origin" not in policy
    assert "allow-top-navigation" not in policy
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("name", ["成品.html", "文档.xhtml"])
def test_executable_document_head_request_is_also_sandboxed(
        artifact_client, name):
    client, _ = artifact_client
    response = client.head(f"/api/media/成都文旅/{name}")
    assert response.status_code == 200
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_xhtml_response_media_type_is_explicit(artifact_client):
    client, _ = artifact_client
    response = client.get("/api/media/成都文旅/文档.xhtml")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/xhtml+xml")


def test_login_qr_png_remains_accessible_but_other_internal_media_does_not(
        artifact_client):
    client, _ = artifact_client
    assert client.get("/api/media/_login/xiaohongshu.png").status_code == 200
    assert client.get("/api/media/_login/secret.png").status_code == 403


def test_normal_images_remain_accessible(artifact_client):
    client, _ = artifact_client
    response = client.get("/api/media/成都文旅/封面.png")
    assert response.status_code == 200
    assert response.content == b"png"
    assert "content-security-policy" not in response.headers


def test_output_text_endpoint_still_reads_normal_artifacts(artifact_client):
    client, outputs = artifact_client
    (outputs / "成都文旅" / "成品.md").write_text("hello", encoding="utf-8")
    response = client.get("/api/output/成都文旅/成品.md")
    assert response.status_code == 200
    assert response.json()["content"] == "hello"


def test_symlink_to_internal_state_is_rejected(artifact_client):
    client, outputs = artifact_client
    link = outputs / "成都文旅" / "leak.json"
    try:
        os.symlink(outputs / "_login" / "xiaohongshu.json", link)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")
    assert client.get("/api/output/成都文旅/leak.json").status_code == 403
    assert client.get("/api/media/成都文旅/leak.json").status_code == 403


def test_windows_junction_escape_is_rejected(artifact_client):
    if os.name != "nt":
        pytest.skip("仅 Windows junction")
    client, outputs = artifact_client
    junction = outputs / "成都文旅" / "junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction),
         str(outputs / "_login")],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        pytest.skip(f"无法创建 junction：{result.stderr.strip()}")
    assert client.get(
        "/api/media/成都文旅/junction/xiaohongshu.json").status_code == 403


def _junction(link: Path, target: Path) -> bool:
    if os.name != "nt":
        return False
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return result.returncode == 0


def test_output_tree_skips_junction_to_private_and_external_dirs(
        artifact_client, tmp_path):
    client, outputs = artifact_client
    private = tmp_path / "private"
    private.mkdir()
    (private / "internal-sentinel.txt").write_text("secret", encoding="utf-8")
    (private / ".easel.json").write_text(
        '{"title": "PRIVATE_METADATA_SENTINEL"}', encoding="utf-8")
    if not _junction(outputs / "linked-project", private):
        pytest.skip("当前环境不允许创建 junction")
    if not _junction(outputs / "成都文旅" / "nested-link", private):
        pytest.skip("当前环境不允许创建嵌套 junction")

    response = client.get("/api/outputs")
    assert response.status_code == 200
    body = response.text
    assert "internal-sentinel" not in body
    assert "PRIVATE_METADATA_SENTINEL" not in body
    assert "linked-project" not in body
    assert "nested-link" not in body
    # 普通项目仍完整
    assert "成都文旅" in body and "成品.html" in body


def test_output_tree_survives_junction_cycle(artifact_client):
    client, outputs = artifact_client
    loop = outputs / "成都文旅" / "loop"
    if not _junction(loop, outputs / "成都文旅"):
        pytest.skip("当前环境不允许创建 junction")
    response = client.get("/api/outputs")
    assert response.status_code == 200
    data = response.json()
    assert any(item["name"] == "成都文旅" for item in data)

    def has_expanded_loop(node: dict) -> bool:
        if str(node.get("path", "")).startswith("成都文旅/loop/"):
            return True
        return any(has_expanded_loop(child)
                   for child in node.get("children", []))

    assert not any(has_expanded_loop(item) for item in data), \
        "环必须被切断，不得展开 loop/loop/..."


def test_output_tree_skips_junction_to_internal_login(artifact_client):
    client, outputs = artifact_client
    if not _junction(outputs / "login-link", outputs / "_login"):
        pytest.skip("当前环境不允许创建 junction")
    response = client.get("/api/outputs")
    assert response.status_code == 200
    assert "login-link" not in response.text
    assert "xiaohongshu" not in response.text


def test_project_metadata_symlink_to_private_is_not_read(
        artifact_client, tmp_path):
    client, outputs = artifact_client
    private = tmp_path / "private-meta.json"
    private.write_text('{"title": "PRIVATE_METADATA_SENTINEL"}',
                       encoding="utf-8")
    link = outputs / "成都文旅" / ".easel.json"
    if link.exists():
        return
    try:
        os.symlink(private, link)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")
    response = client.get("/api/outputs")
    assert response.status_code == 200
    assert "PRIVATE_METADATA_SENTINEL" not in response.text


def test_worker_upload_local_still_denied(tmp_path, monkeypatch):
    from easel.worker.guard import WorkerIdentity

    monkeypatch.setattr(web, "_WORKER_IDENTITY",
                        WorkerIdentity("ws-a", "token-a"), raising=False)
    monkeypatch.setattr(web, "OUTPUTS_DIR", make_outputs(tmp_path))
    monkeypatch.setattr(web, "PROJECT_ROOT", tmp_path)
    headers = {"X-Easel-Workspace-Id": "ws-a",
               "X-Easel-Service-Token": "token-a"}
    with TestClient(web.app, base_url=LOCAL,
                    client=("203.0.113.10", 50000)) as client:
        response = client.post(
            "/api/upload/local", headers=headers,
            data={"sessionId": "s"},
            files={"files": ("a.png", b"x", "image/png")})
    assert response.status_code == 403
