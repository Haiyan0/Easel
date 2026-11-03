# 验收实例 503 修复 R1：清理屏障与实际更新流程

父基线：实施 worktree HEAD `966d650`，分支 `fix/acceptance-503-attempt-recovery`，继续原隔离 worktree。固定 Agent Python/PowerShell，先核对干净状态。必读 [首轮审计](../audits/2026-11-02-acceptance-503-review-round-1.md)、[原执行单](2026-11-02-acceptance-503-repair.md)。本轮只收口四项，不重开业务功能或真实平台验收。

## R1-01 接管与清理屏障

- 原样运行主审 `probe_pre_takeover_commit.py <源码根>`，不得修改主审探针来消除反例。新增正式 Event/barrier 回归：owner A 持过期 starting/pending，B 恢复进入 commit/restore 窗口，A 激活失败进入真实 `_cleanup_candidate`；断言实际物理清理与 DB/attempt 终态一致。
- 在完成 owner/generation/attempt 交接前，不得用空 attempt 提交其他 owner 仍有权清理的 pending。检查所有 lazy/pre-takeover commit 调用点；覆盖成功提交后补提交、接管成功/失败、迟到 abort、崩溃重启与实际 gateway 替换。
- 不接受简单放宽 attempt/实例校验、不区分来源地将 completed 都当成清理已完成，或取消必要清理。选择最小、自洽的状态/归属方案，说明其不再提前关闭屏障的依据。

## R1-02 旧实例到新实例的引导更新

- 先复现正式 Start 在旧 provisioner 活跃且身份配置相同时复用旧进程，新接口未生效；同时证明直接运行新 Stop 仍是在旧 control 执行 drain。不能把“改了磁盘源码”当成旧进程已升级。
- 实现/提供受限、可复现的更新步骤，使新版 control 与新版 provisioner 都真正运行，再让现场坏状态通过新恢复路径收口。保护活动 Run/接纳屏障、精确实例、原卷/Key/HOME/attempt，禁止删卷、SQL 改 ready、忽略身份、按广泛进程名 kill 或直接绕过 Stop。
- 若需要正式脚本的小适配，先红测，限定本轮维护引导所需，不扩成通用部署框架。核验实际进程加载源码/协议能力及镜像摘要，配置身份相同不足以证明代码已更新。
- 在全新隔离项目先运行旧版本/旧 provisioner，构造同构旧坏状态，再执行正式更新流程，证明恢复后首屏 6/6、短聊天、正常 Stop→Backup→Verify 可走通。保留正式命令、退出码、旧/新进程身份和清理证据，不以新建 A4 全绿替代升级测试。

## R1-03 操作模板参数

- 删除 Stop 不支持的 `-WorkspaceRoot`；对报告中全部 Start/Stop/Backup/Verify/Restore 参数做契约核对。补当前窗口执行策略或显式子进程策略；Distro/GID 来自实际登记/现场读取，不能假定永远 1000。
- 明确恢复前保护材料与回退点，不把“先更新再备份”描述成已有升级前备份。受支持一致备份需成功 Stop，当前 Stop 被阻断时的引导步骤必须在 R1-02 证明安全且可执行。
- 不因配置变更随意推荐 Initialize 覆盖当前实例；具体命令要写清使用的源码、镜像、项目和数据目录。

## R1-04 就绪预算

- 原样运行主审 `probe_commit_budget.py <源码根>`，补正式延迟 commit 回归。取得 workspace 锁后的 commit 不应额外重置固定 15 秒窗口。
- 将队列/启动/必要远程等待纳入明确预算，或将非必要成功后收口设计为不阻塞当前响应的可靠重试；不能把已提交的 ready 仅因附加 commit 超时改成失败。补并发等待、取消、commit 持续失败与后续可恢复用例。

## 验证与交付

重跑新增红绿、受影响 runtime/provisioner/workspace API 与 desktop 契约；前端不再改则可引用首轮 193 Node/build/lint，无需反复重跑已关闭页面矩阵。新随机资源执行上述旧→新恢复流程；生产文件变化需更新相应镜像并核对源码哈希，其他镜像可复用。

证据写入本组独立 R1 目录，显式受 Git 跟踪，日志脱敏，历史证据不覆盖。列四项关闭依据、SHA、正式更新/备份/回退命令和 not-run。交付停止等待主审；不自动合并/推送/更新用户验收实例，不执行真实模型/平台、不重放用户 prompt。用户当前实例继续保留原现场。
