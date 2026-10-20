"""Task 7：浏览器 profile 的 workspace 隔离、标识校验与跨进程锁。"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import web.app as web
from easel.control.types import WorkspacePaths
from easel.worker.browser import BrowserProfiles, ProfileLock
from easel.worker.guard import WorkerIdentity
from browser_state import launch_persistent_locked

REPO = Path(__file__).resolve().parents[2]


def profiles(tmp_path: Path, name: str) -> BrowserProfiles:
    root = tmp_path / name
    return BrowserProfiles(WorkspacePaths(
        code_root=tmp_path / "code", workspace_root=root / "workspace",
        state_root=root / "state", home=root / "home"))


def test_browser_profiles_are_scoped_to_workspace(tmp_path):
    a, b = profiles(tmp_path, "a"), profiles(tmp_path, "b")
    assert a.profile_dir("xiaohongshu", "default") != \
        b.profile_dir("xiaohongshu", "default")
    assert a.profile_dir("xiaohongshu", "default").is_relative_to(
        tmp_path / "a" / "home" / ".easel-browser-profiles")
    assert a.profile_dir("xiaohongshu", "default") != \
        a.profile_dir("xiaohongshu", "second")


@pytest.mark.parametrize(("platform", "account_id"), [
    ("../escape", "default"),
    ("xiaohongshu", "../escape"),
    ("/abs", "default"),
    ("xiaohongshu", "/abs"),
    ("a/b", "default"),
    ("xiaohongshu", "a\\b"),
    ("xiaohongshu", "."),
    ("xiaohongshu", ".."),
    ("xiaohongshu", ".hidden"),
    ("xiaohongshu", "a\0b"),
])
def test_profile_dir_rejects_unsafe_identifiers(tmp_path, platform,
                                                account_id):
    manager = profiles(tmp_path, "a")
    with pytest.raises(ValueError):
        manager.profile_dir(platform, account_id)
    assert not (tmp_path / "a" / "home").exists()


def test_persistent_profile_creates_private_directory_and_releases(tmp_path):
    manager = profiles(tmp_path, "a")
    with manager.persistent_profile("xiaohongshu", "default",
                                    timeout=5) as profile:
        assert profile.is_dir()
        assert (profile.parent / ".locks"
                / f"{profile.name}.lock").is_file()
    # 释放后可再次获取
    with manager.persistent_profile("xiaohongshu", "default", timeout=1):
        pass


HOLDER = r"""
import sys, time
from pathlib import Path
from easel.control.types import WorkspacePaths
from easel.worker.browser import BrowserProfiles
paths = WorkspacePaths(Path({code!r}), Path({workspace!r}),
                       Path({state!r}), Path({home!r}))
manager = BrowserProfiles(paths)
with manager.persistent_profile("xiaohongshu", "default", timeout=5):
    print("HELD", flush=True)
    time.sleep(30)
