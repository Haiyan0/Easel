"""Task 5 broker 转发：模型约束、头净化、流式、重定向与额度释放。"""
from __future__ import annotations

import asyncio
import json
import time
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
def broker_env(tmp_path, defaults: dict, *, transport=None, clock=None,
               lease_ttl: int = 120, lease_clock=None):
    with Store.open(tmp_path / "control.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        config = ConfigService(store, MASTER_KEY)
        config.publish(admin, {"defaults": defaults}, expected_version=0)
        conversation = store.create_conversation(member)
        run = store.reserve_run(member, conversation.id, "req-1", "digest", 1)
        egress = EgressGateway(transport=transport) if transport is not None else None
        broker = BrokerService(store, config, clock=clock or (lambda: 1000.0),
                               egress=egress, lease_ttl=lease_ttl,
                               lease_clock=lease_clock)
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


def multipart_parts(parts, *, closing: bool = True) -> bytes:
    body = bytearray()
    for headers, value in parts:
        body.extend(f"--{BOUNDARY}\r\n".encode())
        body.extend(headers)
        body.extend(b"\r\n\r\n")
        body.extend(value)
        body.extend(b"\r\n")
    if closing:
        body.extend(f"--{BOUNDARY}--\r\n".encode())
    return bytes(body)


async def parse_multipart_form(body: bytes, content_type: str):
    """用真实 Starlette/python-multipart 解析最终上游字节。"""
    from starlette.requests import Request

    messages = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [
            (b"content-type", content_type.encode("latin-1")),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    }, receive)
    return await request.form()


MODEL_HEADER = b'Content-Disposition: form-data; name="model"'
FILE_HEADER = (b'Content-Disposition: form-data; name="file"; '
               b'filename="a.wav"\r\nContent-Type: audio/wav')


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


def test_multipart_extended_parameters_cannot_rename_a_field(tmp_path):
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
                body=aiter_body(data, chunks=2))
            return b"".join([chunk async for chunk in response.chunks()])

        name_star = (b"Content-Disposition: form-data; name=\"ignored\"; "
                     b"name*=utf-8''model")
        name_star_only = b"Content-Disposition: form-data; name*=utf-8''model"
        continuation = (b"Content-Disposition: form-data; name*0=\"mo\"; "
                        b"name*1=\"del\"")
        filename_star = (b"Content-Disposition: form-data; name=\"file\"; "
                         b"filename*=utf-8''a.wav")
        escaped_filename = (b"Content-Disposition: form-data; name=\"file\"; "
                            b"filename=\"a\\\"; name=model\"")
        escaped_name = (b"Content-Disposition: form-data; "
                        b"name=\"mod\\\\el\"")
        spaced_name = (b"Content-Disposition: form-data; name=\" model \"")

        cases = [
            multipart_parts([
                (MODEL_HEADER, b"tts-default"),
                (name_star, b"forbidden-expensive-model"),
            ]),
            multipart_parts([
                (name_star_only, b"forbidden-expensive-model"),
                (MODEL_HEADER, b"tts-default"),
            ]),
            multipart_parts([(name_star_only, b"tts-default")]),
            multipart_parts([(continuation, b"tts-default")]),
            multipart_parts([
                (MODEL_HEADER, b"tts-default"),
                (filename_star, b"RIFF"),
            ]),
            multipart_parts([
                (MODEL_HEADER, b"tts-default"),
                (escaped_filename, b"RIFF"),
            ]),
            multipart_parts([(escaped_name, b"tts-default")]),
            multipart_parts([
                (MODEL_HEADER, b"tts-default"),
                (spaced_name, b"forbidden-expensive-model"),
            ]),
        ]
        for body in cases:
            record.pop("calls", None)
            with pytest.raises(DomainError) as invalid:
                asyncio.run(scenario(body))
            assert_error(invalid, 400, "invalid_request")
            assert record.get("calls", 0) == 0

        # 正常 multipart 仍能通过，且真实下游解析器只看到一个授权 model
        valid = multipart_parts([
            (MODEL_HEADER, b"tts-default"),
            (FILE_HEADER, b"RIFF-audio"),
        ])
        asyncio.run(scenario(valid))
        assert record["body"] == valid
        form = asyncio.run(parse_multipart_form(
            record["body"],
            f"multipart/form-data; boundary={BOUNDARY}"))
        assert form.getlist("model") == ["tts-default"]
        assert "file" in form


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


