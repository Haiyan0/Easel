# Task A3 R1 复审：启动超时清理定点返工

父提交：`b88d2369dc762f2bc0ee8e8917a2ad73a2ee2550`。
审查提交：`a384d2a0048eb80c786d8d1b0356cd6712146a95`。

## 裁决

上轮 A3-R1-01/02/03 的修复认可：保留既有运行实例、接入 Uvicorn 信号处理和有界 drain、接管前核对宿主/容器服务凭证。原协议与真实 SIGTERM 证据保留。

新引入的 cidfile 异常清理分支仍有一项 P2，修复后再关闭 A3，不进入 gateway/A4。不扩展任务范围或重做已完成盘点。

## A3-R2-01（P2）：docker run 超时/取消后遗留本轮 worker

位置：deploy/multiuser/provisioner.py 1817–1818（await run 后才读 cidfile）、1846–1850（异常清理使用仍为 None 的 instance_id 并删除 cidfile），以及 `_cleanup_failed_start`。

真实 Docker 的 daemon 创建与 CLI 返回不是同一完成点。即使 docker run 的 CLI 超时，容器可能已启动且 cidfile 已写入。当前该异常发生在 `_read_cidfile` 前，instance_id 仍为 None，故清理跳过 worker，却调用 stop_sidecar；finally 又删除本轮 cidfile。

主审沿用现有 FakeDocker，通过真实 Provisioner.ensure 调用链，在 run 已模拟创建运行实例并写 cidfile 后抛 TimeoutExpired，输出：

```text
CIDFILE_WRITTEN True
ENSURE_TIMEOUT=True
WORKER_STILL_RUNNING True
REMOVED []
CIDFILE_PRESERVED False
SIDECAR_STOP_CALLED 1
```

独立审查复现相同结果。仅使用临时目录与 Docker runner 桩，不接触真实用户资源。

## 最小修复要求

1. 异常路径也读取、验证本轮 cidfile，并按已核验的不可复用实例 ID 完成清理；不能因为 await 抛异常就假定 daemon 未创建。继续禁止按同名容器兜底 rm -f。
2. 若 worker 未确认删除或尚不能确定本轮创建是否结束，不先拆除它的 sidecar，不吞掉清理失败，不删除唯一追踪材料。明确返回/记录可恢复错误；保存材料不得含 token。
3. 取消路径不能只补一次立即 read：当前 Docker 调用经 to_thread，取消 await 不代表底层 CLI 已停止。必须协调本轮命令的终结与唯一清理，覆盖取消后才写 cidfile 的交错；不得让 finally 抢先删文件，或让被取消的等待者取消后台清理。
4. 清理终态确认后才能删除本轮 cidfile；不存在精确目标可幂等成功，inspect/daemon 错误不能当作已删除。合法实例与别的代次不受影响。

## 测试与验收

- 红：run 先写 cidfile/创建实例再抛 TimeoutExpired；旧实现遗留实例且删除 cidfile。
- 红：ensure 被取消时底层 run 尚未结束，受控 gate 放行后才写 cidfile；最终本轮 ID 被清理且无后台残留。
- 清理删除失败/inspect 不可用时，保留可恢复追踪，不停止仍有 worker 使用的 sidecar，不回报成功。
- 同名另一代实例保持存活；合法创建与正常新建失败清理回归保持。
- 跑 provisioner/sidecar/stop-recovery、server/lifecycle/runs 的指定回归，diff check。
- 真实 Docker 以临时命名/标签资源验证 CLI 超时不等于未创建、按 cidfile ID 清理。使用已有或当前固定执行镜像即可，本项不改变应用代码时无需重做整组真实 SIGTERM/ACP 场景。

## 本轮主审验证边界

- 指定 server/lifecycle/runs/provisioner/stop-recovery/sidecar：**184 passed, 1 skipped**（8.13 秒）。
- `git diff b88d236 a384d2a --check`：退出 0。
- 主审与独立审查均执行了上述超时反例。
- 实施方最终 execution 镜像摘要当前本机不可 inspect；本轮主审未重新构建或复跑真实容器信号场景，不以报告冒称主审新运行。已有短 Run/挂起 Run 的真实信号证据按本轮修复范围保留。

## 给代码 Agent

仅修 A3-R2-01。先补超时与取消交错测试，收敛本轮 Docker 命令与唯一清理的所有权；保护主审文档。显式路径中文提交，报告父/新 SHA、红绿、真实临时 Docker 超时清理证据与剩余边界。提交后停下，不进入 gateway/A4。
