# Task A6：真实 Harness 双用户浏览器与故障恢复闭环

> **For agentic workers:** 使用 superpowers:executing-plans；先红后绿，真实浏览器操作先读取适用浏览器技能。每阶段如实记录 pass/fail/not-run；提交后停止等待主审，不进入 A7。

**Goal:** 用同源前端、两个独立浏览器 context 和 A5 真实 Harness，验证用户实际可用的登录、独立聊天、权限问答、停止、SSE 恢复与服务重启续接。

**Architecture:** 复用 A4 正式 control→gateway→worker 与逐 workspace sidecar→broker 链路，使用 A5 runtime。先以受控 TLS provider 完成全部可重复浏览器反例，再在确有批准受管凭证时做有界真实 DeepSeek 请求；两类证据独立，fake provider 的通过不能覆盖真实供应商 not-run。

**Tech Stack:** Windows Docker Desktop/指定 WSL provisioner、现有 React 前端、HTTPS/ngrok、真实 Chromium、真实 A5 Harness；现有 Python 集成测试和浏览器脚本。

**Spec:** Desktop 装配计划 Task A6 与原多用户 spec。A5 裁决见 `../audits/2026-10-28-desktop-a5-accepted.md`。

## 输入、文件与约束

- Easel 基线 `1e70afab0a55bee4cf41f985b691b6ebbf82c604`；先报告 HEAD/工作树，保护主审文档。A5 runtime 固定镜像 `sha256:6830c2b4b2f3424dde6fff492803c05cbe77cc03d4e5921a14e26e3a7d738434`；实际检查镜像存在及 manifest，不能用 A4 fake ACP worker 替代。
- 建议新增 `tests/integration/multiuser/test_desktop_chat_live.py` 作为正式拓扑/证据 runner，`tests/frontend/browser_desktop_chat.py` 承担真实 UI 流程。复用已有 A4/A5 创建与清理工具、Task 11/12 的 HTTPS 浏览器经验；不另造认证或测试专用公开后门。
- 必要产品改动仅限本轮真实反例暴露的装配/UI 缺陷。保留 G1/G2 audience/代次/精确路由、A4 公钥与 Engine、启停 Mutex、停止屏障、Run 幂等与恢复，不扩展仍 503 的发布/账号等业务。
- Docker 与浏览器 context 均为本轮测试专用。资源用随机 run-id、归属标签和精确 ID；清理逐项确认，CLI 超时也能定位，不依赖 --rm 自动成功，不全局 prune、不动开发者实例/HOME/profile。
- 不改 ngrok authtoken 或现有隧道。可通过既有 CLI 使用已配置账号，为本轮 control 回环端口创建自己可识别的隧道；仅停止自己启动的 PID/隧道。账户限制导致不能另开时，不抢占别人的隧道。
- ngrok HTTPS 场景必须严格 Origin、Secure/HttpOnly Cookie、CSRF、公开 CA；不得全局 ignore-certificate-errors。若公网暂不可用，允许范围受限的本轮 TLS/SPKI 作为浏览器功能证据，但 ngrok 公网门槛明确 not-run，不宣称 A6 全部完成。

## Review Focus

1. UI 的身份切换/刷新/异步回包不能让 A 的会话、历史、未决权限或流进入 B。
2. 断网重连/重复点击不能重复 prompt、丢事件或把失败写成成功；按持久 after 恢复。
3. token/Run/Harness 子进程变化后仍续接同一原生历史；gateway 故障不误结束 worker Run。
4. 撤销/禁用必须终止浏览器流访问，不能靠 UI 隐藏代替服务端拒绝。
5. 最终证据必须区分真实浏览器、真实 Harness、fake/真实 provider、ngrok/本轮 TLS 与未验证生产门槛。

## A6.1：正式拓扑与 HTTPS 浏览器基线

