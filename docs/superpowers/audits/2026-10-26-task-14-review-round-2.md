# Task 14 R1 复审与 R2 单项返工

日期：2026-10-26（Asia/Shanghai）。返工父基线：ced2a6464b46fa2afef25016832ad00e40b9e04f；复审提交：35d2c2eae563bc82bb2ec760456142d08f406e96。

结论：暂不通过，仅余 T14-R2-01（P2），属于原 R1-02 的门槛结果分类未完全关闭。原 R1-01/03/04/05 已关闭；固定正式证据阻断项有效，当前整体不会误报 passed。无需重写多用户产品主流程。

依据：[Task 14 R1 五项返工单](2026-10-26-task-14-review-round-1.md)。主审及独立审查者仅运行本次临时 pytest/子进程、受控 transport、Chromium context 和本机回归；未修改产品、未 stage/提交，未运行真实供应商、Docker 或目标宿主安全配置。

## 已关闭与独立验证

- R1-01：execute_gate 捕获 stdout/stderr 并按 UTF-8 文本处理，真实微型 pytest 成功、失败、skip、collection 错误及 runner 参数契约测试包含在本轮全量回归中；原始输出不进入公开报告。
- R1-03：显式纳入真实出口测试与受控代理前提；XFS 实际限额、宿主防火墙矩阵、最终 runtime/真实 DSH 三项仍为 blocked。主审模拟所有已列命令全绿，得到 exit=1/release_status=blocked；这是控制流验证，不是正式环境证据。
- R1-04：主审重放上一轮完整原 live 函数的 B 失败探针，当前在 _collect 明确拒绝；独立审查者另验证 failed/interrupted/cancelled 和 SSE done/Run failed 的不一致均拒绝。
- R1-05：主审重放上一轮真实 Chromium 原 live 函数的缓存值污染探针，当前在 cache_checks 明确拒绝；独立审查确认合法 A/B 页面可通过，B 缓存值/可见正文含 A 身份会失败。
- tests/multiuser：998 passed、4 skipped、2 个既有 websockets 弃用 warning，102.27 秒。
- 真实 Chromium test_browser_profiles_real.py：12 passed、2 个既有依赖弃用 warning，18.24 秒。
- 原八文件兼容回归加 test_runtime_env.py、test_document_paths.py：289 passed、20 skipped，23.18 秒。
- Windows 检查器 --json：退出 1/release_status=failed；live 无 flag：6 skipped；显式 flag 且移除测试环境变量：6 errors，不 skip。

本轮没有重复运行未修改的前端 Node/build，也未运行全仓测试、正式 live/Docker/Linux 门槛。Task 8 未通过项与 control outputs 等非聊天业务 503 状态不变。

## T14-R2-01（P2）：必选门槛仍会接受不完整/错误统计的通过结果

定位：[scripts/check_multiuser_release.py](../../../scripts/check_multiuser_release.py) 约 263–288 行 _count/classify_gate_result，以及 342–377 行 gates 的完整选集配置。

已删除 allow_skips，但结果来源和集合完整性仍有两条可复现假绿路径。主审与独立审查者分别运行真实微型 pytest，保留真实 CompletedProcess，再交给当前 run_release_gates 的实际分类配置；不连接任何真实门槛资源。

### 反例 A：非 live 必选门槛允许 deselected

只有 live_two_users 启用 require_marker_coverage，其余七组未检查 deselected。真实 pytest 仅运行公开允许用例、筛掉私网拒绝用例，摘要为 1 passed, 1 deselected。当前分类：live 失败，egress_live、container/network/topology/browser/sidecar/backend 七组均 pass；evaluate_evidence 还把 egress_proxy 标为 verified。

例如 PYTEST_ADDOPTS 或测试选择配置可使必选拒绝用例漏选。缺失正式测试不应被已执行的少数通过用例替代。原 R1-02 明确要求必选门槛漏选失败，此要求不只适用于 live marker。

### 反例 B：警告正文的 0 skipped 覆盖真实摘要中的 1 skipped

_count 用 re.search 从整个 stdout 取首次匹配。真实临时 pytest 包含：

```python
def test_ok():
    warnings.warn("0 skipped")

def test_skip():
    pytest.skip("mandatory unavailable")
```

实际摘要为 1 passed, 1 skipped, 1 warning，退出 0，但警告详情先出现 0 skipped。当前 _count 返回 skipped=0，全部八组 gate 被分类为 pass。主审实际输出：

```text
ACTUAL_PYTEST_SUMMARY 1 passed, 1 skipped, 1 warning in 0.01s
PARSED_SKIP_COUNT 0
ACTUAL_SKIP_GATE_RESULTS: 全部八组 pass
FILTERED_PYTEST_SUMMARY 1 passed, 1 deselected in 0.01s
FILTERED_EGRESS_EVIDENCE: egress_proxy=verified
```

这不是正式发布已被绕过：三个固定 evidence blockers 仍阻止整体 passed。但单项 gate 和 egress verified 的证据声明已经错误，不能按“任何 skip/漏选均失败”验收。

## 定点修复与验收

- 使用可信的最终测试统计或结构化结果，不从任意警告/错误/用例文本的首个数字匹配推导执行结果。无法确认完整统计时失败关闭，不靠输出中的 passed 字样判成功。
- 所有被选作正式必选证据的 gate 均检查漏选/空集合，包含出口、容器、网络、浏览器和 sidecar。普通回归的平台选择应明确分组，不用宽松分类掩盖必选用例缺失。
- 先补当前版本会失败的真实微型 pytest 回归，覆盖“警告含 0 skipped + 实际 skip”以及“1 passed/1 deselected 的非 live gate”；同类 xfail/xpass/空集合也保持拒绝。
- 验证真实 runner 配置和 evaluate_evidence：不完整/跳过的出口结果不得变 verified；完整通过能正确分类，原始输出仍被捕获，JSON 仍为单个文档，三个固定正式证据阻断项保持有效。
- 不扩展产品业务、环境权限或真实部署范围。没有目标 Linux/真实供应商时如实未执行，不能为满足测试去取消 blocked。

## 可复制给执行 Agent

```text
从 35d2c2eae563bc82bb2ec760456142d08f406e96 开始，仅修复本审计单 T14-R2-01。
先为真实 pytest 统计被警告覆盖、非 live 必选门槛漏选两条反例跑红，再修结果统计与选集完整性校验。
保留已关闭的输出捕获、正式证据 blockers、B done 断言和缓存键值隔离，不扩大产品接口。
运行 tests/multiuser、定向 runner 故障测试、兼容回归、文档路径检查及 git diff --check；未修改浏览器断言则不必重跑整套浏览器。
仅显式提交必要实现/测试/正式说明，不提交主审 plan/spec、审计或任务单。
交付父/新 SHA、逐项真实红绿和未执行门槛后停止等待复审；不宣称正式发布通过。
```
