# Task 6 首轮审计：需要返工 R1

- 审计基线：5497e40f7144d198d937e1a9006d3eebb754873c。
- 被审提交：7f1755919e2bf89bbe0265b27dc23e20c26f59eb。
- 结论：三项 P2 阻塞，暂不进入 Task 7。
- 本轮仅审计和出具返工指令，未修改产品代码、暂存或提交文件。

## T6-R1-01 [P2] 接纳结果未约束真实执行，重复快照仍启动 Harness

位置：`web/app.py:80` 的 `_load_worker_runtime`、启动赋值及 `_run_harness_chat`。

接纳表能正确返回 duplicate=True，但生产入口只把结果保存到 `_WORKER_ADMISSION`，后续执行不读取该结果。当前并发测试中的“仅执行一次”由测试代码自行判断 duplicate 实现，不能证明真实入口有执行门禁。

独立复现使用真实 ASGI `/api/chat`、合法 worker 内部凭证和计数 fake adapter；不调用供应商。相同签名文件、同一接纳库，重复加载 worker runtime，再分别请求不同 session：

```text
startup0 duplicate=false http=200 fake_count=1
startup1 duplicate=true  http=200 fake_count=2
```

同一个 workspace/run/nonce 因而能再次启动适配器，与任务要求的持久化原子接纳约束不符。

修复要求：在实际执行及其副作用之前接入持久化原子执行认领。相同 run/nonce 的重复请求、重载及并发只能读取原状态，不能再次调用适配器。不能仅在启动时检查一个进程内布尔值。无需提前实现完整 relay，但本任务要求的最小执行门禁必须生效。

验收：真实 HTTP/运行入口配合 fake adapter，覆盖同进程重复、重载后重复、两个独立连接并发；实际适配器调用计数只能为 1。不同 nonce/内容冲突保留 409，正常首次执行和原有会话续接测试保持通过。

## T6-R1-02 [P2] 快照只在启动时校验时效，过期后仍能开始新执行

位置：`web/app.py:911` 的 `_managed_harness_settings`、`web/app.py:3059` 的 `_run_harness_chat`。

快照启动验签后被缓存。真实新执行路径使用缓存的 agent/routes/run 信息，但不再次检查 expires_at。延续上述 HTTP 探针，把业务时间推进到快照到期之后，再请求一个新 session：

```text
after_expiry http=200 fake_count=3 all_same_run=true
```

签名虽有到期字段，实际执行授权却能无限延续。

修复要求：在新执行的实际认领/启动边界校验快照仍有效且绑定正确，再允许进入 Harness。到期应明确拒绝，且不得启动适配器或产生执行副作用；已在有效期内合法启动的执行不应仅因快照到期被强行中断。与重复接纳门禁一起设计，但分别验证，避免仅靠 duplicate 拒绝掩盖到期校验缺失。

验收：启动时有效、实际首次执行前已到期的快照必须拒绝且调用次数为 0；有效期内首次调用成功；已接纳的合法在途执行跨过到期点仍能正常完成。

## T6-R1-03 [P2] 脚本输出路径吞掉 worker 配置错误并回退

位置：`skills/shared/scripts/output_paths.py` 第 11 行、`skills/shared/scripts/runtime_paths.py` 第 42 行。

`_discover_root` 在 worker 模式下捕获 helper 的 RuntimeError 后继续执行旧的 EASEL_ROOT/源码扫描逻辑，helper 导入失败也继续回退。另外，脚本侧 helper 没有主 worker resolver 中的代码根与可写根重叠、EASEL_ROOT 别名一致性校验。

独立只读探针结果：worker 模式仅设置 EASEL_ROOT、缺少四个显式根时，直接调用 resolve_runtime_paths 抛错，但执行 output_paths 模块仍成功把 PROJECT_ROOT 设为该 EASEL_ROOT。提供四个根但令 code_root 等于 workspace_root 时，脚本 helper 也接受。调用方由此能绕过本任务的失败关闭和只读代码根约束；此探针未写文件。

修复要求：worker 下导入失败、缺项或非法路径直接失败，不执行 local 回退；脚本 helper 与主 resolver 的 worker 路径约束保持一致，包括解析后的双向重叠检查和 EASEL_ROOT 一致性。保留 local 原有解析行为。

验收：通过实际 output_paths 导入/脚本入口覆盖缺根、相对路径、非法别名、code/write 根相等或相互包含、helper 不可用等情况；全部在创建目录/文件之前拒绝。合法分离根使用 workspace 输出，local 回归通过。

## 本轮独立验证

使用 C:\Users\hyh\anaconda3\envs\Agent\python.exe：

- tests/multiuser：453 passed，26.34 秒。
- 七文件指定回归：256 passed、20 skipped，20.10 秒。文件为 test_harness_workspace、test_agent_harnesses、test_runtime_env、test_local_write_guard、test_web_security、test_web_model_config_parity、test_settings_stale_mirror。
- tests/test_output_paths.py：8 passed。
- git diff 5497e40 HEAD --check：退出 0。
- 上述真实 HTTP fake-adapter 探针及脚本路径探针独立复现成功。

现有测试通过不覆盖上述真实入口缺口。签名验证、环境允许列表、ACP 受管命令接入、RPC workspace 命名空间及 token 轮换续接已有实现与回归证据，本轮不要求重做。

Linux/容器只读根、真实 DSH/供应商、浏览器、完整 relay、前端与全仓门槛未执行，不以 mock 替代；它们不属于本轮新增阻塞。

## 可复制给执行 Agent 的指令

基于 7f1755919e2bf89bbe0265b27dc23e20c26f59eb，仅修复 T6-R1-01、T6-R1-02、T6-R1-03。先补能在当前提交失败的真实入口测试，再做最小实现；不要只测试 helper 后由测试代码模拟门禁。不要进入 Task 7，不改历史迁移，不夹带审计/任务文档或其他工作。若出现工作树变更先检查归属，显式路径提交，勿覆盖其他改动。

交付父/新 SHA、文件清单、三项红绿证据、真实入口调用次数与无副作用证据；运行 tests/multiuser、上文七文件回归和 tests/test_output_paths.py，并运行基线到新提交的 git diff --check。未执行的门槛如实记录。完成后停止，等待复审。
