# 本地 Docker 开发与发布验收分离补充

日期：2026-10-22（Asia/Shanghai）。用户已明确只有本地 Docker，并将定位配置、安排执行和给出交接指令交由本会话处理，不再要求用户手工找路径、commit 或填写 profile。

本补充只调整执行顺序与验收状态表达，不放宽隔离要求，不修改运行时代码，不开启未经验收的多人部署。

## 1. 执行决定

- 当前开发环境使用现有 Docker Desktop 的 Linux 容器，不新增远端服务器作为继续写代码的前置条件。
- 原计划 Task 9 只依赖 Task 1+6，这两个依赖及其代码审计已完成，立即允许外部代码 Agent 开始 Task 9。
- Task 8 当前状态为“已记录代码/测试返工项关闭；真实部署验收尚未完成”。不伪造 Task 8 全通过，也不因其未完成而阻断与该真实环境无关的会话存储开发。
- Task 9 完成后仍逐任务独立审计，再决定 Task 10；不一次性授权跳过后续审计。
- 2026-10-22 更新：Task 9 已在 9813d42511e9ca2db84de5ca4bd0463bf5a6786c [验收通过](../audits/2026-10-22-task-09-accepted.md)，现在允许按 [Task 10 交接单](../tasks/2026-10-22-task-10-authorized-relay.md)继续研发。原 Task 10 对 Task 8 的代码接口依赖可使用已审查实现；Task 8 的真实部署验收仍未通过，缺正式装配时运行时仍须失败关闭。完成 Task 10 后停止审计，不自动进入 Task 11。
- 2026-10-23 更新：Task 10 已在 e4ffc83c685762b94bdc063d7a6200c35b861725 [代码验收通过](../audits/2026-10-23-task-10-accepted.md)，现在允许按 [Task 11 交接单](../tasks/2026-10-23-task-11-identity-ui.md)继续前端研发，完成后停止审计、不进入 Task 12。Task 10 未装配的非聊天业务仍 503，Task 8 未通过的正式部署门槛不变。
- 2026-10-23 Task 11 首轮审计更新：30a4e8688b2963e4423cef9f7e9baa3a6ce32eb4 暂不通过；现有 Node/后端/浏览器主路径及生产构建通过，但确认身份竞态、401 私有页面清理、会话切换与 SSE 恢复、分页和归档读取七项问题。仅按[Task 11 R1 定点返工单](../audits/2026-10-23-task-11-review-round-1.md)修复，停止等待复审，不进入 Task 12。
- 2026-10-23 Task 11 R1 复审更新：5e4908b73d12c74eb906b5c1477dcb8e8a194c59 两项 P1 已关闭，已有回归通过，但旧 Run 回调、连续 SSE 失败、分页边界/选择稳定及失效缓存仍有遗漏；仅按[Task 11 R2 五项定点返工单](../audits/2026-10-23-task-11-review-round-2.md)修复，停止等待复审，不进入 Task 12。
- 2026-10-23 Task 11 R2 复审更新：249f79d72a276adcc5c3b6bbbf1cfc184321125c 的连续 SSE 失败、尾页重复、401 缓存清理已关闭；新请求等待 202 时的旧历史提交、目录补页时覆盖用户新选择仍需按[Task 11 R3 两项返工单](../audits/2026-10-23-task-11-review-round-3.md)修复，不进入 Task 12。
- 2026-10-23 Task 11 R3 复审更新：410be6aeb2eb620feada3d87249d7b8ba975f23a 已修复两种 202 顺序及目录补页期间的用户选择；新请求失败时，上一轮已完成助手回答仍缺显示收敛，按[Task 11 R4 单项返工单](../audits/2026-10-23-task-11-review-round-4.md)补齐，不进入 Task 12。
- 2026-10-25 Task 11 R4 复审更新：9936d87499826aa48168510fed633792f37aba06 已修复失败恢复原问题，但放宽初次历史提交导致旧快照可覆盖已完成新轮；仅按[Task 11 R5 单项回归单](../audits/2026-10-25-task-11-review-round-5.md)修复，不进入 Task 12。
- 2026-10-25 Task 11 验收更新：a6fc17713c65cd0acced72fffdccf72586878d6c 的范围内阻塞已关闭，见[Task 11 验收记录](../audits/2026-10-25-task-11-accepted.md)；允许按[Task 12 交接单](../tasks/2026-10-25-task-12-control-settings.md)继续个人覆盖和管理员设置，完成后停止等待审计，不进入 Task 13。Task 8 正式部署门槛保持原状态。
- 2026-10-26 Task 12 首轮审计更新：359fcba90b712f43e81899d982e7a0c864e371e5 暂不通过；仅按[Task 12 R1 三项返工单](../audits/2026-10-26-task-12-review-round-1.md)修复，不进入 Task 13。管理事务、配置 CAS/加密与聊天既有边界保留，正式发布门槛不变。
- 目标宿主网络/XFS/最终镜像/真实 Harness/Chromium 和 live 出口等发布门槛保留到最终发布验收阶段，正式多人模式不能因此放行。
- 旧 Task 8 审计里的“不进入 Task 9/等待目标环境”是当时执行指令，自本补充起被新的 Task 9 交接单替代；不重写旧结论或把未执行改成通过。

