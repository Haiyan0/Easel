"""Task 5C2：账号二维码、凭证保护与精确路由的安全红绿测试。"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

from easel.gateway.app import GatewayError, GatewayLimits, _match_path
from web import app as worker

LIMITS = GatewayLimits()


def _matched(method: str, path: str):
    return _match_path(path.encode("ascii"), method, LIMITS)


def test_gateway_allows_only_exact_account_routes():
    assert _matched("GET", "/api/accounts").path == "/api/accounts"
    assert _matched(
        "GET", "/api/analytics/platforms").path == "/api/analytics/platforms"
    assert _matched(
        "GET", "/api/login/xiaohongshu/status"
    ).path == "/api/login/xiaohongshu/status"
    assert _matched(
        "POST", "/api/login/xiaohongshu/sms"
    ).path == "/api/login/xiaohongshu/sms"
    assert _matched(
        "POST", "/api/logout/xiaohongshu"
    ).path == "/api/logout/xiaohongshu"
    assert _matched(
        "GET", "/api/accounts/xiaohongshu/credentials"
    ).path == "/api/accounts/xiaohongshu/credentials"
    assert _matched(
        "POST", "/api/accounts/xiaohongshu/credentials"
    ).path == "/api/accounts/xiaohongshu/credentials"
    assert _matched(
        "GET", "/api/accounts/xiaohongshu/qr"
    ).path == "/api/accounts/xiaohongshu/qr"
    assert _matched(
        "GET", "/api/accounts/wechat-oa/mp-login/status"
    ).path == "/api/accounts/wechat-oa/mp-login/status"

    with pytest.raises(GatewayError):
        _matched("PUT", "/api/accounts/xiaohongshu/credentials")
    with pytest.raises(GatewayError):
        _matched("POST", "/api/accounts/xiaohongshu/credentials/extra")
    with pytest.raises(GatewayError):
        _matched("GET", "/api/accounts/evil-platform/qr")
    with pytest.raises(GatewayError):
        _matched("GET", "/api/accounts/xiaohongshu/whoami")
    with pytest.raises(GatewayError):
        _matched("GET", "/api/accounts/xiaohongshu/qr/extra")


def test_worker_qr_serves_only_fixed_platform_file(tmp_path, monkeypatch):
    login_dir = tmp_path / "_login"
    login_dir.mkdir()
    png = login_dir / "xiaohongshu.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    monkeypatch.setattr(worker, "LOGIN_DIR", login_dir)
    response = asyncio.run(worker.api_account_qr("xiaohongshu"))
    assert Path(response.path) == png.resolve()
    assert response.media_type == "image/png"

    with pytest.raises(HTTPException) as unknown:
        asyncio.run(worker.api_account_qr("../xiaohongshu"))
    assert unknown.value.status_code == 404
    with pytest.raises(HTTPException) as missing:
        asyncio.run(worker.api_account_qr("zhihu"))
    assert missing.value.status_code == 404

    mp = login_dir / "wechat-oa-mp.png"
    mp.write_bytes(b"\x89PNG\r\n\x1a\nmp")
    mp_response = asyncio.run(worker.api_mp_login_qr("wechat-oa"))
    assert Path(mp_response.path) == mp.resolve()
    with pytest.raises(HTTPException) as wrong_platform:
        asyncio.run(worker.api_mp_login_qr("xiaohongshu"))
    assert wrong_platform.value.status_code == 404


def test_worker_qr_rejects_link_escape(tmp_path, monkeypatch):
    login_dir = tmp_path / "_login"
    login_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "xiaohongshu.png").write_bytes(b"\x89PNG\r\n\x1a\noutside")
    linked = login_dir / "xiaohongshu.png"
    if os.name == "nt":
        # Unix symlink 在无特权 Windows 上通常不可用；用 junction 覆盖同一
        # resolve() 越界判定。
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(linked), str(outside)],
            capture_output=True, text=True)
        if result.returncode != 0:
            pytest.skip("当前环境不允许创建目录 junction")
    else:
        try:
            linked.symlink_to(outside)
        except OSError:
            pytest.skip("当前环境不允许创建 symlink")
    monkeypatch.setattr(worker, "LOGIN_DIR", login_dir)
    with pytest.raises(HTTPException) as rejected:
        asyncio.run(worker.api_account_qr("xiaohongshu"))
    assert rejected.value.status_code == 404


def test_credentials_verify_before_save_preserves_old_config(
        tmp_path, monkeypatch):
    config_path = tmp_path / "wechat.yaml"
    config_path.write_text(
        "accounts:\n  web:\n    name: 旧号\n    app_id: old-app\n"
        "    app_secret: old-secret\n", encoding="utf-8")
    monkeypatch.setattr(worker, "_wechat_config_path", lambda: config_path)
    saved = []
    monkeypatch.setattr(worker, "_wechat_save_credentials",
                        lambda *args, **kwargs: saved.append((args, kwargs)))
    monkeypatch.setattr(worker, "_wechat_verify_credentials",
                        lambda app_id, secret: (False, "invalid_secret"))
    result = worker._verify_and_save_credentials(
        "wechat-oa", "new-app", "new-secret", "新号", "")
    assert result["ok"] is False
    assert saved == []
    text = config_path.read_text(encoding="utf-8")
    assert "old-app" in text and "new-app" not in text
    assert "old-secret" in text and "new-secret" not in text

    monkeypatch.setattr(worker, "_wechat_verify_credentials",
                        lambda app_id, secret: (True, ""))
    result = worker._verify_and_save_credentials(
        "wechat-oa", "new-app", "new-secret", "新号", "")
    assert result["ok"] is True
    assert saved and saved[0][0][:2] == ("new-app", "new-secret")


def test_credentials_handoff_requires_active_run_and_masks_secret(
        tmp_path, monkeypatch):
    config_path = tmp_path / "wechat.yaml"
    monkeypatch.setattr(worker, "_wechat_config_path", lambda: config_path)
    monkeypatch.setattr(worker, "_SERVICE_MODE", "worker")
    monkeypatch.setattr(worker, "_wechat_verify_credentials",
                        lambda app_id, secret: (True, ""))
    monkeypatch.setattr(worker, "_write_login_marker", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_WHOAMI_LOCK", worker._WHOAMI_LOCK)

    class Manager:
        def __init__(self):
            self.committed = False

        def begin_commit(self, run_id):
            self.committed = True
            return True

        def end_commit(self, run_id):
            self.committed = False

        def state(self, run_id):
            return "done"

    monkeypatch.setattr(worker, "_RUNS", Manager())

    request = worker.WechatCredentials(
        appId="wx1234567890", appSecret="super-secret",
        name="公众号", author="", runId="run-cred")
    with pytest.raises(HTTPException) as rejected:
        asyncio.run(worker.api_save_credentials("wechat-oa", request))
    assert rejected.value.status_code == 409
    assert not config_path.exists()

    async def scenario():
        job = asyncio.create_task(
            worker.wait_credentials_job("wechat-oa", "run-cred", 5))
        await asyncio.sleep(0.02)
        response = await worker.api_save_credentials("wechat-oa", request)
        result = await job
        assert response == result
        assert response["ok"] is True
        assert "super-secret" not in response["message"]

    asyncio.run(scenario())
    text = config_path.read_text(encoding="utf-8")
    assert "wx1234567890" in text
    assert "super-secret" in text  # 只允许落在本人受管配置，不回传/进事件

    status = asyncio.run(worker.api_get_credentials("wechat-oa"))
    assert status["configured"] is True
    assert status["appIdMasked"] == "wx1234***7890"
    assert "super-secret" not in json.dumps(status)
