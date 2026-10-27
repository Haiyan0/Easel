# G2 R3 红绿记录

## 红（主审 R3 复现）

```text
FIRST_ABORT_SUCCESS True ATTEMPT_RECORD_EXISTS False
RECOVERY_RETRY gateway_abort_superseded
RECOVERY_RETRY gateway_abort_superseded
PHYSICAL_GATEWAY_STOP_CALLS 1
```

物理清理成功后删除 attempt 文件，control 尚未提交 DB 时恢复重试永久
`gateway_abort_superseded`，DB 卡 stopping。

## 绿

```text
G2 新增单元
39 passed
```

```text
指定 provisioner/runtime/store/stop/gateway/拓扑回归
288 passed, 6 skipped
```

```text
tests/multiuser + tests/integration/multiuser/test_desktop_topology.py
1261 passed, 9 skipped, 2 warnings
```

```text
EASEL_RUN_CONTAINER_TESTS=1
tests/integration/multiuser/test_gateway_runtime_real.py
3 passed, 78.08s
```

真实 gateway-only 证据：

```text
GATEWAY_ONLY_KEPT_WORKER
{"gateway_removed": "7a95f45eb3a1dfe30aacdda22b152bc7ddf16c3895ce3d74a82f1727a15d636c",
 "running": true,
 "worker": "3f87209c183c40a306717d3be105620df1fe0915ec99d061bdea0964ab6e4b68"}
```

新增/修改代码与测试 Ruff 通过；`git diff --check` 通过。
