# Task A6 证据：真实 Harness 双用户浏览器与故障恢复闭环

日期：2026-10-28/29（Asia/Shanghai）
基线（父提交）：`1e70afab0a55bee4cf41f985b691b6ebbf82c604`
本轮提交：见 `git rev-parse HEAD`；仅实施 A6，不进入 A7。

## 1. 固定输入与镜像

| 项 | 实际值 |
|---|---|
| 正式前端 | `web/frontend/dist`（control 镜像构建期 `npm run build`；本轮 118 前端测试 + build + lint 通过） |
| control 镜像 | `easel-control-test:a6` = `sha256:2209ce6d1a4dc5889810ff5e9c1ac7e0b3db7d33d21ad496cf17d6960d50ea87` |
| gateway 镜像 | `easel-gateway-test:g2` = `sha256:c0a35aaa9f304c47f9f5c3e73add7d9d5920041baef156e2ea674f3000c5580a` |
| worker（真实 Harness） | `easel-worker-harness:a6` = `sha256:984bfac19412c0c6251e93568508fc9c078970727e4a9b515d5cb455a82128e9`，2689778768 bytes |
| Harness 源码 | `687f282e8625c6b8d11ff58211a90524be2e4317`，Node `24.22.1`，pnpm `11.28.5` |
| 构建期真实门槛 | `DSH_PROFILE_OK commit=687f282e8625c6b8d11ff58211a90524be2e4317 node=24.22.1 patch_sha256=8ebd72c06981bd099fd3d8e1da1370c61e8b4826bf0972cc0ffc528b1254ad09 model=easel-broker/deepseek-chat` + `WORKER_SMOKE_OK` |
| 浏览器 | 真实 Chromium `151.0.7922.34`（Playwright，headless） |
| 平台 | Windows 11 Pro；Docker Desktop + WSL `Ubuntu-26.04` provisioner；本轮测试容器为非 root、只读 rootfs、cap-drop ALL、`no-new-privileges` |

worker 镜像以 A5 固定 commit/Node/pnpm 重新构建，原因是本轮真实反例暴露了
worker 侧启动失败后的会话绑定/陈旧锁恢复缺陷；A5 原始镜像
`sha256:6830c2b4b2f3424dde6fff492803c05cbe77cc03d4e5921a14e26e3a7d738434`
仍保留，A5 真实 Harness 集成回归用它复跑通过。镜像内
`easel/worker/runs.py`、`easel/control/proxy.py`、`easel/control/runtime.py`
与提交源码 SHA-256 逐一相同（6e5b9236… / 15da3d08… / d37efde6…）。

## 2. 拓扑与边界

- 正式 control `serve-control` + 正式 Auth/Session/Runtime/WorkerRelay/Broker、
  WSL provisioner、gateway、逐 workspace sidecar、A4 正式网络与 A5 真实
  Harness；临时 DB/master/signing key/策略/workspace。
- 两个浏览器 context：独立 Cookie/存储；邀请兑换、建会话、发消息、权限
  问答、停止、重连全部经真实 UI（API/DB/容器只用于事实核对）。
- 本地 HTTPS：本轮 localhost 自签证书 + Chromium SPKI pin（`ignore_https_errors=False`），
  不使用全局 `ignore-certificate-errors`。
- ngrok：本轮自建隧道（公开 CA），严格 Origin、Secure/HttpOnly Cookie、
  CSRF；未修改 authtoken/既有隧道，测试结束只停止本轮 PID。
- 供应商 Key 只在 control 加密配置；worker 只持有逐 Run opaque broker
  token，sidecar 只把精确 broker 路由上的标准 Bearer 映射为 control 内部
  token。没有开放任意代理路径。

## 3. 真实浏览器结果

最终命令：

```text
EASEL_RUN_CONTAINER_TESTS=1 python -m pytest \
  tests/integration/multiuser/test_desktop_chat_live.py -q -s
4 passed, 1 skipped in 290.63s
```

### A6.1 双用户聊天、刷新/重登与隔离

- UI 邀请登录 → 新建会话 → A 两轮、刷新后继续第三轮、退出重登；B 独立
  context 两轮。`A6_1_PASS`：provider chat 6，A/B worker 容器 ID 不同，
  workspace 不同。
