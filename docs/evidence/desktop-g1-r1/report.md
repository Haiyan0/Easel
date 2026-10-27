# Task G1 R1 复审返工证据

日期：2026-10-27（Asia/Shanghai）

G1 基线：`8a5c16be2b80328f234ad4693b00b6795a23a12f`
上一轮审查提交：`64c64305bde1d4d5333a7e667eb8b044add806fe`
本轮仅修复 G1-R1-01/02/03；未修改 provisioner、WorkerHandle、数据库或
现有 relay/control 装配，未进入 G2/A4。

## G1-R1-01：唯一上游关闭任务

- 每个上游 `httpx.Response` 创建一个 `_ResponseCleanup`，所有 EOF、异常、
  超时、主动关闭、客户端断开和 `StreamingResponse` background 都等待同一个
  cleanup task。
- 直接消费 `response.stream.__aiter__()`，移除 `aiter_raw`；不再让 httpx
  在自然 EOF 取得首次 `aclose` 所有权。
- cleanup 用 `asyncio.shield` 等待；取消读取者/等待者只取消等待，不取消
  cleanup task。`response.aclose()` 完成后才释放槽位；取消 cleanup 本身
  不提前释放槽位。
- buffered 和 streaming 路径共用同一关闭所有权；取消路径只 `start()` cleanup，
  不在已取消任务里等待慢关闭。
- 真实 ASGI send 背压/断连与开始迭代前断连均有测试，不只直接驱动
  `body_iterator`。
- 共享/注入 `AsyncClient` 不被单请求关闭；默认 params/headers/cookies/auth
  不修改，连续两个请求复用同一 client。

## G1-R1-02：最终出站 URL

- `build_request` 合并注入 client 的默认 params 后，立即把最终 Request URL
  恢复为固定 `worker_origin + 已验签 path + canonical query`。
- 测试覆盖默认未知参数、覆盖 `after`/`nonce`、无 query 路由注入、重复/顺序、
  `%2F` 编码和非规范默认 params；捕获实际 transport URL，断言精确匹配。
- 调用后断言 client 默认 params 未变、client 未关闭，并第二次请求复用。

## G1-R1-03：验签前不读 body/不占受保护槽位

- 新增 `verify_gateway_assertion_metadata`：先验签并校验固定
  method/path/query/audience 与签名声明的 body digest，不读取 body。
- 读取有界 body 后才比较真实摘要；匹配后才原子接纳 nonce 并转发。
- 缺失、畸形、错误签名、错误 audience、过期断言在读取 body 和取得槽位前
  返回 401/403；两个无断言慢 POST 不占用槽位，合法 stop 仍可执行。
- 慢 body 完成时重新检查断言有效期，不能借慢 body 延长授权。
- Content-Length 在 `int` 转换前按十进制位数与 body 上限做有界比较：
  5000 位长度返回 413 而非 500；max+1 返回 413；重复/framing 规则保留。
- 合法断言但实际 body digest 不符返回 403，不访问 worker，不接纳 nonce。

## 红绿

实现前红测试（当前实现的真实失败）：

```text
test_streaming_eof_cancel_does_not_truncate_unique_close_or_slot
  EOF close 取消后槽位提前归零，upstream 未确认关闭

test_buffered_cancel_holds_slot_until_unique_close_finishes
  慢关闭取消后槽位提前归零

test_client_default_params_cannot_rewrite_signed_final_url[*] (4 cases)
  实际 URL 被 client 默认 params 改写

test_unauthenticated_slow_body_rejected_before_read_or_slot
  无断言慢 body 被读取并得到 408，未在 receive 前 401

test_oversized_declared_content_length_is_bounded_4xx
  5000 位 Content-Length 触发 500 gateway_internal
```

修复后：

```text
gateway auth/forward/stream/TCP 四文件：112 passed
受影响 auth/policy/worker guard/proxy/event 回归：226 passed
tests/multiuser + Desktop 拓扑静态：1203 passed, 9 skipped
真实 TCP test_gateway_tcp.py：1 passed（含慢流断连）
Ruff 新代码/测试：All checks passed
```

## 真实 ASGI / TCP 证据

- `test_streaming_natural_eof_holds_slot_until_unique_close_finishes`：
  自然 EOF 的慢 `aclose` 期间槽位保持 1，gate 放行后 stream 关闭、槽位归零。
- `test_streaming_eof_cancel_does_not_truncate_unique_close_or_slot`：
  EOF 关闭的第一读取者取消后，cleanup 继续，槽位仍为 1；background 共享
  同一任务；gate 放行后关闭一次且槽位归零。
- `test_buffered_natural_eof...` / `test_buffered_cancel...`：buffered 路径
  同样在关闭完成前保持槽位，取消不截断 cleanup。
- `test_real_asgi_send_backpressure_and_disconnect_closes_upstream`：真实
  ASGI `send` 阻塞时只读到上游第一块；disconnect 后上游关闭、槽位归零。
- `test_real_asgi_disconnect_before_iteration_closes_upstream`：生成器尚未
  开始即断连时，background 完成关闭与槽位释放。
- `test_streaming_read_exception...`、idle timeout、主动关闭和重复关闭均
  复用唯一 cleanup；无残留读取/清理任务。
- 真实 TCP 测试使用随机回环端口和两个临时 uvicorn（真实 worker guard +
  gateway）；新增慢流断连：客户端关闭后 worker stream 被关闭，第二块
  不再产出。测试 finally 停止两个线程，无 Docker 容器/网络或临时文件残留。

## 清理与边界

- 未创建 Docker 资源、未执行全局 prune、未操作开发者容器/卷/网络/HOME/
  浏览器 profile/ngrok。
- G1-R1 未修改 provisioner、WorkerHandle/WorkerLease、schema、
  WorkerRelay 或现有 control 路由；未进入 G2/A4。
- 真实 Harness、供应商、ngrok、Chromium、完整多用户聊天仍未运行。
