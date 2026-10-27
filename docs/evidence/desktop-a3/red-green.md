# A3 红绿记录

基线：`81589c0f24f3507b954e46b37ce85d181c619520`。

## 红

1. 增加 lifecycle payload 的 `active_runs`/`execution_capable` 断言后，当前实现先报：

```text
TypeError: build_payload() got an unexpected keyword argument 'active_runs'
```

2. 增加 RunManager active/drain 测试后，当前实现先报：

```text
AttributeError: 'RunManager' object has no attribute 'active_runs'
```

3. 首次持久路径探针：子进程 argparse 缺少 root/phase，输出
   `lock_cross_process:false`；修正子命令分支后为 true。该红证明探针没有把
   锁语义失败当成通过。

## 绿

```text
pytest tests/integration/multiuser/test_worker_execution_real.py -q (显式容器)
1 passed in 441.60s

pytest tests/multiuser/test_provisioner.py ... test_worker_server.py ... -q
299 passed, 1 skipped in 10.90s

pytest tests/multiuser/test_desktop_deployment.py tests/test_document_paths.py tests/integration/multiuser/test_desktop_topology.py -q
61 passed, 5 skipped in 28.73s
```

真实容器隔离非 Chromium 子集为 7 passed、2 deselected；Chromium 门槛未运行。
