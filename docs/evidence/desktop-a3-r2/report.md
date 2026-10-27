# Task A3 R2 启动超时清理证据

日期：2026-10-27（Asia/Shanghai）

A3 基线：`81589c0f24f3507b954e46b37ce85d181c619520`
审查提交：`a384d2a0048eb80c786d8d1b0356cd6712146a95`
本轮只修复 A3-R2-01：`docker run` CLI 超时/取消后按 cidfile 实例 ID
完成唯一清理；不进入 gateway/A4。

## 1. 修复内容

- `docker run` 改为受 `asyncio.shield` 保护的独立任务；外层
  `ensure` 被取消时，清理任务继续等待 in-thread Docker CLI 真正结束。
- 清理读取 CLI 结束后写出的 cidfile，校验 64 位实例 ID，按不可复用 ID
  `rm -f`；不从同名容器兜底删除。
- `cidfile` 保留到 worker 清理确认、当前同名 worker 复核和 sidecar 决策
  完成之后才删除。
- 清理失败（rm 失败、inspect/daemon 不可用、sidecar 停止失败）保留
  cidfile，不停止仍可能被 worker 使用的 sidecar，非取消路径返回
  `worker_start_cleanup_incomplete`；取消路径记录可恢复错误并保留 cidfile。
- 同名新代次/其他实例存在时不停止其 sidecar，也不删除该代次。

## 2. 红绿

新增反例在当前实现上失败：

- `test_ensure_run_timeout_after_cidfile_cleans_created_instance`：
  FakeDocker 创建容器并写 cidfile 后抛 `TimeoutExpired`；旧路径遗留实例并
  删除 cidfile。
- `test_ensure_cancel_waits_for_cli_and_cleans_late_cidfile`：
  取消 `ensure` 后受控 gate 放行才写 cidfile；旧路径清理时 `instance_id`
  仍为 None，线程随后留下容器。
- `test_ensure_cleanup_failure_keeps_cidfile_and_does_not_stop_sidecar`
  与 `test_ensure_cleanup_inspect_failure_keeps_cidfile`：
  清理失败不得吞错或删除追踪材料。
- `test_worker_timeout_cleanup_failure_preserves_sidecar_and_cidfile`
  与 `test_worker_timeout_cleanup_success_removes_only_this_round`：
  worker 未确认删除时保留 sidecar/cidfile；确认后只清理本轮。

修复后：

```text
tests/multiuser/test_worker_lifecycle.py
tests/multiuser/test_worker_server.py
tests/multiuser/test_runs.py
tests/multiuser/test_provisioner.py
tests/multiuser/test_sidecar_provisioner.py
tests/multiuser/test_stop_recovery.py
191 passed, 1 skipped
```

真实 Docker 测试：

```text
EASEL_RUN_CONTAINER_TESTS=1 pytest \
  tests/integration/multiuser/test_provisioner_cleanup_real.py -q
1 passed in 8.80s
```

该测试用临时 cmd 包装器让真实 `docker run -d` 创建容器后延迟返回，触发
Provisioner CLI 超时；确认 cidfile 已写出、容器 inspect 存在，再用
`_cleanup_start_attempt` 按 cidfile ID 清理，最后确认容器/cidfile/网络
均消失。使用本地固定 `python:3.12-slim` 镜像，不发布端口，不操作开发者
资源。

## 3. 全量回归

```text
pytest tests/multiuser tests/integration/multiuser/test_desktop_topology.py -q
1091 passed, 9 skipped
```

未重做 ACP/SIGTERM 场景：本轮未修改 worker 应用代码，保留上一轮真实
短 Run/挂起 Run SIGTERM 证据。

## 4. 清理与边界

- 真实测试的临时容器、网络、cmd 包装器按名称/标签清理；测试后无
  `easel-worker-*` / `easel-ws-*` 残留，包装进程退出。
- 未执行全局 prune，未操作开发者容器、卷、网络、HOME、微信配置、浏览器
  profile 或 ngrok。
- gateway、A4、真实 DeepSeek Harness/供应商、Chromium 沙箱、完整多用户
  聊天仍未实现/未运行。
