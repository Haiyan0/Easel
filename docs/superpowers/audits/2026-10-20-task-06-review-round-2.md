# Task 6 R1 复审：仅需返工 T6-R2-01

- 父提交：7f1755919e2bf89bbe0265b27dc23e20c26f59eb。
- 被审提交：954c41fc2a472f77ce79bd7b490354069b6cb205。
- 结论：仍有一项 P2 阻塞；不进入 Task 7。
- 范围：只复审上一轮三项修复及相关入口，不扩大功能范围。未修改产品代码、暂存或提交。

## 上轮关闭情况

- T6-R1-01：持久化原子认领已生效，重复/重载/并发不再重复调用适配器。但流式入口在认领之前破坏既有事件记录，尚未完全满足“重复只读原状态、拒绝无执行副作用”。剩余问题收敛为下方一项。
- T6-R1-02：新执行前检查快照到期与绑定，拒绝不消耗认领；合法在途执行跨过到期点可完成。本项通过。
- T6-R1-03：脚本 worker 模式无错误回退，helper 增加双向重叠和别名检查，local 回归通过。本项通过。

## T6-R2-01 [P2] 流式入口在认领前清空事件日志，重复请求破坏原任务回放

位置：`web/app.py` 的 api_chat_stream → supervisor，约第 3416–3437 行；`_worker_execution_claim` 要到后续 `_run_harness_chat` 中才调用。

supervisor 先通过 event_path.write_text 清空已有 jobs 事件文件，再把 activity 写进去，随后才进入执行认领。重复认领虽然正确拒绝适配器调用，错误和 done 又写入同一个文件。结果是原执行的 token 历史被拒绝请求覆盖；客户端重试或恢复连接后无法再读取原任务内容。

独立真实 HTTP 探针：使用 tests/multiuser/test_worker_execution_gate.py 的签名快照 fixture、合法内部凭证和计数 fake adapter，在临时目录连续请求 `/api/chat/stream`，两次使用同一 sessionId 和 turnId。主审与独立复审分别复现：

```text
http=[200,200]
adapter_calls=1
first_events=[activity,token,done]
second_events=[activity,error,done]
original_preserved=false
```

这不是要求提前实现完整 Task 10 relay，而是当前任务“认领必须先于副作用”的流式调用方遗漏。只增加非流式测试无法覆盖。

### 修复边界

将 worker 的校验/原子认领接到流式真实入口，在创建、截断或追加运行事件之前完成；失败认领不能覆盖既有 run/turn 结果。成功认领只发生一次，避免外层认领后内层再次认领导致首个合法请求也被拒绝。保留其他调用方的门禁和 local 行为。

重复请求可以返回明确冲突，或读取已有状态/事件；不得清空、追加拒绝事件到原任务日志，也不得重新调用适配器。无需新增完整作业服务或 SSE 协议。过期拒绝同样不能先创建本轮持久事件文件。会话同步所需的锁文件不是本项要求清除的对象。

### 必须补充的红绿证据

1. 真实 `/api/chat/stream` 首次成功，记录原日志字节；同 session/turn 重复后适配器计数仍为 1，原日志字节完全不变，原 token 仍可读取。
2. 首次执行尚在途时并发重复，只有一次执行；拒绝方不截断或混入原事件日志，首个执行能正常结束。
3. 启动时有效、首次流式执行前过期：适配器调用 0，接纳仍 accepted，不创建/改写本轮事件文件。
4. 合法首次流式与非流式请求均可执行，证明没有重复认领；已有重载、独立连接争用与到期回归保持通过。

## 独立验证

使用 C:\Users\hyh\anaconda3\envs\Agent\python.exe：

- tests/multiuser：482 passed，27.73 秒。
- 七文件回归加 tests/test_output_paths.py：264 passed、20 skipped，19.85 秒。
- git diff 7f17559 HEAD --check：退出 0。
- 上述真实 HTTP 重复流式请求探针复现日志覆盖，所有写入仅在临时目录。

未独立复跑全仓与其 42 个基线失败对照，不将交付报告的这一部分标为本轮独立验证。真实 DSH/供应商、Linux/容器、浏览器与前端未执行。

## 可复制给执行 Agent 的指令

基于 954c41fc2a472f77ce79bd7b490354069b6cb205，仅修复 T6-R2-01：worker 流式请求必须在事件日志持久副作用之前完成有效性校验和唯一原子认领，重复/过期拒绝不得改写原日志。先补真实流式入口红测试，再做最小修改，避免双重认领；不实现完整 relay，不进入 Task 7，不重做已关闭的路径/签名修复。

运行 tests/multiuser，以及 test_harness_workspace、test_agent_harnesses、test_runtime_env、test_local_write_guard、test_web_security、test_web_model_config_parity、test_settings_stale_mirror、test_output_paths 回归；检查父提交到新提交的 git diff --check。显式路径提交，只包含本项必要实现和测试，不夹带审计/任务文件或其他工作。交付父/新 SHA、文件清单、四组入口证据和实际测试结果，然后停止等待复审。
