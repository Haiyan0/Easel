# Task 13 首轮审计与 R1 定点返工

日期：2026-10-26（Asia/Shanghai）。父基线：7d49c814532fefcb31c1e340b5735487766fe9fc；审计提交：8a98040f6cdd3b8c5bb4c0641564236397fd7c24。

结论：暂不通过。下面四项 P2 均已在临时数据上独立复现。仅修本单，不进入 Task 14。依据：[原实施计划 Task 13](../plans/2026-10-16-multiuser-workspaces.md)、[原规格运维约束](../specs/2026-10-16-multiuser-workspaces-design.md)。

主审及两个独立审查者只读审查产品；仅使用本次临时目录、测试 key/SQLite、真实临时 Git 与 Windows junction，没有操作开发者账号、持久区、真实供应商、Docker 或宿主防火墙。未修改产品、未 stage 或提交。问题已按 systematic-debugging 的先复现、定位根因流程确认。

## 独立验证及已成立部分

- tests/multiuser：962 passed、3 skipped、2 个既有 websockets 弃用 warning，85.59 秒；包括本批 32 项 operations 测试。
- 八文件兼容回归加 tests/test_document_paths.py：282 passed、6 skipped，22.45 秒（包含原八文件 272 项及文档 10 项）。
- 本批四份修改文档的路径校验：0 错误；git diff 7d49c81 HEAD --check：退出 0。
- SQLite backup API、WAL 转单文件、快照清理 worker lease 明文 token、已有密文与 master key 校验、恢复摘要/额外文件/非空目标拒绝的现有回归通过。
- 本轮没有重复运行未修改的前端 Node/build/lint或浏览器；交付报告相关结果为执行方证据，不算主审本轮运行结果。未运行全仓测试或真实部署门槛。

现有迁移测试从自建的错误布局读回，现有链接测试只覆盖 workspace 子目录自身/内部链接；这些绿色结果不能证明下面边界成立。

## T13-R1-01（P2）：迁移没有使用真实 worker 的产物根和状态根

定位：[easel/control/cli.py](../../../easel/control/cli.py) 约 993–1003 行 _import_sessions、1060–1066 行静态文件复制；对照 [web/app.py](../../../web/app.py) 的 OUTPUTS_DIR（约 326 行）、_workspace_session_store，以及 [easel/worker/runs.py](../../../easel/worker/runs.py) 约 1000 行。

源为 outputs 时，静态 projects/demo.txt 被复制为目标 `/workspace/projects/demo.txt`，但 worker 实际读取 `/workspace/outputs/projects/demo.txt`。会话则通过 WorkspaceSessionStore(staging, ...) 写入 `/workspace/sessions/...`，而真实 worker 使用 state_root，部署固定为 `/workspace/state`，因此读取 `/workspace/state/sessions/...`。迁移返回 imported 成功，真实消费者却都读不到。

主审以真实 migrate_local、测试源和 WorkspaceSessionStore 独立复现：

```text
MIGRATION_SUCCESS True
EXPECTED_OUTPUT_EXISTS False
ACTUAL_ROOT_OUTPUT_EXISTS True
ACTUAL_WORKER_STATE_MESSAGES 0
WRONG_ROOT_MESSAGES 1
```

返工：根据实际 runtime paths 建立明确的本机源→目标布局映射，outputs 进入目标 outputs，私有会话进入真实 state_root。迁移中生成的会话文件应与静态源内容隔离，不能由源中同名 sessions/state 等路径混入或覆盖。保留不可续接静态历史的明确位置和报告，不替换 native_id，也不因为文件成功写入就声称真实 Harness/UI 续接通过。

验收：先补基线反例，再使用与 worker 相同的 RuntimePaths/state_root 和会话读取入口验证迁移结果，不能继续使用 WorkspaceSessionStore(target) 自证。普通产物通过 worker 实际产物读取路径可达，消息顺序和身份 envelope 正确；不兼容原生 cwd 仍只导入静态历史，源指纹不变。

## T13-R1-02（P2）：workspace ID 祖先 junction 可使备份越过数据根

定位：[easel/control/cli.py](../../../easel/control/cli.py) 约 373–382 行 create_backup 的 source 构造和 _copy_tree 调用。

只检查 `<data>/<id>/workspace` 自身及其子节点，不检查 `<data>/<id>`。当 ID 目录是指向外部目录的 junction 时，workspace 本身仍是普通目录，_copy_tree 接受并复制根外数据。

主审与独立审查者均在 Windows 创建临时 `<data>/<合法id>` junction → `<outside>`，令 outside/workspace/sentinel.txt 为普通文件。create_backup 返回成功；备份包含该根外 sentinel，主审输出 BACKUP_FOLLOWS_OWNER_JUNCTION=True。测试后 junction 已删除，未接触真实用户路径。