def test_duplicate_and_trailing_multipart_fields_are_rejected(tmp_path):
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
                body=aiter_body(data, chunks=3))
            return b"".join([chunk async for chunk in response.chunks()])

        cases = {
            # 同一 chunk 内重复 model
            "same_chunk": multipart_parts([
                (MODEL_HEADER, b"tts-default"),
                (MODEL_HEADER, b"forbidden-model"),
                (FILE_HEADER, b"RIFF"),
            ]),
            # 后置重复 model（跨 chunk 时在已缓冲前缀之后）
            "later_chunk": multipart_parts([
                (MODEL_HEADER, b"tts-default"),
                (FILE_HEADER, b"RIFF" * 4096),
                (MODEL_HEADER, b"forbidden-model"),
            ]),
            # model 之后追加禁用路由字段
            "trailing_forbidden": multipart_parts([
                (MODEL_HEADER, b"tts-default"),
                (FILE_HEADER, b"RIFF"),
                (b'Content-Disposition: form-data; name="base_url"',
                 b"https://evil.example/v1"),
            ]),
            # 文件字段名不能冒充 model
            "filename_shadow": multipart_parts([
                (b'Content-Disposition: form-data; name="file"; '
                 b'filename="model"', b"tts-default"),
                (MODEL_HEADER, b"forbidden-model"),
            ]),
            # 同一 part 重复 name 参数：上游选择哪个都不确定
            "duplicate_name_param": multipart_parts([
                (b'Content-Disposition: form-data; name="model"; '
                 b'name="file"', b"tts-default"),
            ]),
            # 缺少结束 boundary 的畸形 multipart
            "missing_closing": multipart_parts([
                (MODEL_HEADER, b"tts-default"),
                (FILE_HEADER, b"RIFF"),
            ], closing=False),
        }
        for name, body in cases.items():
            record.pop("calls", None)
            with pytest.raises(DomainError) as invalid:
                asyncio.run(scenario(body))
            assert_error(invalid, 400, "invalid_request")
            assert record.get("calls", 0) == 0, name


def test_large_multipart_is_spooled_and_forwarded_unchanged(tmp_path):
    record: dict = {}
    transport = capture_transport(record)
    with broker_env(tmp_path, {"speech": speech_route()},
                    transport=transport) as (
            store, config, broker, admin, member, run):
        token = broker.issue(member, run.id, "speech")
        file_bytes = b"RIFF" + b"\x00\xff" * (1024 * 1024)
        body = multipart_parts([
            (MODEL_HEADER, b"tts-default"),
            (FILE_HEADER, file_bytes),
        ])

        async def scenario():
            response = await broker.forward(
                token=token, workspace_id=member.workspace_id, run_id=run.id,
                channel="speech", method="POST",
                relative_path="audio/transcriptions",
                headers={
                    "content-type": f"multipart/form-data; boundary={BOUNDARY}",
                },
                body=aiter_body(body, chunks=16))
            return b"".join([chunk async for chunk in response.chunks()])

        asyncio.run(scenario())

    assert record["body"] == body
    assert record["calls"] == 1


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


class GatedTransport(httpx.AsyncBaseTransport):
    """在返回响应头前阻塞，用于验证前四阶段的租约丢失终止。"""

    def __init__(self):
        self.started = 0
        self.gate = asyncio.Event()
        self.two_started = asyncio.Event()

    async def handle_async_request(self, request):
        self.started += 1
        if self.started >= 2:
            self.two_started.set()
        await self.gate.wait()
        return httpx.Response(200, stream=ChunkStream([b"late"]))


class ReadGateStream(httpx.AsyncByteStream):
    """先产生一块，再等待 read_gate；用于让 anext 保持等待状态。"""

    def __init__(self, state):
        self.state = state

    async def __aiter__(self):
        yield b"one"
        await self.state["read_gate"].wait()
        yield b"two"

    async def aclose(self):
        self.state["stream_closed"] = True


class GatedCloseStream(ReadGateStream):
    """底层 stream.aclose 在 close_gate 放行前不完成。"""

    async def aclose(self):
        self.state["close_started"].set()
        await self.state["close_gate"].wait()
        self.state["closed"] = True


class GatedCloseTransport(httpx.AsyncBaseTransport):
    """底层 transport.aclose 在 close_gate 放行前不完成。"""

    def __init__(self, state):
        self.state = state

    async def handle_async_request(self, request):
        return httpx.Response(200, stream=ReadGateStream(self.state))

    async def aclose(self):
        self.state["close_started"].set()
        await self.state["close_gate"].wait()
        self.state["closed"] = True


def _close_gate_state() -> dict:
    return {
        "close_started": asyncio.Event(),
        "close_gate": asyncio.Event(),
        "read_gate": asyncio.Event(),
        "closed": False,
    }


def _fail_renew(lease_id, expires_at):
    raise RuntimeError("renew failed")


def raw_lease_count(store) -> int:
    """包含已过 TTL 但尚未完成清理的行；用于断言额度未被提前删除。"""
    return store._connection.execute(
        "SELECT COUNT(*) FROM broker_leases").fetchone()[0]


