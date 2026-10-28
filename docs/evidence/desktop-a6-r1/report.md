# Task A6 R1 证据：取消清理期间的同会话互斥

日期：2026-10-29（Asia/Shanghai）
基线（父提交）：`acef78b3ae3b1b1f318a297bf2c5cb06200aa76b`
范围：仅 A6-R1-01；提交后停止等待复审，不进入 A7。新提交见
`git rev-parse HEAD`。

## 1. 结论与修复

A6-R1-01 的根因是：`RunManager.stop` 先写持久终态 `cancelled`，再取消
supervisor 并等待 ACP `session/cancel`、子进程与连接清理结束；旧自愈
逻辑却把 registry 终态当成“executor 已退出”，因此取消清理窗口内第二个
同会话请求会清掉活锁并进入执行。

修复仅改 `easel/worker/runs.py` 的 `_prepare` 互斥判定：

- 只有本地 holder task 不存在或 `done()` 时，才修复陈旧锁；
- holder task 未结束时保持 `conversation_busy`（409），不执行 adapter、
  不追加用户/助手消息、不改绑 native ID；
- 真正的陈旧锁自愈与启动失败空 native 绑定恢复保留。

## 2. 镜像与源码一致性

| 项 | 实际值 |
|---|---|
| worker（A6-R1） | `easel-worker-harness:a6-r1` = `sha256:e48eb63b6155b6fc14c37a2525d138e7419f7a8024f46a58d209c40d3a5bd993`，`2689779306` bytes |
| A6 原 worker（保留） | `easel-worker-harness:a6` = `sha256:984bfac19412c0c6251e93568508fc9c078970727e4a9b515d5cb455a82128e9` |
| control（未改） | `easel-control-test:a6` = `sha256:2209ce6d1a4dc5889810ff5e9c1ac7e0b3db7d33d21ad496cf17d6960d50ea87` |
| gateway（未改） | `easel-gateway-test:g2` = `sha256:c0a35aaa9f304c47f9f5c3e73add7d9d5920041baef156e2ea674f3000c5580a` |
| 构建期门槛 | `DSH_PROFILE_OK commit=687f282e8625c6b8d11ff58211a90524be2e4317 node=24.22.1 patch_sha256=8ebd72c06981bd099fd3d8e1da1370c61e8b4826bf0972cc0ffc528b1254ad09 model=easel-broker/deepseek-chat` + `WORKER_SMOKE_OK` |
| 镜像内源码 | `easel/worker/runs.py` = `f2498cecb5cf9306753d80b14a1179fa63615cb629996335e36833b63d537235`；宿主提交源码 = `f2498cecb5cf9306753d80b14a1179fa63615cb629996335e36833b63d537235` |

worker 由当前源码重建，control/gateway/前端未改，不重做整套镜像基础层。
构建期网络下载使用本机代理（与 A6/A5 相同），仅作用于构建阶段。

## 3. 红绿测试

详见 `red-green.md`。修复前两个新测试均为
`Failed: DID NOT RAISE DomainError`；修复后：

```text
python -m pytest tests/multiuser/test_runs.py -q \
  -k "cancel_cleanup_keeps or real_acp_gated_cancel"
2 passed, 27 deselected in 8.09s
```

测试不是用固定 sleep：RunManager 红测用 executor 的清理 gate；真实 ACP
红测用 `tests/fixtures/harness_rpc.py` 新增 `gated-cancel` 模式，fixture
收到 `session/cancel` 后写 `fixture/cancel-waiting` 并等待测试创建文件。
取消协议预算放大到 30s 只是为了不让 2 秒协议期限抢先；放行完全由测试
文件信号决定。

## 4. 物理关闭证据

真实 ACP 子进程测试在取消 gate 期间断言：

- `stop` 任务未完成、registry 已是 `cancelled`、holder task 未完成；
- 旧 ACP 子进程仍存活（`returncode is None`），取消 gate 尚未放行；
- 第二个同会话 `submit` 得到 `conversation_busy`，审计文件没有新增
  `initialize`/`session/prompt`，消息只有第一轮用户消息，native 绑定
  仍为 `native-session`；
