# Task G1 独立 gateway 核心证据

日期：2026-10-27（Asia/Shanghai）

G1 基线：`8a5c16be2b80328f234ad4693b00b6795a23a12f`
范围：仅独立 gateway 的签名认证、固定实例绑定、精确路由、头净化、流式清理
与真实 TCP 转发。未修改 provisioner、WorkerHandle/WorkerLease、数据库、
WorkerRelay 或现有 control 装配；未进入 G2/A4。

## 1. 协议接口

- 断言域：`easel.desktop.gateway.v1`。
- 唯一内部头：`X-Easel-Gateway-Assertion`。
- 头值：`v1.<base64url(canonical_json)>.<base64url(signature)>`；canonical
  JSON 为 `sort_keys=True,separators=(',',':'),ensure_ascii=True`。
- 固定字段：workspace_id、gateway_instance_id、boot_generation、
  worker_container_id、config_version、method、path、canonical_query、
  body_sha256、issued_at、expires_at、nonce、request_id。逐字段类型、长度、
  格式与未知/缺失/重复字段均拒绝。
- 有效期最多 30 秒，未来 iat 容忍 5 秒；nonce 使用有界（4096）线程安全
  缓存原子单次接纳，容量满失败关闭且不驱逐仍有效 nonce。
- `create_gateway_app(...)` 接收 `GatewayConfig`、只读 Ed25519 公钥、可注入
  时钟与 HTTP transport/client；`activate()` 之前所有管理请求返回 503
  `gateway_inactive`。进程启动必须显式提供新的 `boot_generation`，旧断言
  即使未过期也因 audience 不符拒绝。
- control 签名工具位于 `easel/control/gateway_auth.py`，只输出单个断言头，
  本轮不接入任何已有 control 路由。

## 2. 路由、转发与清理

- 只接受 topology routes 的七项 method/path；method/path/query 精确匹配，
  不按前缀放行。未知参数、重复参数、空值、尾斜杠、重复斜杠、绝对 URI、
  编码 slash/反斜杠/NUL、非规范整数、非法 run/session ID 均拒绝。
- question_id 只允许在单个路径段内规范解码/重编码；其他段禁止百分号编码
  别名。
- 下游目标、Host 和身份只来自 `GatewayConfig`；最终 `httpx.Request` 在
  client 默认头/cookie/auth 合并后删除 Cookie、Authorization、Forwarded、
  X-Forwarded-*、X-Easel-*、Origin、Referer、Via、X-Real-IP，再重建
  worker workspace/service token。
- 请求头 64 KiB、body 64 MiB（按实际读取累计）、连接 10 秒、I/O/流 idle
  120 秒、总并发 8、流并发 7；饱和立即 429 + Retry-After，保留槽位避免
  长流饿死 stop/权限回答。
- 3xx 不跟随、按失败返回 502；正常 4xx/5xx 与 JSON 原样保留；响应剥离
  Set-Cookie、Location 与逐跳头。
- 当前 worker `/events` 的持久事件 JSON 原样转发；SSE 逐块转发，不缓存整段，
  客户端断开/idle 超时关闭上游响应并释放槽位。

## 3. 红绿

实现前红：

- `tests/multiuser/test_gateway_auth.py`：
  `ModuleNotFoundError: No module named 'easel.gateway'`。
- `tests/multiuser/test_gateway_forward.py`：
  `ModuleNotFoundError: No module named 'easel.gateway.app'`。
- control 签名工具测试：
  `ModuleNotFoundError: No module named 'easel.control.gateway_auth'`。

实现迭代中发现的真实失败：

- 首轮认证实现 12 个测试失败，原因是 Ed25519 `verify` 参数顺序颠倒；修正后
  `45 passed`。
- 首轮转发测试 14 个失败：测试 `MockTransport` 响应已被读取导致
  `httpx.StreamConsumed`，合法查询路由未把 query 放进请求 URL，编码字面量
  `%6c` 被当成 404。分别修正测试 transport 为未消费 AsyncByteStream、测试 URL 和字面量编码拒绝后
  全绿。
- 流式测试最初使用 `httpx.ASGITransport`，其缓冲与提前 disconnect 会造成
  假性的槽位释放/200；改为直接驱动 `StreamingResponse.body_iterator` 做
  背压/取消/idle 单元测试，并用真实 uvicorn TCP 验证线上流式行为。没有降低
  实现要求。

修复后：

```text
新 gateway 四个测试文件：96 passed
受影响认证/策略/worker guard/代理/事件回归：226 passed
tests/multiuser + Desktop 拓扑静态：1187 passed, 9 skipped
```

## 4. 真实 TCP 边界

`tests/multiuser/test_gateway_tcp.py` 启动两个随机回环端口上的临时 uvicorn：

1. 独立 worker HTTP 服务：每个路由调用真实 `easel.worker.guard.verify_worker_request`；
2. gateway：`create_gateway_app`，worker_origin 固定指向该临时 worker。

验证项：

- 合法 lifecycle 转发成功，worker 实际收到固定 workspace/service token，
  且没有 Authorization/Cookie/断言头；Host 指向固定 worker origin。
- 直接绕过 gateway 访问 worker 缺少内部凭证返回 401，证明真实 guard 生效。
- 跨 workspace 断言返回 403、错误公钥签名返回 401，worker 请求计数不增加。
- stop 与 session history 路由实际转发并保留 JSON/状态。
- `/events` 的 JSON 原样返回；SSE 返回 `text/event-stream`，按块收到两段
  `data:`，Set-Cookie 被剥离，生成器正常关闭。
- 这是协议夹具，不是真实 Harness；未使用 Docker、ngrok、供应商或浏览器。

## 5. 资源清理与边界

- 真实 TCP 测试只使用随机回环端口和进程内 uvicorn 线程；测试 finally 将
  gateway/worker 的 `should_exit` 置位并 `join`，断言线程退出。无 Docker
  容器/网络、无临时文件、无全局 prune、无开发者资源操作。
- 测试密钥与 service token 均为测试内生成/夹具，不是真实凭证；真实私钥
  仍只在 control 签名侧，gateway 只接收公钥。
- 本轮未创建 gateway 容器、未接 provisioner/WorkerHandle/schema/control 路由，
  未启用完整多用户运行时。
- 未进入 G2/A4；未运行真实 DeepSeek Harness、ngrok、Chromium 或完整聊天。
