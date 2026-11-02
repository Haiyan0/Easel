# 原 Web Task 5C1 首轮复审：四项定点返工

日期：2026-10-30。父基线 `710d673bc1458c19e3f4d7301fc4d7680bd22fce`；实施 `1195a17`；证据tip `c9df47738c344d382d45e91aec91f6019f0fbaa0`。

结论：暂不通过，仅以下四项P2需修复。正常技能执行/停止/结果找回、画像六维基线和原组件接线成立，不重做原Web，不进入5C2/5C3/6。见 [R1执行单](../tasks/2026-10-30-original-web-task-05c1-rework-r1.md)。

## T5C1-R1-01：新任务模型快照与broker路由分叉（P2）

实施树 `easel/control/proxy.py:424` 的 _sign 仅task_type==chat时用issued.route覆盖agent.model/allowed_models。本组开启skill/profile后，它们使用chat通道broker，却仍签名静态配置模型。

正式build_relay_factory + 真实ConfigService/BrokerService、临时Store/RuntimeManager和HTTP捕获探针确认：skill/profile均为SIGNED_AGENT_MODEL=deepseek-chat，ISSUED_BROKER_MODEL=deepseek-flash，MODEL_MATCH=False。未调用真实供应商。新受管任务会重新出现Task2已关闭的模型选择分叉，不能以夹具不使用模型为通过依据。

证据：[模型与指令探针](../../evidence/original-web-task-05c1-review/probe_profile_and_model.py)、[输出](../../evidence/original-web-task-05c1-review/probe-profile-and-model.txt)。主审独立复跑同结果。

## T5C1-R1-02：技能/画像等待权限时没有题卡入口（P2）

`web/frontend/src/hooks/useWorkspace.ts:281–284` 的runTask只接state/token，没有onQuestion；`SkillDrawer.tsx:59–72`恢复查询仅处理running/result；`OnboardingWizard.tsx:95–106`只轮询终态。`workspace_tasks.py:40–42`又把awaiting_permission折叠成running，状态返回不含问题。

当前真实build + 隔离control/gateway/worker RunManager，受控执行器实际ask合法问题：技能和画像worker均awaiting_permission、各有1条持久question事件，但原抽屉/向导可见而题卡和允许按钮均0，状态API仅running且无question；技能问题API请求数0。画像向导也无停止入口。用户无法在任务流程中回答，不能把“会一直等待”当作成功。

证据：[真实浏览器探针](../../evidence/original-web-task-05c1-review/probe_task_questions.py)、[输出](../../evidence/original-web-task-05c1-review/probe_task_questions_output.json)。主审独立复跑退出0。未等超时，技能用原停止按钮收尾，画像用本测试身份停止该假Run返回200，临时资源清理完成。

## T5C1-R1-03：画像增强只发送表单摘要，丢失任务指令（P2）

新worker baseline返回_form_to_instruction摘要，`workspace_tasks.py:272`直接将其作为message。旧api_profile_build在摘要外另包“执行/skill-profile-builder、完善已存在画像、写profiles/name六维、抓取失败降级不臆造、完成度摘要”等实际增强指令，新路径没有这个包装；worker也没有按profile类型补任务指令。

真实baseline→WorkspaceTasks→relay捕获确认六文件已创建，但message仅表单信息，执行技能/目标目录/完善文件指令全部不存在。本轮正常浏览器TASK5C1_EXECUTIONS也直接显示同一摘要。夹具执行器按task_type固定返回PROFILE-RESULT，没有验证真实增强语义，不能据此声称增强文件成功。这里不推测真实模型一定怎么回答，只确认必需任务指令在迁移中丢失。

证据同模型与指令探针。需恢复服务端固定增强指令并绑定校验后的目标，不让浏览器自行提交任意路径/命令。

## T5C1-R1-04：准备期没有画像资源互斥，可删除后继续接纳（P2）

`workspace_tasks.py:243`先创建基线，后面才reserve/submit Run。profile_active只检查已存在Run；`control/app.py:944,963`执行检查后另行转发编辑/删除，没有覆盖准备期的资源声明。

真实control/worker handler + 临时Store探针在基线完成但Run尚未接纳时暂停：删除画像返回200；恢复构建后仍200/status=running，真实ledger为running、relay已提交一次，画像目录却不存在。互斥只覆盖“Run已运行”不能保护整个增强准备/接纳过程，可能对用户已删除的资源执行迟到增强。

证据：[准备期竞态探针](../../evidence/original-web-task-05c1-review/probe_profile_preparation_race.py)、[输出](../../evidence/original-web-task-05c1-review/probe-profile-preparation-race.txt)。主审独立复跑同结果，退出0且清理完成。修复可最小串行化准备/接纳与编辑删除，或检测资源变化后拒绝派发；不要求另建任务调度器。

## 主审本轮验证与保留范围

- workspace_tasks、worker_skills_profile、gateway_workspace、task_leases：96 passed。
- policy、proxy、runs、persona_security：135 passed。两组文件不重叠。
- 全frontend Node：146 passed、0 failed；tsc/Vite build（index-t86hh5YP.js）、oxlint通过。
- 5C1浏览器4/4、5B五组、5A四组、Task3 33组、Task4八组均FAILURES=[]；只是现有正常矩阵通过，不抵消上述独立反例。
- baseline到tip diff --check通过，实施树干净；浏览器截图写入临时目录并清理，未覆盖交付证据。既有5A fixture清理coroutine警告与构建chunk提示不另立生产阻塞。

本轮主审未运行Docker；交付隔离容器结果只作为提交证据读取。未操作用户容器/卷/socket或真实Key/Cookie/邀请，未重放prompt、未付费或真实发布。真实Harness/供应商、账号/分析/发布、完整Compose/生产Linux与整体上线仍未验收。
