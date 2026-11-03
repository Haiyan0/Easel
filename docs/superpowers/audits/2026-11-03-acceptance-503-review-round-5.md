# 验收实例503修复R4独立复审

父基线 `ac863e2`；生产 `8dc45e2`；tip `34ede47`。实际源码 `C:\Code\Easel-acceptance`，分支 `fix/acceptance-503-r3`，交付工作树干净。

结论：**超时/连接丢失被捕获、协作取消后的恢复已验证；R4整体仍有一项P1，暂不批准合并或更新用户实例。** 仅按 [R4-R1补单](../tasks/2026-11-03-acceptance-503-rework-r4-r1.md) 收口控制进程退出窗口。

## 主审验证

- 交付所列12文件受影响契约组：255 passed，4 skipped，57.95s，exit 0。
- `tests/multiuser/test_store.py`：71 passed，exit 0，与上组文件不重叠。共326 passed、4 skipped，不宣称全量通过。
- 原 `probe_r3_ready_refresh_loss.py` 独立复跑：首次超时后DB为starting，DB/远端attempt一致，旧红态ready断言失效退出1，符合修复预期。正式正向回归覆盖随后G2恢复、停止与迟到写回。
- `git diff ac863e2..HEAD --check`无输出；实施工作树干净。
- 主审只读核验镜像ID：`sha256:0f9c337ef926f6bb3e300b7d90301552a652b1b78b84318f6a24c01f51d6cb69`。运行一个无挂载、无网络、只读、自动删除的临时摘要容器，镜像内runtime/store与宿主逐一一致：
  - runtime：`c8c301d698a39f6dda00c7d08b04d35ca902237d68af71bc2e2af360a4f6b816`
  - store：`4183d0a59520450b2ee349e75ed5d49f558f5a120cdbe6813d32f864a41eff2e`
- 未复跑生命周期容器链/前端/真实平台，沿用未变路径的历史证据，不写作主审本轮通过。

## R4-R1-01 / P1：异常处理之前进程退出，新attempt仍永久丢失

`easel/control/runtime.py:582` 正常ready刷新在新attempt尚未写DB时调用远端ensure；`:586`至`:604`只在异常/取消处理里补fence。显式restore的`:748`同样先远程调用、再依赖handler。若远端G2/pending已持久化而控制进程直接退出，handler没有执行机会，DB仍是ready/G1/旧attempt。

主审独立复跑 [probe_r4_ready_crash.py](../../evidence/acceptance-503-review/probe_r4_ready_crash.py)：真实子进程先用RuntimeManager形成正常ready，再从ensure_ready入口刷新；假远端通过真实attempt持久化写入G2，然后子进程 `os._exit(73)`，不运行控制端异常处理。父进程重开SQLite/manager恢复：

```text
1202: worker_start_failed，cause=gateway_cleanup_pending，DB ready/旧attempt
2000: worker_start_failed，cause=gateway_cleanup_pending，DB ready/旧attempt
4000: worker_start_failed，cause=gateway_cleanup_pending，DB ready/旧attempt
远端始终pending/新attempt，DB始终未保存它。
```

探针退出0，临时目录自动删除；无Docker/网络/用户材料。外层错误被直连假Adapter泛化，底层cause由探针逐次断言。此窗口仍属于R4要求的远程发出前后、DB staging前恢复，不是新业务需求。超时和CancelledError测试能运行handler，无法证明进程崩溃持久性。

需要在可能产生远程副作用之前建立可恢复的持久归属，或提供同等严格的崩溃恢复证明。不能仅继续增加异常类型、取消shield或吞掉pending冲突；突然退出时这些处理都不会运行。

## 范围和保护

仅该项返工；R3原子请求屏障、已接纳Run收尾与既有更新脚本范围不重开。没有合并、推送、更新用户实例、操作用户容器/DB/Key/Cookie/attempt，也没有真实模型/平台请求。源码通过不等于6A整体通过。
