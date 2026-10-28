# A6-R1-01 红绿记录

基线：`acef78b3ae3b1b1f318a297bf2c5cb06200aa76b`
范围：仅 A6-R1-01；不进入 A7。

## 1. 红测（修复前）

### 1.1 真实 RunManager + 取消清理 gate

命令：

```text
python -m pytest tests/multiuser/test_runs.py -q \
  -k cancel_cleanup_keeps -x
```

修复前结果：

```text
FAILED tests/multiuser/test_runs.py::test_cancel_cleanup_keeps_conversation_lock_until_holder_task_ends
Failed: DID NOT RAISE DomainError
```

测试先让第一轮在 native 绑定后挂起，用 `manager.stop()` 产生
`registry=cancelled`，executor 收到 `CancelledError` 后停在
`cleanup_gate`；此时 `stop` 未返回、holder task 未完成。修复前第二轮
`manager.submit` 被接纳，所以预期 `conversation_busy` 的断言失败。

### 1.2 真实 ACP 子进程 + 文件 gate 慢取消

命令：

```text
python -m pytest tests/multiuser/test_runs.py -q \
  -k real_acp_gated_cancel -x
```

修复前结果：

```text
FAILED tests/multiuser/test_runs.py::test_real_acp_gated_cancel_blocks_new_prompt_until_cleanup_closes
Failed: DID NOT RAISE DomainError
```

测试使用现有 `tests/fixtures/harness_rpc.py` 的 ACP 子进程与
`gated-cancel` 模式：fixture 收到 `session/cancel` 后写
`fixture/cancel-waiting` 并等待测试创建 gate 文件；此刻旧 ACP 子进程
仍活着、连接清理未完成，但修复前的第二轮仍被 worker 接纳。

## 2. 修复

`easel/worker/runs.py` 的 `_prepare` 只在本地 holder task 不存在或已
`done()` 时修复陈旧锁；registry 的 `cancelled/failed/done` 不再单独
解除活会话锁。holder task 未结束时返回 `conversation_busy`（409），
不执行 adapter、不追加用户/助手消息、不改绑 native ID。

## 3. 绿测（修复后）

```text
$ python -m pytest tests/multiuser/test_runs.py -q \
    -k "cancel_cleanup_keeps or real_acp_gated_cancel"
..                                                                       [100%]
2 passed, 27 deselected in 8.09s
```

物理断言（真实 ACP 子进程）：

```text
FIRST_STATE=cancelled
FIRST_STOP_PENDING=True
HOLDER_TASK_DONE=False
cancel-waiting=True  cancel-completed=False
OLD_ACP_PROCESS_RUNNING=True
SECOND_SUBMIT=conversation_busy(409)
AUDIT_METHODS_UNCHANGED_AFTER_SECOND=True
MESSAGES_AFTER_REJECT=[user:第一轮]
BINDING_AFTER_REJECT=native-session
GATE_RELEASE -> OLD_ACP_PROCESS_RETURNCODE_SET=True
POOL_AFTER_RELEASE=empty
NEW_INITIALIZE=2  NEW_PROMPT=2
RESUMED_BINDING=native-session
```

单位 gate 的对应断言：

```text
EXECUTOR_PROMPTS_AFTER_REJECT=1   # 第二轮没有进入 executor
MESSAGES_AFTER_REJECT=[user:第一轮]
BINDING_AFTER_REJECT=native-slow
```

第二轮被拒绝时 executor 计数仍为 1（仅第一轮），历史只有第一轮用户
消息；gate 放行并等待 stop 完成后，第三轮正常完成，native 绑定保持
`native-session`。没有用固定 sleep 或取消超时替代 gate：放行由测试创建
文件驱动；取消协议预算仅放大以确保 gate 交错不被 2 秒协议期限抢先。

## 4. 保留路径

```text
$ python -m pytest tests/multiuser/test_event_stream.py -q \
    -k "stale_terminal_conversation_lock or failed_startup_binding"
2 passed, 12 deselected in 0.47s
```

- 真正 holder task 已结束的陈旧锁继续自愈。
- 启动失败留下空 native 绑定且无活动 Run 时，同会话下一轮仍可恢复。
