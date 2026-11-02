# Task 1 复审：第 3 轮

日期：2026-10-19（Asia/Shanghai）

被审修复：e94fda4..66beba56120938196923dce5127d2b927db80369。代码变化仅 store.py 与 test_store.py。

结论：**仍需一处 P2 修复：只读 URI 的百分号编码。保留已经通过的 R1/R2 修复，不进入 Task 2。**

前次依据：[R2 审计](2026-10-16-task-01-review-round-2.md)、[规格](../specs/2026-10-16-multiuser-workspaces-design.md)。

## 当前工作区与验证范围

实际 HEAD 为 9144d1e，并存在大量其他已暂存变更。本轮审查限定提交 66beba5 的修复；已确认工作区 store.py 和 test_store.py 与该提交一致。审计不操作暂存区，不修改产品代码，不审核或夹带其他开发工作。

使用 AGENTS.md / CLAUDE.md 指定的 Python 3.10 环境运行：

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser/test_store.py -q
# 58 passed in 2.28s

& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/test_agent_harnesses.py tests/test_harness_workspace.py -q
# 99 passed, 6 skipped in 15.18s

git diff e94fda4 66beba5 --check
# 无输出，退出 0
```

99/6 是当前工作区回归结果，不能冒充报告提交时的 97/6；当前暂存的其他开发工作不属于本审计。未复跑报告中的全量排除命令，不确认其全仓基线归因。

## R2 条目复核

- R2-01 普通路径：已解决。主审使用子进程创建带未 checkpoint WAL 的未来版本库，Store.open 返回 unsupported_schema，主库与 WAL 字节保持不变。mode=ro 确实读取 WAL 中的版本，未使用 immutable。
- R2-02：已解决。独立审查者用真实持续读锁在 WAL 转换入口制造竞争，预算耗尽返回 wal_unavailable/503，失败连接不可再用，释放锁后能重新打开为 WAL；非锁 disk I/O error 只尝试一次、原样抛出，connect 失败关闭连接。
- 只读预检新增了下述路径编码问题，既影响正常重开，也会使 R2-01 的保证在合法文件名下失效。

## R3-01 / P2：只读 URI 漏转义百分号，预检可能打开另一数据库

位置：easel/control/store.py 第 625–628 行（目标提交与当前工作区一致）。

_read_only_uri 只替换问号和井号，未编码百分号。SQLite 会把文件名中的 %20 当 URI 转义解码为空格，而普通读写连接仍按原始 Path 打开，两次连接可能指向不同文件。

主审与独立审查者分别在临时目录复现：

1. 用 Store.open 创建 control%20db.sqlite3，关闭后再次打开同一合法路径。如果对应的空格文件不存在，第二次打开失败。
2. 同目录先创建当前版本的 control db.sqlite3，再用子进程为 control%20db.sqlite3 创建未来版本、未 checkpoint 的 WAL。只读预检实际读取空格文件；随后读写连接打开真正的未来库，拒绝时又执行 checkpoint。

```text
R2_01_NORMAL_REJECT unsupported_schema 500
R2_01_NORMAL_MAIN_AND_WAL_UNCHANGED True True

PERCENT_REOPEN_ERROR OperationalError unable to open database file

PERCENT_FUTURE_REJECT DomainError unsupported_schema
PERCENT_FUTURE_MAIN_BYTES_CHANGED True WAL_EXISTS False
```

这些文件名在 Windows 和 Linux 均合法；不需要并发替换文件或任意外部修改来触发。

## 最小修复要求

- 使用完整、标准的 Path 到 file URI 转换，确保只读和读写连接读取同一文件。建议基于绝对 Path 的 as_uri() 加上 mode=ro，避免继续手写不完整的转义；保留现有相对路径兼容。
- 增加含百分号转义样式文件名（如 %20、%23、%25）的创建后重开测试，覆盖目录名及相对路径的相同问题。空格、中文、井号仍需可用。
- 增加“含 %20 的未来 WAL 库 + 空格名称的当前库”双文件回归，断言正确拒绝目标库且原始主库/WAL 均保持。
- 测试快照必须直接检查目标 Path 的字节，并用独立标准 URI 读取，不要复制生产代码的同一手工转义错误而读错同一个库。
- 保留只读预检、事务内版本复核、WAL 耗尽失败与非锁错误原样抛出；无需修改 schema 或扩大到 Task 2。

## 给执行 Agent

只处理 R3-01。先补失败测试，再做最小修复；运行 Task 1 全套与相关 ACP 回归，报告当前基线的实际计数。遵守 AGENTS.md / CLAUDE.md，显式暂存本轮目标文件，不使用 git add . 或无路径 git commit（当前暂存区包含其他工作）。不要夹带审计文件，不重写 spec，不进入 Task 2。交付新 SHA、红绿证据及结果，停止等待复审。
