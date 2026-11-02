# 原 Web 修复 Task 1 首轮审计：仅一项诊断解析回归需返工

日期：2026-10-29（Asia/Shanghai）。
父基线：`1a8811096c7c86bb9079a5c88b92cd225d1907b1`。
审查提交：`9f95e5dfea96312550eecd3101ed61f7188ab68b`。
隔离 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。

## 裁决

Task 1 暂不通过；仅修 T1-R1-01 一项 P2，不进入 Task 2。阶段标记、固定词表错误、旧 HarnessError 兼容、stderr 有界排空和既有 Run 语义已通过本轮所查范围，不能因为单项返工重做其它部分。

已核对提交仅 4 个生产文件、2 个测试/fixture、6 个实施证据文件，worktree 干净；没有主审文档或用户运维文件混入该提交。root 工作区仍在原基线，未 cherry-pick、未切换运行实例。

## T1-R1-01 / P2：无效 status 字段导致整个 RPC reader 退出

位置：实施 worktree 的 `easel/harnesses/rpc.py:48`–`:49`（`_status_value`），关联 `:318` 调用分类器及 `:325`–`:330` 的 reader 失败收尾。

`value.isdigit()` 不保证 `int(value)` 成功。例如上游 JSON-RPC error.data.status 为字符串 `²`；或者为 4301 位数字字符串，指定 Agent Python 的 int 转换均抛 ValueError。分类器没有就地忽略无效字段，异常冒出到 `_read` 外层，整个 reader 提前结束，所有 pending 调用都被替换为“Agent 协议读取失败”。

该字段是可选诊断证据，解析失败不能改变基础 RPC 消费语义。即使同一个 data 已有有效 `code=rate_limited`，也会被丢弃，用户看见“未获得可分类证据”。这不是要求猜测原始 message，而是正确处理已有结构化字段。

### 主审确定性反例

分别加载 root 基线与实施 worktree 模块，对真实 RPC._read 注入内存 StreamReader 的两帧：id1 为合法 JSON-RPC error，data 包含 status 和 code=rate_limited；id2 为正常 result={ok:true}。仅诊断字段不同，协议帧本身合法。

```text
基线，status="²" 或 4301 位数字：
  id1=Agent 协议错误：fixture error
  id2={ok:true}

实施提交，status="429"：
  id1=Agent 协议错误：fixture error，category=rate_limited
  id2={ok:true}

实施提交，status="²" 或 4301 位数字：
  id1=Agent 协议读取失败，category=None
  id2=Agent 协议读取失败，category=None
```

主审探针：`docs/evidence/original-web-task-01-review/probe_status.py`，指定 Agent Python 执行 exit 0。探针只用内存帧及假环境，不读秘密、不启动模型/容器。首次运行遇 Windows stdout 编码不一致，修正探针 UTF-8 后完整复跑得出上述结果；不是产品失败。

独立复核另用临时真实假 ACP 子进程确认：status="429" 且 code=rate_limited 得到 prompt/rate_limited；status="²" 且相同 code 得到 prompt/unknown/agent_failed。假子进程仅继承基本 OS 启动变量，临时目录已清理。

### 最小修复与回归要求

- status 字符串先做有限长度、ASCII 十进制和 100..599 范围检查；转换不成功的字段无效，不抛出诊断解析异常。
- 无效 status 不掩盖后续可用的 status 字段或已知 code/error；都无效才回 unknown。
- 保持 reader 消费后续正常帧，并保持原 pending/取消/清理语义；不能捕获后简单退出 reader 来“修复”。
- 补 Unicode digit、超长数字、正常 429、无有效证据 fallback，以及错误帧后正常响应的行为红绿测试。
- 本项不要求改模型/profile/UI、扩大 stderr 输出、调整状态/租约协议或新增服务。

## 已通过部分与新验证结果

主审在实施 worktree 新运行：

```text
Agent Python -m pytest tests/multiuser/test_run_diagnostics.py tests/multiuser/test_runs.py tests/test_agent_harnesses.py tests/test_rpc_client_pressure.py -q
147 passed, 4 skipped in 26.26s
git diff 1a881109 9f95e5d --check：退出 0
```

独立 worker 复核新测 + runs：46 passed（9.57 秒），与主审范围重叠，不相加。真实临时 RunManager/SQLite 探针确认：非法诊断字段类型和 bool status 回 unknown，DomainError 原 reason 保持、超时 reason=timeout，只有一个字符串 error；秘密 message 不进入事件/日志。

RPC 新 stderr 缓冲固定 4096 字节、按 8192 块持续排空，原关闭/取消清理保留；本轮未确认其它取消或信息泄露回归。不将只能人工动态构造的异常类名当作已证实生产泄露。

作者的红 15 failed/2 passed、相关 35 passed/2 skipped 和真实 TCP 假 ACP 2 passed 作为作者证据保留；主审本轮不重复相加、不把 fixture 称真实 Harness/供应商。旧线上失败的直接触发点仍是历史不可追溯，模型分叉留 Task 2。

## 后续与运行边界

仅执行 [单项返工单](../tasks/2026-10-29-original-web-task-01-rework-r1.md)，基于当前实施提交修复。用户现有 control/worker/gateway/sidecar 保持原样，主审和独立复核未操作 Docker、真实凭证或运行实例。交付后等复审，不自动进入 Task 2。