- 创建 gate 文件并等待 `stop` 完成后，旧进程 `returncode` 已写回、
  session 池为空；
- 第三轮正常完成，审计中 `initialize` 与 `session/prompt` 各 2 次，
  native 绑定仍为 `native-session`。

## 5. 定向浏览器复跑

```text
EASEL_RUN_CONTAINER_TESTS=1 \
EASEL_A6_SCREENSHOTS=C:/Code/Easel/docs/evidence/desktop-a6-r1/screenshots \
python -m pytest tests/integration/multiuser/test_desktop_chat_live.py -q -s \
  -k "permission_stop_sse or restart_rebuild"
2 passed, 3 deselected in 254.14s (0:04:14)
```

```text
A6_2_PASS {"allow_requests": 2, "deny_requests": 2, "hang_state": "cancelled",
 "logout_close_seconds": 0.03, "retry_runs": 1, "slow_events": 4}
A6_3_PASS {"gateway_restart_same_id": true, "hang_state": "interrupted",
 "provider_chat": 16,
 "worker_before": "32b2ed7e...", "worker_after": "9e62e4a7...",
 "failure_notices": ["Agent 执行失败，请检查本机 CLI、登录和模型配置", ...]}
```

- UI stop：服务端 `cancelled`、token 撤销、worker 未停；随后同会话
  `harness-after-stop` 正常 done，未受取消清理阻塞。
- 启动失败：Run failed、UI 稳定错误、provider 0；恢复配置后同会话新
  Run done；日志**没有** `A6_STARTUP_RETRY`，说明没有再撞上 409 收尾窗口。
- A6.3 内的 control 重启、gateway 重启（同 ID）、worker 重建（前后的
  容器 ID 不同，native 历史续接）、401/429/500/断流与秘密边界均按原
  断言通过。
- 截图写入新目录，未覆盖 A6 原图：`04-permission.png`、`05-failure.png`。

## 6. 回归与计数

| 项 | 命令/结果 |
|---|---|
| A6-R1 新测试 | `tests/multiuser/test_runs.py -k "cancel_cleanup_keeps or real_acp_gated_cancel"` → 2 passed |
| 陈旧锁/启动失败保留 | `tests/multiuser/test_event_stream.py -k "stale_terminal_conversation_lock or failed_startup_binding"` → 2 passed |
| 受影响 worker/control 回归 | runs/sessions/proxy/messages relay/event stream/session runs/stop barrier/stop recovery/runtime gateway/ACP → **213 passed, 5 skipped** |
| 全 multiuser + 桌面拓扑 | `tests/multiuser tests/integration/multiuser/test_desktop_topology.py` → **1395 passed, 10 skipped**（166.53s） |

## 7. 未执行与边界

| 项 | 状态 | 说明 |
|---|---|---|
| 真实 DeepSeek 供应商请求 | **not-run** | 无批准的 control 受管 channel/凭证；未读取开发者 `.env`/HOME；sentinel fake provider 不冒充真实供应商 |
| A6 全套浏览器矩阵/ngrok/前端 | 未重做 | 本轮未改前端/ngrok/control；原 A6 证据保留；只定向复跑 stop/续接与启动失败 |
| A7 备份/升级/回滚/恢复 | **not-run** | 按任务单不进入 A7 |
| 生产 Chromium 沙箱/XFS/Linux 防火墙/IPv6 | 未验证 | 不把宿主 Chromium 或文本聊天成功当作这些门槛通过 |
| 错误公钥/运行期缺 broker UI 反例 | 仍未单独复跑 | 沿用 A6 的未执行边界，不写成通过 |

## 8. 清理

- 测试资源随机归属；浏览器测试结束后核对无本轮容器/网络/卷、无
  workspace 容器、无 provisioner/ngrok 进程。
- 未执行全局 prune，未触碰开发者容器/卷/网络或已有 ngrok 隧道。
- 保留供复审：`easel-worker-harness:a6-r1`、A6 原 worker 镜像、
  `easel-control-test:a6`、`easel-gateway-test:g2`。

## 9. 停止门

提交后停止，等待 A6-R1 复审；不进入 A7。
