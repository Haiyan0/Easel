# Task 1 复审：第 2 轮

日期：2026-10-16（Asia/Shanghai）

被审范围：e0ad421..e94fda4，包含 b7fbc78 和 e94fda4；仅两个 Task 1 文件变化。

结论：**尚未通过。保留已完成修复，处理下述两个 P2 后复审；不进入 Task 2。**

上一轮：[R1 审计报告](2026-10-16-task-01-review-round-1.md)。正式依据：[spec](../specs/2026-10-16-multiuser-workspaces-design.md)、[plan](../plans/2026-10-16-multiuser-workspaces.md)。

## 已独立复核的结果

| 上轮编号 | 结论 | 依据 |
|---|---|---|
| R1-01 | 已解决 | 默认预算下第二个同会话新请求为 409 conversation_busy；同请求返回原 Run 和原配置版本；不同会话容量保留 |
| R1-02 | 已解决 | failed/stopped 下完成和异常收尾可执行；容量释放；新接纳仍拒绝，外空间拒绝未放松 |
| R1-03 | DELETE 模式用例已解决，WAL 恢复场景仍未解决 | 普通未来库拒绝后字节不变；带未 checkpoint WAL 的未来库仍被改写，见 R2-01 |
| R1-04 | 原两个竞争窗口已解决；另有新引入的静默降级 | 版本在写锁内复查；独立审查者在打开前同步的 50 库×4连接共200次均成功且为 WAL；但持续启用失败后仍返回 Store，见 R2-02 |
| R1-05 | 已解决 | 两种确定性顺序和并发不变量均修正，不再将合法先创建顺序判为错误 |

实际执行：

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser/test_store.py -q
# 53 passed in 1.97s

& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/test_agent_harnesses.py tests/test_harness_workspace.py -q
# 97 passed, 6 skipped in 11.85s

git diff e0ad421 e94fda4 --check
# 无输出，退出 0
```

额外将 e0ad421 的真实 store.py 在隔离模块中载入，用旧实现创建临时数据库，再用当前 Store 打开，原会话内容保留。此结论来自真实旧实现生成的库，不是“用当前实现建库再用当前实现重开”的替代测试。

未独立复跑交付报告的全量排除命令，不据此宣称整个仓库所有既有失败均与变更无关。

## R2-01 / P2：未来版本 WAL 数据库的拒绝路径会 checkpoint 主库

位置：easel/control/store.py 第 209–218、232–242 行。

connect 使用普通读写连接打开库，在发现未来版本后关闭连接。对于尚未 checkpoint 的 WAL，SQLite 的读写连接关闭可以触发 checkpoint。因此“没有执行写 SQL”不等于“拒绝路径不改写主库”。

真实复现使用子进程创建 WAL 数据库，写入 future_only 哨兵和 user_version=999，commit 后 os._exit(0)，保留一次正常崩溃恢复会遇到的未 checkpoint 日志。主进程调用 Store.open：

```text
BEFORE_REJECT_MAIN_BYTES 4096 WAL_EXISTS True SHM_EXISTS True
FUTURE_WAL_REJECTED unsupported_schema 500
AFTER_REJECT_MAIN_BYTES 8192 MAIN_UNCHANGED False WAL_EXISTS False SHM_EXISTS False
```

修复要求：

- 对已存在的库使用不会因拒绝触发主库 checkpoint 的版本预检方案；只读版本预读是一条已验证可行的方向。
- 必须正确读取 WAL 中的 user_version；不能用忽略 WAL 的 immutable 读取绕过未来版本检查。
- 仍保留取得迁移写锁后的版本复核，不能为修复拒绝路径恢复锁外迁移判断。
- 增加真实子进程保留 WAL 的测试，断言 unsupported_schema、主库字节不变、待恢复 WAL 保留、版本及哨兵内容保持；后续验证读取不要先以读写连接触发 checkpoint 再快照。
- SQLite 只读 WAL 访问可能更新 SHM 的读者槽；不把这种读者 bookkeeping 的字节变化误判为业务写入。本条要求不 checkpoint 主库、不删掉待恢复 WAL，不要求 SHM 每个字节都固定。

## R2-02 / P2：WAL 重试耗尽后静默返回可用 Store

位置：easel/control/store.py 第 275–291 行。

_convert_to_wal 把所有 sqlite3.OperationalError 变成 False，_enable_wal 20 次失败后直接结束，connect 因而仍返回 Store。正式约束要求启用 WAL，报告中“仅属部署属性”的解释不是已经获准的范围变更；目前也没有实际部署检查来阻止该状态上线。

主审复现：预置当前 schema 的 DELETE 库，只在 _enable_wal 入口安排另一条真实连接持读锁，然后执行原 _enable_wal；测试中将重试预算缩短为两次、等待间隔为零，保持同一“预算耗尽”分支。结果：

```text
WAL_FAILURE_OPEN_RETURNS_SUCCESS delete
```

调度覆写只用于安排锁的时间，不替换 WAL SQL 或其结果。返回时仍为 DELETE，调用方未收到任何失败信息。

修复要求：

- 短暂锁冲突可以有限重试；耗尽必须明确失败，不能返回已就绪 Store。
- 只重试可恢复的锁竞争。非锁类 OperationalError 不能被吞掉并伪装成转换成功或普通竞争；实现须兼容项目 Python 3.10。
- 异常失败关闭连接，不能遗留事务、锁或句柄；不要由此回退到 local 模式。
- 增加持续真实锁跨越整个重试窗口的用例，证明打开失败且锁释放后能再次成功打开为 WAL。
- 增加非锁类 OperationalError 不被重试/吞掉的测试，并保留短暂锁恢复成功的原用例。

## 非阻塞的覆盖建议

tests/multiuser/test_store.py 的 open_concurrently 把 Barrier 放在 Store.open 之后，未确保“首次打开”这一步同步竞争。此次主审用独立压力探针确认原窗口修复；建议在本轮相同测试文件中把首次打开测试的同步点移到打开之前，并收集全部线程异常。

e0ad421 真实旧库已由主审临时验证可用。后续 Task 2 增加 schema 时，升级测试应使用冻结的 v1 schema/fixture 建库后再升级，而不是仅使用当前 Store 建库并更新当前指纹常量。该建议不要求本轮额外改变 schema。

## 本轮返工指令

仅处理 R2-01、R2-02，保留 R1-01/02/05 和原冷启动竞争修复。先给出对应红测试，再修实现并验证 Task 1 全套及 ACP 回归；不扩大到 Task 2，不改变 spec/plan 来接受静默降级，不修改或夹带审计报告与既有 dirty 文件。

交付新提交 SHA、两项分别的失败复现/修复说明/最终命令结果，指出未执行检查。使用 CLAUDE.md 指定解释器和中文提交信息，完成后停止等待复审。
