# A2 与 A2 R1 红绿记录

基线：`f1c4e5ef98ea756513071367919fe57fac7dbf17`。

## 红 1：初始契约文件缺失

在创建 `deploy/desktop/topology.json` 前运行：

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest `
  tests/integration/multiuser/test_desktop_topology.py -q
```

```text
Fsssss                                                                   [100%]
1 failed, 5 skipped in 0.27s
FAILED tests/integration/multiuser/test_desktop_topology.py::test_desktop_topology_contract_is_explicit_and_secret_free
AssertionError: A2 必须先提交 deploy/desktop/topology.json
```

## 红 2：R1 清理顺序暴露 active endpoint

加入精确路由和清理反例后，首次真实运行业务断言通过，但清理在成功测试后变红：

```text
FAILED test_worker_http_is_blocked_from_windows_and_wsl
FAILED test_bridge_control_and_worker_networks_are_isolated
FAILED test_joining_control_to_worker_network_is_bidirectional
3 failed, 6 passed
A2 cleanup failed: network ... has active endpoints (...)
```

修正为容器先于网络清理后，网络删除成功，真实套件转绿。该红证明
“清理失败不能吞掉、成功测试不能因残留变绿”。

## 绿：静态、纯逻辑清理与探针反例

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest `
  tests/integration/multiuser/test_desktop_topology.py -q
```

```text
......sssss                                                              [100%]
6 passed, 5 skipped in 0.09s
```

纯逻辑测试覆盖：

- `test_cleanup_aggregates_failures_and_continues_to_later_items`
- `test_named_client_after_cli_timeout_is_cleaned`
- `test_tracked_cleanup_failure_makes_successful_test_red`
- `test_tracked_cleanup_error_preserves_original_exception`
- `test_wsl_probe_requires_contract_exit_and_blocked_marker`

删除非零/超时、inspect 超时、归属不符、删除后已不存在、named client CLI
超时均继续处理后续项并汇总错误；WSL 探针只接受 rc=3 + `BLOCKED`，基础设施
失败不会被当成隔离成功。

## 绿：真实临时资源

```powershell
$env:EASEL_RUN_CONTAINER_TESTS = '1'
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest `
  tests/integration/multiuser/test_desktop_topology.py -q
```

```text
...........                                                              [100%]
11 passed in 22.60s
```

真实测试覆盖：Windows/WSL HTTP 阻断、独立 bridge 隔离、control 加入
worker 网络的双向反例、Windows/WSL bind 可见性、WSL socket 挂载与 UID
10001 peer credential；跨网络测试前均先验证两端容器回环 HTTP 200。

## 回归

```text
pytest tests/multiuser/test_desktop_deployment.py \
  tests/multiuser/test_topology_support.py tests/test_document_paths.py -q
64 passed in 27.11s

pytest tests/multiuser/test_provisioner.py tests/multiuser/test_worker_lifecycle.py -q
107 passed, 1 skipped in 1.07s

ruff check tests/integration/multiuser/test_desktop_topology.py
All checks passed!

python -m json.tool deploy/desktop/topology.json
exit 0
```

## 未实现

A2 R1 只修订拓扑契约和测试；gateway、`docker exec` healthcheck 运行时接入、
`WorkerHandle.gateway_instance_id`/`gateway_generation` 与 stop 协议均未实现，
等待复审批准后再下发实施任务，不进入 A3/A4。
