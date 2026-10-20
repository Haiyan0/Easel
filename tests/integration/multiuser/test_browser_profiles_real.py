"""Task 7 真实 Chromium：临时 context 隔离、持久 profile 保留与锁释放。"""
from __future__ import annotations

from pathlib import Path

import pytest

from easel.control.types import WorkspacePaths
from easel.worker.browser import BrowserProfiles

playwright = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright.sync_playwright


def _launch_or_skip(p):
    try:
        return p.chromium.launch(headless=True)
    except Exception as error:  # noqa: BLE001
        pytest.skip(f"Chromium 不可用：{error}")


def _profiles(tmp_path: Path) -> BrowserProfiles:
    root = tmp_path / "workspace"
    return BrowserProfiles(WorkspacePaths(
        code_root=tmp_path / "code", workspace_root=root,
        state_root=root / "state", home=tmp_path / "home"))


def test_two_temporary_contexts_do_not_share_cookies():
    with sync_playwright() as p:
        browser = _launch_or_skip(p)
        try:
            first = browser.new_context()
            second = browser.new_context()
            try:
                first.add_cookies([{"name": "session", "value": "a",
                                    "domain": "example.test", "path": "/"}])
                second.add_cookies([{"name": "session", "value": "b",
                                     "domain": "example.test", "path": "/"}])
                assert first.cookies()[0]["value"] == "a"
                assert second.cookies()[0]["value"] == "b"
            finally:
                first.close()
                second.close()
        finally:
            browser.close()


def test_persistent_profile_keeps_cookie_across_restart(tmp_path):
    manager = _profiles(tmp_path)
    with sync_playwright() as p:
        browser = _launch_or_skip(p)
        browser.close()
        with manager.persistent_profile("xiaohongshu", "default",
                                        timeout=10) as profile:
            context = p.chromium.launch_persistent_context(
                str(profile), headless=True)
            try:
                context.add_cookies([{
                    "name": "persisted", "value": "yes",
                    "domain": "example.test", "path": "/",
                    "expires": 4102444800}])
            finally:
                context.close()
        with manager.persistent_profile("xiaohongshu", "default",
                                        timeout=10) as profile:
            context = p.chromium.launch_persistent_context(
                str(profile), headless=True)
            try:
                cookies = {cookie["name"]: cookie["value"]
                           for cookie in context.cookies()}
                assert cookies.get("persisted") == "yes"
            finally:
                context.close()


def test_exception_while_browser_open_releases_profile_lock(tmp_path):
    manager = _profiles(tmp_path)
    with sync_playwright() as p:
        browser = _launch_or_skip(p)
        browser.close()
        with pytest.raises(RuntimeError):
            with manager.persistent_profile("xiaohongshu", "default",
                                            timeout=10) as profile:
                context = p.chromium.launch_persistent_context(
                    str(profile), headless=True)
                try:
                    raise RuntimeError("boom")
                finally:
                    context.close()
        with manager.persistent_profile("xiaohongshu", "default", timeout=2):
            pass
