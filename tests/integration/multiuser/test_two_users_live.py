"""Task 14：真实部署控制面的双用户 HTTP/SSE 契约。

只在显式 ``--multiuser-live`` 下运行；缺少邀请、控制 URL 或目标
workspace-content 路由时失败，不把 skip 当作发布通过。邀请只由
``live_users`` session fixture 兑换一次，本文件不重复兑换。
"""
from __future__ import annotations

import json
import uuid

import pytest

pytestmark = pytest.mark.multiuser_integration

TERMINAL = frozenset({"done", "failed", "interrupted", "cancelled"})


def _iter_sse(response):
    frame: dict[str, str] = {}
    data_lines: list[str] = []
    for line in response.iter_lines():
        if line == "":
            if frame or data_lines:
                yield {"id": frame.get("id"), "event": frame.get("event"),
                       "data": "\n".join(data_lines)}
            frame = {}
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value.lstrip(" ")
        if name == "data":
            data_lines.append(value)
        else:
            frame[name] = value
    if frame or data_lines:
        yield {"id": frame.get("id"), "event": frame.get("event"),
               "data": "\n".join(data_lines)}


def _state(frame: dict) -> str | None:
    if frame.get("event") != "state":
        return None
    return json.loads(frame["data"]).get("state")


def _collect(client, url: str, headers: dict[str, str]) -> list[dict]:
    frames: list[dict] = []
    with client.stream("GET", url, headers=headers) as response:
        if response.status_code != 200:
            response.read()
            pytest.fail(
                f"SSE 返回 HTTP {response.status_code}，未进入流式读取",
                pytrace=False)
        for frame in _iter_sse(response):
            frames.append(frame)
            if _state(frame) in TERMINAL:
                break
    if not frames or _state(frames[-1]) not in TERMINAL:
        pytest.fail(
            "SSE 未在读取窗口内到达终态；不把超时当作通过",
            pytrace=False)
    return frames


def _tokens(frames: list[dict]) -> str:
    parts: list[str] = []
    for frame in frames:
        if frame.get("event") != "token" or not frame.get("data"):
            continue
        value = json.loads(frame["data"])
        if isinstance(value, str):
            parts.append(value)
    return "".join(parts)


def _create_session(user, title: str) -> str:
    response = user.client.post(
        "/api/sessions", headers=user.write_headers(),
        json={"title": title})
    assert response.status_code == 201, response.status_code
    session_id = response.json().get("id")
    assert isinstance(session_id, str) and session_id
    return session_id


def _submit(user, session_id: str, message: str) -> str:
    response = user.client.post(
        "/api/chat/stream", headers=user.write_headers(),
        json={"sessionId": session_id,
              "turnId": "live-" + uuid.uuid4().hex,
              "message": message})
    assert response.status_code == 202, response.status_code
    run_id = response.json().get("runId")
    assert isinstance(run_id, str) and run_id
    return run_id


def _messages(user, session_id: str) -> list[dict]:
    response = user.client.get(
        f"/api/sessions/{session_id}/messages",
        headers=user.read_headers())
    assert response.status_code == 200, response.status_code
    body = response.json()
    messages = body.get("messages")
    assert isinstance(messages, list)
    return messages


def _not_found(response, label: str) -> None:
    assert response.status_code == 404, (label, response.status_code)
    assert response.json().get("code") == "not_found", label


