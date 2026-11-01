"""Task 5C3：worker 发布执行器、媒体快照与导出包的红灯测试。

不启动真实浏览器、不访问真实平台；受控进程替换只验证命令/状态/清理边界。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easel.worker import browser_tasks
from web import app as web

PLATFORM = "xiaohongshu"


@pytest.fixture
def outputs(tmp_path, monkeypatch):
    root = tmp_path / "outputs"
    root.mkdir()
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    monkeypatch.setattr(web, "OUTPUTS_DIR", root)
    monkeypatch.setattr(web, "PUBLISH_DIR", root / "_publish")
    monkeypatch.setattr(web, "LOGIN_DIR", root / "_login")
    monkeypatch.setattr(web, "PROFILES_DIR", profiles)
    monkeypatch.setattr(web, "_SERVICE_MODE", "worker")
    from easel import persona
    monkeypatch.setattr(persona, "PROFILES_DIR", profiles)
    return root


def _write(root: Path, relative: str, data: bytes = b"media") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _directory_link(link: Path, target: Path) -> bool:
    """优先 symlink；Windows 无权限时退化为 junction（同样会 resolve 到根外）。"""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True)
        return result.returncode == 0
    return False


def _publish_descriptor(**overrides):
    value = {
        "action": "publish", "platform": PLATFORM, "title": "标题",
        "body": "正文", "tags": "工具", "media": ["project/cover.png"],
        "persona": "",
    }
    value.update(overrides)
    return value


def _export_descriptor(**overrides):
    value = {
        "action": "manual-export", "platform": PLATFORM,
        "title": "标题", "body": "正文", "tags": "工具",
        "media": ["project/cover.png"], "reviewed": True,
    }
    value.update(overrides)
    return value


def _run(coro):
    return asyncio.run(coro)


def test_parse_action_accepts_only_strict_publish_and_export_fields():
    parsed = browser_tasks.parse_action(
        json.dumps(_publish_descriptor()), "publish")
    assert parsed["action"] == "publish"
    assert parsed["media"] == ["project/cover.png"]
    assert browser_tasks.parse_action(
        json.dumps(_export_descriptor()), "publish")["reviewed"] is True

    bad = [
        _publish_descriptor(extra="x"),
        _publish_descriptor(platform="../x"),
        _publish_descriptor(media=["/abs.png"]),
        _publish_descriptor(media=["_login/qr.png"]),
        _publish_descriptor(media=["project/a\\b.png"]),
        _publish_descriptor(media=["project/a.exe"]),
        _publish_descriptor(title="x" * 1001),
        _publish_descriptor(body="x" * 100_001),
        _export_descriptor(reviewed=False),
        _export_descriptor(reviewed=1),
        _export_descriptor(action="publish"),
    ]
    for value in bad:
        with pytest.raises(browser_tasks.BrowserTaskError):
            browser_tasks.parse_action(json.dumps(value), "publish")
    with pytest.raises(browser_tasks.BrowserTaskError):
        browser_tasks.parse_action(
            json.dumps(_publish_descriptor()), "browser")


def test_browser_task_types_include_publish():
    assert "publish" in browser_tasks.BROWSER_TASK_TYPES


def test_media_staging_copies_into_run_dir_and_rejects_escape(outputs):
    source = _write(outputs, "project/cover.png", b"original")
    staged, run_dir = _run(web._stage_managed_media(
        ["project/cover.png"], "run-123"))
    assert len(staged) == 1
    assert staged[0].parent == run_dir / "media"
    assert staged[0].name == "01_cover.png"
    assert staged[0].read_bytes() == b"original"
    # 签名后原文件变化不能改变本次 Run 已快照的媒体。
    source.write_bytes(b"changed")
    assert staged[0].read_bytes() == b"original"

    for relative in ("../outside.png", "_login/qr.png", "project/a.exe"):
        with pytest.raises(web.HTTPException):
            _run(web._stage_managed_media([relative], "run-456"))
    outside = outputs.parent / "outside"
    outside.mkdir()
    (outside / "cover.png").write_bytes(b"outside")
    link = outputs / "linked"
    if not _directory_link(link, outside):
        pytest.skip("当前环境不允许创建目录链接")
    with pytest.raises(web.HTTPException):
        _run(web._stage_managed_media(["linked/cover.png"], "run-789"))


def test_managed_publish_success_records_calendar_once_and_hides_raw_output(
        outputs, monkeypatch):
    _write(outputs, "project/cover.png", b"image")
    calls = []

    async def fake_process(platform, cmd, env, status_file, code_file,
                           timeout, emit):
        calls.append((platform, cmd, status_file, code_file))
        return {"returncode": 0, "status": None, "stderr": "secret-raw"}

    monkeypatch.setattr(web, "_execute_managed_publish", fake_process)
    result = json.loads(_run(web.run_managed_publish(
        _publish_descriptor(), "run-success", lambda *args: None)))
    assert result["state"] == "success"
    assert result["ok"] is True
    assert result["calendarRecorded"] is True
    schedule = json.loads((outputs / "_schedule.json").read_text(
        encoding="utf-8"))
    assert len(schedule) == 1
    assert schedule[0]["status"] == "published"
    assert "secret-raw" not in json.dumps(result, ensure_ascii=False)
    assert not (outputs / "_publish" / "runs" / "run-success").exists()
    assert calls and calls[0][0] == PLATFORM


def test_managed_publish_rejects_unknown_or_unsafe_persona(outputs):
    _write(outputs, "project/cover.png", b"image")
    for persona in ("missing-profile", "../outside"):
        with pytest.raises(web.HTTPException) as error:
            _run(web.run_managed_publish(
                _publish_descriptor(persona=persona), "run-persona",
                lambda *args: None))
        assert error.value.status_code == 400
    assert not (outputs / "_publish" / "runs" / "run-persona").exists()


def test_managed_publish_unverified_is_unknown_not_success(
        outputs, monkeypatch):
    _write(outputs, "project/cover.png", b"image")

    async def fake_process(*args, **kwargs):
        return {"returncode": 5, "status": None}

    monkeypatch.setattr(web, "_execute_managed_publish", fake_process)
    result = json.loads(_run(web.run_managed_publish(
        _publish_descriptor(), "run-unknown", lambda *args: None)))
    assert result["state"] == "unknown"
    assert result["ok"] is False
    assert result.get("calendarRecorded") is not True
    assert not (outputs / "_schedule.json").exists()


def test_managed_publish_definite_failure_is_error_without_calendar(
        outputs, monkeypatch):
    _write(outputs, "project/cover.png", b"image")

    async def fake_process(*args, **kwargs):
        return {"returncode": 1, "status": None}

    monkeypatch.setattr(web, "_execute_managed_publish", fake_process)
    result = json.loads(_run(web.run_managed_publish(
        _publish_descriptor(), "run-error", lambda *args: None)))
    assert result["state"] == "error"
    assert result["ok"] is False
    assert not (outputs / "_schedule.json").exists()


def test_execute_managed_publish_kills_process_tree_on_cancel(
        outputs, monkeypatch):
    import sys
    captured = []
    real_popen = subprocess.Popen

    def capture(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        captured.append(proc)
        return proc

    monkeypatch.setattr(web.subprocess, "Popen", capture)

    async def scenario():
        task = asyncio.create_task(web._execute_managed_publish(
            PLATFORM, [sys.executable, "-c",
                       "import time; time.sleep(60)"],
            dict(os.environ), None, None, 30, lambda *args: None))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run(scenario())
    assert captured and captured[0].poll() is not None


def test_execute_managed_publish_timeout_kills_process(outputs, monkeypatch):
    import sys
    captured = []
    real_popen = subprocess.Popen

    def capture(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        captured.append(proc)
        return proc

    monkeypatch.setattr(web.subprocess, "Popen", capture)
    result = _run(web._execute_managed_publish(
        PLATFORM, [sys.executable, "-c",
                   "import time; time.sleep(60)"],
        dict(os.environ), None, None, 0.3, lambda *args: None))
    assert result["timed_out"] is True
    assert captured and captured[0].poll() is not None


def test_managed_publish_cancellation_cleans_staging_and_code_file(
        outputs, monkeypatch):
    _write(outputs, "project/cover.png", b"image")

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(web, "_execute_managed_publish", cancelled)
    with pytest.raises(asyncio.CancelledError):
        _run(web.run_managed_publish(
            _publish_descriptor(), "run-cancel", lambda *args: None))
    assert not (outputs / "_publish" / "runs" / "run-cancel").exists()


def test_managed_export_writes_only_owned_package(outputs):
    _write(outputs, "project/cover.png", b"image-bytes")
    result = json.loads(_run(web.run_managed_manual_export(
        _export_descriptor(), "run-export", lambda *args: None)))
    assert result["published"] is False
    assert result["path"].startswith("_publish/manual/easel-xiaohongshu-")
    package = outputs / result["path"]
    assert package.is_file()
    with zipfile.ZipFile(package) as archive:
        names = set(archive.namelist())
        assert names == {
            "title.txt", "body.txt", "tags.txt", "manifest.json",
            "使用说明.txt", "media/01_cover.png"}
        assert archive.read("media/01_cover.png") == b"image-bytes"
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["status"] == "ready_for_manual_submission"
        assert "cookie" not in " ".join(names).lower()
        assert "secret-key" not in " ".join(names).lower()


def test_managed_export_failure_leaves_no_partial_package(outputs):
    _write(outputs, "project/cover.png", b"image")
    with pytest.raises(web.HTTPException):
        _run(web.run_managed_manual_export(
            _export_descriptor(media=["project/missing.png"]),
            "run-missing", lambda *args: None))
    assert not list((outputs / "_publish" / "manual").glob("*.zip"))


def test_manifest_and_media_do_not_leak_absolute_paths(outputs):
    _write(outputs, "project/cover.png", b"image")
    result = json.loads(_run(web.run_managed_manual_export(
        _export_descriptor(), "run-manifest", lambda *args: None)))
    with zipfile.ZipFile(outputs / result["path"]) as archive:
        manifest = archive.read("manifest.json").decode("utf-8")
    assert str(outputs) not in manifest
    assert str(outputs.resolve()) not in manifest


def test_export_package_download_is_a_narrow_media_exception(outputs):
    _write(outputs, "project/cover.png", b"image")
    result = json.loads(_run(web.run_managed_manual_export(
        _export_descriptor(), "run-download", lambda *args: None)))
    with TestClient(web.app, base_url="http://localhost:7860") as client:
        response = client.get(f"/api/media/{result['path']}")
        assert response.status_code == 200, response.text
        assert response.content == (outputs / result["path"]).read_bytes()
        # 其他 _publish 文件仍不能从通用媒体接口读取。
        other = outputs / "_publish" / "manual" / "other.zip"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_bytes(b"private")
        blocked = client.get("/api/media/_publish/manual/other.zip")
        assert blocked.status_code == 403, blocked.text


def test_build_package_rejects_export_directory_escaping(outputs):
    outside = outputs.parent / "outside"
    outside.mkdir()
    if not _directory_link(outputs / "_publish", outside):
        pytest.skip("当前环境不允许创建目录链接")
    _write(outputs, "project/cover.png", b"image")
    with pytest.raises(ValueError):
        web.build_package(
            outputs, PLATFORM, "标题", "正文", "", ["project/cover.png"],
            True)
    assert not list(outside.rglob("*.zip"))
