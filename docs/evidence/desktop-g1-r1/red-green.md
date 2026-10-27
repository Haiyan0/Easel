# G1 R1 红绿记录

## 红（修复前实际失败）

```text
FAILED tests/multiuser/test_gateway_stream.py::
  test_streaming_eof_cancel_does_not_truncate_unique_close_or_slot
FAILED tests/multiuser/test_gateway_stream.py::
  test_buffered_cancel_holds_slot_until_unique_close_finishes
FAILED tests/multiuser/test_gateway_stream.py::
  test_client_default_params_cannot_rewrite_signed_final_url (4 cases)
FAILED tests/multiuser/test_gateway_stream.py::
  test_unauthenticated_slow_body_rejected_before_read_or_slot
FAILED tests/multiuser/test_gateway_stream.py::
  test_oversized_declared_content_length_is_bounded_4xx

8 failed, 12 passed
```

具体失败：

- EOF/慢关闭取消后 `active_requests` 提前为 0，upstream `closed` 仍为 false；
  `response.is_closed` 已置真导致后续 close 无法补救。
- buffered 慢关闭取消同样在关闭完成前释放槽位。
- 注入 client 的默认 params 使最终 URL 变成
  `...events?after=99&unknown=1`，与已验签的 `after=1` 不一致。
- 无断言慢 POST 在 receive 上被读取并得到 408，而不是 receive 前 401。
- 5000 位 Content-Length 触发 `int` 转换异常，返回
  `{"error":"gateway_internal"}` 500。

## 绿（修复后）

```text
tests/multiuser/test_gateway_auth.py
tests/multiuser/test_gateway_forward.py
tests/multiuser/test_gateway_stream.py
tests/multiuser/test_gateway_tcp.py
112 passed
```

```text
tests/multiuser/test_auth_api.py
tests/multiuser/test_policy.py
tests/multiuser/test_worker_guard.py
tests/multiuser/test_proxy.py
tests/multiuser/test_event_stream.py
tests/multiuser/test_worker_runtime.py
tests/multiuser/test_runs.py
tests/multiuser/test_worker_execution_gate.py
226 passed
```

```text
pytest tests/multiuser tests/integration/multiuser/test_desktop_topology.py -q
1203 passed, 9 skipped
```

```text
ruff check gateway/auth/control 签名工具及四个 G1 测试文件
All checks passed!
```

真实 TCP：`test_gateway_tcp.py` **1 passed**，包含正常 SSE 关闭和客户端
断连后 worker 不再产出第二块；两个临时 uvicorn 线程在 finally 退出。
