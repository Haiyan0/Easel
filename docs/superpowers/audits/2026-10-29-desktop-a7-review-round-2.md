# Task A7 R1 复审：三项恢复与升级边界未关闭

日期：2026-10-29（Asia/Shanghai）。
基线：`04b5f23e36879e72fbafa345dc2ed06abd26a5ae`。
审查提交：`e8376753d63eb681560ae868aa2d1528b2756c85`。

## 裁决

A7 暂不通过，仅定点修复以下三项，不进入 A8、不自动上线。R1-02 的源根绑定/历史空间缺目录拒绝、R1-04 的已有身份缺 key 拒绝路径通过本轮检查；保留正常备份/恢复/升级和作者真实演练证据，不重开已关闭部分。

R1-01 的全文件摘要与复制结果复核已经接入，但 prepare 前未验证输入、合法 prepared 包 verify 不只读仍有确定性反例。R1-03 已把完整备份 verify 提到 compose up 前，但还没有绑定当前这一轮 Stop 承诺。

## A7-R2-01 / P1：prepare 把已损坏的 staging DB 重新登记为合法内容

位置：`easel/control/cli.py:1310`–`:1320`；摘要改写在 `:1328`–`:1345`。

prepare 只解析 manifest，未在 Store.open 和撤销登录/清屏障前验证当前文件摘要。随后它把数据库当前大小与摘要写回清单；因此 restore 后发生的有效 SQLite 内容损坏会被合法准备转换掩盖，materialize 接受修改后的内容。

主审与独立复核均使用真实 Store、完整 v2 备份、临时目录复现：restore_backup 成功后，对 staging 的 users.display_name 做 UPDATE，manifest 保持原字节；verify_backup 此时正确拒绝，但再执行 prepare 和 materialize 就成功。

```text
TAMPER_VERIFY=backup_integrity_failed
MATERIALIZED_NAME=CORRUPTED-STAGING
```

最小修复：任何准备修改前，按原清单验证整个 staging 及数据库/key/签名材料；拒绝时数据库、清单及文件集合均不变。只有已验证的源状态才能转换并登记新摘要。增加上述有效 SQLite 行变化反例及正常 prepare 正例，不跳过 DB 哈希、不改原备份、不以重算摘要修复未知损坏。

## A7-R2-02 / P2：合法 prepared 包第一次 verify 新建 WAL/SHM，第二次失败

位置：`easel/control/cli.py:1194`，关联 `verify_backup` 的树集合校验与 prepare 后的 WAL 模式。

普通 SQLite mode=ro 连接不能保证没有辅助文件写入。合法备份 restore/prepare 后，首次 verify 在 control 下创建清单之外的 WAL/SHM；第二次 verify 因额外文件报完整性错误。手册所述只读验证和可验证 staging 流程因此不稳定。

主审及独立复核在无损坏的正常 prepared 包上复现：

```text
FIRST_VERIFY_ADDED=["control/control.sqlite3-shm", "control/control.sqlite3-wal"]
SECOND_VERIFY=backup_integrity_failed
```

最小修复：对已确认静止、完整的备份数据库使用真正不改变源树的验证方式；不得无条件忽略清单外 WAL/SHM，不得删除未知文件让校验通过。增加连续至少两次 verify、随后 materialize 的正常回归；逐次比较源文件集合与所有字节均不变。与 R2-01 一起保证准备前校验无副作用。

## A7-R2-03 / P1：升级接受另一轮 Stop 的旧备份

位置：`deploy/desktop/EaselDesktop.Common.ps1:611`–`:647`；`deploy/desktop/Start-WorkspaceEasel.ps1:522`–`:536`；关联 `easel/control/store.py:776`。

提前的预检仅匹配部署项目、镜像、配置和根目录，再验证备份完整性；没有将 manifest.runtime_state.stop 与正式控制库当前停止完成记录匹配。compose up 后的检查也只拿当前 Stop 和旧配置比较，upgrade_stop_completion 只接收备份摘要，未验证它来自哪一轮 Stop。

真实 Store 反例：old-stop 后完整备份；reopen 后新增 AFTER-BACKUP 会话；相同部署配置再次 new-stop。旧备份真实 verify 成功，正式 Assert-EaselUpgradeBackup 接受，真实 Store 的 upgrade_stop_completion(new-stop, ..., 旧备份摘要) 同样接受。

```text
OLD_BACKUP_REAL_VERIFY=True
OLD_BACKUP_PRECHECK_ACCEPTED=True
BACKUP_STOP=old-stop
CURRENT_STOP=new-stop
UPGRADE_WITH_OLD_BACKUP_ACCEPTED=True
```

预检探针实际执行正式 Common 函数，只替换其外部 WSL 容器调用；成功输出对应先前真实 verify 的结果。没有启动 Docker/WSL，也不声称整套 Start 已真实运行。Start 的后半段未比较备份 Stop 的事实由代码检查和真实 Store 接受结果交叉验证。

后果：误选同配置旧备份仍能授权升级，备份后新增历史缺少可回滚副本。原 R1-03 已要求升级前匹配停止承诺，本项属于原范围。

最小修复：新 control 可能写/迁移正式 DB 前，通过只读、兼容旧 schema 的路径核对当前 barrier/停止完成记录与备份记录精确匹配（含停止代次、项目、旧配置、完成态）；必要的 key 来源也应保持一致。失败不得 compose up/改完成记录/reopen。保留启动后的最终复核，不为绕过校验清屏障。补正式脚本旧备份/新 Stop 的顺序反例，保证同一轮匹配备份正常升级。

## 本轮验证

- 主审 A7 三文件：38 passed（12.39 秒）。
- 主审 operations/v1/A7/脚本/部署/workspace/identity/lifecycle 八文件：147 passed、1 skipped（59.55 秒）；含上方 38 项，不相加。
- 独立备份复核 test_operations_a7：24 passed；独立脚本复核 test_desktop_operations_r1：7 passed，均为重叠范围。
- 主审三个反例脚本 exit 0，输出见 `docs/evidence/desktop-a7-r2-review/probe-output.txt`；源码 `probe.py` 可复跑，只用临时假材料。
- `git diff 04b5f23 e837675 --check` 退出 0。
- 作者 1433 passed/10 skipped 与 258.93 秒真实容器演练保留为作者证据；主审本轮未重跑全套、Docker、ngrok、真实供应商或生产 Linux。

首次主审/独立探针各因未关闭的临时 SQLite 连接，在进程退出前自动清理失败，留下两个仅含假材料的目录：`C:\Users\hyh\AppData\Local\Temp\easel-a7-review-84utvufl`、`C:\Users\hyh\AppData\Local\Temp\easel-review-a7-z273s86o`。递归清理被自动审批以 blocked by policy 拒绝，仍保留。后续探针显式关闭连接，全部临时目录正常自动清理；未触碰开发者资源。

## 后续

仅执行 `../tasks/2026-10-29-desktop-a7-rework-r2.md`。先确定性红测，再最小修复与定向回归；原始备份、失败输入和既有目标保护保持。无变化的 Harness/worker/gateway 不要求重建；如需重跑容器，仅用新建专用资源。交付后停止等复审，不把 A7 功能验收等同 production-release。
