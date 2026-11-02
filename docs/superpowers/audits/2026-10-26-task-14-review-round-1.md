# Task 14 首轮审计与 R1 定点返工

日期：2026-10-26（Asia/Shanghai）。父基线：5bebd87c65f723e3be656596a71acf1af9bc5948；审计提交：ced2a6464b46fa2afef25016832ad00e40b9e04f。

结论：暂不通过。确认下面五项验收实现问题：检查器两项 P1、一项 P2，live 断言两项 P2。本机双用户路径已有有效进展，不要求重写产品主流程；正式发布仍未通过。

依据：[原计划 Task 14](../plans/2026-10-16-multiuser-workspaces.md)、[Task 13 验收与下一步](2026-10-26-task-13-accepted.md)、[本地研发/发布分离补充](../specs/2026-10-22-local-docker-development-gates.md)。主审及两个独立审查者未修改产品、未提交代码；仅使用临时目录、测试子进程、受控 transport 和临时 Chromium context，未连接真实供应商、账号、Docker 或目标宿主。

## 本轮独立验证与成立部分

- tests/multiuser：987 passed、4 skipped、2 个既有 websockets 弃用 warning，93.54 秒；包含双用户与检查器 15 项。
- 原八文件兼容回归加 test_runtime_env.py、test_document_paths.py：289 passed、20 skipped，23.70 秒。
- Windows 正式检查器 --json：退出 1，platform_not_linux、gates 为空；本机不冒充 Linux 的边界成立。
- 三个 live 文件不带 --multiuser-live：6 skipped；显式带 flag 且在测试子进程移除测试环境变量：6 errors、退出 1。未兑换任何真实邀请。
- 两份修改手册路径校验：0 错误；git diff 5bebd87 HEAD --check：退出 0。
- 独立审查者另运行本地 test_two_users.py：4 passed，核对其确经真实 control app 和两个独立 worker 子进程，session fixture 每用户仅兑换一次邀请。

本地同 native ID 的 workspace/state 隔离、实际 HTTP/SSE、worker 服务 token/文件隔离及数据级备份恢复已形成有效测试；control outputs 未装配保持 503，并被正式 live 用例明确拒绝。下面问题是验收代码的假红/假绿与覆盖遗漏，不代表已观察到产品实际泄漏用户数据。

本轮没有运行未修改的前端构建/Node、全仓测试、Docker 大镜像、原生 Linux、真实供应商或正式 live。绿色回归不能替代以下故障注入。

## T14-R1-01（P2）：门槛子进程未捕获输出，真实通过也被判空并直通原始输出

定位：[scripts/check_multiuser_release.py](../../../scripts/check_multiuser_release.py) 约 329–334 行 run_release_gates。

调用 _run 时只给 cwd/env，没有 capture_output/text。真实 subprocess.run 的 stdout/stderr 因而为 None，随后分类得到空字符串并返回 gate_empty。子进程原始输出同时继承终端，破坏“只输出状态/稳定错误码”及 --json 的纯 JSON 契约，也可能把测试异常中夹带的敏感内容输出。

主审和独立审查者各用真实小子进程，保持原 runner kwargs，仅替换待执行命令为输出正常通过摘要和 RAW_OUTPUT_SENTINEL。结果：七组原始输出直达终端，七组全部 fail/gate_empty。现有测试执行器无论 kwargs 都预造 stdout 字符串，掩盖了真实 subprocess 契约。

返工与验收：实际门槛执行应捕获 stdout/stderr，明确文本解码策略，报告层仅输出脱敏状态。增加真实 subprocess/微型 pytest 成功和失败测试（不连接 Docker）：成功能被正确分类；失败、异常与 stderr 的 sentinel 不出现在公开输出；--json 可直接解析为单个完整 JSON。不能仅调整 fake executor 返回值。

## T14-R1-02（P1）：必选安全门槛用 allow_skips=True 放行 skip/xfail

定位：[scripts/check_multiuser_release.py](../../../scripts/check_multiuser_release.py) 约 300–325 行非 live 门槛的 True 标志，以及 258–264 行分类逻辑。

容器、网络、拓扑、真实浏览器、sidecar 等门槛传入 allow_skips=True，连 xfailed/xpassed 也一起放行。主审和独立审查者用真实临时 pytest 得到 exit=0、1 passed/1 skipped/1 xfailed，将这个实际 CompletedProcess 交给 run_release_gates 分类，除 live 外六组全部 pass。

这是当前可触发的路径：[test_browser_profiles_real.py](../../../tests/integration/multiuser/test_browser_profiles_real.py) 在 Chromium 启动失败时 skip，同时同文件有不启动 Chromium 的锁测试可 pass；检查器会将缺浏览器证据的混合结果当成浏览器门槛通过。

返工与验收：必选正式门槛任一 skip/xfail/xpass、空集合、漏选均失败，不用文件级宽松开关。若普通回归存在合法的平台专属 skip，应将其与正式证据分组、精确选择并如实记录，不得据此允许真实安全用例缺失。测试必须走实际 gate runner 配置，覆盖所有必选 gate 的混合摘要，不能仅测试 classify_gate_result 的默认参数。

## T14-R1-03（P1）：release_status=passed 没有覆盖仍属必需的正式门槛