- 刷新/重登后历史完整，native binding 不变：重登后的 provider 请求包含
  前两轮用户消息与助手回复。
- A 读 B 的 session/messages/runs/SSE 均服务端 404；B 页面不含 A 消息，
  A 页面不含 B 消息；Cookie 不同。
- 截图：`screenshots/01-a-two-rounds.png`、`02-a-after-relogin.png`、
  `03-b-two-rounds.png`。

### A6.2 权限、幂等、停止、SSE 与注销

- 工具允许：真实 `tool_calls`（bash + danger-full-access）→ ACP
  `session/request_permission` → UI “执行授权”卡片 → 允许一次 → 实际
  `echo harness-tool-ok` → provider 第二回合收到 `role=tool`；provider
  2 次请求。
- 工具拒绝：无 `harness-tool-ok` 文本/工具结果，未绕过权限；Run 正常
  收尾；provider 2 次请求，第二回合无 echo 工具结果。
- 同 turn 重试（首次 POST 被浏览器中断）：服务端只有 1 个 Run、1 次
  provider 请求，事件 ID 单调且仅 1 个 done；双击发送同样只有 1 个 Run/1
  次 prompt。
- 传输中断：在 Run 执行中关闭本轮 TLS 连接，UI 出现断线提示；点击
  “重连（只回放）”后 URL 携带 `after>0`，最终助手文本只出现一次；刷新
  后历史仍只有一份。
- UI 停止：provider 请求到达后停止，Run 进入 `cancelled`，broker token
  撤销，worker 保持运行；同会话下一轮成功（容量释放）。
- 注销在途慢流：SSE 在 `0.03s` 内停止读取（≤5s），旧 Cookie 后续 401，
  同浏览器 context 登录 B 后看不到 A 内容；A 的后台 Run 真实 done 且
  历史可读，未伪造成 worker done。
- 截图：`04-permission.png`。

### A6.3 重启、失败与秘密边界

- control 重启（docker restart，同一持久卷）：Cookie/会话仍有效，历史
  可读；在途 broker 请求被真实打断，Run 如实收敛为 failed（不伪造
  done），provider prompt 只 1 次；重启后新 Run 成功。
- gateway 重启：worker ID 与 gateway ID 不变，原 token 未被误撤销
  （Run 结束才撤销），SSE 自行恢复/只回放，provider 1 次。
- worker 消失/重建：删除 worker 容器后，下一次发送在同一接纳调用内识别
  实例身份消失、失败旧租约并重建；旧在途 Run `interrupted`、token 撤销；
  新会话 Run 成功；回到原会话在新 worker 上续接 native 历史（provider
  请求含重建前的用户消息）。`A6_3_PASS` worker before/after 不同。
- provider 401/429/500/断流：四种模式各只 1 次 provider 请求；UI 显示稳定
  错误（“Agent 执行失败，请检查本机 CLI、登录和模型配置”）；旧成功历史
  不被覆盖；之后新 Run 成功（容量收口）。
- 错误 broker channel：control 启动期即 `managed_config_invalid`、exit 2
  （fail-closed），不带着不可签发路由接客；恢复原配置后新 Run 成功。
  运行期 capability 503 路径由既有 broker/worker 回归覆盖（见 §5）。
- 启动失败（missing patch）：Run failed、UI 稳定错误、provider 0 次；
  恢复配置后同会话新 Run 成功（worker 会话绑定/陈旧锁修复）。
- 秘密边界：供应商 sentinel Key 不出现在浏览器页面、公开 API 响应、control
  日志、worker 日志或 worker env；worker/gateway 无发布端口；provider
  请求只带 control 端 sentinel Bearer，说明模型经本空间 broker。
- 截图：`05-failure.png`。

### A6.4 ngrok 公网与真实供应商

- ngrok 公网 HTTPS 通过：本轮自建隧道
  `https://acts-revise-unrented.ngrok-free.dev`（测试结束已停止），
  Chromium 以公开 CA 校验（无 pin/无 ignore），UI 登录并发消息成功；
  Cookie `Secure` + `HttpOnly` + SameSite；公开 Origin 严格匹配。
  `A6_4_NGROK_PASS`。截图 `06-ngrok-chat.png`。
