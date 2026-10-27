"""Task G1 gateway 控制断言协议：域、字段绑定、时间窗与 nonce 单次接纳。"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from easel.control.gateway_auth import build_gateway_assertion_headers
from easel.gateway.auth import (
    GATEWAY_ASSERTION_DOMAIN,
    GATEWAY_ASSERTION_HEADER,
    GatewayAudience,
    GatewayConfig,
    GatewayError,
    GatewayNonceCache,
    canonical_query,
    new_boot_generation,
    sign_gateway_assertion,
    verify_gateway_assertion,
)

DOMAIN = GATEWAY_ASSERTION_DOMAIN.encode("ascii") + b"\0"
NOW = 1_750_000_000
WORKSPACE = "a" * 32
GATEWAY = "gateway-instance-1"
BOOT = "b" * 32
WORKER = "c" * 64
TOKEN = "service-token-secret"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _raw_token(private_key: Ed25519PrivateKey, payload: bytes) -> str:
    signature = private_key.sign(DOMAIN + payload)
    return f"v1.{_b64(payload)}.{_b64(signature)}"


def _payload(audience: GatewayAudience, **overrides) -> dict:
    value = {
        "workspace_id": audience.workspace_id,
        "gateway_instance_id": audience.gateway_instance_id,
        "boot_generation": audience.boot_generation,
        "worker_container_id": audience.worker_container_id,
        "config_version": audience.config_version,
        "method": "GET",
        "path": "/internal/lifecycle/health",
        "canonical_query": "",
        "body_sha256": hashlib.sha256(b"").hexdigest(),
        "issued_at": NOW,
        "expires_at": NOW + 30,
        "nonce": "nonce-" + "n" * 20,
        "request_id": "request-" + "r" * 20,
    }
    value.update(overrides)
    return value


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _audience() -> GatewayAudience:
    return GatewayAudience(WORKSPACE, GATEWAY, BOOT, WORKER, 7)


def _verify(public_key, token: str, audience: GatewayAudience, *,
            method: str = "GET",
            path: str = "/internal/lifecycle/health",
            query: str = "", body: bytes = b"", now: int = NOW):
    return verify_gateway_assertion(
        public_key, token, audience=audience, method=method, path=path,
        canonical_query=query, body_sha256=hashlib.sha256(body).hexdigest(),
        now=now)


def test_sign_and_verify_binds_the_exact_request():
    private_key = Ed25519PrivateKey.generate()
    audience = _audience()
    body = b'{"run":"x"}'
    token = sign_gateway_assertion(
        private_key, audience, method="POST",
        path="/internal/runs/" + "d" * 32, query={"after": "3"},
        body=body, issued_at=NOW, expires_at=NOW + 30,
        nonce="nonce-fixed", request_id="request-fixed")

    assertion = _verify(
        private_key.public_key(), token, audience, method="POST",
        path="/internal/runs/" + "d" * 32, query="after=3", body=body)

    assert assertion.workspace_id == WORKSPACE
    assert assertion.gateway_instance_id == GATEWAY
    assert assertion.boot_generation == BOOT
    assert assertion.worker_container_id == WORKER
    assert assertion.config_version == 7
    assert assertion.method == "POST"
    assert assertion.canonical_query == "after=3"
    assert assertion.body_sha256 == hashlib.sha256(body).hexdigest()
    assert assertion.nonce == "nonce-fixed"
    assert assertion.request_id == "request-fixed"
    assert GATEWAY_ASSERTION_HEADER == "X-Easel-Gateway-Assertion"
    assert token.startswith("v1.")
    assert "=" not in token


def test_canonical_query_is_sorted_and_percent_encoded():
    assert canonical_query({"nonce": "a/b c", "after": "0"}) == \
        "after=0&nonce=a%2Fb%20c"
    assert canonical_query({}) == ""


@pytest.mark.parametrize("field,change", [
    ("workspace_id", "b" * 32),
    ("gateway_instance_id", "gateway-instance-2"),
    ("boot_generation", "e" * 32),
    ("worker_container_id", "f" * 64),
    ("config_version", 8),
])
def test_assertion_rejects_any_audience_field_mismatch(field, change):
    private_key = Ed25519PrivateKey.generate()
    audience = _audience()
    payload = _payload(audience)
    payload[field] = change
    token = _raw_token(private_key, _canonical(payload))

    with pytest.raises(GatewayError) as denied:
        _verify(private_key.public_key(), token, audience)
    assert denied.value.status == 403


@pytest.mark.parametrize("change", [
    {"method": "POST"},
    {"path": "/internal/runs/" + "d" * 32},
    {"canonical_query": "after=1"},
    {"body_sha256": hashlib.sha256(b"other").hexdigest()},
])
def test_assertion_rejects_request_field_mismatch(change):
    private_key = Ed25519PrivateKey.generate()
    audience = _audience()
    payload = _payload(audience, **change)
    token = _raw_token(private_key, _canonical(payload))

    with pytest.raises(GatewayError) as denied:
        _verify(private_key.public_key(), token, audience)
    assert denied.value.status == 403


def test_assertion_rejects_wrong_public_key_and_corrupt_signature():
    private_key = Ed25519PrivateKey.generate()
    audience = _audience()
    token = _raw_token(private_key, _canonical(_payload(audience)))

    with pytest.raises(GatewayError) as denied:
        _verify(Ed25519PrivateKey.generate().public_key(), token, audience)
    assert denied.value.status == 401

    prefix, payload, signature = token.split(".")
    flipped = signature[:-1] + ("A" if signature[-1] != "A" else "B")
    with pytest.raises(GatewayError) as denied:
        _verify(private_key.public_key(),
                f"{prefix}.{payload}.{flipped}", audience)
    assert denied.value.status == 401


@pytest.mark.parametrize("payload", [
    b'{"workspace_id":"x"}',
    b'{"workspace_id":"x","workspace_id":"y"}',
    b'{ "workspace_id": "x" }',
])
def test_assertion_rejects_non_canonical_duplicate_or_malformed_json(payload):
    private_key = Ed25519PrivateKey.generate()
    token = _raw_token(private_key, payload)

    with pytest.raises(GatewayError) as denied:
        _verify(private_key.public_key(), token, _audience())
    assert denied.value.status == 401


def test_assertion_rejects_non_finite_numbers():
    private_key = Ed25519PrivateKey.generate()
    payload = _payload(_audience())
    payload["expires_at"] = float("nan")
    raw = json.dumps(payload, sort_keys=True,
                     separators=(",", ":")).encode("ascii")
    token = _raw_token(private_key, raw)

    with pytest.raises(GatewayError) as denied:
        _verify(private_key.public_key(), token, _audience())
    assert denied.value.status == 401


def test_assertion_rejects_unknown_and_missing_fields():
    private_key = Ed25519PrivateKey.generate()
    audience = _audience()
    extra = _payload(audience, unexpected=True)
    missing = _payload(audience)
    missing.pop("request_id")

    for payload in (extra, missing):
        token = _raw_token(private_key, _canonical(payload))
        with pytest.raises(GatewayError) as denied:
            _verify(private_key.public_key(), token, audience)
        assert denied.value.status == 401


@pytest.mark.parametrize("field,value", [
    ("config_version", True),
    ("config_version", "7"),
    ("issued_at", "1"),
    ("expires_at", None),
    ("nonce", "x" * 129),
    ("request_id", ""),
    ("method", "get"),
    ("path", "internal/lifecycle/health"),
    ("body_sha256", "not-a-hash"),
])
def test_assertion_rejects_invalid_field_types_and_shapes(field, value):
    private_key = Ed25519PrivateKey.generate()
    audience = _audience()
    payload = _payload(audience)
    payload[field] = value
    token = _raw_token(private_key, _canonical(payload))

    with pytest.raises(GatewayError) as denied:
        _verify(private_key.public_key(), token, audience)
    assert denied.value.status == 401


def test_assertion_rejects_expired_future_and_overlong_lifetime():
    private_key = Ed25519PrivateKey.generate()
    audience = _audience()
    for issued_at, expires_at in (
            (NOW - 60, NOW - 30),
            (NOW + 6, NOW + 36),
            (NOW, NOW + 31)):
        payload = _payload(
            audience, issued_at=issued_at, expires_at=expires_at)
        token = _raw_token(private_key, _canonical(payload))
        with pytest.raises(GatewayError) as denied:
            _verify(private_key.public_key(), token, audience)
        assert denied.value.status == 401


def test_assertion_accepts_exact_ttl_and_future_skew_boundary():
    private_key = Ed25519PrivateKey.generate()
    audience = _audience()
    exact = _raw_token(private_key, _canonical(_payload(
        audience, issued_at=NOW, expires_at=NOW + 30)))
    assert _verify(private_key.public_key(), exact, audience).nonce

    skewed = _raw_token(private_key, _canonical(_payload(
        audience, issued_at=NOW + 5, expires_at=NOW + 35)))
    assert _verify(private_key.public_key(), skewed, audience).nonce


@pytest.mark.parametrize("token", [
    "",
    "v1",
    "v1.payload",
    "v1..signature",
    "v1.payload.signature.extra",
    "v1." + "A" * 5000 + ".signature",
    "v1." + "A" * 4 + "=.signature",
    "v1.***.signature",
])
def test_assertion_rejects_malformed_header_encoding(token):
    with pytest.raises(GatewayError) as denied:
        _verify(Ed25519PrivateKey.generate().public_key(), token, _audience())
    assert denied.value.status == 401


def test_signer_rejects_lifetime_over_thirty_seconds():
    with pytest.raises(GatewayError) as denied:
        sign_gateway_assertion(
            Ed25519PrivateKey.generate(), _audience(), method="GET",
            path="/internal/lifecycle/health", body=b"",
            issued_at=NOW, expires_at=NOW + 31)
    assert denied.value.status == 400


def test_nonce_cache_rejects_duplicate_and_never_evicts_valid_entries():
    cache = GatewayNonceCache(capacity=1)
    cache.accept("first", expires_at=101, now=100)

    with pytest.raises(GatewayError) as replay:
        cache.accept("first", expires_at=101, now=100)
    assert replay.value.code == "gateway_replay"
    assert replay.value.status == 401

    with pytest.raises(GatewayError) as full:
        cache.accept("second", expires_at=101, now=100)
    assert full.value.code == "gateway_nonce_capacity"
    assert full.value.status == 503

    # 有效 nonce 不被驱逐；过期后才允许新 nonce 进入
    cache.accept("second", expires_at=102, now=101)
    assert cache.active_count == 1


def test_nonce_cache_single_accept_is_thread_atomic():
    cache = GatewayNonceCache(capacity=16)
    barrier = threading.Barrier(8)
    outcomes: list[str] = []
    lock = threading.Lock()

    def contender() -> None:
        barrier.wait()
        try:
            cache.accept("same-nonce", expires_at=101, now=100)
        except GatewayError as error:
            outcome = error.code
        else:
            outcome = "accepted"
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=contender) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(outcomes) == 8
    assert outcomes.count("accepted") == 1
    assert outcomes.count("gateway_replay") == 7


def test_new_boot_generation_is_fresh_and_hex():
    first = new_boot_generation()
    second = new_boot_generation()
    assert first != second
    assert len(first) == 32
    assert all(character in "0123456789abcdef" for character in first)


def test_gateway_config_hides_token_and_validates_origin():
    config = GatewayConfig(
        workspace_id=WORKSPACE,
        gateway_instance_id=GATEWAY,
        boot_generation=BOOT,
        worker_container_id=WORKER,
        config_version=7,
        worker_origin="http://127.0.0.1:7860/",
        worker_service_token=TOKEN,
    )
    assert TOKEN not in repr(config)
    assert config.audience == _audience()
    assert config.worker_origin == "http://127.0.0.1:7860"

    for origin in (
            "http://user:pass@127.0.0.1:7860",
            "http://127.0.0.1:7860/path",
            "http://127.0.0.1:7860?x=1",
            "http://127.0.0.1:7860#fragment",
            "http://127.0.0.1:7860?",
            "http://127.0.0.1:7860#",
            "http://127.0.0.1:0",
            "ftp://127.0.0.1:7860",
            "http://"):
        with pytest.raises((GatewayError, ValueError)):
            GatewayConfig(
                workspace_id=WORKSPACE,
                gateway_instance_id=GATEWAY,
                boot_generation=BOOT,
                worker_container_id=WORKER,
                config_version=7,
                worker_origin=origin,
                worker_service_token=TOKEN,
            )


def test_gateway_audience_rejects_invalid_identifiers():
    for values in (
            ("", GATEWAY, BOOT, WORKER, 7),
            (WORKSPACE, "bad id", BOOT, WORKER, 7),
            (WORKSPACE, GATEWAY, "", WORKER, 7),
            (WORKSPACE, GATEWAY, BOOT, "short", 7),
            (WORKSPACE, GATEWAY, BOOT, WORKER, True),
            (WORKSPACE, GATEWAY, BOOT, WORKER, -1)):
        with pytest.raises((GatewayError, ValueError)):
            GatewayAudience(*values)


def test_sign_and_verify_reject_non_ascii_or_noncanonical_paths():
    private_key = Ed25519PrivateKey.generate()
    audience = _audience()
    for path in (
            "internal/lifecycle/health",
            "/internal//lifecycle/health",
            "/internal/lifecycle/health/",
            "/internal/lifecycle/health?x=1",
            "/internal/中",
            "/internal/lifecycle/health\x00"):
        with pytest.raises(GatewayError):
            sign_gateway_assertion(
                private_key, audience, method="GET", path=path)


def test_verify_is_independent_of_private_key_object():
    private_key = Ed25519PrivateKey.generate()
    token = sign_gateway_assertion(
        private_key, _audience(), method="GET",
        path="/internal/lifecycle/health",
        issued_at=NOW, expires_at=NOW + 30)
    public = private_key.public_key()
    assert _verify(public, token, _audience()).nonce


def test_dataclass_replace_can_rebuild_audience_without_mutation():
    audience = _audience()
    changed = replace(audience, config_version=8)
    assert audience.config_version == 7
    assert changed.config_version == 8


def test_control_signing_tool_emits_exactly_one_internal_header():
    private_key = Ed25519PrivateKey.generate()
    audience = _audience()
    headers = build_gateway_assertion_headers(
        private_key, audience, method="GET",
        path="/internal/lifecycle/health", body=b"",
        issued_at=NOW, expires_at=NOW + 30,
        nonce="control-nonce", request_id="control-request")
    assert set(headers) == {GATEWAY_ASSERTION_HEADER}
    assertion = _verify(
        private_key.public_key(), headers[GATEWAY_ASSERTION_HEADER], audience)
    assert assertion.nonce == "control-nonce"
    assert assertion.request_id == "control-request"
