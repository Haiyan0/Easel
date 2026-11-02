# Task 7 复审通过

日期：2026-10-21（Asia/Shanghai）。接受提交：efdd4edc1dc7e3ae7f21aa12804258e329aaef8a；父提交：bde8e3229b323b6e7786ff568ee575cb5b40c4bb。

结论：Task 7 代码与本地验证通过，允许进入 Task 8。[R2 最后一项阻塞](2026-10-20-task-07-review-round-2.md)关闭；此前状态路径迁移、可执行文档响应限制、产物树越界/环处理、注销互斥与普通异常清理的已关闭结论保持。本轮未发现新的实证阻塞。

## 最后一项核验

_run_browser 用 PlaywrightOwner 托管 sync_playwright，注册包装器的幂等释放回调；keep_open=True 的正常返回、RuntimeError 及 SystemExit 均经过 driver 退出再释放无主 profile 锁。普通显式 close 路径保持关闭完成后释放。

真实 Chromium 新增调用者回归覆盖 keep_open=False/True 与预检异常、SystemExit、成功三种结果；返回同一解释器后可再次获得相同 profile 锁。慢 close 测试验证关闭未完成时竞争者拿不到锁。

独立复审另在真实 _run_browser 的释放回调执行前检查 Windows 进程句柄：RuntimeError、SystemExit(6) 两路径中 driver 与 4 个 Chromium 子进程均已终止，之后锁可重取；成功路径返回 0，driver 已终止，锁可重取。该证据直接验证退出顺序，不仅依赖 mock 的回调顺序。探针仅使用临时 profile/本地页面，没有真实平台登录或发布。

## 主审独立验证

PowerShell，C:\Code\Easel，C:\Users\hyh\anaconda3\envs\Agent\python.exe：

```text
pytest tests/multiuser -q
563 passed, 2 skipped in 36.82s

pytest tests/test_account_stats_login.py tests/test_login_start.py tests/test_output_paths.py tests/test_web_security.py tests/test_local_write_guard.py tests/test_harness_workspace.py tests/test_agent_harnesses.py tests/test_runtime_env.py tests/test_web_model_config_parity.py tests/test_settings_stale_mirror.py -q
291 passed, 20 skipped in 20.02s

pytest tests/integration/multiuser/test_browser_profiles_real.py -q
11 passed, 2 warnings in 18.39s

git diff bde8e32 HEAD --check
退出 0
```

浏览器测试的两个警告为现有 websockets/uvicorn 依赖弃用警告。符号链接创建受 Windows 权限限制的两项仍明确 skipped，不将其当作通过；Windows junction 的边界测试实际运行。

## 验收边界和后续

本轮未独立复跑全仓；不把此前执行 Agent 提供的全仓基线失败对照标为本轮验证。真实平台账号登录/发布、真实 DSH/供应商、Linux 只读挂载与容器隔离仍未验收。正式多人模式必须继续满足后续容器、网络、配额和端到端门槛。

按已批准 [plan](../plans/2026-10-16-multiuser-workspaces.md) 继续 [Task 8 交接单](../tasks/2026-10-21-task-08-worker-runtime.md)。本次只新增审计/任务文档，未修改产品代码或提交；文档不夹带进产品实现提交。
