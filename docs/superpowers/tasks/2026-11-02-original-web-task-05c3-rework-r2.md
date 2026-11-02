# 原 Web Task 5C3 R2：区分刷新恢复与主动修改素材

父基线：`40fca7c`，继续原隔离worktree与Agent Python。仅 T5C3-R2-01 / P2，依据 [R1复审](../audits/2026-11-02-original-web-task-05c3-review-round-2.md)。原R1策略迁移/Start-Stop-Restore、稳定请求号与完整payload持久化、同ID输入摘要校验保留关闭，不重开后端或部署方案。

## 问题与最小修复

PublishPage.findPublishIntent 的fallback只比较title/body/tags便复用旧payload，无法区分刷新丢失媒体/画像和用户主动修改。原build实证：首次POST未转发未接纳，用户取消A图、选择B图后再次发布，实际POST仍含A图。

- 有意图不代表可静默覆盖当前用户选择。明确区分恢复的空状态与用户显式编辑媒体/画像；可跟踪编辑来源/稿件版本，或先明确恢复原意图并让页面展示原稿。实现方式由实施者选择，但最终可见审核对象、确认对象与实际提交payload必须一致。
- 继续保留刷新/切身份返回后恢复完整payload和原requestId，不能通过移除fallback或丢弃原意图重新引入重复发布。对旧意图接纳未知且用户要改稿时，先明确处理旧意图；不能自动补发，也不能悄悄用旧图。
- 同平台同意图重试一次派发、明确新稿/主动再次发布为新意图、同ID异输入409、跨身份隔离与迟到回包保护均不变。local原行为不受影响。不要求新建后台账本或重做发布页。

## 红绿与验收

- 先红：真实build+受控fixture，首POST在客户端截断且不forward，后台executions=0；明确取消cover.png并选择other.png，保持标题/正文/tags不变，第二次操作不得偷偷提交cover.png。允许明确阻止/引导解决旧意图；若提交，应与用户当前确认的稿件一致。
- 覆盖主动改选/移除媒体、切换画像两个维度；对比单纯刷新/身份返回丢选择时仍正确复用旧payload/requestId。可通过组件/Node测试补画像边界，至少媒体改选必须真实浏览器验证。
- 保留11组原5C3浏览器及其lost-ack-running/done、刷新恢复、stored-payload、explicit-new-intent、identity-switch场景；增加本项，断言POST请求内容、requestId归属与实际受控执行次数，不只检查文案。
- 主审只读探针 root docs/evidence/original-web-task-05c3-review/probe_r1_changed_media.py 不修改。修复后的绿态用明确正向断言，不将旧探针等待失败算充分证据。

预计仅前端意图/页面逻辑、针对测试与证据。Node全量/build/lint、本组完整浏览器矩阵足以作为本项主回归；无后端变更则不要求重复全multiuser、桌面部署或重建worker/Harness/Chromium。已有63组页面回归结论保留，必要时才扩跑受影响身份/会话组。

证据放本组r2子目录（实施时创建），列父/实现/证据SHA、精确文件、红绿/实际POST/次数/not-run/清理；截图不覆盖历史。root主审audit/task/plan/probe只读。用户实例和真实平台材料不动、不真实发布/短信/付费、不接管历史受阻临时目录、不全局prune。提交后停止复审，不合并部署或进入Task6。
