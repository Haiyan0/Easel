# 原 Web 修复 Task 1 R1：可选 status 解析不得破坏 RPC reader

基线：`9f95e5dfea96312550eecd3101ed61f7188ab68b`。
继续原隔离 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。
仅一项 T1-R1-01 / P2；依据 [首轮审计](../audits/2026-10-29-original-web-task-01-review-round-1.md)。

## 修改范围

仅 `easel/harnesses/rpc.py` 的诊断字段解析，以及 `tests/multiuser/test_run_diagnostics.py` 和必要的假 ACP fixture；可补充本任务实施证据。已通过阶段/安全文本/stderr/Run 行为保持，不改 base/acp/runs 其它逻辑，不修改模型、UI、主审文件或用户运行资源。

## 反例与最小目标

- error.data={status:"²",code:"rate_limited"} 或 status 为 4301 位数字字符串；当前 int 解析抛异常导致 reader 退出、后续正常 id2 响应丢失。
- 无效 status 应当被忽略，继续检查其它结构化 status/code/error；结构化 code 存在时正常分类，无证据才 unknown。
- 接受合法 ASCII 状态字符串（例如 "429"）；长度/字符/范围不合法都不能抛到 reader。不要先将任意长度数字转换为 int 再校验。
- 真实 RPC._read 收到该错误帧后必须继续消费后续正常响应；不能只给 classify_error_evidence 写镜像式单测。

## 验证

先确定性红测，再最小修复、定向绿与原四文件回归：

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser/test_run_diagnostics.py tests/multiuser/test_runs.py tests/test_agent_harnesses.py tests/test_rpc_client_pressure.py -q
git diff --check
```

主审复现脚本在 root 的 `docs/evidence/original-web-task-01-review/probe_status.py`；它用于比较两个源码目录，后续实施时不要修改主审探针。新测试在本 worktree 完成。

使用假协议帧/假子进程即可，不构建镜像、不需要 Docker/真实 Harness/模型调用。禁止触碰用户容器、卷、socket、Key 和邀请；不读原失败消息、不重发 prompt。

保护 root 和本 worktree 的主审说明；显式文件提交，交付父/新 SHA、单项红绿、后续正常响应证据、原回归、清理情况。提交后停止等复审，不进入 Task 2。
