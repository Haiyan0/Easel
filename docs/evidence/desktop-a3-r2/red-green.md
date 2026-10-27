# A3 R2 红绿记录

## A3-R2-01

### 红

- `test_ensure_run_timeout_after_cidfile_cleans_created_instance`
  在旧实现上失败：`docker run` 已写 cidfile 并创建容器后抛
  `TimeoutExpired`，旧异常路径 `instance_id=None`，跳过 worker 清理，
  finally 删除 cidfile。
- `test_ensure_cancel_waits_for_cli_and_cleans_late_cidfile`
  在旧实现上失败：取消 await 后 to_thread CLI 继续运行，受控 gate 放行后
  才写 cidfile；旧路径已退出清理并留下容器。
- `test_ensure_cleanup_failure_keeps_cidfile_and_does_not_stop_sidecar`
  与 `test_ensure_cleanup_inspect_failure_keeps_cidfile` 在旧实现上失败：
  异常清理吞掉缺失实例并删除 cidfile。
- `test_worker_timeout_cleanup_failure_preserves_sidecar_and_cidfile`
  在旧实现上失败：先 `_stop_sidecar` 再结束，sidecar 被拆且 cidfile 被删。
- `test_worker_timeout_cleanup_success_removes_only_this_round`
  验证修复后只删除本轮 worker/sidecar，不影响同名其他代次。

### 绿

```text
A3 R2 指定回归: 191 passed, 1 skipped
全量单测 + Desktop 静态: 1091 passed, 9 skipped
真实 Docker CLI 超时清理: 1 passed in 8.80s
```

真实测试使用临时 cmd 包装器和真实 Docker；确认 CLI 超时后容器实际存在，
随后按 cidfile ID 清理，最终容器、cidfile、测试网络均不存在。
