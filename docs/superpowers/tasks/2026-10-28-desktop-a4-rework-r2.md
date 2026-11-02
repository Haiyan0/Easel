# Task A4 R2：可信接线核验与正常重开

基线：`f5451df4a1f752605ed17fcb2f7434258ddb3011`。

必读：[A4 R1 复审单](../audits/2026-10-28-desktop-a4-review-round-2.md)。本轮仅以下三项：

1. **A4-R2-01**：公钥核验以 control key 卷内真实私钥对应公钥为基准，比较宿主副本/provisioner/相关策略副本；两套真实 key 的错误接线必须拒绝，不能以同一错误副本自证一致。
2. **A4-R2-02**：Start/Status 比较受信本机/指定 WSL Docker Engine ID/context 和 socket owner/权限；actual 非空不等于正确，采集失败拒绝；不修改全局 context、不盲杀现有实例。
3. **A4-R2-03**：成功停止与失败/取消屏障有可恢复且带 fencing 的区别；正常 Stop→Start 连续两轮可成功，失败屏障不得无条件清除，不重新引入 drain/down 间接纳竞态。

修复新增 PowerShell 身份测试无法进入分支的问题，提供可复制测试环境与绿证据；不要以 skipped 或忽略失败替代验证。

先报告 HEAD/工作树与最小实现设计，再按项红绿；不改主审审计/任务单/plan/spec，不重做已验收 G1/G2/A3 与已关闭 R1 项。若必须迁移只追加，不改历史迁移。

验证：定向真实 key/Engine 漂移反例、真实 Store/跨进程停止恢复、PowerShell 正式入口与 A1 兼容；受影响回归。确需容器只操作随机归属测试资源，核验正式接线和合法重开，无变化基础层不强制重建。报告完整 SHA、文件、红绿、固定镜像与清理、真实/未执行边界，提交后停止，不进入 A5/A6。
