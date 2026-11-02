# Task 10 R2 复审与 R3 定点返工

日期：2026-10-23。基线：ee16f0da343bbc13dda769e151616187dce8cfc2，包含 9add192 与 ee16f0d；返工父基线 07a202e4f5841856a2bdb390540615f3908079d2。

结论：暂不通过。本轮剩余四项；只做本单修复，不进入 Task 11。

依据：[R2 返工单](2026-10-23-task-10-review-round-2.md)、[Task 10 交接计划](../tasks/2026-10-22-task-10-authorized-relay.md)。主审与两名独立审查者仅只读审查、运行现有测试和临时探针，未改产品或提交，未运行 Docker、真实供应商或用户 Harness。

## 已确认的进展

- R2-01：GET/stop 失败不再释放任务、原实例幂等停止确认后才收尾，普通失败恢复测试成立，可关闭该原问题。
- R2-02：独立认证 watcher 已接真实响应，普通发送背压和慢上游清理已修；严格时间窗口还存在 R3-03。
- R2-03：原子 replace 与四时点真实进程退出已修复半行损坏；恢复材料失败分支仍见 R3-02。新实现引入整份历史读内存，见 R3-04。
- R2-04：按问题分区、非法回答不消耗 pending、label/opaque 碰撞补强已验证，可关闭。
- R2-05：并发请求唯一准备者、签发前所有权、派发过期约束已修；后台收尾缺少同一原子条件，见 R3-01。

## T10-R3-01（P1）：后台拿旧 pending 行，无条件中断后来已派发的 Run

定位：./easel/control/proxy.py 的 _reconcile_run 无 container 分支（约 795–807 行）与 _finish_system；Store.sync_run_status。

准备所有权只保护 submit。reconcile_once 先读取整批活动行，随后基于这份旧快照决定“没有实例、尚未派发”，最后 _finish_system 未带预期 dispatch/prepare 状态。该行在扫描后可能已经被合法准备并派发。

独立审查者及主审分别实跑真实 Store、RunManager、BlockingExecutor 探针，控制以下交错：reserve 后暂停 → 后台读到过期 pending/无 owner → 暂停后台收尾 → 合法 submit 取得 owner 并派发 → 后台按旧行收尾。时间快进用于模拟 reserve 后的暂停，不实际等待一分钟；dispatch_grace 保留默认 60 秒。

```text
BEFORE worker=running control=running active=1
RECONCILE checked=1 finished=1
AFTER worker=running control=interrupted active=0 revoked=1 stops=[]
NEXT checked=0
```

任务仍在执行，但账本被中断、释放额度且退出后续监视；与上轮要求保护的成功提交方相同，只是破坏方换成后台对账。

返工：未派发准备的超时收尾须在同一写事务内原子确认当前仍满足观察到的前提，包括 dispatch_state、prepare_owner/期限、实例绑定和未终态等必要字段；CAS 不匹配就放弃本次旧判断，重新对账。事务中同步完成相应 token 撤销和容量释放。不能只在 _finish_system 前再 SELECT 一次，也不能用进程内锁代替多 control 连接安全。其它基于旧活动行作破坏性收尾的分支一并检查，但不重写已关闭的执行路径。

验收：两个真实 Store 连接/两个 relay，在扫描后、收尾提交前完成 preparation/dispatch；旧扫描不得改写已派发 Run、不得撤销成功方 token。仍未准备/已过期且真正未派发的 Run 可以收尾一次。重复对账和 control 重启不重复执行业务。

## T10-R3-02（P2）：保存失败仍会删除唯一恢复输出

定位：./easel/worker/runs.py 的 _complete（约 1340–1348 行）、_preserve_failed_output、_recover_finalizing 末尾孤儿清理（约 1431–1446 行）。

两个实测分支未满足“只有一致提交后才清除恢复材料”：

1. 普通 _complete 追加助手消息失败后，直接 _discard_output，本轮完整暂存文本消失。
2. 重开恢复时追加失败，先标 failed，再将暂存文件搬到 failed_outputs；若搬移也因 I/O/权限失败，_preserve_failed_output 虽保留原文件，但随后孤儿清理认为它不再 finalizing，又将原文件删除。

独立真实 RunManager 临时探针：

```text
恢复追加失败 + 移动失败：
BEFORE True
AFTER state=failed staged=False failed_outputs=[]
普通完成追加失败：
NORMAL_FAILURE state=failed staged=False failed_outputs=[]
```

返工：统一普通完成与恢复失败的材料保留规则。移动失败时保留原位置且不被本轮或下一次启动清理；清理只能删除有明确成功提交/无需恢复证据的文件，不能以“不在 finalizing 集合”当唯一依据。failed 状态可保持，不能伪造 done；仍需保留 workspace/run 关联、秘密隔离及可审查的失败原因，不自动重发 prompt。

