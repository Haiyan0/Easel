# Task A6 红绿记录

日期：2026-10-28/29（Asia/Shanghai）
基线：`1e70afab0a55bee4cf41f985b691b6ebbf82c604`

本轮所有失败先实际运行复现，再修装置或产品代码，最后重跑绿色。真实浏览器
使用仓库既有 Playwright + 真实 Chromium，两个独立 context；真实 Harness
使用 A5 固定源码构建，仅新增 worker 恢复修复后重建本轮镜像。

## 装置红灯（测试基础设施，不改产品）

| 红灯 | 实际输出/现象 | 修复 |
|---|---|---|
| 终结器容器挂载 Windows 路径走 WSL Docker | `invalid volume specification: 'C:CodeEaseltestsfixtures:/fixture:ro'` | 改用宿主 `docker run` 与本轮绝对路径 |
| Chromium `set_offline` 不移除已建立 SSE | 30 秒内无“连接中断”提示，慢流继续送达 | 本轮 TLS 终结器增加受控 `drop` 端口，关闭真实 socket；浏览器按 after 重连 |
| 慢流 token 被真实 Harness 缓冲到结束 | 首 token 与完成同时到达（26.88s） | 断流时机改为“Run 执行中、SSE 连接已建立”，不依赖部分文本 |
| 停止点击早于 provider 请求 | `provider_requests == 0` | 先等待 provider 审计出现该 Run 的请求，再点停止 |
| fake provider 按整段历史匹配 marker | 停止后的下一轮因历史含 `harness-hang` 再次挂起 | 只按“当前轮 user 文本”选择响应模式（跳过运行上下文快照） |
| 注销后立即用旧 Cookie 断言 401 | 一次性 200（logout 请求仍在途） | 有限轮询至 401，并核对 logout 响应状态（204） |
| `latest_run` 在已有历史会话取到旧 Run | 打开旧 Run 的 SSE 等待超时 | 新增 `_wait_new_run(previous)`，只认本轮新接纳 Run |
| control 重启后“断线提示”缺失 | 终结器未把上游 EOF 传给浏览器 | 终结器任一方向结束即关闭双端 |
| control 重启后的在途 Run | broker 请求被打断，Run 如实 failed（不伪造 done） | 断言改为诚实终态 + 重启后新 Run 成功 |
| gateway 重启未产生 UI 断线提示 | control 自行恢复上游，SSE 可保持 | 不强制提示，只等待结果与 after 回放 |
| 错误 broker channel 期望 UI 503 | control 启动期 `managed_config_invalid`，exit 2 | 改为真实 fail-closed 验证；运行期 503 由既有 broker 回归覆盖 |
| 启动失败后的下一轮 409 conflict | 新会话留下空 native 绑定 + 消息，永久 `session_binding_incomplete` | worker 修复：无活动 Run 时允许重新 session/new |
| worker 明确 409 后 reserved Run 卡死 | 控制库 Run 永久 reserved，占容量 | control 修复：400/409 派发拒绝时结算 interrupted 并撤销 token |
| worker 实例消失后 ready 租约永久 503 | 新 Run 永远 `worker_identity_mismatch`，旧 Run 不收敛 | control 修复：失败旧租约并在同一 ensure_ready 内重建 |
| worker 重建后旧会话历史 502 | 消息读取仍按旧 Run 实例定位 | control 修复：从同 workspace 当前实例读取历史 |

## 产品修复的红绿单测

| 修复 | 红灯测试 | 结果 |
|---|---|---|
| ready 租约身份消失 → 失败旧租约 + 同调用重建 | `test_missing_worker_identity_fails_ready_lease_and_restarts` | RED：`RuntimeUnavailable worker_identity_mismatch`；GREEN：25 passed |
| 旧实例消失后消息回退当前实例 | `test_messages_falls_back_to_current_instance_after_worker_rebuild` | 真实 A6 红灯 502；GREEN：5 passed |
| worker 400/409 派发拒绝 → 结算 reserved | `test_worker_dispatch_rejection_settles_reserved_run` | RED：status `reserved`；GREEN：34 passed（proxy/messages/runtime） |
| 启动失败空绑定恢复 | `test_failed_startup_binding_is_repaired_for_next_run` | RED：`conflict` 409；GREEN：13 passed（event stream） |

## 绿色复跑（最终镜像）

见 `report.md` 的最终命令与计数。
