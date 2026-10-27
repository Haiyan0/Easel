# G2 R1 红绿记录

## 红（主审 R1 已复现的旧终态）

```text
CURRENT_LEASE ready owner-b
OLD_ABORT_SENT True
ABORT_RESOURCE_IS_CURRENT True True CREATED_FLAGS True True
```

```text
RUN_AFTER_REFRESH_CRASH interrupted STOP_CALLS []
```

```text
UNREADY_WORKER_RETURNED True
RECOVERY worker_identity_mismatch unavailable ENSURE_CALLS 1
RECOVERY worker_identity_mismatch unavailable ENSURE_CALLS 1
```

```text
CREATE_TIMEOUT_NO_RECOVERY True
```

本轮补测过程中的额外红反馈：

- refresh 临时 ensure 失败被包装成 `worker_start_failed`，未保留
  gateway 503 终态；
- 真实集成首轮 v2 ensure 的 `attempt_id` 未进入白名单，返回
  `unknown_field`；
- stage 后租约被当成 cold start，B 会清空同一 worker 身份。

## 绿

```text
tests/multiuser/test_gateway_server.py
tests/multiuser/test_gateway_runtime.py
tests/multiuser/test_runtime_gateway.py
tests/multiuser/test_store_gateway.py
tests/multiuser/test_gateway_create_recovery.py
38 passed
```

```text
provisioner/runtime/store/stop/gateway/拓扑指定回归
284 passed, 6 skipped
```

```text
tests/multiuser + tests/integration/multiuser/test_desktop_topology.py
1257 passed, 9 skipped, 2 warnings
```

```text
EASEL_RUN_CONTAINER_TESTS=1
tests/integration/multiuser/test_gateway_runtime_real.py
2 passed, 341.40s
```

新增/修改代码与测试的 Ruff 检查通过；`git diff --check` 通过。
中途一次全量运行出现 Windows `WinError 10055/10053` 的既有 socket
资源/连接中断，相关用例单独及后续全量复跑通过。