def test_cleanup_gate_holds_lease_until_response_stream_closes(tmp_path):
    state = _close_gate_state()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, stream=GatedCloseStream(state)))
    with broker_env(tmp_path, {"speech": speech_route()},
                    transport=transport, lease_ttl=3) as (
            store, config, broker, admin, member, run):
        token = broker.issue(member, run.id, "speech")
        original_renew = store.renew_broker_lease
        store.renew_broker_lease = _fail_renew

        async def scenario():
            response = await broker.forward(
                token=token, workspace_id=member.workspace_id, run_id=run.id,
                channel="speech", method="POST",
                relative_path="audio/speech",
                headers={"content-type": "application/json"},
                body=aiter_body(json.dumps(
                    {"model": "tts-default"}).encode()))
            chunks = response.chunks()
            assert await anext(chunks) == b"one"
            pending = asyncio.create_task(anext(chunks))

            await asyncio.wait_for(state["close_started"].wait(), 4)
            # gate 未放行：底层关闭未完成，租约不能提前回收
            assert not pending.done()
            assert raw_lease_count(store) == 1
            assert state["closed"] is False

            state["close_gate"].set()
            state["read_gate"].set()
            with pytest.raises(DomainError) as lost:
                await pending
            assert lost.value.code == "lease_lost"
            assert state["closed"] is True
            assert raw_lease_count(store) == 0
            assert response._guard._cleanup_task.done()
            assert response._guard._heartbeat.done()

        try:
            asyncio.run(scenario())
        finally:
            store.renew_broker_lease = original_renew


def test_cleanup_gate_holds_lease_until_transport_closes(tmp_path):
    state = _close_gate_state()
    transport = GatedCloseTransport(state)
    with broker_env(tmp_path, {"speech": speech_route()},
                    transport=transport, lease_ttl=3) as (
            store, config, broker, admin, member, run):
        token = broker.issue(member, run.id, "speech")
        original_renew = store.renew_broker_lease
        store.renew_broker_lease = _fail_renew

        async def scenario():
            response = await broker.forward(
                token=token, workspace_id=member.workspace_id, run_id=run.id,
                channel="speech", method="POST",
                relative_path="audio/speech",
                headers={"content-type": "application/json"},
                body=aiter_body(json.dumps(
                    {"model": "tts-default"}).encode()))
            chunks = response.chunks()
            assert await anext(chunks) == b"one"
            pending = asyncio.create_task(anext(chunks))

            await asyncio.wait_for(state["close_started"].wait(), 4)
            assert not pending.done()
            assert raw_lease_count(store) == 1
            assert state["stream_closed"] is True
            assert state["closed"] is False

            state["close_gate"].set()
            state["read_gate"].set()
            with pytest.raises(DomainError) as lost:
                await pending
            assert lost.value.code == "lease_lost"
            assert state["closed"] is True
            assert raw_lease_count(store) == 0
            assert response._guard._cleanup_task.done()
            assert response._guard._heartbeat.done()

        try:
            asyncio.run(scenario())
        finally:
            store.renew_broker_lease = original_renew


def test_cleanup_waits_after_reader_cancel_and_active_aclose(tmp_path):
    state = _close_gate_state()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, stream=GatedCloseStream(state)))
    with broker_env(tmp_path, {"speech": speech_route()},
                    transport=transport, lease_ttl=3) as (
            store, config, broker, admin, member, run):
        token = broker.issue(member, run.id, "speech")
        original_renew = store.renew_broker_lease
        store.renew_broker_lease = _fail_renew

        async def scenario():
            response = await broker.forward(
                token=token, workspace_id=member.workspace_id, run_id=run.id,
                channel="speech", method="POST",
                relative_path="audio/speech",
                headers={"content-type": "application/json"},
                body=aiter_body(json.dumps(
                    {"model": "tts-default"}).encode()))
            chunks = response.chunks()
            assert await anext(chunks) == b"one"
            pending = asyncio.create_task(anext(chunks))

            await asyncio.wait_for(state["close_started"].wait(), 4)
            state["read_gate"].set()
            pending.cancel()

            # 读取方取消后主动 aclose：仍等待同一次底层清理
            close_waiter = asyncio.create_task(response.aclose())
            await asyncio.sleep(0)
            assert not close_waiter.done()
            assert raw_lease_count(store) == 1
            assert state["closed"] is False

            state["close_gate"].set()
            await close_waiter
            assert state["closed"] is True
            assert raw_lease_count(store) == 0
            assert response._guard._cleanup_task.done()
            assert response._guard._heartbeat.done()

            # 被取消的读取任务也必须在清理完成后结束，不留 anext
            with pytest.raises((asyncio.CancelledError, DomainError)):
                await pending
            current = asyncio.current_task()
            leftovers = []
            for _ in range(5):
                await asyncio.sleep(0)
                leftovers = [
                    task for task in asyncio.all_tasks()
                    if task is not current and not task.done()]
                if not leftovers:
                    break
            assert not leftovers, leftovers

        try:
            asyncio.run(scenario())
        finally:
            store.renew_broker_lease = original_renew


