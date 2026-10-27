"""Task 5 受限出口策略：HTTPS、公网地址、管理员例外与解析期校验。"""
from __future__ import annotations

import json

import httpx
import pytest

from easel.control.egress import (
    EgressGateway,
    EgressPolicy,
    load_policy,
    parse_proxy_authority,
)
from easel.control.types import DomainError

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:4700:4700::1111"


def assert_error(error: pytest.ExceptionInfo, status: int, code: str = "") -> None:
    assert isinstance(error.value, DomainError)
    assert error.value.status == status, error.value
    if code:
        assert error.value.code == code


def test_public_https_is_accepted_without_dns_side_effects():
    policy = EgressPolicy()
    policy.validate_url("https://api.deepseek.com/v1")
    policy.validate_url(f"https://{PUBLIC_V4}/v1")
    policy.validate_url(f"https://[{PUBLIC_V6}]/v1")


@pytest.mark.parametrize("url", [
    "http://provider.example/v1",
    "file:///etc/passwd",
    "https://user:pass@provider.example/v1",
    "https://provider.example/v1?token=1",
    "https://provider.example/v1#fragment",
    "https://provider.example/v1\\@evil.example",
    "https://provider.example/v1 ",
])
def test_invalid_or_non_https_urls_are_rejected(url):
    with pytest.raises(DomainError) as invalid:
        EgressPolicy().validate_url(url)
    assert invalid.value.status in (400, 403)


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1",
    "169.254.169.254", "0.0.0.0", "224.0.0.1", "192.0.2.1",
    "::1", "::", "fe80::1", "fc00::1", "ff02::1",
    "::ffff:127.0.0.1", "::ffff:10.0.0.1",
])
def test_private_loopback_metadata_and_mapped_addresses_are_rejected(address):
    host = f"[{address}]" if ":" in address else address
    with pytest.raises(DomainError) as denied:
        EgressPolicy().validate_url(f"https://{host}/v1")
    assert_error(denied, 403, "destination_denied")


@pytest.mark.parametrize("host", [
    "localhost", "api.localhost", "metadata", "metadata.google.internal",
    "instance-data", "kubernetes.default",
])
def test_known_control_plane_hostnames_are_rejected(host):
    with pytest.raises(DomainError) as denied:
        EgressPolicy().validate_url(f"https://{host}/v1")
    assert_error(denied, 403, "destination_denied")


@pytest.mark.parametrize("host", [
    "control", "worker", "provisioner", "metadata.google.internal",
])
def test_control_plane_hostnames_are_denied_at_connect_time(host):
    policy = EgressPolicy(resolver=lambda name, port: ["10.0.0.5"])
    with pytest.raises(DomainError) as denied:
        policy.validate_url(f"https://{host}/", resolve=True)
    assert_error(denied, 403, "destination_denied")


def test_admin_approved_host_and_network_exceptions_are_exact():
    policy = EgressPolicy(
        approved_hosts=frozenset({"provider.internal"}),
        approved_networks=(__import__("ipaddress").ip_network("10.20.0.0/16"),))
    # 管理员批准的主机允许内网 HTTPS
    policy.validate_url("https://provider.internal/v1")
    # 精确批准网段允许其中的地址
    policy.validate_url("https://10.20.3.4/v1")
    # 未被批准的同网段外地址仍拒绝
    with pytest.raises(DomainError):
        policy.validate_url("https://10.21.3.4/v1")
    # 批准名单不会扩大到相似主机：连接阶段解析到私网仍拒绝
    with pytest.raises(DomainError):
        EgressPolicy(
            approved_hosts=frozenset({"provider.internal"}),
            resolver=lambda host, port: ["10.0.0.5"],
        ).validate_url("https://provider.internal.evil.example/v1",
                       resolve=True)


def test_resolved_addresses_are_all_validated_and_pinned():
    policy = EgressPolicy(resolver=lambda host, port: [PUBLIC_V4])
    assert policy.resolve_and_validate("provider.example", 443) == PUBLIC_V4

    policy = EgressPolicy(resolver=lambda host, port: ["127.0.0.1"])
    with pytest.raises(DomainError) as denied:
        policy.resolve_and_validate("provider.example", 443)
    assert_error(denied, 403, "destination_denied")

    # 解析结果混入私网时必须整体拒绝，避免轮询到控制面
    policy = EgressPolicy(
        resolver=lambda host, port: [PUBLIC_V4, "10.0.0.5", "::1"])
    with pytest.raises(DomainError) as mixed:
        policy.resolve_and_validate("provider.example", 443)
    assert_error(mixed, 403, "destination_denied")

    policy = EgressPolicy(resolver=lambda host, port: [])
    with pytest.raises(DomainError) as empty:
        policy.resolve_and_validate("provider.example", 443)
    assert_error(empty, 502, "destination_unresolved")

    def failing(host, port):
        raise OSError("dns down")

    with pytest.raises(DomainError) as unresolved:
        EgressPolicy(resolver=failing).resolve_and_validate("provider.example", 443)
    assert_error(unresolved, 502, "destination_unresolved")


