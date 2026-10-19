"""Task 5 broker HTTP 边界：内部鉴权、流式响应与不绕过 Cookie/CSRF。"""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from easel.control.app import (
    BrokerStreamingResponse,
    ControlConfig,
    create_app,
)
from easel.control.auth import AuthService
from easel.control.broker import BrokerService
from easel.control.config import ConfigService, MASTER_KEY_BYTES
from easel.control.egress import EgressGateway
from easel.control.store import Store

ORIGIN = "https://easel.test"
MASTER_KEY = b"k" * MASTER_KEY_BYTES
ADMIN_SECRET = "admin-secret-sentinel-api-broker"


def speech_route() -> dict:
    return {
        "provider": "openai-compatible",
        "model": "tts-default",
        "base_url": "https://provider.example/v1",
        "protocol": "openai",
        "secret": ADMIN_SECRET,
        "options": {},
    }


def chat_route() -> dict:
    return {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "base_url": "https://chat.example/v1",
        "protocol": "openai",
        "secret": {"CHAT_API_KEY": "chat-secret-sentinel"},
        "options": {},
    }


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks, closed: dict | None = None):
        self.chunks = list(chunks)
        self.closed = closed
        self.was_closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.was_closed = True
        if self.closed is not None:
            self.closed["closed"] = True


def capture_transport(record: dict, *, status: int = 200,
                      headers: dict | None = None,
                      chunks=(b"ok",), stream=None):
    def handler(request: httpx.Request) -> httpx.Response:
        record["method"] = request.method
        record["url"] = str(request.url)
        record["headers"] = dict(request.headers)
        record["body"] = request.content
        record["calls"] = record.get("calls", 0) + 1
        return httpx.Response(
            status, headers=headers or {},
            stream=stream if stream is not None else ChunkStream(list(chunks)))
    return httpx.MockTransport(handler)


