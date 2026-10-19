"""Task 5 首批 provider adapter：精确路径、协议和上游认证契约。"""
from __future__ import annotations

import pytest

from easel.control.types import DomainError, EffectiveRoute
from easel.providers.openai import (
    DeepSeekChatAdapter,
    OpenAIImageAdapter,
    OpenAISpeechAdapter,
    adapter_for_route,
    adapter_supported,
    support_matrix,
)


def route(channel: str = "speech", **overrides) -> EffectiveRoute:
    values = {
        "channel": channel,
        "provider": {"speech": "openai-compatible", "image": "openai",
                     "chat": "deepseek"}[channel],
        "model": "model-default",
        "base_url": "https://provider.example/v1",
        "protocol": "openai",
        "credential_ref": "snapshot:test",
        "source": "admin",
        "version": 1,
        "options": {},
        "allowed_models": ("model-default",),
    }
    values.update(overrides)
    return EffectiveRoute(**values)


def assert_error(error: pytest.ExceptionInfo, status: int, code: str = "") -> None:
    assert isinstance(error.value, DomainError)
    assert error.value.status == status, error.value
    if code:
        assert error.value.code == code, error.value


def test_route_matrix_is_exact_and_has_no_v1_prefix_passthrough():
    expected = {
        DeepSeekChatAdapter: {("POST", "chat/completions"), ("GET", "models")},
        OpenAIImageAdapter: {("POST", "images/generations"), ("GET", "models")},
        OpenAISpeechAdapter: {
            ("POST", "audio/speech"), ("POST", "audio/transcriptions"),
            ("GET", "models")},
    }
    for adapter_cls, routes in expected.items():
        adapter = adapter_cls({})
        assert {(item.method, item.relative_path)
                for item in adapter.routes} == routes
        for method, path in routes:
            assert adapter.accepts(method, path)
        for method, path in (
            ("POST", "v1/chat/completions"),
            ("POST", "/chat/completions"),
            ("POST", "chat/../chat/completions"),
            ("POST", "chat/completions?x=1"),
            ("POST", "https://evil.example/chat/completions"),
            ("POST", "chat/completions/extra"),
            ("DELETE", "chat/completions"),
        ):
            assert not adapter.accepts(method, path), (method, path)


def test_prepare_rebuilds_auth_and_ignores_client_identity_headers():
    adapter = OpenAISpeechAdapter({"VOICE_API_KEY": "voice-secret-sentinel"})
    prepared = adapter.prepare(
        route(), "POST", "audio/speech",
        {"content-type": "application/json", "accept": "audio/mpeg",
         "authorization": "Bearer client", "host": "evil.example",
         "x-forwarded-for": "203.0.113.9", "cookie": "c=1"})
    headers = {name.lower(): value for name, value in prepared.headers.items()}
    assert prepared.method == "POST"
    assert prepared.url == "https://provider.example/v1/audio/speech"
    assert headers["authorization"] == "Bearer voice-secret-sentinel"
    assert headers["content-type"] == "application/json"
    assert headers["accept"] == "audio/mpeg"
    for forbidden in ("cookie", "x-forwarded-for", "host"):
        assert forbidden not in headers
    # 上游认证属于敏感信息，默认 repr 不显示
    assert "voice-secret-sentinel" not in repr(prepared)


def test_prepare_rejects_protocol_and_unsafe_base_urls():
    adapter = OpenAISpeechAdapter({"VOICE_API_KEY": "secret"})
    with pytest.raises(DomainError) as unsupported:
        adapter.prepare(route(protocol="anthropic"), "POST", "audio/speech", {})
    assert_error(unsupported, 503, "capability_unavailable")

    for base_url in ("http://provider.example/v1", "file:///etc/passwd",
                     "https://user:pass@provider.example/v1",
                     "https://provider.example/v1?x=1",
                     "https://provider.example/v1#fragment"):
        with pytest.raises(DomainError) as invalid:
            adapter.prepare(route(base_url=base_url), "POST", "audio/speech", {})
        assert_error(invalid, 400, "invalid_destination")


def test_image_auth_header_and_api_version_are_controlled_by_bundle():
    adapter = OpenAIImageAdapter({
        "IMG_API_KEY": "image-secret",
        "IMG_API_KEY_HEADER": "api-key",
        "IMG_API_VERSION": "2024-01-01",
    })
    prepared = adapter.prepare(
        route("image"), "POST", "images/generations",
        {"content-type": "application/json"})
    headers = {name.lower(): value for name, value in prepared.headers.items()}
    assert prepared.url == (
        "https://provider.example/v1/images/generations?api-version=2024-01-01")
    assert headers["api-key"] == "image-secret"
    assert "authorization" not in headers

    unsafe = OpenAIImageAdapter({
        "IMG_API_KEY": "image-secret", "IMG_API_KEY_HEADER": "bad header"})
    with pytest.raises(DomainError) as invalid:
        unsafe.prepare(route("image"), "POST", "images/generations", {})
    assert_error(invalid, 500, "secret_unavailable")


def test_support_matrix_reflects_actual_adapters_not_just_metadata():
    assert adapter_supported("chat", "deepseek") is True
    assert adapter_supported("image", "openai") is True
    assert adapter_supported("voice", "openai-compatible") is True
    assert adapter_supported("video", "dashscope") is False
    assert adapter_supported("music", "dashscope") is False

    matrix = {(item["group"], item["provider"]): item
              for item in support_matrix()}
    chat = matrix[("chat", "deepseek")]
    assert chat["supported"] is True
    assert chat["override_allowed"] is False
    assert chat["protocols"] == ["openai"]
    assert {item["path"] for item in chat["routes"]} == {
        "chat/completions", "models"}
    video = matrix[("video", "dashscope")]
    assert video["supported"] is False
    assert video["routes"] == []

    # adapter_for_route 同样按实际实现判断
    with pytest.raises(DomainError) as unavailable:
        adapter_for_route(route("image", provider="dashscope"))
    assert_error(unavailable, 503, "capability_unavailable")
