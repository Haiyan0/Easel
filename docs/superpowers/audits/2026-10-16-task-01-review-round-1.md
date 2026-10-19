# Task 1 审计：第 1 轮

日期：2026-10-16（Asia/Shanghai）

结论：**未通过，先修复本轮问题，不进入 Task 2。**

被审提交：e0ad42175c80c64c3de5138d93a210ecb0ae23ac；基线：d3b7489。范围仅限该提交新增的五个文件，不包含原有 ACP、统计、前端或文档修改。

规格：[设计](../specs/2026-10-16-multiuser-workspaces-design.md)；[计划](../plans/2026-10-16-multiuser-workspaces.md)。

## 验证证据

使用 CLAUDE.md 指定的解释器：

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser/test_store.py -q
# 34 passed in 0.74s

& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/test_agent_harnesses.py tests/test_harness_workspace.py -q
# 97 passed, 6 skipped in 11.73s

git diff d3b7489 e0ad421 --check
# 无输出，退出 0
```

另外使用临时数据库运行边界探针，未修改产品代码和测试；独立审查者交叉检查迁移初始化。绿色测试只证明现有用例通过，不证明下面的缺失约束已经成立。未独立复跑交付报告中不完整列出的全量基线命令，不认可“所有其他失败都已证明无关”为本次审计结论。

## R1-01 / P1：缺少每会话单任务互斥

位置：easel/control/store.py 第 303–321、421–430 行；tests/multiuser/test_store.py 第 372–417 行。

reserve_run 只检查 workspace/global 容量，没有检查同一 conversation 是否已有活跃 Run。默认预算 2 下，同会话的不同新请求都成功，两个 Run 同时为 reserved。独立探针输出：

```text
SAME_CONVERSATION_SECOND_RUN_ACCEPTED reserved reserved active= 2
```

原测试把 workspace 预算改为 1，验证的是空间容量；另一个测试甚至要求同一会话同时接纳两个任务，和既定单 prompt 规则相反。

修复要求：在同一 BEGIN IMMEDIATE 事务中，幂等识别之后、创建新 Run 之前检查会话活跃占用。不同请求争用同会话返回 409；同请求合法重试仍返回原 Run。空间容量测试使用不同会话，默认预算保持 2。增加顺序和两个独立连接并发用例，证明同会话只有一个被接纳，而不同会话可以占满两个名额。

## R1-02 / P1：workspace 非 ready 时无法收尾，额度被占住

位置：easel/control/store.py 第 366–399、447–452 行。

allow_disabled 只跳过用户禁用判断，仍要求 workspace.state 为 ready。任务接纳后空间变成 failed 或 stopped，finish_run 和 mark_interrupted 都被拒绝，Run 无法进入终态且继续计入全局额度。

独立探针先接纳任务，再通过另一个合法数据库状态变更将空间设为 failed：

```text
FAILED_WORKSPACE_CLEANUP finish_run REJECTED unauthorized 403
FAILED_WORKSPACE_CLEANUP mark_interrupted REJECTED unauthorized 403
ACTIVE_AFTER_FAILED_WORKSPACE_CLEANUP 2
```

修复要求：将“确认用户/空间归属”与“允许新任务接纳的状态”分开。内部收尾在 failed/stopped 和用户禁用时仍可完成，但不能放松外空间拒绝或重新开放用户普通请求。为不同非 ready 状态、禁用组合、重复完成和外空间收尾增加测试，确认容量只释放一次。

## R1-03 / P2：拒绝新版数据库前已经修改文件

位置：easel/control/store.py 第 194–199、215–220 行。

connect 先执行 PRAGMA journal_mode=WAL，再读取 user_version 并拒绝未来版本。对 journal_mode=DELETE、user_version=999 的库调用 Store.open，虽然抛出 unsupported_schema，但数据库字节改变、journal_mode 变为 wal。原测试只检查表和 user_version，没有验证无写入承诺。

```text
FUTURE_SCHEMA_REJECTED unsupported_schema
FUTURE_SCHEMA_BYTES_CHANGED True
FUTURE_SCHEMA_JOURNAL_MODE wal
```

修复要求：拒绝未知版本不得先变更 journal mode 或执行迁移写入。回归需断言拒绝前后的数据库字节、journal mode、版本和哨兵数据保持不变；异常路径释放连接。不能仅保留“没有删表”的测试。

## R1-04 / P2：首次初始化存在两个并发窗口

位置：easel/control/store.py 第 196、217–231 行。

窗口一：冷库同时设置 WAL，会抛出 database is locked。独立审查者用 50 个新数据库、每库 4 个线程同步打开，200 次打开中 45 次失败，异常均来自第 196 行。设置 busy_timeout 并未自动消除该问题。

窗口二：user_version 在写事务外读取。预建 WAL、version=0 的库，用两个独立连接在进入原始迁移事务前同步，两个连接都读到 0；后拿锁者不重读版本，重复执行 migration 1：

```text
COLD_DB_CONCURRENT_OPEN ['OK', 'OperationalError: table users already exists']
COLD_DB_VERSION_AFTER 1
```

修复要求：处理 WAL 初始化的真实竞争；取得迁移写锁后重新读取并检查 schema 版本。增加空文件/不存在文件首次并发打开、预置 WAL 的并发迁移测试。不要仅用 CREATE TABLE IF NOT EXISTS 隐藏迁移错误；版本记录和整个 migration 必须原子一致。

## R1-05 / P2：禁用竞争测试对合法顺序误判

位置：tests/multiuser/test_store.py 第 579–605 行。

两个并发操作都执行完后，用户一定已被禁用。即使 create_conversation 先成功，disable_user 仍已经执行且返回 True；第 605 行再次禁用时却断言结果等于 bool(conversations)，从而在该合法顺序下期望 True。

保留原 Barrier 并用 Event 确定性安排“先创建成功，再禁用完成”，原测试稳定在第 605 行失败。现有 34/34 通过取决于调度顺序。

修复要求：显式测试两种顺序；两种顺序结束后重复 disable_user 都应为 False，后续新请求都拒绝。不能通过删除竞争测试、固定为一种顺序或增加 sleep 掩盖问题。

## 范围裁定与后续迁移

Ruling：未实现的后续业务 schema 可以按所属任务增量补齐，不作为本轮运行时缺陷加项，但执行报告必须更正“spec 字段全部保留”的说法。当前缺 Invite、Login、ManagedRevision、WorkspaceOverride、Audit、Conversation.persona、Run.heartbeat_at。分别由 Task 2、4、9、10 在接入前创建并测试，审计时检查：

- Task 2：Invite/Login。
- Task 4：ManagedRevision/WorkspaceOverride。
- Task 9：Conversation.persona。
- Task 10：Audit/Run.heartbeat_at；Task 8 如提前依赖 run 心跳，则提前完成该项迁移。

代价：后续必须显式跟踪这些迁移，不能据当前交付报告假定表/字段存在。已创建的 version=1 数据库必须可升级；需要改 schema 时增加迁移，不回写已经存在的 migration 1。

Store.open 是 contextmanager 工厂、Store.connect 才返回实例，是报告已说明的契约偏差；后续 app lifespan 必须正确持有并关闭 Store，不能把未进入的 contextmanager 当 Store 使用。本轮不要求仅为函数名调整重写实现。

## 给执行 Agent 的返工要求

本轮为 Task 1 修复轮 R1，完成 R1-01 至 R1-05 后停止。先分别写能复现问题的失败测试，再实现修复并运行 Task 1 与上述 ACP 回归。修复前后的测试输出应在报告中对应每个编号；新增覆盖不能靠降低默认容量或改成相反业务语义来通过。

保留 CLAUDE.md、spec/plan 和既有 dirty 修改。只修改 Task 1 必要文件，不做 Task 2、不重写 spec、不擅自迁移真实数据。审计者新建的本报告也不要夹带进代码提交。若 schema 需要变化，增加增量迁移并验证 e0ad421 创建的库可升级。

提交使用中文信息，显式暂存目标文件；交付新 SHA、diff 摘要、每编号修复说明、完整测试命令/结果、未执行检查及未解决项，等待复审。
