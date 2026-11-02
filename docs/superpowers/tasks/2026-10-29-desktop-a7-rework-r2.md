# Task A7 R2：准备转换、只读校验与当前停止承诺

基线：`e8376753d63eb681560ae868aa2d1528b2756c85`。
必读：[R2 终审记录](../audits/2026-10-29-desktop-a7-review-round-2.md)。

仅修以下三项及必要测试/手册：

1. **R2-01**：prepare 前验证 staging 的完整输入，拒绝有效 SQLite 行变化等损坏；拒绝时 DB/manifest/集合均不变。合法转换才登记新摘要，原始备份保持只读。
2. **R2-02**：合法 prepared 包连续 verify 不创建 WAL/SHM、不改变任何字节或集合，随后 materialize 仍成功；不能忽略额外文件或通过删文件让测试变绿。
3. **R2-03**：升级在 compose up/新程序写正式 DB 前绑定备份与当前同一轮 Stop。旧备份 → reopen 新增数据 → 再 Stop 的正式脚本反例必须提前拒绝，同一轮匹配备份正常升级。旧 schema 的读取不得触发迁移，后置最终复核保留。

复现脚本：`docs/evidence/desktop-a7-r2-review/probe.py`；其中脚本预检的外部容器调用使用真实 verify 成功结果的替身，不是真实 Start 演练。

使用 AGENTS.md 指定 Agent Python、PowerShell，逐项红→绿。跑 v1/v2 ops、A7 脚本、部署/身份/生命周期相关回归；如改镜像内代码，证据必须匹配源码。无变化 Harness/worker/gateway 不要求重建，不重复整套无关门槛。

保留 R1-02/04 已关闭结果，保护主审文档及无关改动。仅使用随机归属的测试资源，不读真实供应商秘密、不全局 prune；不得接管上轮被自动审批阻止清理的临时目录。交付父/新 SHA、红绿、正常恢复/升级证据与未执行边界，提交后等复审，不进入 A8、不自动上线。
