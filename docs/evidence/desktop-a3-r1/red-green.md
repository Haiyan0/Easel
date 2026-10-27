# A3 R1 逐项红绿

本文件记录本轮实际观察到的失败/通过顺序，不替代主审独立复核。

## A3-R1-01：探针失败不得强删活跃实例

先新增 `test_provisioner.py` 反例：

- `test_ensure_probe_failure_keeps_running_instance_and_credentials`
- `test_ensure_probe_timeout_keeps_running_instance`
- `test_ensure_ready_false_with_active_run_keeps_instance`
- `test_ensure_paused_restarting_or_missing_state_is_not_replaced`
- `test_new_start_cleanup_uses_only_the_created_instance_id`
- sidecar 反例 `test_probe_failure_does_not_create_or_touch_sidecar`

未修改实现时失败：仍会 `docker rm -f` 当前实例并按名启动替代实例，或
先创建 sidecar。实现后：

```text
tests/multiuser/test_provisioner.py            120 passed, 1 skipped
test_sidecar_provisioner.py + test_stop_recovery.py
                                              29 passed
```

## A3-R1-02：真实 SIGTERM 先 drain，有限宽限收尾

先新增：

- `test_run_manager_aclose_waits_for_runs_within_grace`
- `test_run_manager_aclose_force_cancels_after_grace`
- `test_run_manager_force_stop_skips_remaining_grace`
- `test_worker_server_uses_uvicorn_signal_handler_to_drain_first`
- 真实容器 `test_real_sigterm_grace_drain_and_persistent_recovery`

失败：

```text
TypeError: RunManager.__init__() got an unexpected keyword argument 'shutdown_grace'
ImportError: cannot import name 'WorkerServer' from 'easel.worker.server'
```

真实容器修复前，SIGTERM 后监听 socket 先被 Uvicorn 关闭，探针得到
`RuntimeError`，容器不存在可返回的 drain 窗口；短 Run/挂起 Run 无法在
drain 状态下验证。实现 WorkerServer 的 `handle_exit` + `shutdown` 顺序后：

```text
SIGTERM_SHORT {"active_runs_at_drain": 1, "drain_ms": 296, "exit_ms": 8125}
SIGTERM_HANG {"drain_ms": 328, "exit_ms": 15563}
2 passed (真实 SIGTERM + 协议回归)
```

## A3-R1-03：返回 handle 凭证与真实 worker 一致

先新增：

- `test_ensure_existing_credentials_mismatch_fails_closed`
- `test_ensure_reuses_credentials_only_when_actual_token_matches`
- `test_inspect_does_not_create_missing_host_credentials`
- `test_inspect_wrong_credentials_is_not_cached_as_ready`
- 真实容器 token 错误/缺失场景

失败：

```text
inspect wrong token -> ready=True（旧实现）
inspect missing host token -> 自动生成新 token（旧实现）
```

实现后错误路径均为 `ready=False` 或 `worker_credentials_missing`，原容器
ID/Running 不变，宿主 token 不被新建；合法同 token 复用通过真实
`verify_worker_request`。真实容器 SIGTERM 与协议回归均通过。

## 汇总

```text
affected 定向：184 passed, 1 skipped
多用户全量 + Desktop 静态：1084 passed, 9 skipped
真实 SIGTERM + 协议：2 passed
真实容器隔离（非 Chromium）：7 passed, 2 deselected
```