验收：普通助手追加失败、恢复追加失败、failed_outputs 创建/移动失败及再次重开；每种失败都至少保留一份完整可信输出，旧消息仍可读、无 done 假成功、无重复助手消息。保存成功后才可正常清理。原四种真实进程退出回归保持通过。

## T10-R3-03（P2）：认证周期从检查完成后计时，仍可能超过 5 秒

定位：./easel/control/stream.py 的 AuthGuardedStreamingResponse._watch_auth（约 58–68 行）。

watcher 每次成功检查后再 sleep 4 秒。若一次检查在开始时读到登录有效，之后耗时 0.75 秒才完成，而这段时间内登录撤销，下次检查又耗时 0.75 秒，则总窗口为约 0.75+4+0.75=5.5 秒；单次检查均未超过 0.8 秒上限。

独立审查者与主审分别使用默认参数、真实 ASGI response、阻塞 send、受控 recheck 时序实跑：

```text
REVOCATION_TO_ASGI_CLOSE 5.531 秒
checks [(4.0, True), (8.765, False)]
closed True
```

此证据为受控认证延迟，不冒称真实 SQLite 已出现 0.75 秒延迟；实际 recheck 分 inspect 和 assert_run 两次数据库调度，不能假定其执行时间为零。

返工：以单调时钟的认证截止/固定节拍安排监视，将本次认证耗时计入窗口预算，而非成功完成后重新等满 4 秒。保留单次检查上限、独立发送取消和资源清理；不要仅通过放宽测试阈值、删掉慢复查场景或取消后台 Run 解决。

验收：默认参数，检查已读取允许后发生撤销，成功检查耗时接近允许上限，下一次拒绝也有允许内延迟；真实 ASGI 背压下撤销到关闭仍 ≤5 秒。另覆盖检查超时、即时拒绝、正常长流及下游断开，无残留监视/读取任务。

## T10-R3-04（P2）：原子追加整体读取旧历史，内存随全部历史增长

定位：./easel/worker/sessions.py 的 _append_locked，特别是 path.read_bytes（约 355 行）和 existing + encoded（约 379 行）。

原子 replace 本身正确，但本轮把原先流式追加改为将整个 messages.jsonl 读为 bytes，又复制一份拼接新行。每次小追加也需要约两倍历史大小的分配，破坏长会话下的内存边界；在既定 4 GiB worker 与 10 GiB 持久空间中不能依赖历史始终很小。

主审实际临时 probe：合法 20 条约 1 MB 消息，向约 20 MB 历史追加一条很小消息；用 tracemalloc 仅记录追加阶段：

```text
HISTORY_BYTES=20002944
APPEND_PEAK_BYTES=40025189
```

这不是大输入单条超限：每条均低于既有消息上限。问题是对已有历史的整份读取和复制。

返工：保持同目录临时文件、文件锁、fsync/replace 和身份/格式校验，但用有界流式复制/校验旧记录，不把整份历史放内存。不要退回会被半写崩溃损坏的直接 append，也不要通过私自删除旧历史或大幅限制会话总长绕过。

验收：相同小消息追加到不同大小的合法历史，新增峰值内存不应按历史总大小线性增长；内容、顺序、并发去重和原子崩溃测试仍通过。使用资源行为测试证明边界，不只 mock 某个读取方法未被调用。

## 本轮主审独立运行

- 定向 proxy/runs/lifecycle_rework/event_stream：42 passed，10.38 秒。
- 全部 tests/multiuser：891 passed、3 skipped、2 个既有依赖弃用 warning，68.85 秒。
- 指定六文件兼容回归：209 passed、6 skipped，17.38 秒。
- git diff 07a202e HEAD --check：退出 0。
- R3-01/03 主审实际重跑独立审查者探针，结果如上；R3-04 为主审实测；R3-02 为独立审查者实测并经主审逐行核对失败清理路径。

绿色已有测试没有覆盖上述交错/组合故障/资源增长边界，因此本轮暂不验收。

## 给执行 Agent 的指令

以 ee16f0da343bbc13dda769e151616187dce8cfc2 为基线，仅修复 T10-R3-01～04 及必要联动。先核对 HEAD 与工作区，不 reset 主审 plan/spec 或用户改动。Python 固定 C:/Users/hyh/anaconda3/envs/Agent/python.exe，PowerShell。

四项先补红测试再修改。先前关闭项保持关闭；不重做 Task 8，不实现 Task 11，不请求新服务器。顺序运行新增定向测试、全部 tests/multiuser、Task 10 原指定六文件回归与 git diff --check。

仅显式路径提交实现、测试与必要产品说明，不夹带审计/任务单/主审文档。交付父/新 SHA、文件、逐项红绿、数据库 CAS 条件、失败材料保留规则、实际时间窗口与内存证据，然后停止等待复审。
