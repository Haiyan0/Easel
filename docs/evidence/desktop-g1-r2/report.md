# Task G1 R2 复审返工证据

日期：2026-10-28（Asia/Shanghai）

上一轮审查提交：`174af25f8b69b52e0914b091a53d486bb4f0b885`
本轮仅修复 G1-R2-01；未修改 provisioner、WorkerHandle、数据库、
WorkerRelay 或现有 control 装配，未进入 G2/A4。

## G1-R2-01：关闭失败不再释放槽位或伪成功

`_ResponseCleanup` 现在只有三个明确终态：

| 底层 `response.aclose()` | 终态 | 槽位 | 实例 | 调用者看到 |
|---|---|---|---|---|
| 成功返回 | `CLOSED` | 释放 | 正常 | 正常结束 |
| 抛出普通异常 | `FAILED` | 继续占用 | 隔离 | 脱敏 502 `gateway_upstream_close_failed` |
| 取消 | `CANCELLED` | 继续占用 | 隔离 | `CancelledError`；后续请求 503 |

关闭失败时：

- 唯一 cleanup task 以固定脱敏 `GatewayError` 失败，不再正常返回。
- 所有重复 `close()`、streaming EOF、background 等待者都等待同一 task，读取
  同一失败结果；底层 `aclose` 只调用一次。
- 槽位不释放；`_ConcurrencyLimiter` 被标记隔离，后续已验签请求在取槽时
  返回 503 `gateway_unavailable`，不再创建新的上游资源。
- 不依据 `response.is_closed` 判定关闭成功；httpx 在底层关闭前就可能把它
  置真。
- 取消路径只 `start()` 不等待时，done callback 会取回 task 异常，避免
  “Task exception was never retrieved”。
- 不关闭调用者注入的共享 `AsyncClient`；不重试 POST，不再次发送业务请求。

响应语义：

- buffered：关闭失败发生在响应返回前，返回固定 JSON
  `{"error":"gateway_upstream_close_failed"}`，HTTP 502，不发送上游 body。
- streaming：关闭失败从 body 迭代器抛出，不产生正常 EOF/成功完成；已发送
  的响应按传输失败结束。
- 原始 `OSError`/`httpx` 异常不链接、不记录明细，避免泄露地址、token 或
  body；测试在错误对象中检查了服务和 worker 地址标记。

## 红绿

红阶段新增 8 个反例，修复前实际结果：

```text
8 failed, 24 deselected
```

覆盖：

- streaming 自然 EOF 关闭失败被当作正常 EOF；
- streaming 读取错误/ idle 收尾时关闭失败被吞掉；
- buffered 关闭失败返回 200 而非明确 502；
- buffered 读取错误/ idle 时关闭失败未进入失败终态；
- 失败关闭的并发等待者取消后重复关闭、槽位与终态错误；
- 底层关闭取消后实例未隔离。

绿阶段：

```text
新增关闭失败反例：8 passed
G1 auth/forward/stream/TCP 四文件：120 passed, 2 warnings
受影响 auth/policy/worker guard/proxy/event/runtime/runs 回归：226 passed
tests/multiuser + Desktop 拓扑静态：1211 passed, 9 skipped, 2 warnings
Ruff gateway 与新测试：All checks passed
```

两条 warning 为已有 websockets/uvicorn 弃用信息。

## 清理与边界

- 未创建 Docker 容器/网络/卷，未执行全局 prune，未操作开发者资源、HOME、
  浏览器 profile、ngrok 或供应商。
- 真实 TCP 测试保留并随 G1 四文件运行；使用临时回环 uvicorn，finally
  停止线程，无临时文件/Docker 资源残留。
- 未实现 G2 容器生命周期/地址绑定/公钥挂载/代次持久化、A4 control/
  WorkerHandle/schema 接线，也未运行真实 Harness、Chromium 或完整多用户
  聊天。