- 真实 DeepSeek 供应商请求 **not-run**：本轮没有批准的 control 受管
  channel/凭证；未读取/搜索开发者 `.env`、HOME 或私有 profile。sentinel
  fake provider 的成功不代表真实供应商通过。

## 4. 本轮产品修复（真实反例）

1. `easel/control/runtime.py`：ready 租约的 worker 实例身份消失时，失败
   旧租约（中断残留 Run、撤销 token）并在同一 `ensure_ready` 内重建；修复
   新 Run 永久 503 / 旧 Run 不收敛。
2. `easel/control/proxy.py`：旧 Run 实例消失后，消息历史从同 workspace
   当前实例读取（不创建 worker、不跨空间）；worker 明确 400/409 拒绝派发
   时结算 reserved Run 并撤销 token，避免容量永久占用；网络 502 仍交后台
   对账。
3. `easel/worker/runs.py`：启动失败留下空 native 绑定 + 消息时，无活动
   Run 允许下一轮重新 `session/new`，保留 Easel 历史；终态 Run 的陈旧
   会话锁可自愈。
4. 测试装置：真实 Chromium 双 context、TLS 终结器 drop、fake provider
   慢流/挂起/当前轮路由、A6 正式拓扑 runner。

对应红灯/绿灯与单测见 `red-green.md`。

## 5. 回归与计数

```text
前端 node --test tests/frontend/*.test.mjs     118 passed
前端 npm run build                             成功（dist index-C1w31LuT.js / D-Q9WPHd.css）
前端 npm run lint                              oxlint 成功
tests/multiuser + test_desktop_topology.py     1393 passed, 10 skipped（203.37s）
A4 正式夹具容器回归                             1 passed（86.88s）
A5 真实 Harness 集成（A5 原镜像）               2 passed（75.17s）
A6 真实浏览器矩阵                               4 passed, 1 skipped（290.63s）
```

A6 的 1 skipped 即真实供应商 not-run（无批准凭证）。A5 回归使用 A5 原
镜像 `6830…`，证明本轮 worker 重建/修复没有改变 A5 的受控证据语义；
A5 主审记录的固定镜像仍可复审。

## 6. 失败/未执行清单

| 项 | 状态 | 说明 |
|---|---|---|
| 真实 DeepSeek 供应商文本请求 | **not-run** | 无批准受管 channel/凭证；不读开发者私有配置；sentinel 不能替代 |
| 真实供应商 401/429/5xx/断流 | 覆盖（fake provider） | 真实供应商未计费调用 |
| 运行期“缺 broker/错误 channel”UI 503 | **未单独产生** | control 启动期 fail-closed（exit 2）；运行期 503 由既有 broker 回归覆盖 |
| 错误公钥 UI 反例 | **未单独复跑** | worker 签名/公钥拒绝由既有 gateway/worker 回归覆盖；A6 UI 未以错误公钥启动 worker |
| A6 双用户公网浏览器 | **通过** | ngrok 自建隧道 + 公开 CA |
| A7 备份/升级/回滚/新目录恢复 | **not-run** | 按任务单不进入 A7 |
| 生产 Chromium 容器沙箱/XFS/Linux 防火墙 | **未验证** | 不把宿主 Chromium 或文本聊天成功当成这些门槛通过 |

## 7. 清理

- 无 `easel.a6.browser` 标签容器/网络/卷；无 `easel-worker-*` /
  `easel-gateway-*` / `easel-sidecar-*` / `easel-ws-*` 本轮残留；无
  `/tmp/easel-a6-*` 或 provisioner 进程；本轮 ngrok 进程已停止（0 个）。
- 未执行全局 prune，未触碰开发者容器/卷/网络/HOME/profile 或既有 ngrok
  隧道。
- 保留供复审：`easel-control-test:a6`、`easel-worker-harness:a6`、
  `easel-gateway-test:g2`、A5 原 worker 镜像。

## 8. 边界

- 除 A6 浏览器矩阵外，未宣称生产 Chromium 沙箱、XFS 配额、Linux 防火墙
  或真实供应商计费通过。
- 未进入 A6 之外或 A7 的运维工作；提交后停止等待复审。
