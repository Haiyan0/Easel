"""Task 1：受管聊天失败诊断的确定性验收。

边界：

* 只使用假 ACP 子进程、假 Key/假 broker token；不连接真实供应商、不触碰
  用户运行中的容器/卷/邀请/Cookie。
* 阶段必须可区分：子进程启动、initialize、session、model、prompt、rpc。
* 只有 ACP 错误对象里的结构化字段（data.status / data.code）才能分类；
  message 文本再像鉴权错误也只能落到 unknown。
* stderr 只保留固定上限并持续排空；大输出/无换行/取消后不得留下任务。
* worker 的 error 事件仍是字符串，包含安全中文阶段/类别与 runId，且不泄漏
  prompt、Key、broker token、用户路径或上游原文。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from easel.harnesses import rpc as transport
from easel.harnesses.acp import ACPAdapter
from easel.harnesses.base import HarnessError, RunRequest
from easel.worker.runs import RunManager
from tests.multiuser.test_runs import (
    CLOCK,
    WORKSPACE,
    make_paths,
    request_for,
    sign,
    wait_until,
)

FIXTURE = (Path(__file__).resolve().parents[1]
           / "fixtures" / "harness_diagnostics.py")

BROKER_SENTINEL = "brk-task1-sentinel-9f3c"
PROMPT_SENTINEL = "prompt-task1-sentinel-7a1"
KEY_SENTINEL = "key-task1-sentinel-2b8d"
STDERR_TOTAL = 65536


def _command(tmp_path, mode, *, audit_name=None, status=None,
             error_code=None, error_message=None, stderr_bytes=0):
    audit = tmp_path / (audit_name or f"audit-{mode}.jsonl")
    command = [sys.executable, str(FIXTURE), "--mode", mode,
               "--audit", str(audit)]
    if status is not None:
        command += ["--status", str(status)]
    if error_code is not None:
        command += ["--error-code", error_code]
    if error_message is not None:
        command += ["--error-message", error_message]
    if stderr_bytes:
        command += ["--stderr-bytes", str(stderr_bytes)]
    return command


def _adapter_request(tmp_path, mode, *, model="", **options):
    env = {**os.environ, "OPENAI_API_KEY": KEY_SENTINEL}
    return RunRequest("hello", tmp_path, env, "", model, "read-only", 20,
                      tuple(_command(tmp_path, mode, **options)))


async def _reject(questions):
    raise AssertionError("本测试不应请求权限")


async def _run_adapter(request):
    await ACPAdapter().run(request, lambda *args: None, _reject,
                           lambda identifier: None)


def _adapter_failure(request):
    with pytest.raises(HarnessError) as caught:
        asyncio.run(_run_adapter(request))
    return caught.value


def _worker_manager(tmp_path, key, executor=None):
    paths = make_paths(tmp_path)
    return RunManager(
        workspace_id=WORKSPACE, paths=paths,
        public_key=key.public_key(), executor=executor,
        base_env=dict(os.environ), clock=lambda: CLOCK,
        recover=False, shutdown_grace=0.2)


def _worker_agent(tmp_path, mode, **options):
    return {"runtime": "acp",
            "command": _command(tmp_path, mode, **options),
            "sandbox": "read-only", "timeout": 30}


def _error_event(manager, run_id):
    events = manager.events(run_id, after=0)["events"]
    errors = [event for event in events if event["event"] == "error"]
    assert len(errors) == 1, errors
    return errors[0]["data"]


# ---------------------------------------------------------------------------
# 适配器/传输层：阶段与结构化证据
# ---------------------------------------------------------------------------


def test_acp_missing_cli_reports_start_stage(tmp_path):
    broken = tmp_path / ("broken.exe" if os.name == "nt" else "broken")
    broken.write_bytes(b"not-an-executable")
    broken.chmod(0o755)
    request = RunRequest("hello", tmp_path, dict(os.environ), "",
                         "", "read-only", 10, (str(broken),))
    error = _adapter_failure(request)
    assert getattr(error, "stage", None) == "cli_start"
    assert getattr(error, "category", "unknown") in (None, "unknown")


def test_acp_initialize_structured_auth_is_classified(tmp_path):
    request = _adapter_request(tmp_path, "initialize-error", status=401)
    error = _adapter_failure(request)
    assert getattr(error, "stage", None) == "initialize"
    assert getattr(error, "category", None) == "auth"
    assert getattr(error, "status", None) == 401


def test_acp_session_structured_rejection_is_classified(tmp_path):
    request = _adapter_request(tmp_path, "session-error",
                               error_code="invalid_request")
    error = _adapter_failure(request)
    assert getattr(error, "stage", None) == "session"
    assert getattr(error, "category", None) == "request_rejected"
    assert getattr(error, "status", None) is None


def test_acp_model_structured_rate_limit_is_classified(tmp_path):
    request = _adapter_request(tmp_path, "model-error", model="model-a",
                               status=429)
    error = _adapter_failure(request)
    assert getattr(error, "stage", None) == "model"
    assert getattr(error, "category", None) == "rate_limited"
    assert getattr(error, "status", None) == 429


def test_acp_prompt_structured_unavailable_is_classified(tmp_path):
    request = _adapter_request(tmp_path, "prompt-error", status=503)
    error = _adapter_failure(request)
    assert getattr(error, "stage", None) == "prompt"
    assert getattr(error, "category", None) == "service_unavailable"
    assert getattr(error, "status", None) == 503


def test_acp_text_only_failure_is_not_guessed(tmp_path):
    request = _adapter_request(
        tmp_path, "prompt-error",
        error_message="HTTP 401 unauthorized invalid api key")
    error = _adapter_failure(request)
    assert getattr(error, "stage", None) == "prompt"
    assert getattr(error, "category", "unknown") in (None, "unknown")
    assert getattr(error, "status", None) is None
    assert KEY_SENTINEL not in str(error)


def test_acp_prompt_exit_reports_prompt_stage(tmp_path):
    request = _adapter_request(tmp_path, "exit-at-prompt")
    error = _adapter_failure(request)
    assert getattr(error, "stage", None) == "prompt"
    assert getattr(error, "category", "unknown") in (None, "unknown")


def test_rpc_malformed_frame_reports_rpc_stage(tmp_path):
    async def scenario():
        request = RunRequest("", tmp_path, dict(os.environ))
        async with transport.RPC(
                request, _command(tmp_path, "malformed-idle"),
                lambda *args: None, _reject) as rpc:
            await rpc.send({"id": 7, "method": "start"})
            pending = asyncio.get_running_loop().create_future()
            with pytest.raises(HarnessError) as caught:
                await asyncio.wait_for(rpc.wait_notification(pending), 10)
        return caught.value

    error = asyncio.run(scenario())
    assert getattr(error, "stage", None) == "rpc"
    assert getattr(error, "category", "unknown") in (None, "unknown")


# ---------------------------------------------------------------------------
# stderr：固定上限、持续排空、取消清理
# ---------------------------------------------------------------------------


def test_stderr_is_bounded_and_drained(tmp_path):
    async def scenario():
        request = RunRequest("", tmp_path, dict(os.environ))
        cap = getattr(transport, "MAX_STDERR_BYTES", 0)
        assert cap > 0, "必须声明固定 stderr 上限"
        command = _command(tmp_path, "normal", stderr_bytes=STDERR_TOTAL)
        async with transport.RPC(request, command,
                                 lambda *args: None, _reject) as rpc:
            await rpc.call("initialize", {"protocolVersion": 1}, timeout=10)
            await wait_until(
                lambda: getattr(rpc, "stderr_total", 0) >= STDERR_TOTAL,
                timeout=10)
            buffer = getattr(rpc, "stderr_buffer", b"")
            assert len(buffer) == cap
            assert rpc.stderr_total == STDERR_TOTAL
        assert rpc.stderr.done()
        assert rpc.process.returncode is not None

    asyncio.run(scenario())


def test_stderr_burst_on_failure_leaves_no_tasks(tmp_path):
    async def scenario():
        request = RunRequest("", tmp_path, dict(os.environ))
        cap = getattr(transport, "MAX_STDERR_BYTES", 0)
        assert cap > 0, "必须声明固定 stderr 上限"
        command = _command(tmp_path, "stderr-exit", stderr_bytes=STDERR_TOTAL)
        async with transport.RPC(request, command,
                                 lambda *args: None, _reject) as rpc:
            with pytest.raises(HarnessError):
                await rpc.call("initialize", {"protocolVersion": 1},
                               timeout=10)
            await wait_until(
                lambda: getattr(rpc, "stderr_total", 0) >= STDERR_TOTAL,
                timeout=10)
            assert len(getattr(rpc, "stderr_buffer", b"")) == cap
        assert rpc.stderr.done()
        assert rpc.process.returncode is not None

    asyncio.run(scenario())


def test_cancel_during_stderr_burst_cleans_tasks(tmp_path):
    async def scenario():
        request = RunRequest("", tmp_path, dict(os.environ))
        cap = getattr(transport, "MAX_STDERR_BYTES", 0)
        assert cap > 0, "必须声明固定 stderr 上限"
        command = _command(tmp_path, "hang-stderr",
                           stderr_bytes=STDERR_TOTAL)
        async with transport.RPC(request, command,
                                 lambda *args: None, _reject) as rpc:
            pending = asyncio.create_task(
                rpc.call("start", {}, timeout=30))
            await wait_until(
                lambda: getattr(rpc, "stderr_total", 0) >= STDERR_TOTAL,
                timeout=10)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert len(getattr(rpc, "stderr_buffer", b"")) == cap
        assert rpc.stderr.done()
        assert rpc.reader.done()
        assert rpc.process.returncode is not None

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# worker：安全 error 事件、终态分类、跨 Run 隔离
# ---------------------------------------------------------------------------


def test_worker_reports_safe_stage_category_and_run_id(tmp_path, caplog):
    key = Ed25519PrivateKey.generate()
    manager = _worker_manager(tmp_path, key)
    agent = _worker_agent(
        tmp_path, "prompt-error", status=503, stderr_bytes=STDERR_TOTAL,
        error_message=f"upstream rejected {BROKER_SENTINEL}")
    envelope = sign(
        key, run_id="run-diag", session_id="conv-diag",
        message=PROMPT_SENTINEL,
        routes={"broker_token": BROKER_SENTINEL}, agent=agent)

    async def scenario():
        caplog.set_level(logging.WARNING)
        try:
            await manager.submit(
                "run-diag", envelope,
                request_for("conv-diag", PROMPT_SENTINEL, envelope))
            await wait_until(
                lambda: manager.state("run-diag") == "failed", timeout=20)
            text = _error_event(manager, "run-diag")
            assert isinstance(text, str), "error 事件必须仍是字符串"
            assert "Agent 执行失败" in text
            assert "阶段：执行本轮对话" in text
            assert "上游服务不可用" in text
            assert "状态码：503" in text
            assert "run-diag" in text
            assert manager.registry.admission("run-diag")["reason"] == \
                "harness_service_unavailable"
            # 运维日志必须可追踪，但只包含固定分类字段。
            assert "run_id=run-diag" in caplog.text
            assert "stage=prompt" in caplog.text
            assert "category=service_unavailable" in caplog.text
            assert "status=503" in caplog.text
            visible = json.dumps(manager.events("run-diag", after=0),
                                 ensure_ascii=False)
            for sentinel in (BROKER_SENTINEL, PROMPT_SENTINEL,
                             KEY_SENTINEL, str(tmp_path)):
                assert sentinel not in visible
            assert "upstream rejected" not in visible
            assert BROKER_SENTINEL not in caplog.text
            assert PROMPT_SENTINEL not in caplog.text
            assert not transport._POOLS.get(asyncio.get_running_loop())
        finally:
            await manager.aclose()

    asyncio.run(scenario())


def test_worker_does_not_guess_auth_from_text_or_leak_secrets(
        tmp_path, caplog):
    key = Ed25519PrivateKey.generate()
    manager = _worker_manager(tmp_path, key)
    agent = _worker_agent(
        tmp_path, "prompt-error",
        error_message=f"HTTP 401 unauthorized invalid api key {KEY_SENTINEL}")
    envelope = sign(
        key, run_id="run-text", session_id="conv-text",
        message=PROMPT_SENTINEL, routes={}, agent=agent)

    async def scenario():
        caplog.set_level(logging.WARNING)
        try:
            await manager.submit(
                "run-text", envelope,
                request_for("conv-text", PROMPT_SENTINEL, envelope))
            await wait_until(
                lambda: manager.state("run-text") == "failed", timeout=20)
            text = _error_event(manager, "run-text")
            assert "未知原因" in text
            assert "鉴权" not in text
            assert "401" not in text
            assert "run-text" in text
            assert manager.registry.admission("run-text")["reason"] == \
                "agent_failed"
            assert KEY_SENTINEL not in text
            assert KEY_SENTINEL not in caplog.text
            assert PROMPT_SENTINEL not in text
            assert "HTTP 401" not in caplog.text
        finally:
            await manager.aclose()

    asyncio.run(scenario())


def test_worker_normal_acp_stream_and_terminal_protocol(tmp_path):
    key = Ed25519PrivateKey.generate()
    manager = _worker_manager(tmp_path, key)
    agent = _worker_agent(tmp_path, "normal")
    envelope = sign(
        key, run_id="run-ok", session_id="conv-ok",
        message="正常一轮", routes={}, agent=agent)

    async def scenario():
        try:
            await manager.submit(
                "run-ok", envelope,
                request_for("conv-ok", "正常一轮", envelope))
            await wait_until(
                lambda: manager.state("run-ok") == "done", timeout=20)
            events = manager.events("run-ok", after=0)["events"]
            assert not [event for event in events
                        if event["event"] == "error"]
            tokens = [event["data"] for event in events
                      if event["event"] == "token"]
            assert "".join(tokens) == "你好"
            assert events[-1]["data"] == {
                "state": "done", "reason": "completed"}
            messages = manager.messages("conv-ok")["messages"]
            assert [message["role"] for message in messages] == [
                "user", "assistant"]
            assert messages[1]["content"] == "你好"
        finally:
            await manager.aclose()

    asyncio.run(scenario())


def test_worker_diagnostics_are_isolated_between_runs(tmp_path):
    key = Ed25519PrivateKey.generate()
    manager = _worker_manager(tmp_path, key)
    auth_agent = _worker_agent(
        tmp_path, "initialize-error", status=401,
        audit_name="auth-audit.jsonl")
    unknown_agent = _worker_agent(
        tmp_path, "prompt-error",
        error_message="HTTP 401 unauthorized",
        audit_name="unknown-audit.jsonl")
    auth_envelope = sign(
        key, run_id="run-auth", session_id="conv-auth",
        message="甲", routes={}, agent=auth_agent)
    unknown_envelope = sign(
        key, run_id="run-unknown", session_id="conv-unknown",
        message="乙", routes={}, agent=unknown_agent)

    async def scenario():
        try:
            await manager.submit(
                "run-auth", auth_envelope,
                request_for("conv-auth", "甲", auth_envelope))
            await wait_until(
                lambda: manager.state("run-auth") == "failed", timeout=20)
            await manager.submit(
                "run-unknown", unknown_envelope,
                request_for("conv-unknown", "乙", unknown_envelope))
            await wait_until(
                lambda: manager.state("run-unknown") == "failed", timeout=20)
            auth_text = _error_event(manager, "run-auth")
            unknown_text = _error_event(manager, "run-unknown")
            assert "上游鉴权失败" in auth_text and "run-auth" in auth_text
            assert "run-unknown" not in auth_text
            assert "未知原因" in unknown_text and "run-unknown" in unknown_text
            assert "鉴权" not in unknown_text
            assert "run-auth" not in unknown_text
            assert manager.registry.admission("run-auth")["reason"] == \
                "harness_auth"
            assert manager.registry.admission("run-unknown")["reason"] == \
                "agent_failed"
        finally:
            await manager.aclose()

    asyncio.run(scenario())


class _LegacyExecutor:
    async def __call__(self, request, context, bind, emit, ask):
        raise HarnessError("旧消息")


def test_legacy_harness_error_still_yields_unknown_diagnostic(tmp_path):
    key = Ed25519PrivateKey.generate()
    manager = _worker_manager(tmp_path, key, executor=_LegacyExecutor())
    envelope = sign(
        key, run_id="run-legacy", session_id="conv-legacy",
        message="旧异常", routes={},
        agent={"runtime": "acp", "command": ["fixture-acp"],
               "sandbox": "read-only", "timeout": 30})

    async def scenario():
        try:
            await manager.submit(
                "run-legacy", envelope,
                request_for("conv-legacy", "旧异常", envelope))
            await wait_until(
                lambda: manager.state("run-legacy") == "failed", timeout=20)
            text = _error_event(manager, "run-legacy")
            assert "未知原因" in text
            assert "run-legacy" in text
            assert "旧消息" not in text
            assert manager.registry.admission("run-legacy")["reason"] == \
                "agent_failed"
        finally:
            await manager.aclose()

    asyncio.run(scenario())


def test_harness_error_message_constructor_remains_compatible():
    error = HarnessError("旧消息")
    assert str(error) == "旧消息"
    assert getattr(error, "stage", None) is None
    assert getattr(error, "category", None) is None
    assert getattr(error, "status", None) is None