## 2. 已由主审实际定位的本地 Harness 基线

主审只读检查如下来源，没有启动真实 prompt、没有使用供应商凭证：

| 项目 | 2026-10-22 实际结果 |
|---|---|
| Easel ACP 配置 | C:/Code/Easel/outputs/_harness/acp.json，仅含 command 字段 |
| Harness Git 根 | C:/Users/hyh/Desktop/deepseek-harness |
| origin | https://github.com/deepseek-ai/deepseek-harness.git |
| HEAD | 687f282e8625c6b8d11ff58211a90524be2e4317 |
| 工作树 | 无改动 |
| 包版本 | 0.2.1-rc.2 |
| 当前启动 | node.exe → apps/cli/lib/bin.js → --profile acp |
| 本机 Node | v24.13.0 |
| Harness engines.node | ^22.19.0 或 >=24.0.0 |
| packageManager | pnpm@11.28.5，已有 pnpm-lock.yaml |
| profile 所在位置 | C:/Users/hyh/.dsh/profiles/acp |
| profile 公开 bundle 清单 | @deepseek-ai/dsh-base、@deepseek-ai/dsh-acp-app |
| Docker | desktop-linux；Engine 29.2.1；linux/x86_64；overlayfs |

脱敏机器可读基线：[harness-local-baseline.json](../tasks/2026-10-22-harness-local-baseline.json)。它记录本地已使用版本及 Linux 候选命令，不是 Linux/生产验收证书。

用户不用再手工提供 DSH_COMMIT 或本地 ACP 文件位置。后续容器构建候选先固定上述已在用 clean commit，不追 master/latest；实际 Linux 协议及发行构建通过后再更新验收状态。

## 3. 容器 Harness 的后续责任归属

以下由代码 Agent 依据已知基线处理，不转嫁给用户填模板；不纳入 Task 9 的产品修改范围：

- 依据固定仓库版本的实际 CLI 文档，准备 Linux 候选启动命令 node /opt/dsh/apps/cli/lib/bin.js --profile acp。
- 从固定版本的公开 shipped ACP profile/bundle 生成受管配置；不原封复制用户整个 .dsh、home patch、会话或凭证。
- 当前 Dockerfile 仅 apt 安装 nodejs/npm，并未按 Harness 的 engines 和 pnpm 版本完成明确固定；后续打包须匹配版本、使用锁文件、完成生产运行需要的构建产物。源码 README 明确需要先构建，不能把 npm install 或存在 profile.json 当作真实可运行。
- 明确区分 Easel 的 ACP 元数据 JSON 与 DSH 实际 package.json/cordis profile；生成元数据文件不能代替真实 profile 装配。
- 受管配置不可由用户可写 HOME/.dsh 的覆盖层替换；原生会话/记忆仍写本人数据区。必须验证配置来源与用户状态分离，不复制用户机器绝对路径入镜像。
- 使用隔离测试 HOME/临时 workspace 做 initialize/session/new/prompt/resume。无供应商凭证时先用受控 fake provider 验证真实 Harness 协议；不调用模型收费接口来猜测配置。
- Chromium 先在本地 Docker 内只读排查 No usable sandbox! 的内核/命名空间/seccomp 原因；不能把它简单归因为“没有独立 Linux 服务器”，也不关闭沙箱、privileged 或恢复 capabilities。

正式受管 profile 和最终镜像仍需上述真实验收，不能将此处候选命令直接标记为 Linux 已验收。没有目标服务器只影响目标宿主项目，不影响这些可在本地 Docker 中推进的工作。

## 4. 门槛分离

| 类别 | 现在的安排 |
|---|---|
| 功能与代码开发 | 本地 Python/Node/Docker，按任务测试审计继续，先 Task 9 |
| 本机容器验证 | 使用自建临时资源；已通过和失败逐项保留，缓存镜像证据与从头构建分开 |
| 目标宿主验证 | 以后确定部署宿主再做 XFS、DOCKER-USER/IPv6、宿主健康链，不现在要求用户购买服务器 |
| 发布 | 全部正式隔离与真实协议/网络门槛实际通过后才可放行 |

不引入自动关闭安全检查的 development bypass。测试 fake 仍只用于其明确的测试边界，生产失败关闭逻辑保持不变。

