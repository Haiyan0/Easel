"""Task 5 broker 转发：模型约束、头净化、流式、重定向与额度释放。"""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager

import httpx
import pytest

from easel.control.broker import BrokerService
from easel.control.config import ConfigService, MASTER_KEY_BYTES
from easel.control.egress import EgressGateway
from easel.control.store import Store
from easel.control.types import DomainError

MASTER_KEY = b"k" * MASTER_KEY_BYTES
ADMIN_SECRET = "admin-secret-sentinel-forward"
PERSONAL_SECRET = "personal-secret-sentinel-forward"
BOUNDARY = "----easel-test-boundary"


def assert_error(error: pytest.ExceptionInfo, status: int, code: str = "") -> None:
    assert isinstance(error.value, DomainError)
    assert error.value.status == status, error.value
    if code:
        assert error.value.code == code, error.value


def speech_route(**overrides) -> dict:
    route = {
        "provider": "openai-compatible",
        "model": "tts-default",
        "base_url": "https://provider.example/v1",
        "protocol": "openai",
        "secret": ADMIN_SECRET,
        "options": {},
    }
    route.update(overrides)
    return route


def image_route(**overrides) -> dict:
    route = {
        "provider": "openai",
        "model": "img-default",
        "base_url": "https://image.example/v1",
        "protocol": "openai",
        "secret": {"IMG_API_KEY": "image-secret-sentinel"},
        "options": {},
    }
    route.update(overrides)
    return route


def chat_route(**overrides) -> dict:
    route = {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "base_url": "https://chat.example/v1",
        "protocol": "openai",
        "secret": {"CHAT_API_KEY": "chat-secret-sentinel"},
        "options": {},
        "allowed_models": ["deepseek-chat"],
    }
    route.update(overrides)
    return route


