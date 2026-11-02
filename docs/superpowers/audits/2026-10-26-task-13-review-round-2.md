# Task 13 R1 复审与 R2 单项返工

日期：2026-10-26（Asia/Shanghai）。R1 父基线：8a98040f6cdd3b8c5bb4c0641564236397fd7c24；复审提交：f297a404ecdb732c3d1e304b7af7e357c572fe80。

结论：暂不通过，仅余 T13-R2-01（P2）。原 R1-01/02/03 已关闭；R1-04 的普通递归空目录与新增文件保护成立，但预检后替换 owner 本身为链接的分支尚未关闭。不进入 Task 14。

依据：[Task 13 R1 四项返工单](2026-10-26-task-13-review-round-1.md)。主审和独立审查者未修改产品、未 stage/提交，未访问真实账号、开发者持久区或 Docker；探针仅使用本次临时目录、SQLite/key 和真实 Windows junction。

## 已关闭与验证

- 迁移产物进入真实 outputs，绑定/消息进入真实 state_root；源中的 state/sessions 静态内容与状态根隔离。主审临时探针独立读到目标 outputs 文件、真实 state_root 的消息；普通递归空目录可完成迁移。
- 备份在进入 workspace/home 前拒绝 owner 祖先 junction，包括根外和另一 workspace。相应真实 Windows junction 用例包含在本轮全量回归中。
- 普通持久文件及 Git 对象/分支/stash 不再按构建缓存排除，真实 Git 往返测试通过；POSIX 普通文件 rwx 位保留、特权位清除实现已核对，但 Windows 下执行位测试 skipped，不宣称 Linux 已实测。
- tests/multiuser：970 passed、4 skipped、2 个既有 websockets 弃用 warning，85.53 秒；包含 operations 的 40 passed/1 skipped。
- 原八文件兼容回归加 tests/test_document_paths.py：282 passed、6 skipped，23.18 秒。
- 修改正式手册路径校验：0 错误；git diff 8a98040 HEAD --check：退出 0。

本轮没有运行未修改的前端 Node/build/浏览器、全仓测试或真实部署门槛。Task 8 正式发布门槛及非聊天 503 保持原状态。

## T13-R2-01（P2）：最终提交未重新拒绝 owner 根自身的 junction

定位：[easel/control/cli.py](../../../easel/control/cli.py) 约 1134–1145 行 migrate_local 最终提交；关联 _empty_tree 与 _remove_empty_tree。

初次 _migration_target 会拒绝 owner/target 的重解析点。但复制与会话导入之后，提交只检查 owner.is_dir() 与 _empty_tree(owner)。这两者都会跟随 owner 根本身的 junction，_empty_tree 仅检查其子条目。若预检后 owner 被替换为指向根外空目录的 junction，检查通过，os.replace(staging, target) 经该链接写入根外。

主审确定性交错：先创建合法空 owner，正常完成迁移预检、源备份及 staging；第二次源指纹读取返回前，用真实 Windows junction 将 owner 替换为 outside（本次临时根外空目录）。真实 migrate_local 返回成功，并生成 outside/workspace/outputs/sentinel.txt：

```text
MIGRATION_RETURNED_SUCCESS True
EXTERNAL_WORKSPACE_WRITTEN True
```

独立审查者另在 _import_sessions 完成后注入 owner junction，得到相同成功返回和根外写入。两次均未修改源内容或现有外部文件；临时 junction 已清理。现有“预检后 late.txt”测试只验证 owner 内出现普通文件，没有覆盖 owner 自身被替换为重解析点。

### 定点修复要求

最终提交应重新核验 owner/target 自身的类型、重解析点与解析归属，在跟随路径扫描、删除空目录和 rename 之前拒绝根外/另一空间的 owner。空目录删除辅助函数同样不能假设传入根仍是普通目录，不能先遍历链接目标再判断。保持现有源指纹、目标新增文件保留、staging 清理及普通递归空目录契约；不使用 rm -rf/rmtree 清理待提交目标。

先写当前 f297a40 会失败的真实文件系统交错测试：在预检后、最终提交前，把原本合法/不存在的 owner 变成根外 junction（可用 POSIX 环境用 symlink）。期望明确 4xx、根外无新 workspace/outputs/state 文件，外部 sentinel 和链接保留，源不变，staging 清理。覆盖指向另一 workspace 的同类归属边界，并保持合法空目录、普通新数据冲突回归。无需扩大为新的迁移接口或认证/聊天重构。

## 可复制给执行 Agent

```text
从 f297a404ecdb732c3d1e304b7af7e357c572fe80 开始，仅修复本审计单 T13-R2-01。
先补“预检后 owner 变为 junction → 根外写入”的确定性交错反例跑红，再修最终提交与空目录遍历前的路径归属检查。
保持已关闭的 outputs/state、备份祖先链接拒绝、Git/模式保真及源只读边界。
运行 tests/multiuser、原八文件兼容回归、tests/test_document_paths.py、修改文档路径校验与 git diff --check。
Windows junction 必须真实执行；原生 POSIX 未执行则如实保留，不假称 Linux 验收通过。
仅显式提交必要实现/测试/正式说明，不夹带主审 plan/spec、审计或任务单。
交付父/新 SHA、文件清单、反例红绿与实际测试结果后停止等待复审，不进入 Task 14。
```
