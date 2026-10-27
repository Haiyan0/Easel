# Task A4 装配证据

日期：2026-10-28（Asia/Shanghai）

基线：`785f556bc81c464f59de5105a02c12305b2e6265`。
本轮只实施 A4；未进入 A5/A6，未重做 G1/A3，未重建未变化的应用镜像。

## 1. 结果

A4.1–A4.5 已完成：

- 新增 `easel/control/gateway_client.py`：逐请求 Ed25519 断言、最终 URL/
  canonical query/一次性 UTF-8 body 签名、默认 header/cookie/auth 净化、
  不重试 POST、不跟随重定向、稳定错误码与 `Retry-After`。
- `WorkerRelay` 增加 direct/gateway_required 两种明确 transport；gateway
  模式绝不回退直连或发送 worker service token。
- `RuntimeManager.bound_handle` 区分 gateway 暂不可用与 worker 身份消失；
  新增受限 `restore_gateway`/provisioner v2 `restore_gateway`，只允许在同一
  worker ID/config 上重配/更换 gateway，绝不创建/替换 worker。
- 公开 events/stop/answer/question status/messages/后台 reconciler 在 gateway
  暂不可用时返回 503，不停止 worker、不撤销 broker token、不结算 Run；
  GET 可在恢复后重试一次，POST 不自动重发。已建立 SSE 遇临时 503 保持连接。
- 受管 broker 支持显式 `{"mode":"workspace-sidecar","channel":"chat"}`；
  URL 从认证后重查的 workspace UUID 派生为
  `http://easel-sidecar-<workspace_id>:8899`，旧固定 URL 仍兼容。
- 正式 `serve-control` 强制 gateway-required 时必须有签名 key/受管配置；
  新增可选的受控出口 CA 文件；新增 `admin drain`。
- 新增完整 Desktop 编排 `compose.workspace.yaml` 与
  Initialize/Start/Status/Stop-WorkspaceEasel.ps1；初始化独占创建 master key
  与 Ed25519 签名私钥，重复执行不轮换。

## 2. 红证据

### 2.1 单元红测试

- `tests/multiuser/test_gateway_client.py` 初次收集失败：
  `ModuleNotFoundError: No module named 'easel.control.gateway_client'`。
- `tests/multiuser/test_gateway_relay.py` 在实现前失败于：
  - gateway 模式仍走直连/发送 service token；
  - 缺 gateway 字段未在 HTTP 前失败；
  - GET 403/503 不恢复、不按 after 重试；
  - POST 被错误自动重发；
  - gateway 不可用被当成 worker 消失并结算。
- `tests/integration/multiuser/test_desktop_chat_full.py` 首轮真实拓扑暴露：
  1. `sidecar_not_ready`：Docker Desktop 给 sidecar 追加
     `desktop.docker.io/wsl-distro` 标签，`verify_sidecar_inspect` 用标签
     字典精确相等而拒绝合法 sidecar；改为要求本项目三个归属标签子集。
  2. worker 提交 500 `session_write_failed`：`/mnt/c` bind mount 拒绝
     `fchmod`，`_atomic_write`/`_append_locked` 把可选 chmod 当成必失败；
     改为 mkstemp 已建 0600 时容忍 `fchmod` 的 EPERM，并对目录 fsync 的
     平台不支持显式容忍；输出暂存 `_stage_output` 同步修复。
  3. broker 502 `upstream_unavailable`：受控出口只允许 443，测试 fake
     provider 首次用 8443；且 httpx 使用 certifi，不读取 `SSL_CERT_FILE`。
     改为 fake provider 监听 443，并给 `EgressGateway` 增加显式 CA 文件。
  4. gateway 同容器重启后历史请求 503 `worker_gateway_unavailable`：
     gateway 容器内 `python -m easel.gateway.server status` 失败，且恢复
     与后台 reconciler 竞争；固定为 `python -c` 调用 gateway `main`，并在
     恢复失败后短暂读取已由其他请求 CAS 完成的当前 handle。
  5. 权限问题 ID 误用 `"permission"` 作为记录 ID；公开 API 实际使用 question
     event 的 record id，答案 key 使用 `questionId`。

上述真实失败均是本轮装配暴露的实际缺陷，已修复并由真实链路复跑覆盖。

## 3. 绿证据

```text
A4 定向单元/回归（gateway client/relay/runtime/provisioner/sessions/egress/
desktop deployment/workspace broker/event stream）
362 passed, 2 skipped
```

```text
tests/multiuser + Desktop 拓扑静态
1324 passed, 10 skipped, 2 warnings in 304.44s
```

```text
G1 gateway auth/forward/stream/TCP
120 passed, 2 warnings in 2.07s
```

```text
真实 control→gateway→worker→sidecar→broker（协议 ACP 夹具 + TLS fake provider）
1 passed in 85.26s；另两次复跑 75.05s/83.88s
```

真实链路输出（最终复跑）：

