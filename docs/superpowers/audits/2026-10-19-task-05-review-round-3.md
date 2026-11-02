# Task 5 R2 复审：返工轮 R3

日期：2026-10-19（Asia/Shanghai）。

审计提交：e9a36b63e31d149e4283858208484042a786373e。
父提交：764fa995c57c8cd2952296e28ec6f8e5630b7d58。

结论：暂不通过。本轮仅发现一项与新租约清理实现直接相关的 P2；不进入 Task 6。

依据：[R2 审计](2026-10-19-task-05-review-round-2.md)、[Task 5 交接单](../tasks/2026-10-19-task-05-broker.md)。

## 已完成核验

- T5-R2-01 关闭：Content-Disposition 扩展/连续编码参数在完整语法解析时拒绝。主审独立探针覆盖 name/name* 两种顺序及 name*0/name*1，上游调用为 0；正常表单经真实 Starlette 解析只含一个授权 model。
- T5-R2-02 的响应头前取消与 UPDATE 零行检测分支已实现，新增用例通过。剩余问题是它们共用的新关闭流程存在竞争，并非要求重做已关闭功能。
- 之前关闭的快照原子性、显式出口策略继续保持，没有重新打开。

主审在 PowerShell、C:\Code\Easel，使用 C:\Users\hyh\anaconda3\envs\Agent\python.exe 运行：

```text
python -m pytest tests/multiuser -q
400 passed in 14.75s

python -m pytest tests/test_settings_stale_mirror.py tests/test_web_model_config_parity.py tests/test_local_write_guard.py tests/test_web_security.py tests/test_agent_harnesses.py tests/test_harness_workspace.py -q
248 passed, 6 skipped in 21.44s

python -m pytest tests/integration/multiuser/test_egress.py -q
6 skipped in 0.07s（真实出口未执行）

git diff 764fa99 HEAD --check
退出 0，无空白错误
```

未运行全仓、真实供应商、Linux/容器门槛或前端构建。探针均临时库、假 Key 和 MockTransport，没有联网或修改产品文件。

## T5-R3-01 / P2：关闭竞争取消首次清理，仍在底层关闭完成前释放租约

延续 T5-R2-02。位置：easel/control/broker.py 第 110–121 行（_LeaseGuard.release 取消心跳）、第 139–146 行（心跳 abort）、第 195–214 行（chunks finally/BrokerResponse.aclose）。

竞争顺序：

1. 续期失败，心跳设置 gone，并开始 abort 关闭 response/context/client。
2. chunks 同时因 gone 醒来，进入自己的 finally/aclose。
3. HTTPX 的 Response.aclose / AsyncClient.aclose 在真正 await 底层 stream/transport 关闭前已设置 is_closed。第二次 close 因这个标志直接返回，并不会等待首次关闭完成。
4. 读取方随后 guard.release，取消仍在执行首次关闭的心跳；该次底层关闭被取消，然后租约被删除。

结果是“逻辑关闭标志为真、底层清理未完成、额度已经释放”。这违背了本轮要求的先终止上游再回收，并可能遗留连接/传输资源。正常读取方断开/取消与心跳同时触发也会遇到同一共享清理缺口。

### 确定复现

主审与独立审查分别复跑，本地有限 gate 正常响应取消，不依赖永久阻塞或不响应取消的上游。

- 复用 tests/multiuser/test_broker_forward.py 的 broker_env、ChunkStream、aiter_body，lease_ttl=3。
- 自定义 MockTransport：返回一个可先产生 one、再等待 read_gate 的正常响应流；transport.aclose 设置 close_started，等待可由测试释放的 close_gate，再置 transport_closed=True；收到 CancelledError 则记录 transport_close_cancelled=True 并重新抛出。
- 正常 forward 后从 chunks 读取第一块，再将 renew_broker_lease 注入异常，启动下一次 anext(chunks)。
- 等 close_started，读取下一块任务的结束结果、数据库租约数与 transport 标志；随后释放 close_gate，再检查底层关闭是否完成。

实际输出：

```text
result lease_lost
leases 0
transport_closed False
transport_close_cancelled True
client_is_closed True
after gate transport_closed False
```

把有限 gate 放在 response stream.aclose 也复现同一结果：closed=False、close_cancelled=True，但 response.is_closed=True。现有测试的 aclose 立即完成，没有覆盖这种正常异步清理顺序。

### 修复与验收

- 为一次请求建立可被所有结束路径共同等待的单一清理操作；重复 aclose 需要等待同一次清理完成，不能只看布尔标志提前返回。
- 心跳丢失、chunks finally、外部 aclose/取消都不得取消唯一的底层清理工作后直接释放额度；释放在确认关闭完成之后，且不发生任务自等待/循环等待。
- 可用独立清理任务、共享完成状态及必要取消保护实现；不要求特定写法。单纯增加第二次 response/client.close、吞掉 CancelledError 或检查 is_closed 不足以修复。
- 增加有限 close gate 回归：gate 尚未放行时底层关闭未完成，不能回收该请求额度并接纳超额请求；gate 放行后关闭完成、清理恰当、额度归零。覆盖 response stream 和 transport 两个异步关闭点，至少一个测试从 chunks 已在等待时触发租约丢失。
- 同时覆盖关闭期间读取方被取消/主动 aclose，不遗留关闭任务或 anext 任务；保留响应头前失败、零行续期、正常提前关闭和异常回收既有测试。

## R3 交付边界

仅修复 T5-R3-01 及必要回归，不重做 multipart/快照/出口，不进入 Task 6。先补红测试再最小修复；运行本报告两组指定回归、diff 检查，真实门槛未执行如实记录。显式路径、中文信息提交，不夹带审计/任务单，报告 SHA、红绿与实际结果后停止等待复审。