@contextmanager
def broker_env(tmp_path, defaults: dict, *, transport=None, clock=None):
    with Store.open(tmp_path / "control.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        config = ConfigService(store, MASTER_KEY)
        config.publish(admin, {"defaults": defaults}, expected_version=0)
        conversation = store.create_conversation(member)
        run = store.reserve_run(member, conversation.id, "req-1", "digest", 1)
        egress = EgressGateway(transport=transport) if transport is not None else None
        broker = BrokerService(store, config, clock=clock or (lambda: 1000.0),
                               egress=egress)
        yield store, config, broker, admin, member, run


class ChunkStream(httpx.AsyncByteStream):
    """可控的上游响应流，记录是否被关闭。"""

    def __init__(self, chunks, *, gate: asyncio.Event | None = None,
                 closed_flag: dict | None = None):
        self.chunks = list(chunks)
        self.gate = gate
        self.closed_flag = closed_flag
        self.closed = False

    async def __aiter__(self):
        for index, chunk in enumerate(self.chunks):
            if index == 1 and self.gate is not None:
                await self.gate.wait()
            yield chunk

    async def aclose(self):
        self.closed = True
        if self.closed_flag is not None:
            self.closed_flag["closed"] = True


async def aiter_body(data: bytes, chunks: int = 1):
    size = max(1, (len(data) + chunks - 1) // chunks)
    for offset in range(0, len(data), size):
        yield data[offset:offset + size]


def multipart_body(model: str, file_bytes: bytes = b"RIFF-fake-audio") -> bytes:
    return (
        f"--{BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="model"\r\n'
        "\r\n"
        f"{model}\r\n"
        f"--{BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="file"; filename="a.wav"\r\n'
        "Content-Type: audio/wav\r\n"
        "\r\n"
    ).encode() + file_bytes + f"\r\n--{BOUNDARY}--\r\n".encode()


def multipart_model_after_file(actual_model: str,
                               file_bytes: bytes = b"RIFF") -> bytes:
    """第一段文件内容包含伪 name="model"，真正的模型字段在最后。"""
    return (
        f"--{BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="file"; filename="a.wav"\r\n'
        "Content-Type: audio/wav\r\n"
        "\r\n"
    ).encode() + file_bytes + (
        f"\r\n--{BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="model"\r\n'
        "\r\n"
        f"{actual_model}\r\n"
        f"--{BOUNDARY}--\r\n"
    ).encode()


def capture_transport(record: dict, *, status: int = 200,
                      headers: dict | None = None,
                      chunks=(b"ok",), stream=None,
                      error: Exception | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        record["method"] = request.method
        record["url"] = str(request.url)
        record["headers"] = dict(request.headers)
        record["body"] = request.content
        record["calls"] = record.get("calls", 0) + 1
        if error is not None:
            raise error
        body_stream = stream if stream is not None else ChunkStream(list(chunks))
        return httpx.Response(status, headers=headers or {}, stream=body_stream)
    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------
# JSON 与头净化
# --------------------------------------------------------------------------

def test_json_request_is_validated_and_forwarded_with_sanitized_headers(tmp_path):
    record: dict = {}
    transport = capture_transport(record, headers={
        "content-type": "audio/mpeg",
        "x-request-id": "upstream-1",
        "set-cookie": "upstream=1",
        "connection": "keep-alive, x-drop",
        "x-drop": "drop-me",
        "transfer-encoding": "chunked",
    }, chunks=(b"audio-", b"bytes"))

    with broker_env(tmp_path, {"speech": speech_route()},
                    transport=transport) as (
            store, config, broker, admin, member, run):
        token = broker.issue(member, run.id, "speech")
        body = json.dumps({"model": "tts-default", "input": "你好"}).encode()

        async def scenario():
            return await broker.forward(
                token=token, workspace_id=member.workspace_id, run_id=run.id,
                channel="speech", method="POST",
                relative_path="audio/speech",
                headers={
                    "content-type": "application/json",
                    "accept": "audio/mpeg",
                    "authorization": "Bearer client-supplied",
                    "host": "evil.example",
                    "cookie": "session=client",
                    "x-forwarded-for": "203.0.113.9",
                    "x-easel-broker-token": token,
                },
                body=aiter_body(body))

        async def consume():
            response = await scenario()
            data = b"".join([chunk async for chunk in response.chunks()])
            return response, data

        response, data = asyncio.run(consume())
        assert store.count_broker_leases(1000) == 0

    assert data == b"audio-bytes"
    assert response.status_code == 200
    assert record["method"] == "POST"
    assert record["url"] == "https://provider.example/v1/audio/speech"
    upstream_headers = {name.lower(): value for name, value in record["headers"].items()}
    assert upstream_headers["authorization"] == f"Bearer {ADMIN_SECRET}"
    assert upstream_headers["content-type"] == "application/json"
    assert upstream_headers["accept"] == "audio/mpeg"
    # HTTPX 会按目标 URL 重建 Host；客户端提交的 Host 不能到达上游
    assert upstream_headers["host"] == "provider.example"
    for forbidden in ("cookie", "x-forwarded-for", "x-easel-broker-token"):
        assert forbidden not in upstream_headers
    assert json.loads(record["body"])["model"] == "tts-default"
    assert json.loads(record["body"])["input"] == "你好"

    response_headers = {name.lower(): value for name, value in response.headers.items()}
    assert response_headers["content-type"] == "audio/mpeg"
    assert response_headers["x-request-id"] == "upstream-1"
    assert response_headers["cache-control"] == "no-store"
    for forbidden in ("set-cookie", "x-drop", "connection", "transfer-encoding"):
        assert forbidden not in response_headers


def test_json_model_mismatch_and_routing_fields_are_rejected(tmp_path):
    record: dict = {}
    transport = capture_transport(record)
    with broker_env(tmp_path, {"speech": speech_route()},
                    transport=transport) as (
            store, config, broker, admin, member, run):
        token = broker.issue(member, run.id, "speech")

        async def attempt(payload: dict):
            return await broker.forward(
                token=token, workspace_id=member.workspace_id, run_id=run.id,
                channel="speech", method="POST",
                relative_path="audio/speech",
                headers={"content-type": "application/json"},
                body=aiter_body(json.dumps(payload).encode()))

        for payload in (
            {"model": "other-model", "input": "x"},
            {"model": "tts-default", "input": "x",
             "base_url": "https://evil.example/v1"},
            {"model": "tts-default", "input": "x", "api_key": "client-key"},
            {"model": "tts-default", "input": "x", "headers": {"X": "1"}},
        ):
            with pytest.raises(DomainError) as invalid:
                asyncio.run(attempt(payload))
            assert_error(invalid, 400, "invalid_request")

        assert record.get("calls", 0) == 0
        assert store.count_broker_leases(1000) == 0


def test_missing_json_model_uses_snapshot_model(tmp_path):
    record: dict = {}
    transport = capture_transport(record)
    with broker_env(tmp_path, {"speech": speech_route()},
                    transport=transport) as (
            store, config, broker, admin, member, run):
        token = broker.issue(member, run.id, "speech")

        async def scenario():
            response = await broker.forward(
                token=token, workspace_id=member.workspace_id, run_id=run.id,
                channel="speech", method="POST",
                relative_path="audio/speech",
                headers={"content-type": "application/json"},
                body=aiter_body(json.dumps({"input": "x"}).encode()))
            return b"".join([chunk async for chunk in response.chunks()])

        asyncio.run(scenario())

    assert json.loads(record["body"])["model"] == "tts-default"


def test_image_adapter_uses_custom_auth_header_and_api_version(tmp_path):
    record: dict = {}
    transport = capture_transport(record)
    route = image_route(secret={
        "IMG_API_KEY": "image-secret-sentinel",
        "IMG_API_KEY_HEADER": "api-key",
        "IMG_API_VERSION": "2024-01-01",
    })
    with broker_env(tmp_path, {"image": route}, transport=transport) as (
            store, config, broker, admin, member, run):
        token = broker.issue(member, run.id, "image")

        async def scenario():
            response = await broker.forward(
                token=token, workspace_id=member.workspace_id, run_id=run.id,
                channel="image", method="POST",
                relative_path="images/generations",
                headers={"content-type": "application/json"},
                body=aiter_body(json.dumps({"model": "img-default",
                                            "prompt": "cat"}).encode()))
            return b"".join([chunk async for chunk in response.chunks()])

        asyncio.run(scenario())

    assert record["url"] == (
        "https://image.example/v1/images/generations?api-version=2024-01-01")
    headers = {name.lower(): value for name, value in record["headers"].items()}
    assert headers["api-key"] == "image-secret-sentinel"
    assert "authorization" not in headers


def test_chat_adapter_injects_default_model_and_enforces_protocol(tmp_path):
    record: dict = {}
    transport = capture_transport(record)
    with broker_env(tmp_path, {"chat": chat_route()},
                    transport=transport) as (
            store, config, broker, admin, member, run):
        token = broker.issue(member, run.id, "chat")

        async def scenario():
            response = await broker.forward(
                token=token, workspace_id=member.workspace_id, run_id=run.id,
                channel="chat", method="POST",
                relative_path="chat/completions",
                headers={"content-type": "application/json"},
                body=aiter_body(json.dumps(
                    {"messages": [{"role": "user", "content": "hi"}]}).encode()))
            return b"".join([chunk async for chunk in response.chunks()])

        asyncio.run(scenario())
        assert json.loads(record["body"])["model"] == "deepseek-chat"
        assert record["url"] == "https://chat.example/v1/chat/completions"
        headers = {name.lower(): value for name, value in record["headers"].items()}
        assert headers["authorization"] == "Bearer chat-secret-sentinel"

    # 协议不匹配的受管路由不能签发令牌
    with Store.open(tmp_path / "protocol.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        config = ConfigService(store, MASTER_KEY)
        config.publish(admin, {"defaults": {
            "chat": chat_route(protocol="anthropic")}}, expected_version=0)
        conversation = store.create_conversation(member)
        run = store.reserve_run(member, conversation.id, "req-1", "digest", 1)
        broker = BrokerService(store, config, clock=lambda: 1000.0,
                               egress=EgressGateway(transport=transport))
        with pytest.raises(DomainError) as unsupported:
            broker.issue(member, run.id, "chat")
        assert_error(unsupported, 503, "capability_unavailable")


# --------------------------------------------------------------------------
# multipart
# --------------------------------------------------------------------------

def test_multipart_model_is_validated_and_body_preserved(tmp_path):
    record: dict = {}
    transport = capture_transport(record)
    with broker_env(tmp_path, {"speech": speech_route()},
                    transport=transport) as (
            store, config, broker, admin, member, run):
        token = broker.issue(member, run.id, "speech")
        body = multipart_body("tts-default")

        async def scenario(data: bytes):
            response = await broker.forward(
                token=token, workspace_id=member.workspace_id, run_id=run.id,
                channel="speech", method="POST",
                relative_path="audio/transcriptions",
                headers={
                    "content-type": f"multipart/form-data; boundary={BOUNDARY}",
                    "content-length": str(len(data)),
                },
                body=aiter_body(data, chunks=4))
            return b"".join([chunk async for chunk in response.chunks()])

        asyncio.run(scenario(body))
        assert record["body"] == body
        headers = {name.lower(): value for name, value in record["headers"].items()}
        assert headers["content-type"] == (
            f"multipart/form-data; boundary={BOUNDARY}")
        assert headers["content-length"] == str(len(body))

        for broken in (multipart_body("other-model"), multipart_body("")):
            record.pop("calls", None)
            with pytest.raises(DomainError) as invalid:
                asyncio.run(scenario(broken))
            assert_error(invalid, 400, "invalid_request")
            assert record.get("calls", 0) == 0


def test_multipart_parser_only_reads_part_headers_not_file_content(tmp_path):
    record: dict = {}
    transport = capture_transport(record)
    with broker_env(tmp_path, {"speech": speech_route()},
                    transport=transport) as (
            store, config, broker, admin, member, run):
        token = broker.issue(member, run.id, "speech")

        async def scenario(data: bytes):
            response = await broker.forward(
                token=token, workspace_id=member.workspace_id, run_id=run.id,
                channel="speech", method="POST",
                relative_path="audio/transcriptions",
                headers={
                    "content-type": f"multipart/form-data; boundary={BOUNDARY}",
                    "content-length": str(len(data)),
                },
                body=aiter_body(data, chunks=5))
            return b"".join([chunk async for chunk in response.chunks()])

        # 文件内容里的伪 name="model" 不能冒充表单字段
        forged = multipart_model_after_file(
            "other-model",
            file_bytes=b'name="model"\r\n\r\ntts-default\r\n')
        with pytest.raises(DomainError) as invalid:
            asyncio.run(scenario(forged))
        assert_error(invalid, 400, "invalid_request")
        assert record.get("calls", 0) == 0

        # 真实模型字段在文件之后仍然能正确解析并原样转发
        valid = multipart_model_after_file(
            "tts-default", file_bytes=b'name="model"\r\n\r\nforged')
        asyncio.run(scenario(valid))
        assert record["body"] == valid
        assert record.get("calls") == 1


def test_personal_and_admin_credentials_never_mix(tmp_path):
    record: dict = {}
    transport = capture_transport(record)
    with broker_env(tmp_path, {"speech": speech_route()},
                    transport=transport) as (
            store, config, broker, admin, member, run):
        config.save_override(member, "speech", {
            "provider": "openai-compatible", "model": "personal-model",
            "base_url": "https://personal.example/v1", "protocol": "openai",
            "secret": PERSONAL_SECRET, "options": {}})
        personal_token = broker.issue(member, run.id, "speech")

        async def call(token, model="personal-model"):
            response = await broker.forward(
                token=token, workspace_id=member.workspace_id, run_id=run.id,
                channel="speech", method="POST",
                relative_path="audio/speech",
                headers={"content-type": "application/json"},
                body=aiter_body(json.dumps({"model": model}).encode()))
            return b"".join([chunk async for chunk in response.chunks()])

        asyncio.run(call(personal_token))
        headers = {name.lower(): value for name, value in record["headers"].items()}
        assert headers["authorization"] == f"Bearer {PERSONAL_SECRET}"
        assert record["url"] == "https://personal.example/v1/audio/speech"

        config.clear_override(member, "speech")
        admin_token = broker.issue(member, run.id, "speech")
        asyncio.run(call(admin_token, "tts-default"))
        headers = {name.lower(): value for name, value in record["headers"].items()}
        assert headers["authorization"] == f"Bearer {ADMIN_SECRET}"
        assert record["url"] == "https://provider.example/v1/audio/speech"


# --------------------------------------------------------------------------
# 流式、重定向与释放
# --------------------------------------------------------------------------

def test_sse_chunks_are_yielded_without_buffering(tmp_path):
    gate = asyncio.Event()
    closed: dict = {}
    stream = ChunkStream([b"data: one\n\n", b"data: two\n\n"], gate=gate,
                         closed_flag=closed)
    transport = capture_transport({}, headers={"content-type": "text/event-stream"},
                                  stream=stream)
    with broker_env(tmp_path, {"chat": chat_route()},
                    transport=transport) as (
            store, config, broker, admin, member, run):
        token = broker.issue(member, run.id, "chat")

        async def scenario():
            response = await broker.forward(
                token=token, workspace_id=member.workspace_id, run_id=run.id,
                channel="chat", method="POST",
                relative_path="chat/completions",
                headers={"content-type": "application/json",
                         "accept": "text/event-stream"},
                body=aiter_body(json.dumps(
                    {"model": "deepseek-chat", "stream": True,
                     "messages": []}).encode()))
            chunks = response.chunks()
            first = await anext(chunks)
            # 上游第二块被 gate 阻塞时，第一块必须已经可用
            assert first == b"data: one\n\n"
            assert store.count_broker_leases(1000) == 1
            gate.set()
            second = await anext(chunks)
            assert second == b"data: two\n\n"
            with pytest.raises(StopAsyncIteration):
                await anext(chunks)
            return response

        response = asyncio.run(scenario())
        assert store.count_broker_leases(1000) == 0

    assert response.status_code == 200
    assert closed.get("closed") is True


def test_early_close_releases_stream_and_lease(tmp_path):
    closed: dict = {}
    stream = ChunkStream([b"one", b"two", b"three"], closed_flag=closed)
    transport = capture_transport({}, stream=stream)
    with broker_env(tmp_path, {"speech": speech_route()},
                    transport=transport) as (
            store, config, broker, admin, member, run):
        token = broker.issue(member, run.id, "speech")

        async def scenario():
            response = await broker.forward(
                token=token, workspace_id=member.workspace_id, run_id=run.id,
                channel="speech", method="POST",
                relative_path="audio/speech",
                headers={"content-type": "application/json"},
                body=aiter_body(json.dumps({"model": "tts-default"}).encode()))
            chunks = response.chunks()
            assert await anext(chunks) == b"one"
            await response.aclose()
            await response.aclose()

        asyncio.run(scenario())
        assert store.count_broker_leases(1000) == 0

    assert closed.get("closed") is True


def test_upstream_error_releases_lease(tmp_path):
    transport = capture_transport({}, error=httpx.ConnectError("boom"))
    with broker_env(tmp_path, {"speech": speech_route()},
                    transport=transport) as (
            store, config, broker, admin, member, run):
        token = broker.issue(member, run.id, "speech")

        async def scenario():
            return await broker.forward(
                token=token, workspace_id=member.workspace_id, run_id=run.id,
                channel="speech", method="POST",
                relative_path="audio/speech",
                headers={"content-type": "application/json"},
                body=aiter_body(json.dumps({"model": "tts-default"}).encode()))

        with pytest.raises(DomainError) as unavailable:
            asyncio.run(scenario())
        assert_error(unavailable, 502, "upstream_unavailable")
        assert store.count_broker_leases(1000) == 0


def test_redirect_is_rejected_without_following(tmp_path):
    record: dict = {}
    transport = capture_transport(
        record, status=302,
        headers={"location": "https://evil.example/steal"})
    with broker_env(tmp_path, {"speech": speech_route()},
                    transport=transport) as (
            store, config, broker, admin, member, run):
        token = broker.issue(member, run.id, "speech")

        async def scenario():
            return await broker.forward(
                token=token, workspace_id=member.workspace_id, run_id=run.id,
                channel="speech", method="POST",
                relative_path="audio/speech",
                headers={"content-type": "application/json"},
                body=aiter_body(json.dumps({"model": "tts-default"}).encode()))

        with pytest.raises(DomainError) as redirect:
            asyncio.run(scenario())
        assert_error(redirect, 502, "upstream_redirect")
        assert store.count_broker_leases(1000) == 0

    assert record.get("calls") == 1


def test_missing_egress_proxy_fails_closed_without_direct_fallback(tmp_path):
    calls = {"count": 0}

    def handler(request):
        calls["count"] += 1
        return httpx.Response(200, content=b"should-not-happen")

    with broker_env(tmp_path, {"speech": speech_route()}) as (
            store, config, broker, admin, member, run):
        token = broker.issue(member, run.id, "speech")

        async def scenario():
            return await broker.forward(
                token=token, workspace_id=member.workspace_id, run_id=run.id,
                channel="speech", method="POST",
                relative_path="audio/speech",
                headers={"content-type": "application/json"},
                body=aiter_body(json.dumps({"model": "tts-default"}).encode()))

        with pytest.raises(DomainError) as unavailable:
            asyncio.run(scenario())
        assert_error(unavailable, 503, "egress_unavailable")
        assert calls["count"] == 0
        assert store.count_broker_leases(1000) == 0


def test_upstream_auth_failure_passes_status_without_echoing_key(tmp_path):
    transport = capture_transport(
        {}, status=401,
        headers={"content-type": "application/json",
                 "x-api-key": ADMIN_SECRET},
        chunks=(b'{"error":"invalid key"}',))
    with broker_env(tmp_path, {"speech": speech_route()},
                    transport=transport) as (
            store, config, broker, admin, member, run):
        token = broker.issue(member, run.id, "speech")

        async def scenario():
            response = await broker.forward(
                token=token, workspace_id=member.workspace_id, run_id=run.id,
                channel="speech", method="POST",
                relative_path="audio/speech",
                headers={"content-type": "application/json"},
                body=aiter_body(json.dumps({"model": "tts-default"}).encode()))
            data = b"".join([chunk async for chunk in response.chunks()])
            return response, data

        response, data = asyncio.run(scenario())

    assert response.status_code == 401
    assert data == b'{"error":"invalid key"}'
    assert "x-api-key" not in {name.lower() for name in response.headers}
    assert ADMIN_SECRET not in repr(response.headers)


def test_unknown_path_and_client_target_are_rejected(tmp_path):
    record: dict = {}
    transport = capture_transport(record)
    with broker_env(tmp_path, {"speech": speech_route()},
                    transport=transport) as (
            store, config, broker, admin, member, run):
        token = broker.issue(member, run.id, "speech")

        async def attempt(path: str):
            return await broker.forward(
                token=token, workspace_id=member.workspace_id, run_id=run.id,
                channel="speech", method="POST",
                relative_path=path,
                headers={"content-type": "application/json"},
                body=aiter_body(json.dumps({"model": "tts-default"}).encode()))

        for path in ("audio/../audio/speech", "/audio/speech",
                     "https://evil.example/audio/speech", "audio/speech?x=1",
                     "v1/audio/speech", "audio/unknown"):
            with pytest.raises(DomainError) as invalid:
                asyncio.run(attempt(path))
            assert invalid.value.status in (400, 404)
        assert record.get("calls", 0) == 0
