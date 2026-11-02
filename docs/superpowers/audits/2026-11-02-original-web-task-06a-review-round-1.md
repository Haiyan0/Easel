# Task 6A 首轮复审：尚未整体通过

日期：2026-11-02。父基线 `4382180862e28d43f2974e32a618093c847ec262`；生产提交 `51aacfb505d14e2e6b18498569ff8740bff6f51b`；证据 tip `b582d262e6b99f2ea2e5a8a697b5248bd8506e19`。

裁决：保留已完成分组与本轮部分整合结果，6A 不整体验收通过，不切换用户实例。后续仅执行 [6A R1 收口单](../tasks/2026-11-02-original-web-task-06a-rework-r1.md)。

## 独立验证

- 主审实际执行 `Agent Python -m pytest tests/multiuser/test_original_web_api.py tests/multiuser/test_runtime.py -q`：71 passed、1 skipped。
- 独立审查者额外加入 `test_runtime_gateway.py`：96 passed、1 skipped；这是审查者结果，不能与主审计数相加。
- 已读生产 diff、演练脚本、checks.json、inventory/verification 与手册候选补丁。未重跑 Docker、完整浏览器、Node/build/lint；交付中的这些结果仍为实施方证据。

## 需收口的问题

1. **P2：串行锁排队未计入就绪超时。** `RuntimeManager.ensure_ready` 取得锁后才进入创建 deadline 的 `_ensure_ready`。独立审查者用临时库、阻塞 fake provisioner、ready_timeout=0.05 的四并发实测分别约 0.062/0.125/0.187/0.250 秒失败。尾请求的等待随并发数增长，API 重试还可能放大。锁等待应计入有界预算；补故障并发、取消释放测试。当前 retry 只围绕句柄准备，业务 gateway.request 在循环外，未发现直接重复写入，但应加 POST 一次派发护栏。
2. **整体验收缺项。** 修正画像选择器后的页面阶段、正式 Stop/备份/校验/恢复/升级/回滚、受控账号/分析/发布整体链路未完成。不以旧 A7、5C2、5C3 分组成功替代本轮；网络异常应保存具体命令、错误与资源状态后定位，不先裁决为环境无关产品。
3. **证据门槛不闭合。** 演练 `soft_check` 记录失败并继续合理，但 main 仅凭未抛出外层异常返回 0；应让必需功能阶段的失败最终产生非零退出，同时保留后续采证。当前 Git 跟踪的 main 只有四个 JSON，报告引用的日志/provider audit 等未交付；`.log` 被 ignore。失败尝试也须保留脱敏的受跟踪文本，状态区分“尝试失败/中断”和“从未运行”。
4. **手册候选补丁待修。** `agent.models` 实际可省略或在镜像 manifest 目录内收窄，不要求完全相同；不要把“必须一致”写成运维要求。README 仍称版本行改为 4382180，而补丁已写 51aacfb，应同步。版本摘要说明输入基线、实际源码提交/哈希差异，不能只靠旧 source_commit 证明最终源码一致。候选补丁暂不应用到 root。

## 最快试用与正式切换

优先 Windows 新建独立试用实例，使用新 project/端口/DataRoot/卷/身份，发布策略 manual；先做真实模型最小请求，再体验原 Web。这样无需迁移或停止旧实例。需要用户明确授权实际供应商请求与费用，并由用户在认证管理页面输入 Key；不读取既有秘密。

若要求保留旧数据直接使用，应先完成生命周期演练，再准备具体版本、策略、备份/校验/恢复及回滚步骤，经用户确认维护窗口后切换。真实平台登录、短信和发布另项授权，不是开始聊天/文件/人工导出试用的前置。Linux 生产门槛仍保留 not-run。

本轮未操作用户容器/卷/socket/Key/Cookie/邀请，未合并、推送、部署或付费调用。
