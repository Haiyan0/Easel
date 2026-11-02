# Task 13 验收通过

日期：2026-10-26（Asia/Shanghai）。最终提交：5bebd87c65f723e3be656596a71acf1af9bc5948；R2 父基线：f297a404ecdb732c3d1e304b7af7e357c572fe80。

## 结论与范围

Task 13 的本地管理备份恢复、显式本机迁移及运行手册代码验收通过。[首轮四项](2026-10-26-task-13-review-round-1.md)及[R2 提交前归属复核](2026-10-26-task-13-review-round-2.md)均已关闭。本轮仅复审 R2 两文件修改、相关目录操作和既有回归，未发现需要返工的重要可复现问题。

- 控制 SQLite 使用 backup API 快照，备份持久区/受保护 master key 及完整性清单；worker token 不随控制快照复用。恢复在新/空目录验证摘要、schema、完整性与密钥，失败不留下已提交半成品。
- 备份拒绝 workspace owner/末级/内部链接；用户 Git 提交、分支、stash、工作树保留，不再按构建缓存裁剪普通持久数据。
- 显式迁移把静态文件放入 outputs，把身份化会话放入真实 state_root；不兼容 cwd 保留静态历史并报告，源指纹保持不变。文件级导入不等于已完成真实 Harness 或前端历史续接验收。
- 预检和提交对普通递归空目录规则一致，新增文件不被删除。R2 在空目标删除前与 rename 前重新检查 owner/target 重解析点、解析路径与归属；空目录辅助函数也在遍历前拒绝根自身和祖先链接，关闭原预检后 owner 被替换的根外写入路径。

本结论只覆盖 Task 13 代码和本机隔离测试。POSIX 执行位/原生 symlink 未在 Linux 实测；Task 8 目标 Linux 防火墙/IPv6、XFS、真实 Chromium 沙箱、最终 Harness/runtime 镜像、真实供应商/公网出口和宿主健康链等未通过门槛保持原状态，仍阻断正式发布。非聊天业务未装配者继续 503。

## 主审新鲜验证

固定 Python：C:/Users/hyh/anaconda3/envs/Agent/python.exe。

| 验证 | 结果 |
|---|---|
| tests/multiuser | 972 passed、4 skipped、2 个既有 websockets 弃用 warning，84.99 秒；包括 operations 42 passed/1 skipped |
| 原八文件兼容回归加 tests/test_document_paths.py | 282 passed、6 skipped，21.82 秒 |
| 原 junction 故障重放 | 临时装配父提交返回 success 且根外写入；当前提交 migration_target_escape/400、无根外写入、源不变、staging 清理 |
| git diff f297a40 HEAD --check | 退出 0 |
| 正式运维手册路径检查 | 0 错误 |

故障重放从 Git 只读加载父提交模块到临时测试装配，未回退仓库文件；只创建本次临时目录和 Windows junction，未操作开发者持久区。独立审查者另运行迁移定向测试 17 passed，并确认已存在空 owner 被替换、辅助函数根自身 junction/祖先 junction 的探针均明确拒绝，外部目录和 sentinel 保留。

本轮未修改前端，未运行 Node/build/browser、全仓测试、Docker、真实供应商或原生 Linux 门槛；不把执行方历史结果记为主审本轮证据。主审未修改产品、未 stage 或提交；既有 plan/spec、审计与任务单保持独立。

## 下一步

允许以本次最终提交为基线，按[原实施计划 Task 14](../plans/2026-10-16-multiuser-workspaces.md)和[本地研发/正式发布分离补充](../specs/2026-10-22-local-docker-development-gates.md)继续双用户端到端测试与发布门槛检查器。

先完成本机可执行的 control + 两独立 worker + 测试 Harness HTTP/SSE/文件/历史/恢复验证及证据矩阵；本机 Docker Desktop 可用证据与目标 Linux/真实 Harness/供应商证据分开记录。检查器不能把缺少正式环境、跳过或预期失败算成发布通过；显式请求正式 live/release 检查时缺前提必须失败。不得为了通过检查降低沙箱/权限边界或使用开发者账号/持久区。

完成本地交付后停止等待审计，明确仍未执行/未通过的正式门槛；不以 Task 13 验收或本地双用户测试宣布多用户已经发布。