- [ ] 先写测试失败条件并实际运行；不是新增一个永远返回绿色的测试页。使用正式构建前端、Auth/Session/Runtime/WorkerRelay/Broker、WSL provisioner、gateway 与 A5 worker。
- [ ] 独立临时 DB/master/signing key/策略与 workspace，初始化管理员和两个邀请用户。邀请、Cookie、CSRF、token 不进入截图/原始报告；截图避开登录秘密输入与邀请内容。
- [ ] 创建两个隔离浏览器 context（浏览器原生存储独立），从实际 UI 兑换邀请、进入会话页、创建会话并发送消息。不得用 API-only submit 代替 UI 操作；API/DB/容器观测可用于验证事实。
- [ ] A/B 各至少两轮，刷新页面/退出重登后仍可读自己的历史，native binding 不变；A 无法读 B session/run/messages/events，服务端 404。浏览器截图只证明 UI，结合服务端账本/模型请求计数证明隔离。

## A6.2：停止、权限、幂等与 SSE

- [ ] 真实 Harness 工具调用经 ACP request_permission → UI 显示 → 用户允许一次 → 实际无副作用工具（例如本轮 workspace 的 echo/sentinel）→ 模型收到 tool 结果。另测拒绝：没有实际工具副作用，不绕过权限。
- [ ] 重复点击/相同 turn 重试只创建一个 Run、一次 prompt；对照 provider 请求/adapter 调用与持久事件 ID，不只检查按钮 disabled。
- [ ] 真实浏览器网络中断后恢复 SSE，按 after 接续；无重复助手文本、无丢失/乱序终态，刷新可从服务端历史恢复。
- [ ] UI stop 结束原 Run，worker 状态、容量、broker token 与 UI 一致；不是只把本地 spinner 关闭。
- [ ] 注销/管理员禁用/撤销登录中至少覆盖在途长流：在已有约定的 5 秒窗口内停止读取，后续 HTTP 被拒；后台 Run 按既有策略运行/停止，不把失去浏览器认证伪造成 worker done。

## A6.3：重启、失败与秘密边界

- [ ] 真实 control 重启后 Cookie/会话按持久策略处理，历史可读，账本不重复提交 prompt；Run 后台对账继续。仅操作本轮 control。
- [ ] gateway 新 boot/停止替换时 worker ID 不变，原 Run/token 不误撤销，浏览器 SSE/历史恢复；恢复 POST 不自动重发。
- [ ] 同空间 worker 停止/重建后，新 Run 通过原 native session/私有 HOME 续接；旧在途 Run 若实际中断则明确非 done，不向用户伪装执行未间断。
- [ ] 覆盖错误公钥、缺 broker/错误 channel、供应商 401/429/5xx/断流、启动失败：容量可收口，旧历史不覆盖，失败输出可恢复，UI 显示稳定错误。没有 provider Key/内部 token 出现在前端响应、日志或截图。
- [ ] 验证本轮 worker 无发布端口，不能直连公网/管理面/另一空间，模型实际经过本空间 broker；继续使用已批准出口策略，不为测试开放任意私网。
- [ ] 宿主 Chromium 用于用户 UI 验收，不等于容器浏览器技能通过。容器 sandbox 门槛未通过时保持明确未通过/能力不可用，不加 --no-sandbox 或额外权限。

## A6.4：真实供应商子门槛与交付

- [ ] A5 未执行的真实 DeepSeek 请求在此保留：仅当本轮存在批准的 control 受管 channel/凭证时，通过正式 UI + 真实 Harness 做有界、低输出上限的文本调用；Key 只经已有安全配置渠道装配，不搜读开发者 .env/HOME，不写 argv、截图或报告。不自动计费重试/工具循环。
- [ ] 没有批准凭证时继续完成 fake-provider 浏览器矩阵，将真实供应商项独立标为 not-run，并明确 A6 尚有未关闭门槛；不能用 sentinel Key 的成功代替真实供应商。
- [ ] 按实际修改运行 frontend tests/build/lint、聊天/身份/会话/SSE/gateway/broker/runtime/Desktop 受影响回归；真实浏览器与真实供应商分别列计数/平台/证书验证方式/镜像摘要。
- [ ] 输出 `docs/evidence/desktop-a6/`：可复跑命令、脱敏检查 JSON、少量关键截图、事件/请求计数及失败恢复记录、pass/fail/not-run 清单、清理核验。报告完整父/新 SHA，不提交主审审计/计划/任务单。
- [ ] 交付后停止等待复审，**不进入 A7**。无凭证/公网限制须如实记录，但不成为跳过其它可执行浏览器验收的理由。
