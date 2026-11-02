# 原 Web Task5C1 R1复审：仅技能状态临时失败恢复待收口

日期：2026-10-30。父基线 `c9df47738c344d382d45e91aec91f6019f0fbaa0`；实施 `d6743f177be93251c5cc7a7bf2453bdb12de5d71`；证据tip `4d46c668021e0de884aee0699d6bbe88030cb167`。

结论：暂不整体通过，仅以下一项P2按 [R2单项执行单](../tasks/2026-10-30-original-web-task-05c1-rework-r2.md)收口。原R1模型、完整画像增强指令、准备期互斥均已确认关闭，权限题卡正常显示/回答/停止路径恢复，不重做这些方案。5C2/5C3/6不进入，用户实例不切换。

## T5C1-R2-01：技能状态一次503后停止轮询，恢复后题卡持续禁用（P2）

实施树 `web/frontend/src/components/SkillDrawer.tsx:100–108` 的check外层catch仅设置questionPending=true和错误信息；下一轮setTimeout仅在成功分支安排。一次skill/status请求临时失败就终止唯一轮询链，题卡保留却永远无法重新确认。

主审当前build + 隔离control/gateway/worker真实RunManager浏览器复现：先让技能ask，原题卡已可见并确认pending；下一次GET /api/skill/status仅注入一次503，之后全部放行。4.2秒后（超过awaiting_permission轮询周期两轮），轮询次数仍1；直接请求后端已200、state=awaiting_permission、questionPending=false，前端却仍questionPending=true、提交禁用。executions=1，未重复任务。

证据：[只读复跑脚本](../../evidence/original-web-task-05c1-review/probe_task_status_503.py)、[脱敏输出](../../evidence/original-web-task-05c1-review/probe_task_status_503_output.json)。主审独立复跑同结果，退出0、停止假Run并清理临时目录。Windows Playwright连接关闭回调出现Win10054警告，不影响已完成断言/清理，不当作产品错误。

要求：临时503/断网保留原题卡、Run标识和待确认状态，并在当前归属仍有效时继续有界轮询/恢复；成功确认pending后启用提交，明确终态正常收尾。401保持原身份卸载，不在已卸载或旧任务上继续轮询。不得用清卡、自动允许或重新派发任务替代恢复。

## 原R1关闭证据与本轮验证

- 原profile_and_model探针：skill/profile快照与broker均deepseek-flash、MATCH=True；实际画像prompt中builder执行/本人目标目录/完善六维指令均True。
- 原preparation_race探针：准备期删除409，构建200/running，画像仍存在，relay提交1次、ledger running；新增回归覆盖同名并发、同turn重复、失败释放claim与基线保留。
- task5c1_r1、workspace_tasks、worker_skills_profile、proxy、task_leases：46 passed。
- policy、runs、gateway_workspace、persona_security：193 passed。两组文件无重叠。
- 全frontend Node146 passed；tsc/Vite build成功（index-B-SlLwSa.js）、oxlint通过。
- 5C1浏览器6/6、5B五组、5A四组、Task3 33组、Task4八组全部FAILURES=[]；临时截图清理且未覆盖交付证据。
- 实施树干净；生产/测试源码diff --check通过。完整diff仍有原始证据日志尾随空白，属证据格式项，不扩为生产阻塞；后续整理相关日志时可移除，不能称完整diff检查已通过。

既有5A fixture清理coroutine警告和构建chunk提示继续记录，不另立生产阻塞。主审未运行Docker或全顶层pytest；交付全multiuser/容器结果仅作提交证据读取。顶层已有环境失败未被本轮伪报通过。真实模型/provider、账号/发布、完整Compose/生产Linux和最终上线保持未验收。

用户容器、卷/socket、Key/Cookie/邀请未操作，未重放prompt、未付费/发布、未合并上线。
