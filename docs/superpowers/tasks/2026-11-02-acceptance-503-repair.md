# Windows 验收实例 503 定点修复执行单

目标：修复工作区启动/恢复中的 attempt 归属丢失，使已有实例能安全恢复，原 Web 不再整体 503。用户委托代码 Agent 实施、主会话独立审计；本轮不新增业务功能。

输入：主仓库 `C:\Code\Easel`、`feat/multiuser-task1-store` 的 `665f7e8`（已合并 b582d26）。先核对 HEAD/干净状态，隔离 worktree/分支实施，不在用户正在部署的 `C:\Code\Easel-acceptance` 上边运行边修改。固定 Agent Python/PowerShell。采用 systematic-debugging、TDD、verification-before-completion。

必读：[现场证据](../../evidence/acceptance-503-review/report.md)、[6A R1](2026-11-02-original-web-task-06a-rework-r1.md)。6A R1 的其他生命周期/账号发布整合不在本定点修复中强制重复；本单修复必须与其已知超时边界兼容。

## 一、红测与定位

1. 原样运行主审只读探针；禁止修改主审 probe 以让它变绿。补正式产品回归：A 创建 gateway 并产生 pending attempt；B 同 workspace 复用相同 worker/gateway；A 激活失败/取消并按原 scope 清理；恢复调用与 provisioner 重启均有明确且可收口的结果。不得用立即返回的 fake 隐藏 await 交错。
2. 明确持久 attempt 状态/归属：纯复用无新建资源时是否应创建清理记录；pending 记录能否被其他 attempt 覆盖；completed tombstone 何时回收。保持精确实例/attempt/scope 校验，不能通过“忽略 mismatch”或任意接管修补。
3. 用现场同构夹具覆盖：DB stopping/start_abort/gateway 指向 A，而磁盘存在同实例 B 且 created 均 false，真实 worker/gateway 已就绪。设计受限恢复路径，必须先验证真实身份与清理归属，不能 SQL 强改 ready、清空 attempt 文件或按名称删容器。

## 二、最小修复

- 修正 provisioner 对无新建资源调用的 attempt 持久化，以及 pending 清理承诺的覆盖边界。重启前后读取/abort 的语义必须一致，记录格式自洽；旧版已产生的上述坏记录需要有明确恢复/拒绝及运维路径。
- 复用 6A 首屏并发串行/有限重试/status 修复，但同时修正“排队不计 ready_timeout”的已知缺陷；同 workspace 并发启动失败必须有界，跨 workspace 不串行。POST 业务只转发一次，重试不能重放业务请求。
- 检查 Dashboard/control 状态提示：受管模式不得提示用户安装“control CLI”；gateway 就绪以可信 control 状态为准，未连接时应准确说明。只做与当前故障直接相关的文案/状态调整，不重做 UI。

## 三、验收与回归

- 正式回归覆盖创建/复用交错、激活失败与取消、pending/完成 tombstone、重启读取、旧格式坏记录恢复、跨 owner/代次迟到清理不删新实例、排队超时与取消释放、业务 POST 只派发一次。
- 受影响 runtime/gateway/provisioner/workspace API 测试；前端 Node/build/lint（仅在前端有改动时）。不要为了数字重跑已关闭平台组或真实平台脚本。
- 新随机 project/新卷/假 Key 真实容器冷启动，原 Web 首屏并发加载，验证 status/ideas/schedule/outputs/accounts/personas 成功；真实 Harness 配受控 provider 短消息成功；故障恢复后不丢文件/配置/历史，不重放旧 Run。
- 所有测试清理只认本轮标签和 ID。禁止全局 prune、删除历史网络、用户卷或 profiles。

## 四、用户实例恢复准备与交付

真实实例项目为 `easel`，DataRoot=`C:\Easel\acceptance-data`，WorkspaceRoot=`C:\Easel\acceptance-workspaces`，端口 7863，manual。以实际登记为准，不能照抄 `easel-acceptance` 或旧 `easel-desktop-multi`。

先在复制的同构假材料上证明恢复路径，再提交：实施 SHA、红绿与故障注入证据、镜像源码哈希、旧坏状态恢复步骤、精确的更新/备份/回退命令。正式 Stop 若被当前清理承诺阻断，要给出受限恢复方案并实测，不能绕过屏障直接 down/删卷/手改 DB。

交付后停止等待主审；本轮不自动更新用户运行实例、不重放 prompt、不调用真实模型/平台、不合并或推送。独立复审后再执行实际实例更新，更新后请用户在网页手动重试一条短消息，按状态区分后端恢复与后续模型上游错误。