定位：[scripts/check_multiuser_release.py](../../../scripts/check_multiuser_release.py) 的 REQUIRED_ENV、check_prerequisites 与约 294–326 行 gates 清单。

检查器穷举的门槛没有调用既有 [test_egress.py](../../../tests/integration/multiuser/test_egress.py)，不要求/设置 EASEL_EGRESS_LIVE 和 EASEL_TEST_EGRESS_PROXY。因此公网允许、IPv6 loopback/元数据/DNS 私网拒绝的真实出口门槛可完全没运行，检查器仍存在输出 release_status=passed 的分支。

同类缺口：XFS 只检查挂载选项和 xfs_quota 可执行文件存在，未证明 10 GiB 限额建立/耗尽；没有宿主 DOCKER-USER/IPv6 矩阵证据。主要容器测试自行构建 base/execution（假 Harness），不能证明传入的最终镜像及真实 DSH/profile 已验收。真实 live 聊天返回 token 本身也不能补齐这些证据。

主审用既有模拟 runner 的正式前提夹具记录全部命令，未给真实出口变量、未运行上述测试，main 仍输出 passed/exit=0。此模拟仅证明检查器遗漏和通过分支，不是正式环境通过证据。独立审查者已核对实际测试/命令清单，确认这些门槛未由现有网络测试替代。

返工：建立明确的必需证据集合，区分 prerequisite、测试执行结果和未实现/未验证项。已有真实出口测试应被显式纳入且缺环境失败；尚无可靠自动门槛的正式要求，必须保持 unverified/blocked 并阻断整体 passed。不得接受环境变量/任意文本“已通过”替代真实执行，也不能把 base/execution 镜像结果归到最终 runtime 镜像。

本轮无需借此补齐全部目标 Linux、防火墙、XFS 或真实供应商实现；可以先把缺失证据保留为明确阻断项，使检查器诚实失败。测试覆盖：缺任一必需证据时，即使其余选定命令全绿也不能得到整体 passed；命令、镜像/目标和证据范围应可核对，输出不含秘密。实际门槛未执行如实记录，不索要开发者账号或改宿主全局安全配置。

## T14-R1-04（P2）：B 的 Run 失败仍能通过双用户 live 测试

定位：[test_two_users_live.py](../../../tests/integration/multiuser/test_two_users_live.py) 约 172–177 行 B 的请求/断言；关联 _collect。

_collect 接受 done/failed/interrupted/cancelled 任意终态。B 只检查非空 token 和至少两条历史，最后只查 A 的 Run 为 done。主审独立复跑审查者临时探针：保留原测试及全部 helper，以 MockTransport 提供 B 的 token(partial reply) → state(failed) 和两条历史，整条原测试仍通过，输出 B_SSE_TERMINAL=['failed']。

返工与验收：A/B 每个期望成功的执行都明确断言 done，并复查对应 Run 终态及消息结果；部分 token 不等于成功。为 B failed/interrupted/cancelled、部分输出建立负向敏感性测试，证明原 live 测试会失败；合法双用户成功场景继续通过。此处 transport 故障注入只验证测试敏感性，不算真实 Harness 验收。

## T14-R1-05（P2）：浏览器缓存隔离只查键名，漏掉缓存值中的 A 数据

定位：[test_browser_live.py](../../../tests/integration/multiuser/test_browser_live.py) 约 80–83 行。

Object.keys(localStorage) 仅检查键名。主审独立复跑真实 Chromium 临时 route 故障注入，运行原测试全过程：B 页面 shared-cache 的值含 user-A、ws-A 及 A private，但键名不含身份，原 test_live_browser_contexts_do_not_share_login_or_cache 仍通过。

这证明应用把 A 数据写入 B 缓存时，现有门槛无法检出；不表示独立 Chromium context 自行共享了存储，也不是已确认产品泄漏。

返工与验收：检查所声明缓存边界的实际键和值，用 A 独有消息/身份 sentinel 证明 B 的持久缓存和可见私人状态不含 A 数据。保持合法身份所需缓存，不用清空全部存储让断言空过。补真实浏览器负向敏感性测试：普通键名、值含 A 数据必须失败；合法 A/B 页面通过。不打印真实 token、缓存正文或邀请，故障注入仅用测试 sentinel。

## 执行与交付

从 ced2a6464b46fa2afef25016832ad00e40b9e04f 开始，仅修上述五项及必要测试/说明，不扩展 control 产物代理、平台发布等未开放业务，不改产品认证/聊天生命周期。

先补当前版本会失败的 runner/断言敏感性测试，再修复。使用固定 Agent Python 运行 tests/multiuser、定向新测试、原兼容回归与文档路径检查；live 无 flag/显式缺环境继续验证跳过与报错区别。检查器本机保持平台失败是正确结果，但必须用真实小子进程验证执行器契约、用受控故障验证 live 断言，而不以假摘要代替全部敏感性测试。

仅提交必要实现、测试及正式说明，勿夹带主审 plan/spec、审计或任务单。交付父/新 SHA、逐项红绿、实际证据和未执行门槛后停止等待复审。正式发布始终需要尚未关闭的 Task 8/真实环境证据，不以本轮修复宣称发布成功。
