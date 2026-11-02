# Task 5 R4 复审：关闭等待者取消仍提前停止续期

日期：2026-10-19（Asia/Shanghai）。

审计提交：3bcab98a2e28242d5469eebcd5029c5cb7cf93a9。
父提交：06b989e7f13340703a2cee13ce1446df5547d65f。

结论：暂不通过，仅一项 P2。EOF 首次关闭已修复；本轮只处理 R4 新增 close finally 的取消分支，不重做其他已关闭功能，不进入 Task 6。

依据：[R4 审计](2026-10-19-task-05-review-round-4.md)中“关闭期间取消、有效容量保持”的要求。

## 已验证

直接消费 response.stream 已消除 HTTPX aiter_raw 在 EOF 自行关闭的问题；共享 cleanup 会等待底层关闭，正常未取消的慢关闭期间心跳继续。现有两个 EOF gate 回归及既有关闭回归通过。

主审在 PowerShell、C:\Code\Easel，使用 C:\Users\hyh\anaconda3\envs\Agent\python.exe 实测：

```text
python -m pytest tests/multiuser -q
405 passed in 19.86s

python -m pytest tests/test_settings_stale_mirror.py tests/test_web_model_config_parity.py tests/test_local_write_guard.py tests/test_web_security.py tests/test_agent_harnesses.py tests/test_harness_workspace.py -q
248 passed, 6 skipped in 22.21s

python -m pytest tests/integration/multiuser/test_egress.py -q
6 skipped in 0.07s（真实出口未执行）

git diff 06b989e HEAD --check
退出 0，无空白错误
```

未执行全仓、真实供应商、Linux/容器、前端门槛。探针仅临时 SQLite、假 Key 和 MockTransport，未联网或修改产品文件。

## T5-R5-01 / P2：close 的 finally 在清理完成前停止心跳

位置：easel/control/broker.py 第 149 行附近的 _LeaseGuard.close finally；相关 _join_heartbeat 中 heartbeat.cancel。

close 通过 shield 等待唯一清理，但 finally 无条件执行 _join_heartbeat。关闭等待者在 close gate 未完成时被取消，也会进入该 finally 并停止心跳。此时 shield 保留了后台清理，然而没有任何任务再续期；有效占用到期后可被其他请求回收，旧流仍未关完。

这与本轮新增的“清理完成前持续续期”目标直接冲突。新增测试把 lease_clock 固定在 1000，且先注入续期失败，不能证明没有续期异常时、关闭等待者取消后有效租约继续更新。

### 确定复现

主审及独立审查分别复现：

1. 复用 test_broker_forward.py 的 EofCloseStream 和 broker_env；TTL=3，lease_clock 可控，初始 1000。续期函数正常，不注入任何错误。
2. forward 后读取 one，再请求下一块进入自然 EOF 的唯一关闭；有限 close_gate 暂不放行。
3. 取消正在等待 EOF 关闭的 anext(chunks)。此时 cleanup 仍等待 gate，但 heartbeat 已完成/被取消。
4. lease_clock 先推进至 1002，给下一轮正常心跳一个调度机会，再推进至 1004；尝试为同一 workspace 获取两个新租约。

```text
after reader cancel:
cleanup_pending=True heartbeat_done=True closed=False

renewal opportunity elapsed:
renew_count=0 new_leases=2 old_closed=False cleanup_pending=True
```

仍未关闭的旧请求不再计入容量，可以同时占用两个新额度。释放有限 gate 后旧关闭正常完成，最终清理归零；这不是不响应取消或永久阻塞的上游假设。

### 最小修复边界

- 关闭等待者的取消不能停止仍在进行的唯一清理所需的心跳。仅在 _cleanup_task 真正完成后停止/等待心跳；清理未完成时取消等待者应传播取消，后台清理和续期仍继续。
- 当前已有 cleanup done callback，可以保留其作为结束后停止心跳的路径；不要在等待被取消、清理未结束时无条件调用会取消心跳的 join。具体代码自行选择，保持结束后无遗留任务。
- 不修改 TTL、不冻结租约时钟来规避，不重构 multipart/快照/出口或已通过的底层关闭所有权。

### 必须补的回归

- 不注入续期失败。自然 EOF → close gate → 取消关闭等待者，断言 cleanup 未完成时 heartbeat 仍有效。
- 使用 lease_clock 分步推进，小于 TTL 的时间步之间通过事件等待真实续期发生；不要一次跳过全部续期时机。跨越初始到期点后，实际只允许再获取一条租约，第三条返回 workspace_capacity 429。
- gate 放行后底层关闭完成、旧额度归零，heartbeat/cleanup/读取任务结束。再覆盖主动 aclose 的等待者被取消，证明同一原则成立。
- 保留当前 EOF gate、读取取消、response/transport gate、响应头前失败、零行续期与异常回收回归。

## R5 交付

仅修复 T5-R5-01，先红后绿。运行本报告两组指定回归与 diff 检查；真实门槛未执行继续明确记录。显式路径、中文提交，不夹带审计/任务单。报告 SHA、无续期故障条件下的取消/跨 TTL 红绿证据及实际结果后停止等待复审，不进入 Task 6。
