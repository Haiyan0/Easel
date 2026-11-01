"""Task 5C2：worker 受管浏览器/分析执行器与 RunManager 接线红绿测试。"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException

from easel.control.types import DomainError, WorkspacePaths
from easel.worker import browser_tasks
from easel.worker.browser_tasks import BrowserTaskError, execute_browser_task
from easel.worker.config import run_input_digest, sign_run_snapshot
from easel.worker.runs import RunManager
from easel.worker.sessions import WorkspaceSessionStore
from web import app as worker

WORKSPACE = "a" * 32
CLOCK = 1000


def _paths(tmp_path: Path) -> WorkspacePaths:
    paths = WorkspacePaths(
        code_root=tmp_path / "code",
        workspace_root=tmp_path / "workspace",
        state_root=tmp_path / "state",
        home=tmp_path / "home",
    )
    for root in (paths.code_root, paths.workspace_root, paths.state_root,
                 paths.home):
        root.mkdir(parents=True, exist_ok=True)
    return paths


def _request(message: str, task_type: str) -> SimpleNamespace:
    return SimpleNamespace(prompt=message, task_type=task_type)


def _context(task_type: str = "browser", run_id: str = "run-1"):
    return SimpleNamespace(task_type=task_type, run_id=run_id)


def _run(coro):
    return asyncio.run(coro)


def test_parse_action_rejects_unknown_action_platform_and_fields():
    with pytest.raises(BrowserTaskError):
        browser_tasks.parse_action('{"action":"login","platform":"../x"}',
                                   "browser")
    with pytest.raises(BrowserTaskError):
        browser_tasks.parse_action(
            '{"action":"login","platform":"xiaohongshu","script":"x"}',
            "browser")
    with pytest.raises(BrowserTaskError):
        browser_tasks.parse_action(
            '{"action":"analytics","platform":"xiaohongshu"}', "browser")
    with pytest.raises(BrowserTaskError):
        browser_tasks.parse_action(
            '{"action":"login","platform":"wechat-oa"}', "browser")
    assert browser_tasks.parse_action(
        '{"action":"whoami","platform":"xiaohongshu","force":true}',
        "browser")["force"] is True


def test_login_executor_polls_until_success_and_emits_statuses(monkeypatch):
    statuses = iter([
        {"state": "starting", "message": "", "qr": "", "qrTs": 0},
        {"state": "qr_ready", "message": "请扫码",
         "qr": "_login/xiaohongshu.png", "qrTs": 1},
        {"state": "sms_required", "message": "需短信", "qr": "", "qrTs": 0},
        {"state": "success", "message": "登录成功", "qr": "", "qrTs": 0},
    ])
    started = []
    settled = []

    async def start(platform, restart=False):
        started.append((platform, restart))
        return {"mode": "qr", "state": "starting"}

    def status(platform):
        return dict(next(statuses))

    async def settle(process_key):
        settled.append(process_key)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(worker, "_start_login", start)
    monkeypatch.setattr(worker, "_login_status", status)
    monkeypatch.setattr(worker, "settle_login_process", settle)
    monkeypatch.setattr(browser_tasks, "_pause", no_sleep)
    events = []

    async def scenario():
        return await execute_browser_task(
            _request('{"action":"login","platform":"xiaohongshu"}', "browser"),
            _context(), None, lambda kind, text: events.append((kind, text)),
            None)

    result = _run(scenario())
    assert json.loads(result) == {"state": "success", "message": "登录成功"}
    assert started == [("xiaohongshu", False)]
    assert settled == ["xiaohongshu"]
    emitted = [json.loads(text) for kind, text in events
               if kind == "activity"]
    assert [item["state"] for item in emitted] == [
        "starting", "qr_ready", "sms_required", "success"]


def test_login_cancellation_kills_process_tree(monkeypatch):
    cancelled = []

    async def start(platform, restart=False):
        return {"mode": "qr", "state": "starting"}

    def status(platform):
        return {"state": "qr_ready", "message": "", "qr": "x.png", "qrTs": 1}

    async def cancel(kind, platform):
        cancelled.append((kind, platform))

    async def slow_sleep(_seconds):
        await asyncio.sleep(10)

    monkeypatch.setattr(worker, "_start_login", start)
    monkeypatch.setattr(worker, "_login_status", status)
    monkeypatch.setattr(worker, "cancel_browser_task", cancel)
    monkeypatch.setattr(browser_tasks, "_pause", slow_sleep)

    async def scenario():
        task = asyncio.create_task(execute_browser_task(
            _request('{"action":"login","platform":"xiaohongshu"}',
                     "browser"),
            _context(), None, lambda *_: None, None))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run(scenario())
    assert cancelled == [("login", "xiaohongshu")]


def test_login_error_state_fails_the_run(monkeypatch):
    async def start(platform, restart=False):
        return {"mode": "qr", "state": "starting"}

    def status(platform):
        return {"state": "error", "message": "登录程序退出", "qr": "",
                "qrTs": 0}

    monkeypatch.setattr(worker, "_start_login", start)
    monkeypatch.setattr(worker, "_login_status", status)

    async def scenario():
        with pytest.raises(BrowserTaskError) as error:
            await execute_browser_task(
                _request('{"action":"login","platform":"xiaohongshu"}',
                         "browser"),
                _context(), None, lambda *_: None, None)
        assert error.value.code == "browser_login_error"

    _run(scenario())


def test_whoami_returns_only_identity_and_maps_http_errors(monkeypatch):
    async def whoami(platform, force):
        assert (platform, force) == ("xiaohongshu", True)
        return {"loggedIn": True, "name": "昵称",
                "avatar": "https://cdn.test/a.png", "extra": "drop"}

    monkeypatch.setattr(worker, "_check_account_whoami", whoami)

    async def scenario():
        result = await execute_browser_task(
            _request(
                '{"action":"whoami","platform":"xiaohongshu","force":true}',
                "browser"),
            _context(), None, lambda *_: None, None)
        assert json.loads(result) == {
            "loggedIn": True, "name": "昵称",
            "avatar": "https://cdn.test/a.png"}

    _run(scenario())

    async def timeout(platform, force):
        raise HTTPException(504, "timeout")

    monkeypatch.setattr(worker, "_check_account_whoami", timeout)

    async def failing():
        with pytest.raises(BrowserTaskError) as error:
            await execute_browser_task(
                _request(
                    '{"action":"whoami","platform":"xiaohongshu",'
                    '"force":true}', "browser"),
                _context(), None, lambda *_: None, None)
        assert error.value.code == "whoami_timeout"
        assert error.value.status == 504

    _run(failing())


def test_analytics_returns_original_result_and_preserves_failure(monkeypatch):
    payload = {
        "platform": "xiaohongshu", "loggedIn": True,
        "followers": None, "likes": 12, "following": None, "posts": None,
        "metrics": [], "notes": [], "growth": {}, "fetched_at": 1234,
    }

    async def fetch(platform):
        return payload

    monkeypatch.setattr(worker, "_fetch_analytics", fetch)

    async def scenario():
        result = await execute_browser_task(
            _request('{"action":"analytics","platform":"xiaohongshu"}',
                     "analytics"),
            _context("analytics"), None, lambda *_: None, None)
        assert json.loads(result) == payload

    _run(scenario())

    async def failed(platform):
        raise HTTPException(502, "平台失败")

    monkeypatch.setattr(worker, "_fetch_analytics", failed)

    async def failing():
        with pytest.raises(BrowserTaskError) as error:
            await execute_browser_task(
                _request('{"action":"analytics","platform":"xiaohongshu"}',
                         "analytics"),
                _context("analytics"), None, lambda *_: None, None)
        assert error.value.code == "analytics_failed"

    _run(failing())


def test_credentials_executor_waits_for_handoff_and_never_gets_secret(
        monkeypatch):
    seen = []

    async def wait(platform, run_id, timeout=45):
        seen.append((platform, run_id, timeout))
        return {"ok": False, "message": "验证未通过", "secret": "drop"}

    monkeypatch.setattr(worker, "wait_credentials_job", wait)

    async def scenario():
        result = await execute_browser_task(
            _request('{"action":"credentials","platform":"wechat-oa"}',
                     "browser"),
            _context(run_id="run-cred"), None, lambda *_: None, None)
        assert json.loads(result) == {
            "ok": False, "message": "验证未通过"}

    _run(scenario())
    assert seen == [("wechat-oa", "run-cred", 45)]


def _sign(key, *, run_id, session_id, message, task_type,
          agent, revision=0) -> dict:
    digest = run_input_digest(
        conversation_id=session_id, task_type=task_type, message=message,
        attachments=[], persona=None)
    return sign_run_snapshot(
        key, workspace_id=WORKSPACE, run_id=run_id,
        conversation_id=session_id, input_digest=digest,
        task_type=task_type, revision=revision, routes={}, agent=agent,
        expires_at=CLOCK + 60, nonce="nonce-" + run_id)


def test_run_manager_uses_browser_executor_without_agent_runtime(tmp_path):
    paths = _paths(tmp_path)
    key = Ed25519PrivateKey.generate()
    calls = []

    async def browser_executor(request, context, bind, emit, ask):
        calls.append((request, context))
        emit("activity", "browser-task")
        return '{"ok":true}'

    manager = RunManager(
        workspace_id=WORKSPACE, paths=paths, public_key=key.public_key(),
        sessions=WorkspaceSessionStore(paths.state_root, WORKSPACE),
        executor=lambda *_: (_ for _ in ()).throw(
            AssertionError("chat executor must not run")),
        browser_executor=browser_executor,
        base_env={"PATH": "", "HOME": str(paths.home)},
        clock=lambda: CLOCK, recover=False)
    message = '{"action":"whoami","platform":"xiaohongshu","force":false}'
    envelope = _sign(
        key, run_id="run-browser", session_id="conversation-browser",
        message=message, task_type="browser",
        agent={"timeout": 90})
    request = {
        "sessionId": "conversation-browser",
        "turnId": "turn-browser",
        "message": message,
        "attachments": [],
        "persona": None,
        "taskType": "browser",
        "inputDigest": envelope["input_digest"],
    }

    async def scenario():
        admitted = await manager.submit("run-browser", envelope, request)
        assert admitted["state"] == "running"
        deadline = time.monotonic() + 5
        while manager.state("run-browser") not in (
                "done", "failed", "cancelled", "interrupted"):
            if time.monotonic() > deadline:
                raise AssertionError("browser run did not finish")
            await asyncio.sleep(0.01)
        assert manager.state("run-browser") == "done", manager.events(
            "run-browser", after=0)
        assert len(calls) == 1
        assert calls[0][1].task_type == "browser"
        assert calls[0][0].prompt == message
        messages = manager.messages(
            "conversation-browser", limit=10, offset=0)
        assert messages["messages"][-1]["content"] == '{"ok":true}'

    _run(scenario())


def test_run_manager_rejects_persona_for_browser_task(tmp_path):
    paths = _paths(tmp_path)
    key = Ed25519PrivateKey.generate()

    async def browser_executor(*_args):
        return "{}"

    manager = RunManager(
        workspace_id=WORKSPACE, paths=paths, public_key=key.public_key(),
        sessions=WorkspaceSessionStore(paths.state_root, WORKSPACE),
        executor=browser_executor, browser_executor=browser_executor,
        base_env={"PATH": "", "HOME": str(paths.home)},
        clock=lambda: CLOCK, recover=False)
    message = '{"action":"whoami","platform":"xiaohongshu","force":false}'
    envelope = _sign(
        key, run_id="run-persona", session_id="conversation-persona",
        message=message, task_type="browser", agent={"timeout": 90})
    request = {
        "sessionId": "conversation-persona",
        "turnId": "turn-persona",
        "message": message,
        "attachments": [],
        "persona": "画像",
        "taskType": "browser",
        "inputDigest": envelope["input_digest"],
    }

    async def scenario():
        with pytest.raises(DomainError):
            await manager.submit("run-persona", envelope, request)

    _run(scenario())
