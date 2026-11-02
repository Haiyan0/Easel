# Task 1 验收通过

日期：2026-10-19（Asia/Shanghai）

验收提交：0190780baccdb809aa82cd52755cf433a3ec3a71；父提交 57f5dda。

结论：**Task 1 通过，可以进入 Task 2。** 本结论覆盖当前数据存储任务，不代表多人服务、Linux 隔离或全仓测试已经通过。

## 审计范围与关闭记录

本次限定审查 57f5dda..0190780 的两个文件，确认当前 store.py/test_store.py 与目标提交一致，没有夹带其他变更。

- R1-01：每会话单任务互斥已落实，幂等先于互斥和容量。
- R1-02：failed/stopped/禁用后的内部收尾可执行，外空间隔离保留。
- R1-03、R2-01：普通及特殊文件路径下，未来版本待恢复 WAL 库拒绝时不 checkpoint 主库、不删除 WAL。
- R1-04：迁移写锁内重读版本，原有冷启动竞争已解决。
- R1-05：禁用竞争的两种合法顺序已确定性验证。
- R2-02：WAL 启用失败明确报错，不静默降级；非锁错误不吞掉。
- R3-01：采用标准 Path.as_uri，正确处理百分号、空格、中文、井号和相对路径。独立审查者额外验证五种文件名的绝对/相对路径共十组组合，均通过。

## 独立验证

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser/test_store.py -q
# 66 passed in 2.32s

& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/test_agent_harnesses.py tests/test_harness_workspace.py -q
# 99 passed, 6 skipped in 14.60s

git diff 0190780^ 0190780 --check
# 无输出，退出 0
```

主审额外使用临时目录和真实子进程复现百分号未来 WAL 双库场景，输出：

```text
FUTURE_PERCENT_REJECT unsupported_schema 500
TARGET_MAIN_UNCHANGED True
TARGET_WAL_UNCHANGED True
SPACED_NEIGHBOR_UNCHANGED True
PERCENT_REOPEN_PASS
```

审计未修改产品代码、测试、分支或暂存区；仅新增审计/派工文档。未运行 Linux 容器、实际账号发布、浏览器隔离或全仓测试，不把未运行项视为通过。

## 保留的接口与后续前置

- Store.open 是 contextmanager 工厂；Store.connect 返回需关闭的实例。后续 app/线程池不能把未进入的 contextmanager 当 Store，也不能跨线程复用默认 SQLite 连接。
- v1 migration 不可回写。Task 2 通过新增迁移创建 Invite/Login，升级测试使用冻结旧版结构或真实旧实现创建的数据库。
- 后续 schema 责任保持上一轮裁定：Task 4 的 ManagedRevision/WorkspaceOverride，Task 9 的 persona，Task 10 的 Audit/Run.heartbeat_at；Task 8 如提前依赖 run 心跳则提前迁移。
- 原 Task 1 状态常量保持稳定；后续服务通过显式映射使用，不另造矛盾状态。

下一项：[Task 2 执行任务单](../tasks/2026-10-19-task-02-auth.md)。执行完成后提交证据，审计通过前不进入 Task 3。
