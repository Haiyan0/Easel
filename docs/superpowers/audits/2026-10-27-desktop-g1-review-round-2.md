# Task G1 R1 复审：关闭失败终态定点收口

父提交：`64c64305bde1d4d5333a7e667eb8b044add806fe`。
审查提交：`174af25f8b69b52e0914b091a53d486bb4f0b885`。

## 裁决

G1-R1-02（最终 URL）和 G1-R1-03（预验签/有界长度解析）关闭。G1-R1-01 的唯一清理/EOF/取消路径主要修复认可，但普通关闭异常仍错误地当作关闭成功，需要仅修以下一项，不进入 G2/A4。

## G1-R2-01（P2）：aclose 异常后仍开放额度并正常结束响应

位置：easel/gateway/app.py `_ResponseCleanup._run` 约 165–179 行。

代码捕获 response.aclose 的普通 Exception 后只记录日志，随后 finally 仍释放 slot，共享 cleanup task 则正常返回。若底层 stream.aclose 抛 OSError 且实际尚未关闭，上游资源仍占用，但额度已开放，可重复创建新上游资源。httpx 在 await 底层关闭前已设置 is_closed，所以共享 task/response 的再次 close 也不能补回。

主审真实 httpx.Response + AsyncByteStream 关闭失败探针输出：

```text
UPSTREAM_CLOSE_CALLS 1
UPSTREAM_CLOSED False
ACTIVE_SLOTS 0
AFTER_BACKGROUND_CLOSE_CALLS 1
STREAM_ENDED_NORMALLY True
```

独立复核确认相同失败所有权问题，并核对 httpcore 关闭过程：异常不能被视为物理连接已释放。探针未操作 Docker/账号/文件。

## 最小修复要求

- 将关闭成功、失败、取消区分为明确终态；唯一 cleanup task 不能在底层关闭异常时正常返回或释放可复用槽位。
- 关闭失败必须失败关闭或隔离该请求资源。可保持槽位被占用直至可信 transport/client 关闭确认，或将实例置不可服务；不能仅打日志后继续无限接纳。注入的共享 client 所有权边界仍须保留，不为处理单请求错误擅自关闭调用者的共享 client。
- 所有重复关闭/后台等待者读取同一失败终态，不靠 response.is_closed 认定成功，也不在后台静默丢失异常。不得重试 POST/再次发业务请求来掩盖关闭失败。
- 缓冲响应尚未发送时返回脱敏明确错误；流式响应已发送头时按传输失败收尾，不能吞掉异常形成“正常 EOF/成功完成”。绝不泄露原异常中的 token/body/地址。
- 保留上轮慢关闭 gate：正常关闭完成前继续占槽，等待者取消不取消唯一清理；取消失败终态不提前开放容量。

## 红绿验收

1. streaming EOF 后首次 aclose 抛错且 closed=False：不归零后继续无限接纳、不正常成功 EOF；background/重复 close 观测同一失败状态。
2. buffered、idle/读取错误收尾时关闭也失败：同样失败关闭，错误不伪成功。
3. 并发等待同一次失败 close，取消某个等待者：不重复关闭、不误释放或遗留未取回的 task 异常。
4. 若实现了明确 transport 确认后的恢复释放，测 gate/关闭失败与确认顺序；否则保持隔离直到本实例关闭，不添加模糊“稍后释放”。
5. 回归 G1 四文件与指定兼容测试，真实 TCP 保留；本项不需 Docker/供应商/浏览器，不要求重做已关闭两项。

## 主审本轮验证

- G1 四文件 + 文档路径：**122 passed, 2 warnings**（2.17 秒，含真实 TCP）。两条 warning 为已有 websockets/Uvicorn 弃用信息。
- auth_api/policy/worker_guard/proxy/event_stream：**182 passed**（最终输出见工具记录）。
- `git diff 64c6430 174af25 --check`：退出 0。
- 新关闭失败反例由主审实际执行，独立审查确认；原慢关闭/取消、params 与预验签已有绿测试保留。

## 给代码 Agent

仅修 G1-R2-01，先补失败终态反例再实现。必要路径限 gateway 清理与其测试、契约/证据；保护主审文档，不改 provisioner/数据库/relay。提交完整父/新 SHA、红绿、关闭失败隔离与成功释放的终态证据，停止等待复审，不进入 G2/A4。
