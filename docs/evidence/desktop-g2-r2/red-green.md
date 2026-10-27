# G2 R2 红绿记录

## 红（主审 R2 复现）

```text
FIRST_ERROR gateway_cleanup_failed
BEFORE_RECOVERY stopping WORKER_CREATED False
RECOVERY_FULL_STOP [(workspace, existing_worker_id, gateway_id)]
RUN_STATE running
```

gateway-only abort 失败后只留下通用 stopping；恢复调用完整 worker+gateway
stop，未删除本轮创建资源的边界被破坏。

## 绿

```text
G2/R2 新增单元
39 passed
```

```text
指定 provisioner/runtime/store/stop/gateway/拓扑回归
286 passed, 6 skipped
```

```text
tests/multiuser + tests/integration/multiuser/test_desktop_topology.py
1259 passed, 9 skipped, 2 warnings
```

```text
EASEL_RUN_CONTAINER_TESTS=1
tests/integration/multiuser/test_gateway_runtime_real.py
3 passed, 76.70s
```

真实 gateway-only 证据：

```text
GATEWAY_ONLY_KEPT_WORKER
{"gateway_removed": "be8c973080d768830b1c2028905936407bb65aef983321035ebfd02f522afdd0",
 "running": true,
 "worker": "b3bbc890c802b778cefb11fd4fed0f882d301e8da741de5c8f622c00e6e60872"}
```

新增/修改代码与测试 Ruff 通过；`git diff --check` 通过。