- 2026-10-26 Task 12 R1 复审更新：79fbe371179ad61c2a5058c03afd198f182c546a 的个人原值与多字段凭证已关闭；邀请刷新与旧分页响应仍可交错丢失边界项，仅按[Task 12 R2 单项返工单](../audits/2026-10-26-task-12-review-round-2.md)修复，不进入 Task 13。主审本轮后端 930 passed、前端 Node 114 passed、八文件兼容 272 passed/6 skipped、Task 12 浏览器 8/8；绿色顺序测试不能替代该并发反例，发布门槛不变。

- 2026-10-26 Task 12 验收更新：7d49c814532fefcb31c1e340b5735487766fe9fc 的邀请分页代次修复通过，见[Task 12 验收记录](../audits/2026-10-26-task-12-accepted.md)。主审后端 930 passed、前端 Node 118 passed、八文件兼容 272 passed/6 skipped、Task 12 浏览器 8/8；四项新交错测试在旧组件均失败、当前均通过。允许按原计划 Task 13 继续本地研发，完成后停止审计、不进入 Task 14。Task 8 真实发布门槛和非聊天 503 范围保持不变。

- 2026-10-26 Task 13 首轮审计更新：8a98040f6cdd3b8c5bb4c0641564236397fd7c24 暂不通过，见[Task 13 R1 四项返工单](../audits/2026-10-26-task-13-review-round-1.md)。主审后端 962 passed/3 skipped，兼容与文档 282 passed/6 skipped；临时真实消费者/Git/junction 探针确认迁移落点、根外备份、Git 历史丢失及空目标规则不一致。仅定点修复，不进入 Task 14，正式发布门槛保持未通过。

- 2026-10-26 Task 13 R1 复审更新：f297a404ecdb732c3d1e304b7af7e357c572fe80 暂不通过，见[Task 13 R2 单项返工单](../audits/2026-10-26-task-13-review-round-2.md)。前三项已关闭，普通空目录路径通过，但主审和独立审查者均确认预检后 owner junction 可使最终提交写根外；仅修此项，不进入 Task 14。主审后端 970 passed/4 skipped，兼容与文档 282 passed/6 skipped；POSIX 执行位未实测，Task 8 真实发布门槛保持原状态。

- 2026-10-26 Task 13 验收更新：5bebd87c65f723e3be656596a71acf1af9bc5948 的提交前归属复核通过，见[Task 13 验收记录](../audits/2026-10-26-task-13-accepted.md)。主审后端 972 passed/4 skipped，兼容与文档 282 passed/6 skipped；原 junction 反例旧提交成功写根外、当前明确 400 且无根外写入。允许进入 Task 14 本地双用户端到端及发布证据检查器研发，正式环境缺前提或 skip/xfail 不得计发布通过；POSIX 与 Task 8 未通过项不变，完成后停止等待审计。

- 2026-10-26 Task 14 首轮审计更新：ced2a6464b46fa2afef25016832ad00e40b9e04f 暂不通过，见[Task 14 R1 五项返工单](../audits/2026-10-26-task-14-review-round-1.md)。主审后端 987 passed/4 skipped，含 runtime/document 的兼容回归 289 passed/20 skipped；已复现 runner 未捕获输出、必选门槛放行 skip/xfail、正式必需证据遗漏、B 失败及缓存值污染两项 live 假绿。仅定点修复验收代码，不扩大产品业务，不以代码测试或本机 Docker 结果宣布正式发布通过。

- 2026-10-26 Task 14 R1 复审更新：35d2c2eae563bc82bb2ec760456142d08f406e96 暂不通过，见[Task 14 R2 单项返工单](../audits/2026-10-26-task-14-review-round-2.md)。原输出捕获、缺失正式证据 blockers、B 终态及缓存值断言已关闭；非 live 漏选和全文首个匹配误读真实 skip 仍可使单项 gate/egress verified 假绿。整体当前仍被固定证据项 blocked；主审后端 998 passed/4 skipped、浏览器 12 passed、兼容与文档 289 passed/20 skipped。仅修验收分类，不扩大产品业务，不宣称正式发布。

- 2026-10-26 Task 14 本地代码验收更新：290d94a68b9a4c1e8e3f627b5c96fcbdb8f11f1b 通过，见[Task 14 验收记录](../audits/2026-10-26-task-14-accepted.md)。结果统计/所有必选门槛漏选的两条真实 pytest 反例已关闭；主审后端 1004 passed/4 skipped。三个正式证据 blockers 保持有效，即使已列门槛全绿整体仍 blocked/exit 1。本地研发范围代码审计收尾，原生部署、真实 Harness/供应商、容器沙箱等未通过项和非聊天/产物 503 不变，不得宣称正式发布。
