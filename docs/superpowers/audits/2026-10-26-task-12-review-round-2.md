# Task 12 R1 复审与 R2 单项返工

日期：2026-10-26（Asia/Shanghai）。返工父基线：359fcba90b712f43e81899d982e7a0c864e371e5；复审提交：79fbe371179ad61c2a5058c03afd198f182c546a。

结论：暂不通过，仅余下面一项 P2。原 R1-01、R1-03 已关闭，R1-02 顺序分页成立，但刷新与旧分页交错仍不成立。不进入 Task 13。

依据：[原交接单](../tasks/2026-10-25-task-12-control-settings.md)、[R1 三项返工单](2026-10-26-task-12-review-round-1.md)。主审和独立审查者未修改产品代码，未调用真实供应商、Docker 或开发者账号。

## 已关闭与独立验证

- R1-01：公共响应提供个人原始 personal_model/personal_options；编辑与保存继续区分默认跟随和显式固定，不将合并后的管理员参数写为个人覆盖。HTTP、组件及真实浏览器的本轮回归通过。
- R1-03：公开供应商 schema 不含凭证值；个人页面提交多字段对象、部分输入拒绝、全空复用及轮换边界的本轮回归通过。没有扩展供应商 adapter 或宣称真实调用通过。
- tests/multiuser：930 passed、3 skipped、2 warnings，82.90 秒（两项既有 websockets 弃用警告）。
- 全部前端 Node：114 passed。
- 原交接单八文件兼容回归：272 passed、6 skipped，22.09 秒。
- npm run build、npm run lint：退出 0；构建保留既有大于 500 kB 提示。
- browser_task12.py：8/8 true、FAILURES 为空、退出 0；Chromium 151.0.7922.34，临时目录清理结果 true。
- git diff 359fcba HEAD --check：退出 0。

本轮未独立重跑 Task 11 六个浏览器脚本；报告中的这些结果属于执行方证据。上述绿色测试不覆盖下面的并发反例。

## T12-R2-01（P2）：旧邀请分页响应可覆盖刷新后的游标

定位：[AdminSettingsPage.tsx](../../../web/frontend/src/components/AdminSettingsPage.tsx) 约 267 行的 loadMoreInvites，以及同文件约 283–290 行 refreshInvites。

loadMoreInvites 捕获并检查 loadSeq，但创建/撤销邀请调用的 refreshInvites 只检查 mounted，没有使旧分页请求失效。加载更多期间创建邀请按钮仍可用，因而正常操作可以触发交错。

主审独立复跑实际组件回调探针，确定性顺序如下：

1. 初始 51 条旧邀请 invite-0 到 invite-50，第一页 old0..49，nextOffset=50。
2. 点击加载更多，挂起旧 offset=50 请求。
3. 创建邀请成功，刷新先返回 new + old0..48，nextOffset=50。
4. 旧分页随后返回 old50、nextOffset=null，仍被追加并覆盖新游标。
5. 最终 old49 不可见、old50 可见，加载更多按钮消失；当前列表无法继续找到或撤销漏掉的邀请，必须重新读取恢复。

实际输出：

```text
during load: create invite disabled= false
after stale more: boundary49 visible= false old50 visible= true more= false
REPRODUCED: missing boundary invitation with no continuation
```

探针使用仓库实际组件和现有测试 hook harness，三个树状态断言通过，不是只测试纯 helper。临时探针不作为交付依赖，执行方应在正式测试中重建上述交错。

### 仅此项的返工要求

邀请列表的第一页刷新和继续分页必须共享有效代次/请求归属。刷新使此前旧分页结果失效；过时的成功、失败和 finally 不得覆盖新列表、游标、错误提示或新请求的加载状态。具体采用列表代次、取消或串行方式由执行方选择，避免扩大为整个管理页面重写。

先补当前 79fbe37 会失败的实际组件测试：延迟旧 offset=50 → 创建邀请并刷新第一页 → 放行旧页；随后使用新游标继续分页，所有有效邀请都可达且无重复。补撤销触发刷新同类交错、旧请求失败/收尾不干扰新请求的针对性回归，保持后页成员目标选择稳定。真实浏览器增加至少一次该交错，不能只测顺序加载。

保留已关闭的个人原值、多字段凭证、管理员事务授权、配置 CAS/AAD 及聊天树/SSE 边界；不修改 schema，不扩大业务范围。

### 可复制给执行 Agent

```text
从 79fbe371179ad61c2a5058c03afd198f182c546a 开始，仅修复本审计单 T12-R2-01。
先写实际组件并发反例跑红，再修邀请刷新/分页的请求代次与旧请求收尾。
运行全部前端 Node、tests/multiuser、Task 12 交接单八文件兼容回归、build/lint，
以及补强的 Task 12 真实浏览器脚本；如修改共享身份/聊天代码，再按原交接单运行受影响的 Task 11 浏览器回归。
显式路径提交实现和测试；不得夹带主审 plan/spec、审计或任务单。
交付父/新 SHA、文件清单、确定性交错红绿、实际测试结果与未执行项。
完成后停止等待复审，不进入 Task 13。Task 8 正式发布门槛保持原状态。
```
