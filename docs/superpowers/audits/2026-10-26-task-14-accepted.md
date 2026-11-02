# Task 14 本地代码验收通过，正式发布仍阻断

日期：2026-10-26（Asia/Shanghai）。最终提交：290d94a68b9a4c1e8e3f627b5c96fcbdb8f11f1b；R2 父基线：35d2c2eae563bc82bb2ec760456142d08f406e96。

## 结论与边界

Task 14 的本地双用户端到端测试、live 验收断言和发布检查器代码验收通过。[首轮五项](2026-10-26-task-14-review-round-1.md)及[R2 结果统计/漏选](2026-10-26-task-14-review-round-2.md)均已关闭。本轮只复审 R2 四文件改动与直接相邻分类路径，未发现需要返工的重要可复现问题。

- 本地测试实际连接 control app 和两独立 worker 子进程，用假 ACP 验证同 native ID 的 workspace 隔离、HTTP/SSE、连续追问、消息/文件和数据级备份恢复；不能替代真实 Harness/容器隔离证据。
- 门槛子进程输出被捕获，公开报告只输出状态。skip/xfail/xpass、空集合和所有必选 gate 的漏选均拒绝，不以少量通过用例替代完整证据。
- R2 只从 pytest 最终汇总读取统计，警告/用例正文不覆盖真实 skip；无可信统计明确 gate_unverified。单项出口结果不完整时 egress_proxy 保持 blocked。
- live 的 B Run 部分输出/失败不计成功，A/B Run 状态需 done；浏览器隔离断言检查 local/session storage 键和值及可见身份状态，上一轮故障注入已在 R1 复审关闭。
- 正式出口测试显式纳入；XFS 实际限额/耗尽、宿主防火墙 IPv4/IPv6 矩阵、最终 runtime/真实 DSH 三项固定 evidence blockers 仍有效。即使当前已选 gate 全绿，整体仍退出 1、release_status=blocked。

这是本地研发范围的代码验收，**不是 Task 8 或多用户正式发布通过**。原生 Linux、真实 XFS 10 GiB 配额/耗尽、DOCKER-USER/IPv6、最终 Harness/runtime 镜像与 profile、硬化容器 Chromium 沙箱、真实供应商/公网及宿主健康链等未验证/未通过项保持原状态。control workspace-content（例如 /api/outputs）尚未装配仍为 503，正式 live 测试会因此失败。

## 本轮新鲜验证

固定 Python：C:/Users/hyh/anaconda3/envs/Agent/python.exe。

- 主审 tests/multiuser：1004 passed、4 skipped、2 个既有 websockets 弃用 warning，127.43 秒。
- 主审原八文件兼容回归加 test_runtime_env.py、test_document_paths.py：289 passed、20 skipped，21.67 秒。
- 主审真实微型 pytest：警告含 0 skipped 且实际 skip → 八组均 gate_skipped；1 passed/1 deselected → 八组均 gate_unmarked；两类均使出口证据 blocked。
- 主审完整通过微型 pytest → 八组正确 pass；没有完整汇总的真实子进程 → gate_unverified，未把输出字样当作已验证结果。
- 主审模拟全部已列命令全绿 → exit=1、release_status=blocked，三个固定证据项 blocked。这仅证明判定控制流，不是实际 Linux 门槛已运行。
- Windows 实际 --json → exit=1/release_status=failed、gates/evidence 为空；live 无 flag → 6 skipped；显式 flag 且移除测试环境变量 → 6 errors，不 skip。
- 独立审查者定向 test_release_checker.py：24 passed，25.42 秒；另用真实 ANSI 彩色 pytest 输出复核 skip、deselected、xfail、xpass、空集合和正文噪声，分类符合预期。
- 两份修改手册路径检查 0 错误；git diff 35d2c2e HEAD --check 退出 0。

本轮未修改浏览器断言或产品前端，没有重复运行浏览器整套/Node/build；R1 主审记录的真实 Chromium 12 passed 保留为历史证据，不算本轮运行。未运行全仓测试、Docker、真实账号、供应商或正式 live 环境。主审与独立审查者未修改产品、未 stage/提交；主审既有文档保持独立。

## 后续状态

不再安排 T14-R2-01 代码返工。原计划本地研发范围的审计可以收尾；正式多用户上线仍须补齐并实测部署证据，以及尚未装配的必要产品能力。未来新增真实门槛后应单独审查其目标、镜像、完整选集与证据归属，再决定是否关闭固定 blockers；不得用本次代码验收或手工环境声明解锁发布。
