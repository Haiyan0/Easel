# Task G1 主审 R1

基线：`8a5c16be2b80328f234ad4693b00b6795a23a12f`。
审查提交：`64c64305bde1d4d5333a7e667eb8b044add806fe`。

结论：G1 本轮不通过，以下三项定向返工后复审，不进入 G2/A4。保留 Ed25519 协议、nonce 原子接纳、路由与实际 TCP 验证的已完成成果。

## G1-R1-01（P1）：取消截断首次上游关闭，仍提前释放槽位

位置：easel/gateway/app.py `_streaming_response`，并检查 `_buffered_response` 的相同关闭所有权问题。

当前 response.aiter_raw 在自然 EOF 时由 httpx 自动发起 aclose，先置 is_closed。若首次实际 stream.aclose 正在 await，读取任务被取消，首次关闭被截断；生成器 finally 和 BackgroundTask 再调用 response.aclose 会因 is_closed 提前返回，但 slot 已释放。上游仍未真正关闭，重复关闭兜底不成立。多个 close 调用也没有唯一且可等待的完成状态。

主审用现有 G1 测试助手、真实 httpx.Response 和有限关闭 gate 复现，首块后自然 EOF，等待首次关闭开始，再取消读取：

```text
EOF_CLOSE_CANCELLED True
UPSTREAM_CLOSED False
SLOT_AFTER_CANCEL 0
AFTER_BACKGROUND_CLOSED False
```

要求：每请求唯一清理任务；迭代/EOF/异常/超时/主动关闭/断连和 background 兜底共享同一完成状态。取消等待者不能取消底层唯一清理；底层关闭完成前不释放槽位。避免 aiter_raw 自行取得不可控的首次 EOF 关闭所有权，可参考既有 broker 的直接消费 raw stream + 唯一清理模式，但不复制无关租约逻辑。不以 response.is_closed 等布尔值代表底层已关闭。

验收必须覆盖：

- 自然 EOF + 慢关闭 gate；gate 前上游未关闭、槽仍占用，放行后归零。
- 同一 EOF 关闭期间取消读取者，再执行主动/后台关闭；关闭不被截断，所有等待者等待同一清理完成。
- 正常断连、idle 超时、读取异常、开始迭代前断连、等待清理者取消、重复关闭；无残留读取/清理任务。
- 真实 ASGI send 背压/断连测试补至少一项，不仅直接驱动 body_iterator。
- 共享/注入 HTTP client 不被单请求误关闭，原有头净化和连接复用保留。

## G1-R1-02（P2）：HTTP client 默认 params 改写已验签查询

位置：easel/gateway/app.py `_send_upstream` 的 client.build_request 及 send 前边界。

build_request 合并注入 AsyncClient.params，代码虽在最终请求清洗 headers，却未复核最终 URL。签名绑定 after=1，上游可实际收到 after=99&unknown=1；签名语义和查询白名单在出站被破坏。

主审与独立审查分别复现：

```text
SIGNED_QUERY after=1
ACTUAL_URL http://worker.internal:7860/internal/runs/dddddddddddddddddddddddddddddddd/events?after=99&unknown=1
STATUS 200
```

要求：在 client 默认值合并之后，确保最终 Request URL 精确等于固定 origin + 已验证 path + canonical query；不能让默认 params 添加、覆盖、重排成另一套语义。保留 final headers 清洗与 auth override，不修改共享 client 的 params/headers/cookies/auth。

测试：默认未知参数、覆盖 after/nonce、给无 query 路由注入参数、重复编码/顺序、正常合法查询；捕获实际 transport 请求完整 URL，并断言共享 client 调用后不变。不能只测内部 canonical_query 函数。

## G1-R1-03（P2）：未认证慢 body 占满槽位，阻断合法管理请求

位置：easel/gateway/app.py `_dispatch` 在验签前 acquire + _read_body。

即使请求没有断言，gateway 也先占槽、读取最多 64 MiB/等待 120 秒，之后才拒绝。流预留策略无法阻止普通未认证请求耗尽全部槽位。

主审以 max requests=2、streams=1，两个无断言 POST 的 ASGI receive 挂 gate，随后提交合法签名 stop：

```text
UNAUTH_PENDING 2 ACTIVE_SLOTS 2
SIGNED_STOP_STATUS 429
```

要求：缺失/畸形/错误签名/错误 audience/过期断言在读取 body 和取得受保护上游槽位前快速拒绝。采用分阶段校验：先验签并校验固定请求元数据/声明 body digest，读取有界 body 后比较真实 digest，再执行不可重放接纳/转发；实现者固定原子顺序，不能因此把真实 body 校验或 nonce 门禁移除。并发请求须继续有界，合法 control 请求仍受到大小/时间/额度限制。

同一资源准入检查还需修正超长 Content-Length 的解析：主审输入 5000 位十进制长度（总 header 在 64 KiB 内）得到 500 gateway_internal。应在 int 转换前做有界长度/数值比较，返回明确 400/413，不触发内部错误。重复/framing 歧义规则保留。

测试：上述未认证慢 body 在 receive 被读取前立即 401/403，槽位不增长且合法 stop 可执行；错误 audience/过期/错误签名均相同。合法断言+实际 body 不符不访问 worker，重放与并发同 nonce 仍仅一次成功。超长/边界 Content-Length、缺声明累计超限继续明确 4xx。测试失败不能靠缩短所有合法请求超时规避。

## 主审本轮验证

- G1 auth/forward/stream/TCP 四文件：**96 passed, 2 warnings**（1.75 秒，包含真实 TCP 1 项）。警告为已有 websockets/Uvicorn 弃用信息。
- auth_api/policy/worker_guard/proxy/event_stream + 文档：**192 passed**（14.24 秒）。
- `git diff 8a5c16b 64c6430 --check`：退出 0。
- 上述四段反例均由主审实际执行，临时 asyncio/httpx/ASGI 对象已关闭，无 Docker/ngrok/供应商操作；独立审查另复现查询改写并确认认证代码范围无其它已确认问题。

已有测试全绿不能替代新反例。原真实 TCP 成果保留；本轮不要求大镜像或浏览器场景。

## 交付要求

仅修 G1-R1-01/02/03，先补红测试再实现。必要修改范围为 gateway/auth、control 签名工具（确有接口依赖时）、对应 G1 测试/契约/证据；不改 provisioner、数据库或现有 relay 装配。保护主审文档。

交付完整父/新 SHA、精确红绿与最终实际 TCP/ASGI 取消证据、关闭任务与槽位终态、client 默认 params 不变证据、剩余边界。提交后停止，不进入 G2/A4。
