# 原 Web Task 5C1 R1：模型一致性、权限闭环、画像指令与准备期互斥

父基线 `c9df47738c344d382d45e91aec91f6019f0fbaa0`；继续原worktree `C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。
依据 [首轮审计](../audits/2026-10-30-original-web-task-05c1-review-round-1.md)，仅四项P2，保留 [5C1原任务](2026-10-30-original-web-task-05c1-skills-profile.md)已通过范围。

## T5C1-R1-01：同次签发固定有效模型

- 新skill/profile任务与chat一样，签名agent模型/允许目录必须取自同次broker签发返回的可信有效路由，不回退静态模型。只扩到实际启用任务，不盲目开放后续账号/发布。
- 先红测正式factory+真实config/broker：静态默认chat、有效路由flash，skill和profile快照/token模型与allowed_models一致；配置改变后旧Run/重复请求仍使用原快照，不重新签发或重发prompt。
- 受控worker/ACP映射或provider请求证据覆盖新类型，而非仅修改字符串断言；保留Task2原chat模型回归。无需真实付费供应商。

## T5C1-R1-02：新任务可处理权限问题

- 复用已审题卡、question状态/回答、归属与停止机制；技能及画像任务的用户流程中都能看到并回答当前问题。可直接复用原任务/对话视图或题卡组件，不重写第二套授权体系，也不自动允许。
- 先用真实RunManager ask+当前build写红测：两类任务pending问题可见，可选择允许/拒绝并继续到真实对应终态；刷新/断线重新确认问题状态，不丢题卡、不重复任务。
- 未知状态保持待确认；401卸载私人树，503保留题卡；沿用Task3已审行为。停止任务使待确认失效，画像任务也有明确停止路径。跨身份迟到问题/回答不能改变新用户任务。
- 不能只把awaiting_permission改标签，或要求用户猜测去刷新侧栏找隐藏任务。原流程中需要明确可用的入口和归属。

## T5C1-R1-03：恢复实际画像增强指令

- 服务端将校验后的画像名/固定profile-builder执行说明与用户表单摘要组合为真正增强请求，指明本人profiles目标、完善六维、失败降级/不臆造与结果摘要等旧流程语义。目标不得由浏览器任意指定。
- 先红测真实baseline→任务适配→最终worker RunRequest.prompt，证明包含完整增强动作与正确目标，而非只有字段摘要；输入摘要/签名仍绑定实际prompt。
- 受控执行器应根据接收到的正确任务指令更新本人画像假文件并让原页面读回，缺指令时测试必须失败。不能按task_type无条件返回PROFILE-RESULT就当增强成功；真实模型仍可not-run。

## T5C1-R1-04：保护准备到接纳的资源交错

- 画像基线准备、Run接纳与同画像编辑/删除的交错要有一致资源边界。最小串行化/资源声明或派发前验证都可，复用现有机制，不建立第二套账本。
- 先红测基线已写但Run尚未reserve/dispatch的暂停点，同时发编辑/删除：只能明确忙碌拒绝，或构建明确检测变更后拒绝派发；不能删除200后构建仍running/继续prompt。
- 覆盖同名并发构建、重复turnId、接纳失败的声明释放/基线保留、终态后正常编辑删除；已有Run不重复派发，基线冲突和新请求不能互相覆盖。不要只将profile_active布尔检查移位置而保留检查/操作间同样窗口。

## 复跑、证据与边界

主审只读探针在root docs/evidence下original-web-task-05c1-review；Python脚本的--repo或位置参数见各脚本。四项先红绿，再相关control/worker/任务/模型签名/broker与前端Node/build/lint；5C1扩展矩阵以及5B五组、5A四组、Task3 33组、Task4八组保持，历史截图不覆盖。

仅需受控provider/假ACP证明一次派发、问题回答、正确模型与画像文件更新；隔离容器按受影响部分增量构建，不无缓存重建Harness/Chromium全栈。用户运行实例、原卷/socket/Key/Cookie/邀请不动，不真实付费/登录/发布、不全局prune或删历史网络。

显式文件提交并列父/新SHA、职责、四项红绿/独立探针、回归与not-run、清理情况。证据放本组既有目录的R1子目录；主审audit/task/plan/probe只读。完成停止复审，不进入5C2/5C3/6，不自动上线。
