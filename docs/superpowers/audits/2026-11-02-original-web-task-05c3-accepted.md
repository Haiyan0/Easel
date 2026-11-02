# 原 Web Task 5C3 验收：发布工作台与导出 R2通过

日期：2026-11-02。R2父基线 `40fca7c4551db82aa782ef2db466080421337ed2`；实现 `6b54e49e5dce99ec1fe4b2b3f4ed5703eb357da5`；证据194c61c/4382180；最终tip `4382180862e28d43f2974e32a618093c847ec262`。

## 裁决

Task5C3通过。[R1复审](2026-11-02-original-web-task-05c3-review-round-2.md)唯一剩余T5C3-R2-01关闭，未发现本范围新增阻塞。R1策略迁移、Start/Stop/Restore、请求号与输入摘要结论保留。原Web功能组Task1–5已分组验收；尚需 [Task6A隔离整体验收与手册收口](../tasks/2026-11-02-original-web-task-06a-integration-operations.md)，不因此宣称真实供应商/平台或生产部署已通过。

## R2关闭证据

decidePublishIntent按完整指纹和媒体/画像的持久化、显式编辑来源区分复用与新意图；草稿持久化可见媒体/画像，旧版缺失字段保留undefined。确认框列出实际提交素材/画像，替换未确认旧意图时提示可能重复，取消不发送。未以移除旧意图恢复来掩盖素材错误。

主审与独立审阅者分别在当前真实build/原隔离fixture复跑：

- 原改图反例：首POST未forward、未接纳，A改选B后可见与第二POST均project/other.png，新requestId，实际发布1次；临时目录清理完成，退出0。
- 确认/刷新：确认框包含B、不含A；取消不新增POST；改图后刷新仍显示B，再确认POST为B，执行1次；退出0并清理。
- 原ACK丢失：后台原Run已完成，刷新保留原素材且查回success，publish POST总1、实际执行1、完成Run1，没有补发；退出0并清理。

证据：[原改图探针](../../evidence/original-web-task-05c3-review/probe_r1_changed_media.py)、[本轮输出](../../evidence/original-web-task-05c3-review/probe_r2_changed_media_output.txt)、[确认与刷新探针](../../evidence/original-web-task-05c3-review/probe_r2_confirmation_refresh.py)、[丢ACK恢复探针](../../evidence/original-web-task-05c3-review/probe_r2_lost_ack_refresh.py)。画像显式更换/清空、应用级画像、旧草稿恢复、同指纹复用由纯函数Node测试覆盖，不另称本轮已跑真实平台画像操作。

## 主审本轮新验证

- 全frontend Node：190 passed、0 failed。
- build成功index-BreoCku2.js，SHA256 37529fe5ffd1d78bb905f03afac02df112230ba97c8cb2344e999fa6ddf9ca2f，与交付证据一致；lint退出0。
- 本组浏览器13/13，FAILURES=[]，退出0；原11组保留，含新素材编辑与刷新场景。截图写独立临时目录并清理，未覆盖作者/历史证据。
- 三条独立正向探针均退出0；独立审阅者窄复审无阻塞，相关Node25项通过（为全量190的子集，不重复相加）。
- 生产/测试源码diff --check通过，实施树干净。既有chunk提示与Windows Playwright关闭回调警告不扩为本组产品阻塞。

本轮只有前端/测试/证据变化，按R2单项范围未重复后端665项、桌面部署/容器和其它63组页面矩阵；这些是R1既有验证，不能计作本轮新运行。R1隔离容器的受控平台、原平台脚本未执行、seccomp=unconfined等边界保留。

## 后续与保护边界

当前下发6A：隔离正式部署、真实双worker/受管Harness+受控provider整合、备份恢复升级演练，以及以root当前版本为输入的两份既有运维手册候选补丁。实现完成后停止复审；6B真实模型最小调用、实际平台动作和用户实例切换另行授权。

未操作用户容器/卷/socket/Key/Cookie/邀请，未真实发布/短信/付费，未合并/推送/部署；历史被审批阻止清理的主审目录未接管或重试删除。主审仅在root保留审计/任务/计划，未提交实施分支。