"""


def test_profile_lock_is_cross_process(tmp_path):
    manager = profiles(tmp_path, "a")
    paths = manager.paths
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO)
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER.format(
            code=str(paths.code_root), workspace=str(paths.workspace_root),
            state=str(paths.state_root), home=str(paths.home))],
        cwd=str(REPO), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        assert holder.stdout.readline().strip() == "HELD"
        with pytest.raises(TimeoutError):
            with manager.persistent_profile("xiaohongshu", "default",
                                            timeout=0.5):
                pass
    finally:
        holder.terminate()
        holder.wait(timeout=10)
    with manager.persistent_profile("xiaohongshu", "default", timeout=5):
        pass


def test_waiting_timeout_does_not_release_the_holder_lock(tmp_path):
    manager = profiles(tmp_path, "a")
    with manager.persistent_profile("xiaohongshu", "default", timeout=5):
        result = {}

        def waiter():
            try:
                with manager.persistent_profile("xiaohongshu", "default",
                                                timeout=0.4):
                    result["acquired"] = True
            except TimeoutError:
                result["acquired"] = False

        thread = threading.Thread(target=waiter)
        thread.start()
        thread.join(timeout=10)
        assert result == {"acquired": False}
        # 等待者超时退出后，持有者仍然独占
        with pytest.raises(TimeoutError):
            with manager.persistent_profile("xiaohongshu", "default",
                                            timeout=0.2):
                pass
    with manager.persistent_profile("xiaohongshu", "default", timeout=5):
        pass


HEADERS = {"X-Easel-Workspace-Id": "ws-a",
           "X-Easel-Service-Token": "token-a"}


def _logout_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    profiles = home / ".easel-browser-profiles"
    paths = WorkspacePaths(
        code_root=tmp_path / "code", workspace_root=tmp_path / "workspace",
        state_root=tmp_path / "state", home=home)
    monkeypatch.setattr(web, "_SERVICE_MODE", "worker")
    monkeypatch.setattr(web, "_WORKER_IDENTITY",
                        WorkerIdentity("ws-a", "token-a"), raising=False)
    monkeypatch.setattr(web, "BROWSER_PROFILES", profiles)
    monkeypatch.setattr(web, "LOGIN_DIR", tmp_path / "outputs" / "_login")
    monkeypatch.setattr(web, "LOGIN_PROCESSES", {})
    monkeypatch.setattr(web, "_WHOAMI_CACHE", {})
    return paths, profiles


def _start_holder(paths: WorkspacePaths) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO)
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER.format(
            code=str(paths.code_root), workspace=str(paths.workspace_root),
            state=str(paths.state_root), home=str(paths.home))],
        cwd=str(REPO), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8")
    assert holder.stdout.readline().strip() == "HELD"
    return holder


def test_http_logout_waits_for_profile_lock_and_reports_busy(
        tmp_path, monkeypatch):
    paths, profiles = _logout_env(tmp_path, monkeypatch)
    profile = profiles / "xiaohongshu" / "default"
    profile.mkdir(parents=True)
    sentinel = profile / "cookie-sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    other = profiles / "douyin" / "default"
    other.mkdir(parents=True)
    other_sentinel = other / "cookie-sentinel.txt"
    other_sentinel.write_text("other", encoding="utf-8")

    holder = _start_holder(paths)
    try:
        with TestClient(web.app, base_url="http://127.0.0.1:7860",
                        client=("203.0.113.10", 50000)) as client:
            busy = client.post("/api/logout/xiaohongshu", headers=HEADERS)
            assert busy.status_code == 409, busy.text
            assert sentinel.is_file(), "持锁期间不得删除账号 sentinel"
            assert profile.is_dir()
    finally:
        holder.terminate()
        holder.wait(timeout=10)

    with TestClient(web.app, base_url="http://127.0.0.1:7860",
                    client=("203.0.113.10", 50000)) as client:
        done = client.post("/api/logout/xiaohongshu", headers=HEADERS)
    assert done.status_code == 200, done.text
    assert done.json()["ok"] is True
    assert not profile.exists()
    assert other_sentinel.is_file(), "A 注销不得影响 B"
    lock_file = profile.parent / ".locks" / f"{profile.name}.lock"
    assert lock_file.is_file(), "锁文件必须独立于 profile 目录存活"


def test_wechat_logout_waits_for_login_process_exit_before_delete(
        tmp_path, monkeypatch):
    _, profiles = _logout_env(tmp_path, monkeypatch)
    profile = profiles / "wechat-oa" / "default"
    profile.mkdir(parents=True)
    (profile / "cookie-sentinel.txt").write_text("keep", encoding="utf-8")
    events: list[str] = []

    class FakeProc:
        def poll(self):
            return None

        def terminate(self):
            events.append("terminate")

        def wait(self, timeout=None):
            events.append("wait")
            return 0

    real_rmtree = web.shutil.rmtree

    def tracked_rmtree(path, *args, **kwargs):
        events.append("rmtree")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setitem(web.LOGIN_PROCESSES, "wechat-oa-mp", FakeProc())
    monkeypatch.setattr(web.shutil, "rmtree", tracked_rmtree)
    monkeypatch.setattr(web, "_wechat_clear_credentials",
                        lambda: events.append("clear"))

    result = asyncio.run(web.api_logout("wechat-oa"))

    assert result["ok"] is True
    assert events.index("wait") < events.index("rmtree") < events.index("clear")
    assert not profile.exists()


def test_wechat_logout_process_exit_timeout_does_not_delete(
        tmp_path, monkeypatch):
    _, profiles = _logout_env(tmp_path, monkeypatch)
    profile = profiles / "wechat-oa" / "default"
    profile.mkdir(parents=True)
    sentinel = profile / "cookie-sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    cleared: list[int] = []

    class StuckProc:
        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            raise web.subprocess.TimeoutExpired("login", timeout)

    monkeypatch.setitem(web.LOGIN_PROCESSES, "wechat-oa-mp", StuckProc())
    monkeypatch.setattr(web, "_wechat_clear_credentials",
                        lambda: cleared.append(1))

    with pytest.raises(web.HTTPException) as error:
        asyncio.run(web.api_logout("wechat-oa"))
    assert error.value.status_code == 409
    assert sentinel.is_file()
    assert cleared == []


def test_http_logout_delete_failure_does_not_report_success(
        tmp_path, monkeypatch):
    paths, profiles = _logout_env(tmp_path, monkeypatch)
    profile = profiles / "xiaohongshu" / "default"
    profile.mkdir(parents=True)
    sentinel = profile / "cookie-sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")

    def failing_rmtree(*args, **kwargs):
        raise PermissionError("locked file")

    monkeypatch.setattr(web.shutil, "rmtree", failing_rmtree)

    with TestClient(web.app, base_url="http://127.0.0.1:7860",
                    client=("203.0.113.10", 50000)) as client:
        response = client.post("/api/logout/xiaohongshu", headers=HEADERS)
    assert response.status_code >= 400, response.text
    assert '"ok": true' not in response.text
    assert sentinel.is_file()
    # 删除失败也必须释放锁，后续任务可重试
    with BrowserProfiles(paths).persistent_profile(
            "xiaohongshu", "default", timeout=1):
        pass


def test_lock_is_not_released_before_context_close_finishes(tmp_path):
    profile = tmp_path / "home" / ".easel-browser-profiles" / "test" / "default"
    profile.mkdir(parents=True)
    started = threading.Event()
    finished = threading.Event()

    class SlowContext:
        def close(self):
            started.set()
            time.sleep(0.4)
            finished.set()

    browser = launch_persistent_locked(profile, SlowContext, timeout=1)
    closer = threading.Thread(target=browser.close)
    closer.start()
    try:
        assert started.wait(2)
        # close 尚未完成：另一个使用者不能取得锁
        with pytest.raises(TimeoutError):
            waited = ProfileLock(profile)
            waited.acquire(0.1)
    finally:
        closer.join(timeout=5)
    assert finished.is_set()
    # close 完成后锁可重新获取
    lock = ProfileLock(profile)
    lock.acquire(0.5)
    lock.release()


def test_exception_inside_persistent_profile_releases_lock(tmp_path):
    manager = profiles(tmp_path, "a")
    with pytest.raises(RuntimeError):
        with manager.persistent_profile("xiaohongshu", "default", timeout=5):
            raise RuntimeError("boom")
    with manager.persistent_profile("xiaohongshu", "default", timeout=1):
        pass