def test_approved_host_or_network_can_resolve_to_private_addresses():
    policy = EgressPolicy(
        approved_hosts=frozenset({"provider.internal"}),
        resolver=lambda host, port: ["10.0.0.5"])
    assert policy.resolve_and_validate("provider.internal", 443) == "10.0.0.5"

    import ipaddress
    policy = EgressPolicy(
        approved_networks=(ipaddress.ip_network("10.0.0.0/8"),),
        resolver=lambda host, port: ["10.0.0.5"])
    assert policy.resolve_and_validate("provider.example", 443) == "10.0.0.5"


def test_validate_url_with_resolve_runs_connection_stage_check():
    policy = EgressPolicy(resolver=lambda host, port: ["169.254.169.254"])
    with pytest.raises(DomainError) as denied:
        policy.validate_url("https://provider.example/v1", resolve=True)
    assert_error(denied, 403, "destination_denied")


def test_gateway_requires_explicit_proxy_or_injected_transport():
    with pytest.raises(DomainError) as unavailable:
        EgressGateway().client()
    assert_error(unavailable, 503, "egress_unavailable")


def test_proxy_without_explicit_policy_fails_closed():
    gateway = EgressGateway(proxy_url="http://127.0.0.1:8899")
    assert gateway.configured is False
    with pytest.raises(DomainError) as unavailable:
        gateway.client()
    assert_error(unavailable, 503, "egress_unavailable")


def test_explicit_empty_policy_with_proxy_is_allowed():
    gateway = EgressGateway(EgressPolicy(),
                            proxy_url="http://127.0.0.1:8899")
    assert gateway.configured is True
    client = gateway.client()
    try:
        assert isinstance(client, httpx.AsyncClient)
    finally:
        import asyncio
        asyncio.run(client.aclose())


def test_injected_transport_is_allowed_without_policy():
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    gateway = EgressGateway(transport=transport)
    assert gateway.configured is True
    client = gateway.client()
    try:
        assert isinstance(client, httpx.AsyncClient)
    finally:
        import asyncio
        asyncio.run(client.aclose())


def test_proxy_url_requires_explicit_valid_authority():
    for proxy in ("http://127.0.0.1", "http://user:pass@127.0.0.1:8899",
                  "http://127.0.0.1:8899/path", "ftp://127.0.0.1:8899"):
        with pytest.raises(ValueError):
            EgressGateway(proxy_url=proxy)

    gateway = EgressGateway(proxy_url="http://127.0.0.1:8899")
    assert gateway.proxy_url == "http://127.0.0.1:8899"


def test_proxy_authority_parser_handles_ipv4_and_ipv6():
    assert parse_proxy_authority("provider.example:443") == (
        "provider.example", 443)
    assert parse_proxy_authority("[::1]:443") == ("::1", 443)
    for invalid in ("provider.example", "provider.example:0",
                    "provider.example:70000", "provider.example:abc",
                    "[::1]443", ""):
        with pytest.raises(ValueError):
            parse_proxy_authority(invalid)


def test_policy_file_loader_is_strict(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({
        "approved_hosts": ["provider.internal"],
        "approved_cidrs": ["10.20.0.0/16"],
    }), encoding="utf-8")
    policy = load_policy(path)
    assert "provider.internal" in policy.approved_hosts
    assert policy.approved_networks

    for payload in (
        {"approved_hosts": "not-a-list"},
        {"approved_cidrs": ["not-a-cidr"]},
        {"unknown": []},
        [],
    ):
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError):
            load_policy(path)

    missing = tmp_path / "missing.json"
    with pytest.raises(ValueError):
        load_policy(missing)


def test_load_policy_accepts_utf8_bom(tmp_path):
    from easel.control.egress import load_policy

    path = tmp_path / "policy.json"
    path.write_bytes(
        b"\xef\xbb\xbf" +
        b'{"approved_hosts":["example.test"],"approved_cidrs":[]}')
    policy = load_policy(path)
    assert "example.test" in policy.approved_hosts


def test_egress_gateway_rejects_missing_ca_file(tmp_path):
    from easel.control.egress import EgressGateway

    with pytest.raises(ValueError):
        EgressGateway(ca_file=tmp_path / "missing.pem")
    ca = tmp_path / "ca.pem"
    ca.write_bytes(b"not-a-cert")
    gateway = EgressGateway(
        policy=None, proxy_url="http://127.0.0.1:8899", ca_file=ca)
    assert gateway.ca_file == str(ca)
