"""Task 7：浏览器 profile 的 workspace 隔离、标识校验与跨进程锁。"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from easel.control.types import WorkspacePaths
from easel.worker.browser import BrowserProfiles

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
        assert (profile / ".easel.lock").is_file()
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


def test_exception_inside_persistent_profile_releases_lock(tmp_path):
    manager = profiles(tmp_path, "a")
    with pytest.raises(RuntimeError):
        with manager.persistent_profile("xiaohongshu", "default", timeout=5):
            raise RuntimeError("boom")
    with manager.persistent_profile("xiaohongshu", "default", timeout=1):
        pass
