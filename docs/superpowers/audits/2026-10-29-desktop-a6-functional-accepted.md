# Task A6 功能验收：真实浏览器闭环及取消收尾互斥

原基线：`1e70afab0a55bee4cf41f985b691b6ebbf82c604`。
A6 提交：`acef78b3ae3b1b1f318a297bf2c5cb06200aa76b`。
R1 最终提交：`d9c2f5ba960e892f198ebc4a111b7e2ad7e835af`。

## 裁决与边界

A6 的真实 Harness + 受控 provider 浏览器功能，以及 R1 取消清理互斥通过，可以进入 A7 运维验收。此结论是功能范围验收，不是全部外部门槛通过：真实 DeepSeek 请求仍为 not-run；原错误公钥 UI/运行期缺 broker UI 的未单独执行情况保持记录；生产 Chromium/XFS/Linux firewall/IPv6 不因此关闭。

公开 ngrok HTTPS、两个浏览器 context、真实 Harness、权限允许/拒绝、幂等、SSE 回放、注销、control/gateway/worker 重启等原 A6 证据保留为作者真实运行证据。主审未在本轮重做整套公网浏览器，不能把作者结果写成主审重跑。

## R1 关闭依据

`RunManager._prepare` 不再因 registry 先写 cancelled 就清除活锁；本地 holder task 仍未完成时明确 conversation_busy。真正 task done/不存在的陈旧锁自愈和启动失败空 native 绑定恢复保留。

新增测试使用真实 RunManager 的 executor cleanup gate，以及真实 ACP RPC 子进程的 session/cancel 文件 gate：旧进程仍活/stop 未返回时，第二轮没有 adapter 调用、消息追加、改绑或新 initialize/prompt。放行后旧进程关闭、池条目消失，再启动的新轮正常续接同一 native-session。

## 主审与独立验证

- 主审 runs/proxy/event-stream/sessions/sessions-chat/ACP/workspace 七文件：**183 passed, 7 skipped（35.04 秒）**，包含新增两项取消 gate 与原有会话/失败恢复回归。
- 独立 worker 复核四项：**4 passed**，原确定性交错已关闭，未确认新增阻塞。
- `git diff acef78b HEAD --check` 退出 0。
- 本轮仅运行本机隔离临时存储/ACP fixture 子进程，未操作开发者容器、ngrok、供应商或浏览器 profile。
- 作者以新 worker 镜像实际重跑浏览器停止/续接及启动失败等两场景 **2 passed, 3 deselected**，保留原 A6 公网/前端证据；主审未重跑这一容器矩阵。

最终 worker：`sha256:e48eb63b6155b6fc14c37a2525d138e7419f7a8024f46a58d209c40d3a5bd993`。
control：`sha256:2209ce6d1a4dc5889810ff5e9c1ac7e0b3db7d33d21ad496cf17d6960d50ea87`。
gateway：`sha256:c0a35aaa9f304c47f9f5c3e73add7d9d5920041baef156e2ea674f3000c5580a`。

## 后续

仅执行 `../tasks/2026-10-29-desktop-a7-operations.md`，完成新手可复跑的初始化、启停、备份、升级、回滚及新目录恢复。真实供应商子门槛独立待补，不作为跳过可执行运维验证的理由。A7 不能静默生成新 key、清屏障、改镜像来掩盖不一致，也不能更改原检查器使生产发布变绿。
