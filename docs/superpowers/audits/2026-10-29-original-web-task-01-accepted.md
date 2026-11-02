# 原 Web 修复 Task 1 验收：失败诊断与 R1 reader 回归通过

日期：2026-10-29（Asia/Shanghai）。
原基线：`1a8811096c7c86bb9079a5c88b92cd225d1907b1`。
首轮实施：`9f95e5dfea96312550eecd3101ed61f7188ab68b`。
最终验收：`bcf001a0ff5a9ba7242ba6f686a9ce7eb341081d`。
worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。

## 裁决与范围

Task 1 诊断能力验收通过，T1-R1-01 原反例关闭；本次聚焦复审未确认新增阻塞。允许按 [Task 2 执行单](../tasks/2026-10-29-original-web-task-02-managed-model.md)继续模型/Harness 链路修复，不开始前端任务、不替换用户实例。

通过范围是安全阶段/类别/状态码/Run 关联、有界 stderr 排空、旧异常构造与原 Run/RPC 行为兼容；不表示真实聊天已修好。旧用户失败的 stderr 先前已丢失，不能事后伪造直接根因；配置模型分叉仍待 Task 2。

## R1 关闭依据

`easel/harnesses/rpc.py:48` 在转换前限制 exact str、长度不超过 3、ASCII 十进制，再校验范围；Unicode digit 和超长字符串不会进入 int 转换。`:78`–`:92` 对无效/未映射状态继续检查后续 status 字段及已知 code/error。

没有在 reader 外层以捕获后退出的方式掩盖问题。测试真实消费错误响应和随后正常响应，确认 reader 保持运行。R1 生产改动只在 rpc.py 分类器，base/acp/runs 没有扩大修改。

主审未修改旧探针，新运行 `docs/evidence/original-web-task-01-review/probe_status.py`：

```text
SOURCE=implementation
ascii429: category=rate_limited，第二帧 result={ok:true}
unicode_digit: category=rate_limited，第二帧 result={ok:true}
long_digits: category=rate_limited，第二帧 result={ok:true}
NO_RUNTIME_RESOURCES_TOUCHED=True
```

基线 root 仍为 1a881109；探针按目录加载模块，未合入主工作区或修改产品文件。

## 主审新运行

```text
Agent Python -m pytest tests/multiuser/test_run_diagnostics.py tests/multiuser/test_runs.py tests/test_agent_harnesses.py tests/test_rpc_client_pressure.py -q
156 passed, 4 skipped in 29.45s

git diff 9f95e5d bcf001a0 --check：退出 0
```

四项 skip 是既有平台条件，不算真实环境验收通过。独立复核新增 reader 参数化用例：9 passed、17 deselected（2.46 秒）；与主审范围重叠，不相加。另独立矩阵验证 17 个非法字段回退及 6 个有效/未映射状态/code 回退场景，均通过。

R1 提交为 1 个生产文件、测试/fixture 与实施证据，worktree 干净。作者提供的红 7 failed/19 passed 与新测 26 passed 保留为作者红绿记录；主审新跑四文件和原反例支持本次关闭结论。

## 运行边界与后续

主审和独立复核未操作 Docker、真实 Key、供应商、用户 prompt 或既有运行实例；用户 control/worker/gateway/sidecar 保持原样。本轮只修改 root 中的主审文档，未 cherry-pick 或发布诊断镜像。

下一项仅 Task 2，以最终验收提交为父基线，继续隔离 worktree。真实 Harness + 受控 provider 验证与用户真实通道验证分开计数。前端迁回原 Web、登录主题与其它原页面功能尚未开始，不因本项验收宣称整体完成。
