# G1 gateway 红绿记录

## 红（实现前）

```text
pytest tests/multiuser/test_gateway_auth.py
E   ModuleNotFoundError: No module named 'easel.gateway'

pytest tests/multiuser/test_gateway_forward.py
E   ModuleNotFoundError: No module named 'easel.gateway.app'

pytest tests/multiuser/test_gateway_auth.py  # control 签名工具
E   ModuleNotFoundError: No module named 'easel.control.gateway_auth'
```

## 实现迭代中的失败（真实输出）

- 首轮认证实现：12 failed；断言验签调用把 `data`/`signature` 顺序传反，
  修正后 45 passed。
- 首轮转发实现：14 failed；包含
  `httpx.StreamConsumed: Attempted to read or stream some content...`、
  合法查询路由的 URL 未带 query 导致 403、`%6c` 字面量编码返回 404。
  测试 transport 改为未消费的 AsyncByteStream、测试请求 URL 补 query、字面量编码拒绝后通过；生产代码不再保留测试专用的已读响应兼容分支。
- 流式测试最初使用 `httpx.ASGITransport`，其响应缓冲/提前 disconnect 导致
  假 200 和槽位提前释放；改为直接驱动 `StreamingResponse.body_iterator`
  验证背压/取消/idle，并用真实 uvicorn TCP 验证 wire 行为。

## 绿

```text
tests/multiuser/test_gateway_auth.py
tests/multiuser/test_gateway_forward.py
tests/multiuser/test_gateway_stream.py
tests/multiuser/test_gateway_tcp.py
96 passed
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
1187 passed, 9 skipped
```

```text
ruff check 新 gateway 模块与四个测试文件
All checks passed!
```

真实 TCP：`test_gateway_tcp.py` **1 passed**，两个临时 uvicorn 线程在 finally
退出；无 Docker 容器/网络或临时文件残留。