def test_live_two_users_are_isolated_and_history_resumes(live_users):
    a, b = live_users
    assert a.user_id and b.user_id and a.user_id != b.user_id
    assert a.workspace_id and b.workspace_id
    assert a.workspace_id != b.workspace_id

    session_a = _create_session(a, "正式 A 私有会话")
    session_b = _create_session(b, "正式 B 私有会话")
    assert session_a != session_b

    run_a1 = _submit(a, session_a, "正式环境 A 第一轮追问")
    frames_a1 = _collect(
        a.client, f"/api/chat/jobs/{run_a1}/stream", a.read_headers())
    assert _tokens(frames_a1)

    # B 不能读取 A 的会话、Run、SSE；跨租户在转发前 404。
    _not_found(
        b.client.get(f"/api/sessions/{session_a}/messages",
                     headers=b.read_headers()),
        "B 读 A 消息")
    _not_found(
        b.client.get(f"/api/sessions/{session_a}/runs",
                     headers=b.read_headers()),
        "B 读 A Run 目录")
    _not_found(
        b.client.get(f"/api/chat/jobs/{run_a1}/stream",
                     headers=b.read_headers()),
        "B 读 A SSE")
    _not_found(
        b.client.post("/api/chat/stop", headers=b.write_headers(),
                      json={"runId": run_a1}),
        "B 停止 A Run")
    _not_found(
        b.client.post("/api/chat", headers=b.write_headers(),
                      json={"sessionId": session_a,
                            "turnId": "live-attack",
                            "message": "越权"}),
        "B 向 A 会话发消息")
    _not_found(
        b.client.post("/api/chat/question/status",
                      headers=b.write_headers(),
                      json={"runId": run_a1,
                            "questionIds": ["permission"]}),
        "B 读 A 问题状态")

    # 同一会话第二轮必须由真实 worker/harness 续接；失败不能被当作通过。
    run_a2 = _submit(a, session_a, "正式环境 A 续接追问")
    frames_a2 = _collect(
        a.client, f"/api/chat/jobs/{run_a2}/stream", a.read_headers())
    assert _tokens(frames_a2)
    history_a = _messages(a, session_a)
    assert len(history_a) >= 4
    assert [item.get("role") for item in history_a[:4]] == [
        "user", "assistant", "user", "assistant"]

    run_b = _submit(b, session_b, "正式环境 B 私有内容")
    frames_b = _collect(
        b.client, f"/api/chat/jobs/{run_b}/stream", b.read_headers())
    assert _tokens(frames_b)
    history_b = _messages(b, session_b)
    assert len(history_b) >= 2
    a_contents = {item.get("content") for item in history_a}
    b_contents = {item.get("content") for item in history_b}
    assert "正式环境 A 第一轮追问" not in b_contents
    assert "正式环境 B 私有内容" not in a_contents
    assert "正式环境 A 续接追问" not in b_contents
    assert a_contents != b_contents

    runs_a = a.client.get(
        f"/api/sessions/{session_a}/runs", headers=a.read_headers())
    assert runs_a.status_code == 200
    states = {item["runId"]: item["state"]
              for item in runs_a.json()["runs"]}
    assert states.get(run_a1) == "done"
    assert states.get(run_a2) == "done"

    # 伪造 workspace 头不能改变身份。
    spoofed = b.client.get(
        "/api/auth/me",
        headers={**b.read_headers(),
                 "X-Easel-Workspace-Id": a.workspace_id})
    assert spoofed.status_code == 200
    assert spoofed.json()["workspaceId"] == b.workspace_id


def test_live_workspace_content_route_is_assembled(live_users):
    """产物入口必须真实装配；503/skip 不能算发布通过。"""
    a, b = live_users
    for user in (a, b):
        response = user.client.get(
            "/api/outputs", headers=user.read_headers())
        if response.status_code == 503:
            pytest.fail(
                "control workspace-content 路由未装配（capability_unavailable）；"
                "正式发布门槛不通过", pytrace=False)
        assert response.status_code == 200, response.status_code
        assert isinstance(response.json(), list)


def test_live_invite_is_single_use(live_env, live_users):
    """已兑换邀请不能再次登录；不读取或输出邀请明文。"""
    import httpx

    with httpx.Client(
            base_url=live_env.control_url, trust_env=False,
            timeout=httpx.Timeout(30.0, connect=10.0, read=30.0)) as replay:
        response = replay.post(
            "/api/auth/exchange", headers={"Origin": live_env.origin},
            json={"token": live_env.invite_a})
    assert response.status_code == 401
