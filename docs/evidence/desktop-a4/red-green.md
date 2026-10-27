# A4 红绿记录

## 红

### 单元

```text
tests/multiuser/test_gateway_client.py
ModuleNotFoundError: No module named 'easel.control.gateway_client'
```

新增 relay/runtime 测试在实现前失败：gateway-required 仍走直连或发送
service token；缺 gateway 字段未在 HTTP 前失败；GET 403/503 不恢复；
POST 被错误重发；gateway 暂不可用被当成 worker 消失并结算 Run。

### 真实链路首轮

```text
worker_start_failed
sidecar_not_ready
worker submit 500: session_write_failed
broker 502: upstream_unavailable
gateway restart history 503: worker_gateway_unavailable
question status: not_found（误用 "permission" 作为记录 ID）
```

根因与修复：

- Docker Desktop 追加 sidecar 平台标签，精确标签字典比较误拒；改为归属
  标签子集校验。
- `/mnt/c` bind mount 拒绝 `fchmod`；mkstemp 已建 0600 时容忍 EPERM，目录
  fsync 平台不支持时显式容忍；`runs._stage_output` 同步修复。
- 受控出口只允许 443；fake provider 改监听 443。httpx 不读取
  `SSL_CERT_FILE`；新增显式 `--egress-ca-file`/`EgressGateway.ca_file`。
- gateway 容器内 `python -m easel.gateway.server status` 失败；固定为
  `python -c` 调用 `main`，并增加启动窗口有界等待、control 配置/激活重试
  与并发恢复轮询。
- 停止后的 gateway 在 `sanitize_gateway_inspect` 中因空 `IPAMConfig` 被误判
  为不受管；改为空地址仍保留受管身份，由恢复路径精确删除并替换 gateway。
- 权限记录 ID 使用 question event 的 `id`；答案 key 使用 `questionId`。

## 绿

```text
A4 定向单元/回归
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
真实 control→gateway→worker→sidecar→broker
1 passed in 85.26s；另两次复跑 75.05s/83.88s
A4_REAL_PASS
```

```text
Desktop 脚本临时项目快速验证
初始化/启动/status/停止 全部通过；测试资源已清理
```

全量 `pytest tests`：2650 passed、62 failed、86 skipped；失败集中在未改动的
setup/bash 编码与 Starlette TestClient/httpx2 环境兼容测试；另一次
multiuser 全量出现 `WinError 10055` 环境错误，单文件重跑 4 passed，最终
复跑 1323 passed/10 skipped。