@contextmanager
def broker_api(tmp_path, defaults: dict, *, transport):
    with Store.open(tmp_path / "control.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        config = ConfigService(store, MASTER_KEY)
        config.publish(admin, {"defaults": defaults}, expected_version=0)
        conversation = store.create_conversation(member)
        run = store.reserve_run(member, conversation.id, "req-1", "digest", 1)
        broker = BrokerService(
            store, config, clock=lambda: 1000.0,
            egress=EgressGateway(transport=transport))
        token = broker.issue(member, run.id, "speech")
        app = create_app(
            store=store, auth=AuthService(store),
            config=ControlConfig(public_origin=ORIGIN),
            config_service=config, broker=broker)
        with TestClient(app, base_url=ORIGIN) as client:
            yield (client, store, broker, token, member, run)


def broker_headers(token: str, member, run) -> dict:
    return {
        "X-Easel-Broker-Token": token,
        "X-Easel-Workspace-Id": member.workspace_id,
        "X-Easel-Run-Id": run.id,
    }


def test_missing_and_invalid_tokens_are_rejected_without_upstream_call(tmp_path):
    record: dict = {}
    transport = capture_transport(record)
    with broker_api(tmp_path, {"speech": speech_route()},
                    transport=transport) as (
            client, store, broker, token, member, run):
        missing = client.post("/internal/broker/speech/audio/speech",
                              content=b"{}")
        assert missing.status_code == 401
        assert missing.json()["code"] == "invalid_token"

        invalid = client.post("/internal/broker/speech/audio/speech",
                              content=b"{}",
                              headers={**broker_headers("bad-token", member, run),
                                       "content-type": "application/json"})
        assert invalid.status_code == 401
        assert "bad-token" not in invalid.text
        assert ADMIN_SECRET not in invalid.text
        assert record.get("calls", 0) == 0


def test_cookie_session_cannot_substitute_for_broker_token(tmp_path):
    transport = capture_transport({})
    with broker_api(tmp_path, {"speech": speech_route()},
                    transport=transport) as (
            client, store, broker, token, member, run):
        # 即使带普通浏览器 Cookie，也不能借用 control 的认证分支
        response = client.post(
            "/internal/broker/speech/audio/speech",
            content=b"{}",
            headers={"Cookie": "__Host-easel_session=fake",
                     "content-type": "application/json"})
        assert response.status_code == 401


def test_scope_mismatch_and_unknown_internal_paths_are_404(tmp_path):
    transport = capture_transport({})
    with broker_api(tmp_path, {"speech": speech_route()},
                    transport=transport) as (
            client, store, broker, token, member, run):
        other = store.create_user("Other")
        crossed = client.post(
            "/internal/broker/speech/audio/speech", content=b"{}",
            headers={**broker_headers(token, other, run),
                     "content-type": "application/json"})
        assert crossed.status_code == 404

        wrong_channel = client.post(
            "/internal/broker/image/images/generations", content=b"{}",
            headers={**broker_headers(token, member, run),
                     "content-type": "application/json"})
        assert wrong_channel.status_code == 404

        unknown = client.post("/internal/broker/speech/audio/unknown",
                              content=b"{}")
        assert unknown.status_code == 404
        assert unknown.json()["code"] == "not_found"


def test_json_forward_over_http_preserves_stream_headers_and_strips_secrets(tmp_path):
    record: dict = {}
    transport = capture_transport(
        record, headers={
            "content-type": "audio/mpeg",
            "x-upstream": "keep",
            "set-cookie": "provider=1",
            "connection": "keep-alive, x-hop",
            "x-hop": "drop",
        }, chunks=(b"audio-", b"bytes"))
    with broker_api(tmp_path, {"speech": speech_route()},
                    transport=transport) as (
            client, store, broker, token, member, run):
        response = client.post(
            "/internal/broker/speech/audio/speech",
            content=json.dumps({"model": "tts-default", "input": "hi"}),
            headers={**broker_headers(token, member, run),
                     "content-type": "application/json",
                     "accept": "audio/mpeg",
                     "authorization": "Bearer client-supplied",
                     "x-forwarded-for": "203.0.113.9",
                     "cookie": "client=1"})

        assert response.status_code == 200, response.text
        assert response.content == b"audio-bytes"
        assert response.headers["content-type"] == "audio/mpeg"
        assert response.headers["x-upstream"] == "keep"
        assert response.headers["cache-control"] == "no-store"
        assert "set-cookie" not in response.headers
        assert "x-hop" not in response.headers
        assert store.count_broker_leases(1000) == 0

    headers = {name.lower(): value for name, value in record["headers"].items()}
    assert headers["authorization"] == f"Bearer {ADMIN_SECRET}"
    assert "x-forwarded-for" not in headers
    assert headers["host"] == "provider.example"
    assert json.loads(record["body"])["model"] == "tts-default"


def test_models_endpoint_streams_upstream_json_without_cookie_auth(tmp_path):
    record: dict = {}
    transport = capture_transport(
        record, headers={"content-type": "application/json"},
        chunks=(b'{"data":', b'[{"id":"deepseek-chat"}]}'))
    with broker_api(tmp_path, {"speech": speech_route()},
                    transport=transport) as (
            client, store, broker, token, member, run):
        # 为 chat 通道单独签发 token
        response = client.get(
            "/internal/broker/chat/models",
            headers=broker_headers(token, member, run))
        assert response.status_code == 404  # token 绑定 speech，不能换 channel

    # 使用 chat token 的成功路径
    with Store.open(tmp_path / "chat.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        config = ConfigService(store, MASTER_KEY)
        config.publish(admin, {"defaults": {"chat": chat_route()}},
                       expected_version=0)
        conversation = store.create_conversation(member)
        run = store.reserve_run(member, conversation.id, "req-1", "digest", 1)
        broker = BrokerService(store, config, clock=lambda: 1000.0,
                               egress=EgressGateway(transport=transport))
        token = broker.issue(member, run.id, "chat")
        app = create_app(store=store, auth=AuthService(store),
                         config=ControlConfig(public_origin=ORIGIN),
                         config_service=config, broker=broker)
        with TestClient(app, base_url=ORIGIN) as client:
            response = client.get(
                "/internal/broker/chat/models",
                headers=broker_headers(token, member, run))
            assert response.status_code == 200
            assert response.content == b'{"data":[{"id":"deepseek-chat"}]}'
            assert response.headers["content-type"] == "application/json"

    assert record["url"] == "https://chat.example/v1/models"
    assert record["method"] == "GET"


def test_multipart_forward_over_http_preserves_boundary(tmp_path):
    record: dict = {}
    transport = capture_transport(record)
    boundary = "----api-boundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="model"\r\n'
        "\r\n"
        "tts-default\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="a.wav"\r\n'
        "Content-Type: audio/wav\r\n"
        "\r\n"
    ).encode() + b"RIFF-audio" + f"\r\n--{boundary}--\r\n".encode()
    with broker_api(tmp_path, {"speech": speech_route()},
                    transport=transport) as (
            client, store, broker, token, member, run):
        response = client.post(
            "/internal/broker/speech/audio/transcriptions",
            content=body,
            headers={**broker_headers(token, member, run),
                     "content-type": f"multipart/form-data; boundary={boundary}",
                     "authorization": "Bearer client-supplied"})
        assert response.status_code == 200, response.text
        assert record["body"] == body
        headers = {name.lower(): value for name, value in record["headers"].items()}
        assert headers["content-type"] == (
            f"multipart/form-data; boundary={boundary}")
        assert headers["content-length"] == str(len(body))
        assert headers["authorization"] == f"Bearer {ADMIN_SECRET}"
        assert store.count_broker_leases(1000) == 0


def test_sse_response_is_streamed_and_closed(tmp_path):
    closed: dict = {}
    stream = ChunkStream([b"data: one\n\n", b"data: two\n\n"], closed=closed)
    transport = capture_transport(
        {}, headers={"content-type": "text/event-stream"}, stream=stream)
    with broker_api(tmp_path, {"speech": speech_route()},
                    transport=transport) as (
            client, store, broker, token, member, run):
        response = client.post(
            "/internal/broker/speech/audio/speech",
            content=json.dumps({"model": "tts-default"}),
            headers={**broker_headers(token, member, run),
                     "content-type": "application/json",
                     "accept": "text/event-stream"})
        assert response.status_code == 200
        assert response.content == b"data: one\n\ndata: two\n\n"
        assert response.headers["content-type"] == "text/event-stream"

    assert closed.get("closed") is True


def test_redirect_and_capacity_errors_are_mapped(tmp_path):
    record: dict = {}
    transport = capture_transport(
        record, status=302,
        headers={"location": "https://evil.example/steal"})
    with broker_api(tmp_path, {"speech": speech_route()},
                    transport=transport) as (
            client, store, broker, token, member, run):
        response = client.post(
            "/internal/broker/speech/audio/speech",
            content=json.dumps({"model": "tts-default"}),
            headers={**broker_headers(token, member, run),
                     "content-type": "application/json"})
        assert response.status_code == 502
        assert response.json()["code"] == "upstream_redirect"
        assert "authorization" not in response.text.lower()

        # 填满额度后 HTTP 层返回 429
        first = broker.acquire_lease(token, member.workspace_id, run.id,
                                     "speech")
        second = broker.acquire_lease(token, member.workspace_id, run.id,
                                      "speech")
        over = client.post(
            "/internal/broker/speech/audio/speech",
            content=json.dumps({"model": "tts-default"}),
            headers={**broker_headers(token, member, run),
                     "content-type": "application/json"})
        assert over.status_code == 429
        assert over.json()["code"] == "workspace_capacity"
        broker.release_lease(first)
        broker.release_lease(second)


def test_streaming_response_closes_body_on_send_disconnect():
    class Body:
        closed = False

        async def __aiter__(self):
            yield b"one"
            yield b"two"

        async def aclose(self):
            self.closed = True

    body = Body()
    response = BrokerStreamingResponse(body)
    sent: list[str] = []

    async def send(message):
        sent.append(message["type"])
        if message["type"] == "http.response.body":
            raise OSError("client disconnected")

    async def scenario():
        with pytest.raises(OSError):
            await response.stream_response(send)

    asyncio.run(scenario())
    assert sent[0] == "http.response.start"
    assert body.closed is True, "断开时必须在 finally 关闭上游流"
