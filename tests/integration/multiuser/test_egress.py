"""真实出口门槛：必须显式选择，缺环境直接失败而不是 skip 当通过。

普通测试不联网。运行：

    EASEL_EGRESS_LIVE=1 \
    EASEL_TEST_EGRESS_PROXY=http://127.0.0.1:8899 \
    python -m pytest tests/integration/multiuser/test_egress.py -q
"""
from __future__ import annotations

import os

import httpx
import pytest

LIVE = os.environ.get("EASEL_EGRESS_LIVE") == "1"

pytestmark = pytest.mark.skipif(
    not LIVE,
    reason="真实出口门槛未执行：设置 EASEL_EGRESS_LIVE=1 并启动受控出口代理")


def _proxy() -> str:
    value = os.environ.get("EASEL_TEST_EGRESS_PROXY", "")
    if not value:
        pytest.fail("EASEL_EGRESS_LIVE=1 但缺少 EASEL_TEST_EGRESS_PROXY")
    return value


def _client() -> httpx.Client:
    return httpx.Client(proxy=_proxy(), trust_env=False, timeout=15.0)


def test_public_https_connect_is_allowed():
    with _client() as client:
        response = client.get("https://example.com/")
    assert response.status_code == 200


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/",
    "https://[::1]/",
    "https://169.254.169.254/",
    "https://localtest.me/",  # DNS 解析到 127.0.0.1，验证重绑定路径
])
def test_private_loopback_link_local_and_metadata_targets_are_denied(url):
    with _client() as client:
        with pytest.raises(httpx.HTTPError):
            client.get(url)


def test_plain_http_absolute_form_is_denied():
    # 代理只接受 CONNECT；绝对形式 HTTP 请求得到明确的 405，不会被转发
    with _client() as client:
        response = client.get("http://example.com/")
    assert response.status_code == 405