def test_renewal_failure_before_response_headers_cancels_inflight(tmp_path):
    transport = GatedTransport()
    with broker_env(tmp_path, {"speech": speech_route()},
                    transport=transport, lease_ttl=3) as (
            store, config, broker, admin, member, run):
        token = broker.issue(member, run.id, "speech")
        original_renew = store.renew_broker_lease

        def failing_renew(lease_id, expires_at):
            raise RuntimeError("renew failed")

        store.renew_broker_lease = failing_renew

        async def open_call():
            return await broker.forward(
                token=token, workspace_id=member.workspace_id, run_id=run.id,
                channel="speech", method="POST",
                relative_path="audio/speech",
                headers={"content-type": "application/json"},
                body=aiter_body(json.dumps(
                    {"model": "tts-default"}).encode()))

        async def scenario():
            first = asyncio.create_task(open_call())
            second = asyncio.create_task(open_call())
            await asyncio.wait_for(transport.two_started.wait(), 3)
            assert store.count_broker_leases(int(time.time())) == 2

            results = await asyncio.wait_for(
                asyncio.gather(first, second, return_exceptions=True), 4)
            for result in results:
                assert isinstance(result, DomainError), result
                assert result.status == 503, result
                assert result.code == "lease_lost", result
            assert store.count_broker_leases(int(time.time())) == 0

            # 旧请求已被取消，不能随后成功；额度可被新请求重新接纳
            transport.gate.set()
            third = await open_call()
            fourth = await open_call()
            assert transport.started == 4
            await third.aclose()
            await fourth.aclose()
            assert store.count_broker_leases(int(time.time())) == 0

        try:
            asyncio.run(scenario())
        finally:
            store.renew_broker_lease = original_renew


def test_zero_row_renewal_terminates_open_stream(tmp_path):
    transport = capture_transport({}, chunks=(b"one", b"two"))
    with broker_env(tmp_path, {"speech": speech_route()},
                    transport=transport, lease_ttl=3) as (
            store, config, broker, admin, member, run):
        token = broker.issue(member, run.id, "speech")

        async def open_call():
            return await broker.forward(
                token=token, workspace_id=member.workspace_id, run_id=run.id,
                channel="speech", method="POST",
                relative_path="audio/speech",
                headers={"content-type": "application/json"},
                body=aiter_body(json.dumps(
                    {"model": "tts-default"}).encode()))

        async def scenario():
            first = await open_call()
            second = await open_call()
            assert store.count_broker_leases(int(time.time())) == 2

            # 模拟另一连接回收旧租约（到期清理/重新接纳）
            rows = store._connection.execute(
                "SELECT id FROM broker_leases").fetchall()
            for row in rows:
                store.release_broker_lease(row["id"])

            third = await open_call()
            fourth = await open_call()
            assert store.count_broker_leases(int(time.time())) == 2

            # 下一次 heartbeat UPDATE 0 行，旧流必须被终止
            await asyncio.sleep(1.3)
            for response in (first, second):
                with pytest.raises(DomainError) as lost:
                    await anext(response.chunks())
                assert lost.value.code == "lease_lost"
                assert lost.value.status == 503

            await third.aclose()
            await fourth.aclose()
            assert store.count_broker_leases(int(time.time())) == 0

        asyncio.run(scenario())


def test_open_streams_keep_capacity_after_token_expiry(tmp_path):
    record: dict = {}
    transport = capture_transport(record, chunks=(b"one", b"two"))
    now = {"value": 1000.0}
    with broker_env(tmp_path, {"speech": speech_route()},
                    transport=transport, clock=lambda: now["value"]) as (
            store, config, broker, admin, member, run):
        token1 = broker.issue(member, run.id, "speech")

        async def open_call(token):
            return await broker.forward(
                token=token, workspace_id=member.workspace_id, run_id=run.id,
                channel="speech", method="POST",
                relative_path="audio/speech",
                headers={"content-type": "application/json"},
                body=aiter_body(json.dumps(
                    {"model": "tts-default"}).encode()))

        async def scenario():
            first = await open_call(token1)
            second = await open_call(token1)
            assert store.count_broker_leases(int(time.time())) == 2

            now["value"] = 2800.0
            token2 = broker.issue(member, run.id, "speech")
            with pytest.raises(DomainError) as over:
                await open_call(token2)
            assert_error(over, 429, "workspace_capacity")

            await first.aclose()
            await second.aclose()
            assert store.count_broker_leases(int(time.time())) == 0
            third = await open_call(token2)
            await third.aclose()

        asyncio.run(scenario())


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
