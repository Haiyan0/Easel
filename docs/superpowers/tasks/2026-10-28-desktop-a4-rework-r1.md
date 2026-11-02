# Task A4 R1：六项定向返工

返工基线：`81e9cc18f6ff698fdc16abff7a0418260e0217ee`。

必读审计：[A4 首轮审计](../audits/2026-10-28-desktop-a4-review-round-1.md)，以其中复现、约束和验收为准。原 A4 任务继续有效，已通过功能不推倒重做。

## 执行范围

1. **A4-R1-01**：egress 只挂出口策略，隔离 control master key/签名私钥；真实同 UID 材料隔离断言。
2. **A4-R1-02**：修复已派发且超时 Run 的 gateway 503 对账分支；无成功读取状态时不发停止、不撤 token、不结算。修正原测试 dispatching 宽限期空跑，断言实际 HTTP 次数。
3. **A4-R1-03**：原 control 崩溃后，过期 starting 和 gateway-only stopping 可通过旧 Run 的受限 gateway 恢复入口接管/收口；双 Store 连接验证 CAS 与迟到回应，不冷启动 worker。
4. **A4-R1-04**：Start 复用前和新建后核对真实 provisioner 配置/身份；配置漂移明确拒绝，不中断在途 worker。Status 检查实际必需项，失败非零，不只打印 missing 后成功。
5. **A4-R1-05**：Stop 先建立跨进程有效的停止接纳屏障，再 drain 和复核终态，最后停止 control/provisioner；并发 submit/新 workspace 不得穿透，失败/取消/重开可恢复，不假报停止完成。
6. **A4-R1-06**：将文件系统兼容限制到明确开发平台和已知不支持错误；原生 Linux 目录 fsync EIO 等持久化错误不可吞掉。真实文件/目录 fd 故障注入，保留原数据与输出恢复。

先报告 HEAD/工作树与实施设计；按项先红后绿，再最小代码变更。允许复用已有维护/诊断机制；不默认引入新迁移或另造大系统。如必须迁移，只追加，不改历史迁移。保护主审 README/plan/spec/审计/任务单，勿纳入提交。

## 验证与交付

- 每项确定性反例必须在修复前失败，修复后通过；真实 Store/跨进程边界不能被只检查字符串或单一 fake 状态代替。
- 跑受影响 gateway-client/relay/runtime/provisioner/stop-recovery、会话/Run 持久化、Desktop PowerShell 入口/A1 兼容与文档测试。
- 重跑多用户回归；受影响镜像需要重新构建并明确摘要。无需重建无变化的 Chromium/Playwright 基础层；固定缓存测试镜像的来源和限制如实报告。
- 真实测试至少覆盖策略材料隔离、合法重开、配置漂移拒绝及安全停止。只操作随机归属的测试资源，按标签和精确 ID 清理，不使用全局 prune、不碰开发者数据/ngrok/供应商。
- 报告父/新完整 SHA、显式文件、六项红绿、测试计数、未执行项、资源清理。不要把 mock/TCP 结果表述为真实 DSH 或生产发布通过。
- 完成后停止等待复审，不进入 A5/A6。
