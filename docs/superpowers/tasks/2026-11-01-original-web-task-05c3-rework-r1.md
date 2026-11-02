# 原 Web Task 5C3 R1：发布策略迁移与请求幂等收口

父基线 `aef1a578185d15a90d8c46ab46ffb3adde1edfb3`，继续原隔离worktree与Agent Python。依据 [首轮审计](../audits/2026-11-01-original-web-task-05c3-review-round-1.md)，仅以下三项；原发布工作台、导出、状态终态与其它已通过功能保留，不进入Task6。

## T5C3-R1-01 正式部署策略传递与旧manual迁移（P2）

- 先红：正式Compose/Start/serve-control参数装配不能表达manual，旧worker策略manual而新control默认automated，受控发布可被接纳。新测试必须覆盖正式脚本/配置生成，不只给测试ControlConfig手工传manual。
- 为正式部署提供受管发布策略字段/传递口，持久化到部署配置并与启动/升级一致。已有明确manual不得升级时静默变成automated；无法确定旧部署意图时，要求显式迁移或受控拒绝启动/自动发布，不能静默开放。全新部署既有默认是否保留与旧manual迁移分别处理，成员不能改策略。
- control不读取不可信worker/宿主.env作为实时权限源；迁移只取明确部署输入或可信登记，并校验一致性。正式manual应在Run接纳前拒绝，显式automated才允许受控提交。不得只更新说明要求操作者记住一个正式脚本尚不支持的CLI参数。
- 证据补操作迁移说明与正式路径红绿；root运维手册仍由主审后续统一更新，不覆盖其现有修改。

## T5C3-R1-02 接纳响应丢失后，同稿重试不得再次外部提交（P2）

- 先用真实build/受控发布复现：POST在服务器已接纳，丢掉返回给浏览器的响应，后台完成原Run；当前UI报网络失败。不改稿件点击重试，必须恢复/查询原Run而非生成新请求号再发布。
- 在首次发送前固定一次用户发布意图的稳定requestId，关联平台及稿件版本；接纳结果不确定时保留意图标识，查询/重试同一次请求只得到同一Run。页面显示未知/核对中，不能把网络错误伪装成确定未发布；不得自动补发。
- 刷新与身份切换同样不应丢失未确认意图或串用户；明确完成后用户主动再次发布/新版本是新意图，不能把所有同文永久去重。数据仅按身份/workspace存储，不回退全局旧草稿缓存。
- 真实浏览器红绿覆盖后台仍running与已done两种丢响应、同稿重试/刷新、新稿/用户明确新发布，以及A身份丢响应后切B不暴露或重发A。平台实际受控提交次数必须断言为1。

## T5C3-R1-03 同requestId必须绑定同一输入（P2）

- _join_or_submit在active.client_request_id相等时直接返回旧Run，绕过既有relay输入摘要冲突检查；同requestId改title/body/media也被200接纳为原Run。必须统一走既有输入摘要校验或等价的持久同意图校验，不能只比较requestId字符串。
- 同ID同输入运行中/终态重试应复用；同ID不同输入运行中/终态均明确409且不修改旧Run、不启动新提交。普通发布与导出都覆盖，保留同平台不同ID在途busy和跨用户隔离。避免另建并行账本。
- 使用真实control→gateway→worker受控链证明，不仅用FakeRelay（其测试摘要未包含正文时不能证明输入绑定）。

## 验证与交付

三条主审只读探针在root docs/evidence/original-web-task-05c3-review/：probe_manual_deployment.py、probe_publish_lost_ack.py、probe_request_content.py。探针不修改；修复后用新增正向断言证明拒绝/恢复与一次执行，不把旧缺陷探针超时当充分证据。

先红后绿，相关后端/正式部署脚本/策略/Run与前端Node/build/lint回归。5C3五组加新增交错场景、5C2六组/5C1七组/5B五组/5A四组/Task3 33组/Task4八组保持；浏览器夹具分别在独立Python进程运行，避免全局worker模块相互污染。补本组定向A/B发布页面的草稿、意图、迟到回应验证，不能仅引用聊天页跨身份用例代替。

增量更新受影响镜像，隔离容器验证明确manual拒绝/automated允许、丢ack恢复且实际提交一次、状态与停止仍正常；无需无缓存重建全栈。受控发布不触达真实平台。证据进本组r1子目录（实施时创建）并显式跟踪，原文件职责/SHA/红绿/源码镜像一致性/not-run/归属清理列清楚，历史截图不覆盖。

用户实例/卷/socket/Key/Cookie/邀请不动，不真实发布/短信/付费，不全局prune，不接管或清理主审被阻止删除的临时目录。root主审audit/task/plan/probe只读。完成提交停止复审，不合并、推送、部署或进入Task6。
