# Task A4 R1 复审：三项部署接线与重开问题待修

返工基线：`81e9cc18f6ff698fdc16abff7a0418260e0217ee`。
审查提交：`f5451df4a1f752605ed17fcb2f7434258ddb3011`。

## 裁决

暂不通过，不进入 A5/A6。本轮已关闭 R1-01 的 egress 整 key 卷挂载、R1-02 的 gateway 503 超时误停止、R1-03 的过期 refresh 恢复、R1-06 的 Linux I/O 错误吞没。R1-05 的跨进程接纳竞态已修复，但正常停止后的重开流程仍有缺口；R1-04 的配置核验仍缺可信比较基准。

以下是本轮确认的全部阻断项。只修三项及对应测试，不重开 G1/G2/A3、不重做整个 A4。

## A4-R2-01（P2）：公钥副本未与 control 的真实签名私钥核对

位置：`deploy/desktop/Start-WorkspaceEasel.ps1:206` 的宿主公钥摘要与 expectedIdentity 构造；关联 Status 的身份检查。

keygen --check 只验证 key 卷里的私钥与卷内公钥。Start 的 workerPublicSha256/gatewayPublicSha256 却来自 DataRoot/public，provisioner 的 observed 摘要也来自同一宿主副本。两边比较的是同一个可能错误的副本，未证明它对应 control 实际私钥。

主审与独立审查分别生成两套真实 Ed25519 key：control 卷保持第一套；宿主 public 副本用第二套。当前两道检查均通过：

```text
CONTROL_KEY_CHECK=exists
HOST_PUBLIC_MATCHES_CONTROL=False
IDENTITY_MISMATCHES=[]
```

于是 Start 可以成功，但 control 签名与 worker/gateway 验签不匹配，正式聊天/health 被拒。误选 DataRoot、复制另一项目公钥或只更新副本都可触发，不需要破坏私钥。

修复：以 control 私钥对应的卷内已校验公钥为可信基准，仅导出其公钥摘要，与宿主副本及 provisioner 实际 key 摘要逐一比较。策略在 key/egress/public 的副本也应保持一致，不能只有“文件非空”。Start/Status 使用相同契约；漂移失败关闭、不自动换私钥、不重建活跃 worker。测试必须用两套真实 key 证明错副本被拒、合法副本仍可重开；真实容器证明只输出摘要/状态，没有私钥外流。

## A4-R2-02（P2）：Engine/context 只检查非空，仍未验证属于本机部署

位置：`deploy/desktop/provisioner_identity.py:77` 附近 engine 检查、Start 的 expectedIdentity；socket UID 同样未比较。

identity 已报告 Engine/context，但 compare_identity 仅要求两个字符串非空，Start 也不采集当前指定 WSL/本机 Docker Engine 的可信期望值。即使 expected 手动提供 engine，比较函数也忽略；socketUid 参数同理。provisioner 指向另一 Engine 时仍通过“身份已核验”，可能把 worker/gateway 建在 control 不在的 daemon 上，完整拓扑失效。

确定性反例：保留镜像、根、网络、key 摘要等字段，actual engine 改为 wrong-context/wrong-engine，socket UID 改为 9999；expected 是 desktop-linux/expected-engine 与 UID 1000；compare_identity 仍返回 `[]`。现有测试只检查 engine 缺失，不检查不同 Engine。

修复：从本次批准的 Docker CLI/指定 WSL 路径只读采集 Engine ID/context、socket owner 与权限基准，与现有 provisioner 逐项精确比较；明确 Windows 与 WSL 使用同一 daemon 的验证。不得将 observed 自己复制为 expected，不改全局 context、不盲杀其他实例。若 context 别名可不同，应明确归一化契约，但 Engine ID 必须一致。补不同 daemon/非空错误 context、socket UID、采集失败、合法同 daemon 的反例；复用与新建均执行。

## A4-R2-03（P2）：成功 Stop 的持久屏障使正常 Start 必然失败

位置：Stop-WorkspaceEasel.ps1 --barrier 成功路径与 Start-WorkspaceEasel.ps1:388 附近 barrier-status 拒绝分支；README 关于自然失效的描述。

Stop 成功后不清除屏障或写“已完成停止”记录，屏障在 SQLite 中永久保存。下一次 Start 虽先把 control 启动，再检查屏障并返回 admission_barrier_active。因此正常 Stop→Start 与失败 Stop 无法区分；必须先经历一次失败启动，再手工 docker exec resume，再次启动。README“屏障在 control 停止后自然失效”与实现不符；这不是单纯调整文案就能满足已要求的合法重开。

独立审查使用真实 Store 与 cmd_admin_drain(barrier=True)，无活动实例时返回 verified=settled；关闭并重开控制库，barrier-status 仍打印同一 stop ID。主审另行确认：

```text
DRAIN_SETTLED=True
BARRIER_AFTER_REOPEN=stop-success
```

修复：区分成功停止与失败/取消遗留屏障，并给正常重开提供明确安全入口。可记录同 barrier/项目的停止完成承诺，在 Start 复核实例/Run/请求已收口且配置一致后受控恢复；或提供等价的明确重开协议。不允许在 control 仍接纳时提前无条件 clear，也不能把所有启动都自动 resume。失败/取消/并发另一 Stop 的屏障继续失败关闭，旧动作不能清掉新屏障。

必须测试实际 Stop→Start 连续两轮成功，不需先失败或手工修库；另覆盖 drain 失败、down 失败、取消、过期成功记录和新屏障取代旧记录。保留 R1-05 已修复的并发 submit/新 workspace 阻断与 G2 精确实例停止。

## 实际验证与限制

- 主审十六文件运行时/部署回归：**458 passed, 2 skipped（66.74 秒）**。
- 首组七文件：**57 passed, 3 failed（12.66 秒）**；通过用例与扩展回归重叠，不合计。
- 三个失败均在新增 test_workspace_start_identity.py：子 PowerShell 无法解析 Get-FileHash，尚未进入待验证分支。使用单进程限定的 PSModulePath 重跑仍失败；在该子 PowerShell 先 `Get-Command Get-FileHash` 载入后，再执行同一匹配配置 wrapper 可成功。未修改全局模块配置。请修正或明确测试初始化依赖并给可复现绿结果，不能把这三项算成主审已通过，也暂不将其单列为确定的产品缺陷。
- 独立状态审查四文件 43 passed，原过期 starting 探针已恢复 ready，同 worker、Run running，旧 owner commit=False。
- 本轮未重跑 Docker/ngrok/供应商；作者材料隔离和真实链路证据保留，主审只读核验 Compose egress 专用卷配置。未修改运行时代码。
- `git diff 81e9cc1 HEAD --check` 退出 0。原生产 Linux/XFS/Chromium/真实 Harness/供应商门槛不变。

## 给代码 Agent

以 `f5451df4a1f752605ed17fcb2f7434258ddb3011` 为基线，仅修 A4-R2-01/02/03 与关联测试装配。先确定性红，再最小修复与绿；保护主审文档。无需无缓存重建未变化的大镜像。使用随机归属的测试资源，不碰开发者实例/全局配置，记录清理。报告父/新完整 SHA、逐项证据与真实/未执行边界，停止等待复审，不进入 A5/A6。