```text
A4_IMAGES {"control": "sha256:02a540d7403dc523d9fab0b27e57a96ed05e79a0fd445b91cd08b0c6747a9a7a", "gateway": "sha256:c0a35aaa9f304c47f9f5c3e73add7d9d5920041baef156e2ea674f3000c5580a", "worker": "sha256:5969539ecd1918e7d767a206844500f4cc754330c713749bdecf51af9d8bc561"}
A4_HANDLES [{"gateway": "265f143686ffaf4be3e611942dce6d72d3920be5a740ec76a8769014395cf504", "worker": "5383288fff938145e9dd2faed743bc5c4c0155d07327b23b33e940a12169589e", "workspace": "79d6dfb6ea0f42c584a3a6f4ff32bd1d"}, {"gateway": "3f50c9ae00e4822541ee965d8285740264fb4c0183b0a493ddb4e79b28df4600", "worker": "7b27aa0f2c36e1e9b16e89fa257a3ad1f086e1d318dc7169cbce8e33a7e5925e", "workspace": "472b33aa161f4024a188040cc0570dc5"}]
A4_GATEWAY_RECOVERY {"gateway": "265f143686ffaf4be3e611942dce6d72d3920be5a740ec76a8769014395cf504", "same_worker": true, "tokens_after": 1, "tokens_before": 1, "worker": "5383288fff938145e9dd2faed743bc5c4c0155d07327b23b33e940a12169589e", "workspace": "79d6dfb6ea0f42c584a3a6f4ff32bd1d"}
A4_GATEWAY_REPLACEMENT {"new_gateway": "557a584d90c6fb98f630afa8971be554ba8b487607f1198fa87e7c8378a29647", "old_gateway": "265f143686ffaf4be3e611942dce6d72d3920be5a740ec76a8769014395cf504", "same_worker": true, "tokens": 1, "worker": "5383288fff938145e9dd2faed743bc5c4c0155d07327b23b33e940a12169589e", "workspace": "79d6dfb6ea0f42c584a3a6f4ff32bd1d"}
A4_PROVIDER {"cross_workspace": 404, "model": "deepseek-chat", "requests": 2, "sentinel_header_ok": true, "wrong_channel": 404}
A4_REAL_PASS
```

真实链路验证内容：

- 正式 `serve-control` / `create_app` / `build_relay_factory` / 真实 SQLite/
  Auth/Session/RuntimeManager/UnixProvisionerClient；control 容器 → gateway
  容器 → worker 容器 → sidecar 容器 → control broker → TLS fake provider。
- 两个邀请成员 A/B 独立登录；各自 workspace/会话/消息隔离；A 读 B 会话、
  Run、SSE 均 404。
- worker 内真实 ACP 子进程读取受管 `EASEL_BROKER_URL`/`EASEL_BROKER_TOKEN`，
  发起至少一次合法 `chat/chat/completions`；provider 审计显示模型
  `deepseek-chat`、服务端 sentinel Key 的 `Authorization`，worker 侧无真实
  provider Key。A token 访问 B workspace 与错误 channel 均 404。
- 权限状态/回答、stop、消息历史均经公开 API 和 gateway 链路。
- gateway 同容器重启后，公开历史请求恢复 200；worker 容器 ID/Running 不变，
  Run 仍 running，broker token 仍为 1，未重复启动 ACP 适配器（initialize/
  prompt 计数不变）；随后 stop 正常 cancelled。
- gateway 容器被停止后，受限恢复在同一 worker 上创建全新 gateway
  （旧 `228f...` → 新 `fb99...`），worker ID 不变、Run 仍 running、token 仍
  为 1；再次 stop 正常。证明“更换 gateway”不创建/替换 worker。
- gateway/worker 容器无发布端口；宿主不能直接访问内部管理端口。

## 4. Desktop 入口快速验证

使用独立临时项目 `easel-workspace-scripttest`（测试后已清理）：

```text
initialized project=... control_volume=... key_volume=... network=... key=created,signing_key=created
started project=... origin=https://easel.test port=17862 control=... worker=... gateway=... network=... provisioner_socket=/tmp/easel-runtime/.../provisioner.sock
network=... state=present
volume=... control state=present
volume=... keys state=present
control=running healthy
provisioner_socket=present
api_health=ok
stopped project=... drained=True network=...
```

- 重复 Initialize 返回 `key=exists,signing_key=exists`，不轮换 key。
- Stop 在 provisioner socket/control 可达时先 `admin drain`，再 compose down；
  外部卷/网络保留，未删除数据。
- 临时资源已逐项删除；未执行全局 prune。

## 5. 全量测试说明

`pytest tests`：**2650 passed, 62 failed, 86 skipped, 16 warnings**。
失败均位于未改动的 setup/bash 编码与 Starlette TestClient/httpx2 环境兼容
测试（`test_setup_*`、`test_direct_api`、`test_chat_question_replay` 等），
不属于 A4 触及路径；A4 定向、multiuser/拓扑静态、G1 与真实链路均全绿。
另有一次 multiuser 全量运行出现 4 个 `WinError 10055` socket buffer 环境
错误，单文件重跑 **4 passed**，最终全量复跑 **1323 passed, 10 skipped**。

## 6. 清理与边界

- 真实测试后容器与 `easel-a4*`、`easel-ws-*` 网络/卷均为空；provisioner
  进程退出；脚本快速验证资源全部删除。
- 未操作开发者容器/卷/网络/HOME/profile，未读取 ngrok authtoken 或供应商
  秘密，未使用 `--no-sandbox`，未修改 WSL/Docker Desktop 全局设置。
- 未运行 ngrok 公网浏览器、真实 DeepSeek Harness、真实供应商计费、完整
  双用户浏览器闭环；这些分别留 A5/A6。
- Desktop 配额仍为开发模式（`quota-mode none`）；生产 XFS/防火墙/IPv6 门槛
  不变。

## 7. 固定镜像

```text
CONTROL_IMAGE sha256:02a540d7403dc523d9fab0b27e57a96ed05e79a0fd445b91cd08b0c6747a9a7a
GATEWAY_IMAGE sha256:c0a35aaa9f304c47f9f5c3e73add7d9d5920041baef156e2ea674f3000c5580a
WORKER_IMAGE  sha256:5969539ecd1918e7d767a206844500f4cc754330c713749bdecf51af9d8bc561
```

worker 测试镜像由已验收 execution 基础镜像仅覆盖本轮修复的
`easel/worker/sessions.py`/`runs.py` 生成，避免重建未变化的 Chromium/
Playwright 层；生产镜像仍由完整 Dockerfile.worker 多阶段构建。
