import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

import web.app as web


def patch_run(monkeypatch, fake):
    """把 subprocess.run 替身接到受管子进程入口，保持原断言/返回值形状。"""
    async def runner(kind, platform, cmd, env, timeout):
        return await asyncio.to_thread(
            fake, cmd, cwd=str(web.PROJECT_ROOT), env=env,
            timeout=timeout, capture_output=True, text=True,
            encoding="utf-8")
    monkeypatch.setattr(web, "_run_tracked_subprocess", runner)


@pytest.fixture
def account_state(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "LOGIN_DIR", tmp_path)
    monkeypatch.setattr(web, "LOGIN_PROCESSES", {})
    monkeypatch.setattr(web, "_WHOAMI_CACHE", {})
    monkeypatch.setattr(web, "_WHOAMI_LOCK", threading.Lock())
    monkeypatch.setattr(web, "_WHOAMI_TASKS", {})
    return tmp_path


def test_whoami_does_not_start_browser_during_login(account_state, monkeypatch):
    web.LOGIN_PROCESSES["xiaohongshu"] = SimpleNamespace(poll=lambda: None)
    patch_run(monkeypatch,lambda *args, **kwargs: pytest.fail("login owns profile"))
    result = asyncio.run(web.api_account_whoami("xiaohongshu"))
    assert not result["loggedIn"]
    assert not web._WHOAMI_CACHE


def test_whoami_waits_for_successful_login_to_release_profile(account_state, monkeypatch):
    web._write_login_marker("weixin-channels", "success")
    events = []
    process = SimpleNamespace(poll=lambda: None if not events else 0,
                              wait=lambda **kwargs: events.append("released"))
    web.LOGIN_PROCESSES["weixin-channels"] = process
    def run(*args, **kwargs):
        assert events == ["released"]
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "loggedIn": True, "name": "Own channel", "avatar": "https://self.test/channel.png",
        }))

    patch_run(monkeypatch,run)
    result = asyncio.run(web.api_account_whoami("weixin-channels", force=True))
    assert result["name"] == "Own channel"
    assert result["avatar"] == "https://self.test/channel.png"


def test_whoami_reports_profile_save_timeout_without_caching_empty_identity(account_state, monkeypatch):
    web._write_login_marker("weixin-channels", "success")
    def wait(**kwargs):
        raise web.subprocess.TimeoutExpired("login", 8)

    web.LOGIN_PROCESSES["weixin-channels"] = SimpleNamespace(poll=lambda: None, wait=wait)
    patch_run(monkeypatch,lambda *args, **kwargs: pytest.fail("profile still locked"))
    with pytest.raises(web.HTTPException) as error:
        asyncio.run(web.api_account_whoami("weixin-channels", force=True))
    assert error.value.status_code == 409
    assert not web._WHOAMI_CACHE


def test_mp_whoami_reads_backend_account_not_configured_label(account_state, monkeypatch):
    web._write_login_marker("wechat-oa-mp", "success")
    monkeypatch.setattr(web, "_wechat_web_account", lambda: {"name": "Configured label"})
    def run(command, **kwargs):
        assert command[-2:] == ["whoami", "--no-proxy"]
        assert command[1].endswith("weixin_mp_stats.py")
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "loggedIn": True, "name": "Actual MP account", "avatar": "https://self.test/mp.png",
        }))

    patch_run(monkeypatch,run)
    result = asyncio.run(web.api_account_whoami("wechat-oa", force=True))
    assert result["name"] == "Actual MP account"
    assert result["avatar"] == "https://self.test/mp.png"
    assert (account_state / "wechat-oa-mp.json").exists()
    assert not (account_state / "wechat-oa.json").exists()


def test_mp_expired_session_can_use_appid_without_faking_backend_login(account_state, monkeypatch):
    web._write_login_marker("wechat-oa-mp", "success")
    monkeypatch.setattr(web, "_wechat_has_credentials", lambda: True)
    monkeypatch.setattr(web, "_wechat_web_account", lambda: {"name": "Configured label"})
    patch_run(monkeypatch,lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout='{"loggedIn":false,"name":"","avatar":""}'))
    result = asyncio.run(web.api_account_whoami("wechat-oa", force=True))
    assert result == {"loggedIn": True, "name": "Configured label", "avatar": ""}
    assert not (account_state / "wechat-oa-mp.json").exists()


def test_douyin_whoami_does_not_use_model_proxy(account_state, monkeypatch):
    def run(command, **kwargs):
        assert command[-2:] == ["whoami", "--no-proxy"]
        return SimpleNamespace(returncode=0, stdout='{"loggedIn":false,"name":"","avatar":""}')

    patch_run(monkeypatch,run)
    assert not asyncio.run(web.api_account_whoami("douyin", force=True))["loggedIn"]


def test_whoami_network_error_preserves_success_marker(account_state, monkeypatch):
    web._write_login_marker("xiaohongshu", "success")
    initial = (account_state / "xiaohongshu.json").read_bytes()
    patch_run(monkeypatch,lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps({"loggedIn": False, "error": "maintenance"})))
    with pytest.raises(web.HTTPException) as error:
        asyncio.run(web.api_account_whoami("xiaohongshu"))
    assert error.value.status_code == 502
    assert (account_state / "xiaohongshu.json").read_bytes() == initial
    assert not web._WHOAMI_CACHE


