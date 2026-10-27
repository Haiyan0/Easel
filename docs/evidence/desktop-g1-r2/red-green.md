# G1 R2 红绿记录

## 红（修复前实际失败）

命令：

```text
pytest tests/multiuser/test_gateway_stream.py -q \
  -k "close_failure or failed_close or cleanup_cancel"
```

结果：

```text
FFFFFFFF
8 failed, 24 deselected
```

失败测试与观测：

1. `test_streaming_close_failure_is_failed_terminal_and_isolates`
   - EOF 后底层 `aclose` 抛 `OSError`，读取者收到 `StopAsyncIteration`，
     槽位归零；预期脱敏 502 失败终态且保留槽位。
2. `test_streaming_read_error_with_close_failure_is_not_normal_eof`
   - 读取错误后关闭失败被吞掉，流仍以正常 EOF 结束。
3. `test_streaming_idle_with_close_failure_is_not_normal_eof`
   - idle 收尾时关闭失败被吞掉，流仍以正常 EOF 结束。
4. `test_buffered_close_failure_returns_sanitized_error_and_isolates`
   - buffered 关闭失败后返回 200 上游 body，槽位释放，实例继续接纳请求。
5. `test_buffered_read_error_with_close_failure_is_not_success`
   - 原始读取错误返回 502 `gateway_upstream_unavailable`，关闭失败未进入
     终态，槽位归零。
6. `test_buffered_idle_with_close_failure_is_not_success`
   - idle 超时 504 后关闭失败被吞掉，槽位归零。
7. `test_failed_close_cancel_race_keeps_single_terminal_and_slot`
   - 取消一个等待者后，失败关闭仍被当作正常 EOF，槽位归零。
8. `test_cleanup_cancel_terminal_keeps_slot_and_isolates`
   - 关闭取消后槽位虽保留，但实例未隔离，后续请求仍会进入上游。

## 绿（修复后）

```text
tests/multiuser/test_gateway_stream.py -q \
  -k "close_failure or failed_close or cleanup_cancel"
8 passed
```

```text
tests/multiuser/test_gateway_auth.py
tests/multiuser/test_gateway_forward.py
tests/multiuser/test_gateway_stream.py
tests/multiuser/test_gateway_tcp.py
120 passed, 2 warnings
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
1211 passed, 9 skipped, 2 warnings
```

```text
ruff check easel/gateway tests/multiuser/test_gateway_stream.py \
  tests/multiuser/test_gateway_tcp.py
All checks passed!
```
