"""T14-R1-04：live SSE 成功断言对失败终态和部分输出必须敏感。

只用 MockTransport 模拟控制面 SSE，不连接真实服务；用于证明 live
双用户测试不会把 B 的 failed/interrupted/cancelled 当成成功。
"""
from __future__ import annotations

import httpx
import pytest

from tests.integration.multiuser.test_two_users_live import _collect, _state


def _client(body: str) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"},
            content=body.encode("utf-8"))

    return httpx.Client(
        base_url="https://live.test", transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("terminal", ["failed", "interrupted", "cancelled"])
def test_collect_rejects_non_done_terminal_even_with_partial_tokens(terminal):
    body = (
        'event: token\ndata: "partial reply"\n\n'
        f'event: state\ndata: {{"state":"{terminal}"}}\n\n')
    with _client(body) as client, pytest.raises(pytest.fail.Exception):
        _collect(client, "/api/chat/jobs/run/stream", {})


def test_collect_accepts_done_terminal_with_tokens():
    body = (
        'event: token\ndata: "complete reply"\n\n'
        'event: state\ndata: {"state":"done"}\n\n')
    with _client(body) as client:
        frames = _collect(client, "/api/chat/jobs/run/stream", {})
    assert _state(frames[-1]) == "done"
