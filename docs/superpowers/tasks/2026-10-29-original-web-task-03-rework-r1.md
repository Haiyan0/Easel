# 原 Web 修复 Task 3 R1：401 闭环、权限重试、SSE 游标

父基线：`8f1c11253b828eacc8d53f60107356248956bb47`。
继续 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。
仅按 [首轮审计](../audits/2026-10-29-original-web-task-03-review-round-1.md)修三项；原组件复用/守卫/架构保留，不做 Task 4 登录视觉或 Task 5 后端。

## T3-R1-01：401 先正确结束所拥有的身份

- 当前身份的提交、初始历史/active-run、questionStatus 返回 401，必须取消流/请求、清私人视图及缓存、回登录，不残留 pending/题卡/设置。
- 不能在处理 401 之前被 isCurrent/isSelected/owns 吞掉；这些 guard 仍需保护普通迟到结果。
- 迟到 A 401 不踢 B，同账号重新登录的新 epoch 也不受旧响应影响；多请求同时 401 只结束对应身份，不反复清新身份。
- 先写真实浏览器反例，至少覆盖 submit 和 questionStatus 两条路径，并补初始历史路径及迟到旧 401 回归。不要只测 identityFetch 纯函数。

## T3-R1-02：临时回答失败保持题卡，可原地重试

- 回答 503/断网时保留当前题卡、已选答案及明确错误，恢复后同卡可重试成功。WorkspaceApp 不得将 hook 吞掉的失败一律包装 ok:true。
- 只成功或有明确已过期/已回答证据才移除/终结；409 的原过期处理保留并单独测，403 不冒充登录过期，401 按上一项处理。
- 真正服务端仍 awaiting_permission 的反例必须断言题卡仍在，不重发原 prompt、不自动再次批准；用户重试只发送答案。
- 先写真实 QuestionCards/原工作台浏览器测试，不只断言 notice 文本。

## T3-R1-03：统一单调记录已消费事件游标

- thinking/activity/question/error 等与 state/token 一样，在有效连接归属下推进内存和恢复游标；同页重连不重复追加已显示内容。
- 至少测试：state1→thinking2→断线→重连 after2，思考只出现一次、POST /api/chat 次数不增；question/activity 为最后事件也覆盖。
- 旧流迟到事件不回退新连接 cursor；保留原刷新时从服务端回放以重建未完成内容的行为，不用跳过历史帧把刷新后的内容清空。

## 范围与证据

预计修改 hooks/useWorkspace.ts、WorkspaceApp.tsx 及必要的 workspaceClient/auth/QuestionCards 最小接口；不能复制另一套 UI。实际需要的文件显式列出。新增行为测试/浏览器 fixture 及本项证据可提交，主审 audit/task/spec/plan/probe 不修改。

复用 root 主审 `docs/evidence/original-web-task-03-review/probe_browser.py` 的情景，不修改主审探针来变绿。测试使用真实 build、独立假 API/假邀请/独立 context，不接用户容器、真实登录态或供应商，不重建大 Harness 镜像。

先确定性红测再修复，运行原 Node 回归、build、lint 和完整浏览器矩阵，保留 local 验收。原矩阵主审遇一次 admission-ordering 超时、全新 fixture 单项通过：完整复跑若仍波动，明确定位场景共享状态/前置条件并修 fixture，不删用例、改预期或只加长超时。

交付父/新 SHA、文件清单、三项红绿及真实浏览器反例、Node/build/lint/完整矩阵结果、取消/迟到/重试计数与清理。保护运行实例、主审既有改动和旧资源；显式提交后停止等复审，不进入 Task 4/5、不自动上线。
