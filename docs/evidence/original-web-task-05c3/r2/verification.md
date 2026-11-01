# Task 5C3 R2 验收证据：区分刷新恢复与显式改素材/画像

父基线：`40fca7c4551db82aa782ef2db466080421337ed2`
实现提交：`6b54e49e5dce99ec1fe4b2b3f4ed5703eb357da5`
证据提交：本目录所在提交（`git log -1 -- docs/evidence/original-web-task-05c3/r2/verification.md`）
实施 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`
日期：2026-11-02
依据：[R2 复审](../../../superpowers/audits/2026-11-02-original-web-task-05c3-review-round-2.md)、[R2 执行单](../../../superpowers/tasks/2026-11-02-original-web-task-05c3-rework-r2.md)

本文件只记录 T5C3-R2-01 的返工证据。原 5C3 的 11 组浏览器结论、历史截图与
R1 后端/部署证据保持不变；本轮新截图写入 `browser/`，未覆盖 `../browser/`。

## 1. 修复摘要

- `web/frontend/src/lib/publishIntent.ts` 新增纯函数 `decidePublishIntent`：
  - 完整指纹一致：复用原 `requestId` 与落盘 payload。
  - 标题/正文/标签变化：另一篇稿，新建请求。
  - 媒体/画像不同：只有草稿从未保存过该项、且本页用户没有编辑过时，才视为
    刷新/切身份丢失并恢复原 payload；否则按当前可见选择新建请求。
  - 画像未随草稿保存但应用级已选非空且与旧稿不同：按显式切换处理，不回填
    旧画像。
- `store.ts`：
  - `PublishDraft` 增加可选 `media`/`persona` 并做读取校验；旧版草稿字段保持
    `undefined`，以区分“刷新丢失”和“用户主动清空”。
  - 人工审核页只保存文案字段时，保留自动发布页已写入的媒体/画像来源。
- `PublishPage.tsx`：
  - 草稿持久化媒体/画像，刷新/身份返回后可见选择直接恢复，不再出现
    “页面显示空、实际提交旧图”。
  - 用 `mediaEdited`/`personaEdited` 跟踪本页显式编辑；显式改稿时替换旧意图
    并使用新 `requestId`。
  - 确认框列出每个平台实际提交的素材与画像；旧意图尚未确认而本次要换稿时，
    明确提示旧请求可能已被接纳，用户取消则不发任何请求。
  - 刷新/切身份丢失选择且用户未编辑时，仍复用落盘 payload 与原 `requestId`；
    迟到回包、跨身份隔离与同 ID 输入绑定逻辑未改。

## 2. 红灯

命令：

```powershell
$env:EASEL_5C3_EVIDENCE = 'docs/evidence/original-web-task-05c3/r2/browser-red'
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' tests/frontend/browser_original_web_task05c3.py
```

旧构建（`index-CfTWRFga.js`，SHA256 `6f7123fc…`）结果见
[browser-red.log](browser-red.log)：11 组原有场景通过，新增两组失败，失败即
本轮缺陷：

- `explicit-media-edit-after-lost-ack`：首次 POST 在客户端截断且不转发，
  `posts[0].media=['project/cover.png']`、后台执行 0 次；取消 cover.png 后
  再次发布，旧代码没有显示“需附带图片/视频”，而是复用旧 payload 继续提交
  cover.png，等待 `.pv-pubstate.fail` 超时。
- `explicit-media-edit-survives-refresh`：改选 other.png 后刷新，草稿未保存
  媒体，页面可见选择为 `[]`，断言 `selected == ['project/other.png']` 失败。

Node 红灯：新增 `publishIntentDecision.test.mjs` 因模块不存在而失败；
`publishDraftScope.test.mjs` 的“省略字段时保留旧选择”和“损坏字段丢弃”两项
失败。

## 3. 绿灯

### Node 与构建

```powershell
node --test tests/frontend/*.test.mjs
# 190 passed、0 failed（含新增意图判定 13 项、草稿持久化 4 项）

npm run build   # BUILD_EXIT:0
npm run lint    # LINT_EXIT:0
```

输出见 [node-tests.txt](node-tests.txt)、[build.txt](build.txt)、
[lint.txt](lint.txt)。产物 `dist/assets/index-BreoCku2.js`，
SHA256 `37529fe5ffd1d78bb905f03afac02df112230ba97c8cb2344e999fa6ddf9ca2f`。

### 本组浏览器矩阵

```powershell
$env:EASEL_5C3_EVIDENCE = 'docs/evidence/original-web-task-05c3/r2/browser'
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' tests/frontend/browser_original_web_task05c3.py
# TASK5C3_BROWSER_OK True；13/13 场景 true，FAILURES=[]，退出 0
```

完整输出见 [browser-matrix.txt](browser-matrix.txt)。新增两组场景的断言：

- 首次 POST 在客户端截断且不转发，`fake_publisher_executions=0`。
- 显式取消 cover.png 后直接发布：不产生第二次 POST，卡片显示
  “需附带图片/视频”；确认框包含“旧发布请求/重复发布”提示。
- 明确改选 other.png 后发布：第二次 POST 的 `media` 等于可见选择
  `['project/other.png']`；`requestId` 与第一次不同；随后状态查询只使用
  新 `requestId`；受控 publisher 只执行 1 次，发布完成的 Run 数 +1。
- 改选后刷新：草稿恢复可见 other.png；再次提交的 `media` 仍为
  `['project/other.png']`，使用新 `requestId`，publisher 只执行 1 次。
- 原有 lost-ack-running/done、刷新恢复、stored-payload、explicit-new-intent、
  identity-switch 等场景全部保留并通过。
- 画像边界由 Node 判定测试覆盖：显式切换画像新建请求；旧草稿丢失画像时
  可恢复原 payload；应用级已选非空且与旧稿不同时不得回填旧画像。

截图：[14-explicit-media-edit-after-lost-ack.png](browser/14-explicit-media-edit-after-lost-ack.png)、
[15-explicit-media-edit-refresh.png](browser/15-explicit-media-edit-refresh.png)。

### 主审只读探针复跑

未修改 `docs/evidence/original-web-task-05c3-review/probe_r1_changed_media.py`，
以本 worktree 为 ROOT 复跑，输出见
[probe-r2-changed-media.txt](probe-r2-changed-media.txt)：

```json
{
  "first_request_never_forwarded": true,
  "first_request_media": ["project/cover.png"],
  "visible_media_before_second_submit": ["project/other.png"],
  "second_request_media": ["project/other.png"],
  "same_request_id": false,
  "second_request_matches_visible_selection": true,
  "fake_publisher_executions": 1,
  "temporary_directory_cleaned": true
}
```

即：首次请求未转发；用户改选 B 后第二次请求实际提交 B，而不是旧 A；
新请求使用新 `requestId`；受控执行恰好一次。

## 4. 未运行与边界

- 无后端/部署源码变更；按 R2 执行单，未重复全量 multiuser 665 项、桌面部署、
  control/gateway/worker 容器链或重建 worker/Harness/Chromium。已有 74 组
  页面回归与 R1 后端结论保持有效。
- 未访问真实平台、未真实发布/短信/付费；未改动用户实例、容器/卷/socket/
  Key/Cookie/邀请。
- 未接管或删除历史受阻临时目录，未执行全局 prune。本轮浏览器脚本自动清理
  后残留的 `.test-tmp/browser-5c3-<时间戳>/automated` 已人工删除；主审探针
  临时目录 `temporary_directory_cleaned=true`。
- 运行期 Windows Playwright 退出回调出现既有 `ConnectionResetError` 警告，
  未影响 `TASK5C3_BROWSER_OK True`，不扩为本轮产品阻塞。
- 提交后停止复审，不合并部署，不进入 Task 6。

## 5. 精确文件与 SHA256

| 文件 | 角色 | SHA256 |
| --- | --- | --- |
| `web/frontend/src/lib/publishIntent.ts` | 意图判定纯函数 | `09665bbed6d64ee69e57d2fcfe2fe1e70a66a3d9c785d06a90f38bc8e2f118aa` |
| `web/frontend/src/components/PublishPage.tsx` | 页面来源跟踪/确认框 | `2590d8afde475fdc927205255bdc2a6739e96b899e25ffba23c6368521ea9f1d` |
| `web/frontend/src/lib/store.ts` | 草稿媒体/画像持久化 | `f10690ebaeb4c8ee31ad450e7ae9e6d871bcd86cf1483ee6bf624894c150f729` |
| `tests/frontend/publishIntentDecision.test.mjs` | 意图判定/画像边界 | `ba74bc9e89a31df883967e2db1640d6b481c01b1b8b8a1f682dfeb5e05bc51ec` |
| `tests/frontend/publishDraftScope.test.mjs` | 草稿持久化/旧版兼容 | `cedf691ad636f75e8948610d9c9e308033ebe292cc469446b5d59d15aadc0864` |
| `tests/frontend/browser_original_web_task05c3.py` | 13 组浏览器矩阵 | `1ffe78a41f107102f547bd50c98e3545cf8e425681e4ee803026cb4fbaa109aa` |
| `web/frontend/dist/assets/index-BreoCku2.js` | 本轮构建产物 | `37529fe5ffd1d78bb905f03afac02df112230ba97c8cb2344e999fa6ddf9ca2f` |
