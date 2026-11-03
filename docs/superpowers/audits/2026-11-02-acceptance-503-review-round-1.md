# 验收实例 503 修复：首轮复审暂不通过

日期：2026-11-02。基线 `665f7e8`；生产 `7d6599b845011e57b893baa5ffea0afbe84958ae`；证据 HEAD `966d650`。实施树干净。

裁决：attempt 不被纯复用覆盖、旧坏记录受限恢复、锁排队预算与受管提示已有实质修复，但接管交错会提前消解清理承诺，且正式实例更新步骤无法让新 provisioner 生效。暂不批准合并或更新用户实例，仅执行 [R1 收口单](../tasks/2026-11-02-acceptance-503-rework-r1.md)。

## 独立验证范围

- 主审：`test_attempt_ownership_503.py + test_runtime_gateway.py + test_runtime.py + test_original_web_api.py`：118 passed、1 skipped。
- 主审：`test_provisioner*.py + test_worker_execution_gate.py + test_gateway_runtime.py`：160 passed、1 skipped。与上组文件不重叠，共 278 passed、2 skipped。
- 主审 Node 全量：193 passed；build/lint 退出 0，构建 `index-D5UTpITU.js`。仅既有 chunk 体积提示。
- 独立审查者核对协议和 runtime/正式脚本，并使用假材料做反例。下面两个生命周期反例由主审再次运行确认。
- 作者的 A4/A5 容器结果已阅读，本轮主审未重新运行容器或真实供应商/平台；不将作者结果写为主审复跑通过。

## 阻塞项

### R1-01 / P1：接管 CAS 前关闭旧 owner 的 pending 清理

`runtime.py:608–611` 在过期 starting 租约接管前执行空 attempt ID 的 commit；真正 owner/generation CAS 在稍后的 `_commit_gateway_candidate`。原 owner 在此窗口仍有权进入清理。provisioner 的 `completed` 同时被成功提交与 abort 完成使用，旧 owner 的 abort 因而直接返回成功，不执行物理 stop。

主审 [probe_pre_takeover_commit.py](../../evidence/acceptance-503-review/probe_pre_takeover_commit.py) 运行真实 Store、真实 RuntimeManager.restore_gateway 与 `_cleanup_candidate`、真实 Provisioner，仅远程适配/gateway 用假材料：

```text
cleanup_reported_success=true
gateway_stop_count=0
database_state=ready
database_gateway_id_empty=true
attempt_status=completed
TEMP_CLEANUP=ok; LIVE_RESOURCES_TOUCHED=false
```

该问题必须修复，不能以“不会误删”接受：它会把未完成物理清理标成完成，丢失承诺。应先解决有效 owner/generation/attempt 的交接，再允许收口；并核对 ready 补提交/refresh 等所有空 attempt commit 调用点，不能只修一个表面分支。

### R1-02 / P1：更新方案继续复用旧 provisioner，新接口不能生效

交付报告 §6 声称更新源码后 Start 会生成新版启动脚本。但正式 Start 在活跃 socket 配置身份一致时设置 alreadyRunning=true，跳过进程启动分支；该身份不包含源码版本或新增协议能力。现场 provisioner 正在运行，仅切换磁盘源码和 control 镜像不会升级它，新 `recover_abort`/`commit_attempt` 仍不可用。

直接先 Stop 也不能解决：Stop 的 admin drain 在旧 control 容器内执行，无法凭新脚本获得新的 runtime 恢复代码。需给出两端新版真实生效、保留接纳屏障/清理承诺的可执行引导流程，并在从旧实例到新实例的隔离部署中验证；新建全新 A4 不覆盖这个问题。

### R1-03 / P2：Stop 模板传不存在的参数

报告约第 259 行向 Stop 传 `-WorkspaceRoot`，正式 Stop 没有此参数，会在 PowerShell 参数绑定时失败。主审 [probe_rollout_parameters.py](../../evidence/acceptance-503-review/probe_rollout_parameters.py) 只读核对参数表，输出 `report_stop_workspace_root_supported=false`。正式参数是 ProjectName、Distro、WslRepo、DataRoot、LockTimeoutSeconds、Force。修正模板并验证所有实际命令参数、执行策略、实际 Distro/GID，不能写成通用占位命令后称现场已验证。

### R1-04 / P2：新增 commit 仍可超出 ready deadline

`_commit_attempt` 固定等待最多 15 秒，没有接收本次剩余预算，调用时还持 workspace 锁。主审 [probe_commit_budget.py](../../evidence/acceptance-503-review/probe_commit_budget.py) 设置 ready_timeout=0.02、假 commit 等待 0.15 秒：实际 0.156 秒后成功。锁等待修复有效，但完整调用预算仍不闭合。收口工作不应额外占用不受当前 deadline 约束的完整 15 秒；保持“DB 已成功提交时不能仅因 commit 延迟变成失败”的原意。

## 对七项 ruling 的裁决

- pending 不被任意覆盖、纯复用 completed/no-op、旧坏记录只处理精确 gateway-only、drain 失败关闭：方向可保留。
- commit 失败不回滚已提交 ready：可保留，但需遵守预算并保留可重试证据。
- 空 attempt 的 lazy/pre-takeover commit：当前实现不接受，R1-01 已证实清理屏障被提前关闭。
- stopping/unavailable 的受管文案调整可保留；not_started 文案、重复 warning、drain 缺具体码本轮不扩大为阻塞。

未操作用户实例、Key、Cookie、attempt 或 DB；未重发 prompt、未发真实模型/平台请求、未合并部署。所有主审反例使用临时假材料，均已清理。一次主审探针初稿误用 Store.open 上下文管理器而在临时场景报错，修正探针后取得上述成功复现，不是产品测试失败。
