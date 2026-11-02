# 原 Web Task 5C3 首轮复审：部署策略与发布请求幂等待收口

日期：2026-11-01。父基线 `fcb351d532301d22afe3be0c91373b19f3505ca7`；实施 `206060aca2683d905421457822947aa59ec3ea91`，收口877d2a9/8d90f42；证据tip `aef1a578185d15a90d8c46ab46ffb3adde1edfb3`。

结论：暂不通过。原发布页面/导出与受管任务基础链路成立，保留通过部分；仅按 [R1执行单](../tasks/2026-11-01-original-web-task-05c3-rework-r1.md)修复三项P2，补相应定向浏览器与正式部署红绿，不重写发布平台、不进入Task6。

## T5C3-R1-01 正式部署未传递manual策略，升级可静默开放自动发布（P2）

新ControlConfig/CLI默认publish_mode=automated，而正式 `deploy/desktop/compose.workspace.yaml` 的serve-control命令未传--publish-mode，`Start-WorkspaceEasel.ps1`也无对应参数/配置传递。worker原manual设置不再约束新的run_managed_publish。

主审和独立审阅者分别运行正式Compose argv→正式CLI parser（仅捕获参数，不部署）→隔离真实control/gateway/worker Run链。受控旧worker明确manual/automaticSubmission=false，但正式默认解析为automated，control策略变automated/true，带正确身份和CSRF的发布POST200，受控执行1次并success。

这不是要求随意改变全新部署默认，而是已有明确manual的部署缺少可信迁移/传递路径。不能只告知操作者额外传一个正式启动脚本尚不支持的参数；须持久化/传递策略，旧值不确定时不静默放开。

证据：[正式部署策略探针](../../evidence/original-web-task-05c3-review/probe_manual_deployment.py)、[输出](../../evidence/original-web-task-05c3-review/probe_manual_deployment_output.json)。主审独立复跑退出0，成功轮临时材料清理完成。

## T5C3-R1-02 发布接纳响应丢失，同稿重试产生第二次外部提交（P2）

`web/frontend/src/components/PublishPage.tsx:242`每次publishAll生成全新requestId，约267的catch只标fail，未保留未确认接纳的意图标识或查询原Run。

真实build浏览器：首次POST经route.fetch实际接纳，再仅丢弃发回浏览器的响应；界面显示网络不可用。原Run后台已成功done；不改稿件再次点击原发布按钮，payload相同而requestId改变，受控publisher执行1→2、completed Run=2。这是网络失败后的同稿重试，不是用户明确发起第二篇新稿。

应在发送前固定一次发布意图，接纳未知时先查原Run或重试原请求，不能新建请求号补发。明确新稿/再次发布意图与重试分清，同时处理刷新和身份归属。

证据：[丢接纳响应探针](../../evidence/original-web-task-05c3-review/probe_publish_lost_ack.py)、[输出](../../evidence/original-web-task-05c3-review/probe_publish_lost_ack_output.json)。主审独立复跑同结果、退出0，成功轮清理完成。

## T5C3-R1-03 同requestId更改输入被当作原Run接纳（P2）

`easel/control/workspace_publishing.py:323`起_join_or_submit在active.client_request_id匹配时直接返回原Run，未经过relay既有输入摘要冲突校验。

主审真实control→gateway→worker受控链：首次标题/正文接纳200、任务保持在途；同requestId修改标题和正文再POST，仍200且返回同一个Run。原稿并未变成新稿，但响应宣称新输入已接纳，破坏稳定请求号与输入绑定。应同ID同输入复用、同ID异输入409，运行态和终态行为一致；导出复用同一helper也需覆盖。

证据：[请求内容绑定探针](../../evidence/original-web-task-05c3-review/probe_request_content.py)。主审输出：first_status=200、changed_content_status=200、same_run=true、cleanup_stop_status=200、temporary_directory_cleaned=true。既有FakeRelay使用只含turnId的测试摘要，不能代替真实链输入绑定验收。

## 主审验证与限制

- 发布control/worker/真实ASGI链、原导出与资产测试：95 passed。
- 5C2 R1/账号/worker链、Run/stop、Store/gateway/policy/leases/workspace/profile及原登录/分析：523 passed、2 skipped。两组文件无重叠，共618 passed、2 skipped。
- frontend Node166 passed；build成功index-Cnj9zjPQ.js、lint退出0。
- 新5C3浏览器五组全部通过，涵盖真实ZIP下载解包、审核失效、日历、策略、短信/停止/刷新与连续双击。该矩阵不覆盖“已接纳但浏览器丢响应”的重试反例。
- 生产/测试源码diff --check通过，实施树干净。作者容器报告作为提交证据读取，本轮主审未独立重跑Docker或全顶层pytest；受控平台不等于真实平台通过。
- 一个独立审阅者完成策略/前端实证，主审独立复跑；另一后端子审两次工具执行被安全机制拦截，未产生完成审查结论，未计为通过。主审请求内容探针和后端测试是本轮新运行证据。

原浏览器矩阵的同进程串跑出现5C1接口502/超时；改独立进程后5C1七组、5B五组、5A四组、Task3 33组、Task4八组全部FAILURES=[]、退出0；5C2六组在前一轮首个进程已通过。连同本组五组共68组正常场景通过。首次失败属于该复审运行方式的夹具全局状态干扰，不将首次失败伪报通过或立为生产缺陷，后续按独立进程验收。临时截图目录均清理，未覆盖交付证据。

## 临时资源与审批边界

两个首次主审探针自身清理错误（未关闭SQLite/错误SQL后连接留存）各留下一个只含测试材料的临时目录：

- C:\Users\hyh\AppData\Local\Temp\easel-review-request-r9viqqak
- C:\Users\hyh\AppData\Local\Temp\easel-review-5c3-ack-5r2oljkm

自动审批拒绝了精确路径检查后的删除命令，仅返回blocked by policy，未提供更具体理由；未通过换工具重试绕过，两个目录保留且不交给实施者接管。修正探针后新轮正常清理，残留不计产品缺陷。

用户运行容器/卷/socket/Key/Cookie/邀请未操作，未真实发布/短信/凭证/付费，未全局prune、合并、推送、部署或进入Task6。