返工：进入 workspace/home 前检查所属 ID 目录及相关边界，拒绝重解析点，复核解析目标仍在批准的数据根和该 workspace 归属下。不可仅增加文件名黑名单或只判断末级 source。

验收：Windows 实际 junction（可用的 POSIX 环境使用 symlink）覆盖 ID 目录链接到根外、另一 workspace 和末级/内部链接；明确拒绝，备份目标无半成品，不读入外部 sentinel，外部文件不变；普通布局仍备份成功。

## T13-R1-03（P2）：完整备份静默丢弃用户 Git 历史

定位：[easel/control/cli.py](../../../easel/control/cli.py) 约 47 行 _SKIP_BACKUP_NAMES 和 381 行使用。

备份遍历在任意层级无条件跳过 .git。workspace 内由用户/Agent 创建的 Git 仓库属于持久数据；未推送提交、分支、stash 与仅存在 Git 对象库中的旧内容不能从当前工作树重建。原规格要求备份各 workspace 持久区，手册只披露 .credentials 的秘密材料排除，不能把删掉 Git 历史的结果报告为完整成功。

主审与独立审查者均用真实临时 Git：提交 script.txt=v1，再把工作树改为 v2；create_backup + restore_backup 都成功，恢复仅有 v2，.git 不存在，v1 提交无法恢复。

返工：区分代码构建缓存裁剪与持久数据备份策略。备份必须保留用户 Git 历史等不可再生材料；不得套用安装包/镜像排除表。对确实不支持的链接/特殊材料保持明确失败，不得静默伪装完整成功；保留 worker 凭证排除和快照 token 清理边界。

验收：真实 Git 仓库含已提交旧版、未提交新版、额外分支及 stash，备份恢复后 Git 能读取原提交、分支和 stash，工作树修改也保留。核对清单和摘要，来源不变。另复核普通文件模式：当前 _copy_file 在 POSIX 把所有内容 chmod 0600，会丢失脚本执行位；普通持久文件应保留必要模式，秘密文件继续使用受保护权限。本轮未在 Linux 实测执行位，不将其写为已运行通过。

## T13-R1-04（P2）：dry-run 与提交对空目标使用相反规则

定位：[easel/control/cli.py](../../../easel/control/cli.py) 约 870–905 行 _empty_tree/_migration_target，及 1069–1074 行正式提交。

预检明确允许只有空子目录的目标，正式提交却用 any(target.iterdir()) 判非空。因此同一输入 dry-run 成功，实际执行已经完成源备份后才报 migration_target_not_empty。主审独立创建 workspace/empty/，源只有 a.txt，得到：

```text
DRY_RUN_ACCEPTED_EMPTY_SUBDIRECTORY ['a.txt']
ACTUAL_REJECTED migration_target_not_empty 409
```

返工：统一预检与提交阶段的目标规则；若接受递归空目录，提交前安全复核仍为空后完成替换；若选择更严格规则，必须在 dry-run/实际执行创建备份之前同样拒绝。不得递归删除新出现的文件、链接或其他用户数据。

验收：不存在目标、纯空目标、只有空子目录、非空文件/链接目标，以及预检后新增数据的确定性交错。dry-run 与实际准入一致；目标变非空时保留新数据并失败，无半成品。

## 手册同步项

当前手册第 7.2 节传入 workspace-root=/srv/easel/workspaces，却示例 backup-destination=/srv/easel/.migration-backups/...；实现只允许备份目标在 workspace-root 下，该示例必返回 migration_backup_escape（主审以同构临时路径复现）。统一示例、目录树和实际边界，不为适配示例放宽到任意宿主路径。该说明修正随 R1 提交。

## 执行与交付

从 8a98040f6cdd3b8c5bb4c0641564236397fd7c24 开始，仅修上述四项及必要说明/回归。先为每项写会在当前提交失败的真实文件系统/消费者反例，再实现。若拆分 operations 模块以控制 CLI 体积，仅做保持既有命令入口的局部提取，不重构认证或聊天。

运行固定 Agent Python 的 tests/multiuser、原八文件兼容回归、tests/test_document_paths.py、修改文档的路径校验及 git diff --check。新增定向测试必须使用实际 runtime 路径、真实 Git 和可用的真实 junction/symlink；不可仅修改夹具使错误布局自洽。未改前端无须重跑前端构建/浏览器；没有原生 Linux 则明确记录 POSIX 门槛未执行，不降级权限或假称通过。

提交仅含必要实现、测试和正式手册；主审 plan/spec、审计及任务单保持未暂存、未提交。报告父/新 SHA、文件清单、逐项红绿与未执行项。完成后停止等待复审，不进入 Task 14；Task 8 发布门槛及非聊天 503 保持原状态。