def test_whoami_result_before_new_login_cannot_remove_success_marker(account_state, monkeypatch):
    def run(*args, **kwargs):
        web._write_login_marker("xiaohongshu", "success")
        return SimpleNamespace(returncode=0, stdout='{"loggedIn": false}')

    patch_run(monkeypatch,run)
    assert asyncio.run(web.api_account_whoami("xiaohongshu"))["loggedIn"]
    assert (account_state / "xiaohongshu.json").exists()
    assert not web._WHOAMI_CACHE


def test_whoami_invalid_boolean_is_not_a_verified_logout(account_state, monkeypatch):
    web._write_login_marker("xiaohongshu", "success")
    patch_run(monkeypatch,lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout='{"loggedIn": "false"}'))
    with pytest.raises(web.HTTPException) as error:
        asyncio.run(web.api_account_whoami("xiaohongshu"))
    assert error.value.status_code == 502
    assert not web._WHOAMI_CACHE


def test_unknown_account_identity_is_not_returned_as_verified_logout(account_state, monkeypatch):
    patch_run(monkeypatch,lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout='{"loggedIn":false,"name":"","avatar":"","error":"not ready"}'))
    with pytest.raises(web.HTTPException) as error:
        asyncio.run(web.api_account_whoami("weixin-channels", force=True))
    assert error.value.status_code == 502
    assert not web._WHOAMI_CACHE
    assert not (account_state / "weixin-channels.json").exists()


def test_unknown_account_identity_preserves_last_verified_identity(account_state, monkeypatch):
    web._write_login_marker("weixin-channels", "success")
    identity = {"loggedIn": True, "name": "Own channel", "avatar": "https://self.test/channel.png"}
    web._WHOAMI_CACHE["weixin-channels"] = (web.time.time(), identity)
    initial = (account_state / "weixin-channels.json").read_bytes()
    patch_run(monkeypatch,lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout='{"loggedIn":false,"error":"not ready"}'))
    with pytest.raises(web.HTTPException) as error:
        asyncio.run(web.api_account_whoami("weixin-channels", force=True))
    assert error.value.status_code == 502
    assert web._WHOAMI_CACHE["weixin-channels"][1] == identity
    assert (account_state / "weixin-channels.json").read_bytes() == initial


def test_manual_whoami_bypasses_server_cache(account_state, monkeypatch):
    calls = []
    web._WHOAMI_CACHE["xiaohongshu"] = (web.time.time(), {
        "loggedIn": False, "name": "", "avatar": ""})

    def run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='{"loggedIn": true}')

    patch_run(monkeypatch,run)
    assert not asyncio.run(web.api_account_whoami("xiaohongshu"))["loggedIn"]
    assert not calls
    assert asyncio.run(web.api_account_whoami("xiaohongshu", force=True))["loggedIn"]
    assert len(calls) == 1
    assert (account_state / "xiaohongshu.json").exists()


def test_confident_logout_removes_expired_marker(account_state, monkeypatch):
    web._write_login_marker("xiaohongshu", "success")
    patch_run(monkeypatch,lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout='{"loggedIn": false}'))
    assert not asyncio.run(web.api_account_whoami("xiaohongshu"))["loggedIn"]
    assert not (account_state / "xiaohongshu.json").exists()
    assert not web._WHOAMI_CACHE["xiaohongshu"][1]["loggedIn"]


def test_concurrent_account_checks_share_one_browser(account_state, monkeypatch):
    calls = []
    entered = threading.Event()
    release = threading.Event()
    def run(*args, **kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(timeout=2)
        return SimpleNamespace(returncode=0, stdout='{"loggedIn":true,"name":"Me","avatar":""}')

    patch_run(monkeypatch,run)
    async def check():
        first = asyncio.create_task(web.api_account_whoami("xiaohongshu"))
        assert await asyncio.to_thread(entered.wait, 2)
        second = asyncio.create_task(web.api_account_whoami("xiaohongshu", force=True))
        await asyncio.sleep(0)
        release.set()
        return await asyncio.gather(first, second)

    results = asyncio.run(check())
    assert len(calls) == 1
    assert calls[0]["timeout"] == 35
    assert calls[0]["encoding"] == "utf-8"
    assert results[0] == results[1]
    assert not web._WHOAMI_TASKS


def test_incomplete_identity_does_not_stay_cached_for_ten_minutes(account_state, monkeypatch):
    web._write_login_marker("xiaohongshu", "success")
    web._WHOAMI_CACHE["xiaohongshu"] = (web.time.time() - 61, {
        "loggedIn": True, "name": "", "avatar": ""})
    patch_run(monkeypatch,lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout='{"loggedIn":true,"name":"Me","avatar":"https://self.test/avatar"}'))
    result = asyncio.run(web.api_account_whoami("xiaohongshu"))
    assert result["name"] == "Me"
    assert result["avatar"]
