# A2 红绿记录

基线：`f1c4e5ef98ea756513071367919fe57fac7dbf17`。

## 红：契约文件缺失

在创建 `deploy/desktop/topology.json` 前运行：

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest `
  tests/integration/multiuser/test_desktop_topology.py -q
```

实际输出：

```text
Fsssss                                                                   [100%]
1 failed, 5 skipped in 0.27s
FAILED tests/integration/multiuser/test_desktop_topology.py::test_desktop_topology_contract_is_explicit_and_secret_free
AssertionError: A2 必须先提交 deploy/desktop/topology.json
```

失败原因正确：A2 契约文件尚未存在；真实资源测试因未显式开关而跳过。

## 绿：静态契约

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest `
  tests/integration/multiuser/test_desktop_topology.py::test_desktop_topology_contract_is_explicit_and_secret_free -q
```

```text
.                                                                        [100%]
1 passed in 0.13s
```

## 绿：真实临时资源

```powershell
$env:EASEL_RUN_CONTAINER_TESTS = '1'
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest `
  tests/integration/multiuser/test_desktop_topology.py -q
```

```text
......                                                                   [100%]
6 passed in 21.37s
```

通过项：

- `test_desktop_topology_contract_is_explicit_and_secret_free`
- `test_worker_http_is_blocked_from_windows_and_wsl`
- `test_bridge_control_and_worker_networks_are_isolated`
- `test_joining_control_to_worker_network_is_bidirectional`
- `test_windows_and_wsl_bind_sources_are_visible`
- `test_wsl_unix_socket_mount_preserves_peer_uid`

## 回归

```text
pytest tests/multiuser/test_desktop_deployment.py tests/test_document_paths.py -q
55 passed in 26.72s

pytest tests/multiuser/test_provisioner.py tests/multiuser/test_worker_lifecycle.py -q
107 passed, 1 skipped in 1.00s

ruff check tests/integration/multiuser/test_desktop_topology.py
All checks passed!

python -m json.tool deploy/desktop/topology.json
exit 0
```

## 未实现

A2 只提交拓扑契约和真实反例；gateway、`docker exec` healthcheck 运行时接入、
`WorkerHandle.upstream` 变更均未实现，等待主审批准后再进入 A3/A4。
