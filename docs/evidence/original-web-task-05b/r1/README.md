# Task 5B R1 返工证据：首次存储边界与热点旧缓存语义

日期：2026-10-30。父基线：`38955b4c8707e07e51d6153abe6794dbb9a67ed1`。
实施 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。
仅修复首轮审计两项 P2；不进入 5C/6，不操作用户实例。

## T5B-R1-01：缺失文件也验证实际存储根

修复：

- 新增 `_real_directory`/`_workbench_root`：读取、首次创建、临时文件目录和
  calendar context 共用同一 outputs 根边界。
- 即使 `_ideas.json`/`_schedule.json` 尚不存在，也先验证 `OUTPUTS_DIR`
  的父目录和真实解析路径；outputs 不存在时，只有父目录真实且解析一致才
  允许创建。
- 目标文件不存在不再直接返回未经父链验证的路径；首次写入的临时文件只
  创建在验证后的真实 outputs 根内。
- 正常 outputs 目录缺失（父目录存在）仍允许首次建目录/建数据文件。

红测（修复前实际运行）：

```text
pytest tests/multiuser/test_original_web_api.py \
  -q -k "outputs_junction or missing_outputs_directory"
=> test_workbench_outputs_junction_is_rejected_before_first_write
   FAILED: DID NOT RAISE HTTPException
```

修复后同一测试：GET/POST/context 均拒绝，根外目录无
`_ideas.json`/`_schedule.json`/临时文件；`test_workbench_missing_outputs_
directory_still_allows_first_create` 通过。

主审原始 junction 探针复跑：`LINK_KIND=junction` 后首次读取直接抛出受控
`HTTPException 500`（探针未捕获该预期拒绝，因此进程退出 1），未继续执行
创建；根外临时目录被清理。输出见 `probe-storage-root.txt`。本组 pytest
测试进一步覆盖了 GET/POST/context 三条路径并断言根外文件不存在。

## T5B-R1-02：旧热点不能冒充刚刷新

修复：

- 缓存仍在 300 秒内：返回缓存，`ok=true`，`updated` 使用实际缓存时间。
- 新抓取成功：`ok=true`，`updated` 使用本次成功取数时间。
- 过期缓存且上游失败：保留旧条目，但 `ok=false`、`stale=true`、
  `cached_at=<实际缓存时间>`；顶层 `updated` 取实际数据时间（最大值），
  并给出 `stale=true`/`failed_platforms`，绝不把请求时间写成更新时间。
- 无缓存且失败：`ok=false`、空条目、`updated=0`，不显示伪更新时间。
- `WorkspaceData` 保留 `stale`/`cached_at`/`failed_platforms`。
- `TrendsPage`：显示“刷新失败，显示缓存 HH:MM”，平台列显示“旧数据”，
  不再显示“HH:MM 更新”。
- `DashboardPage`：热点卡片显示“旧数据”/刷新失败说明，平台组标旧数据；
  不额外发起请求。

主审后端探针（实际响应）：

```text
UPSTREAM_FETCH_RESULT=EMPTY_FAILURE
RESPONSE_UPDATED_AGE_SECONDS=3600
RETURNED_OLD_TITLE=True
GROUP_OK=False
STALE_MARKER_PRESENT=True
GROUP_FIELDS=cached_at,items,label,ok,platform,stale
```

主审前端原组件探针（传入实际修复后响应）：

```text
hasOldItem=true
hasFailureOrStale=true
misleadingFreshness=false
rendered="...刷新失败，显示缓存 13:37...旧数据...ONE-HOUR-OLD-SENTINEL..."
```

本组浏览器新增受控旧缓存场景：上游固定失败、缓存一小时前，实际 control
响应驱动原页面；断言旧标题、刷新失败/旧数据文案存在且无“更新”。3 次均通过。

## 绿测

| 项目 | 结果 |
|---|---|
| `tests/multiuser` 全量 | **1604 passed, 6 skipped** |
| 全部 frontend Node | **145 passed, 0 failed** |
| `tests/test_trends.py` + manual publish + bugfix（排除无关项） | **63 passed, 1 deselected** |
| control static + persona security | **22 passed** |
| build | 成功，`dist/assets/index-DQjpBhyr.js` |
| lint | 退出 0 |
| 5B 浏览器（含旧缓存场景） | **5/5，3 次 `FAILURES=[]`** |
| 5A 浏览器回归 | **4/4，3 次 `FAILURES=[]`** |
| Task 3 浏览器回归 | **33/33，3 次 `FAILURES=[]`** |
| Task 4 浏览器回归 | **8/8，3 次 `FAILURES=[]`** |
| 隔离容器链路 | **`TASK5B_CONTAINER_OK=True`，`CLEANUP_ERRORS=[]`** |

容器最终镜像：

- control `easel-control:task5b-r1-20261030`
  `sha256:d91b193e19cba2cb483100ca4536e8bf1f34a0e45e54c8f5ef9e4f911e99bde2`
- gateway `easel-gateway:task5b-r1-20261030`
  `sha256:28563b83ba9b2e676949f358326547f55ac470f0447648f9972b153e81c6b2e9`
- worker `easel-worker-execution:task5b-r1-20261030`
  `sha256:8bf1553e4aae00dd5b1c137ae4748e5d96680badd40cdda5c52c4997a454fba3`

容器链路仍覆盖 5A 基础链路、选题/日历 CRUD、context、未知 id、worker 重启
后持久化、删除和匿名 401；未重建 Harness/Chromium。

## 未执行与清理

- 热点公网来源仍 **not-run**；本组使用受控来源，不冒充公网已通过。
- Windows 文件 symlink 仍受环境权限限制，not-run；本轮使用真实目录
  junction 验证 outputs 根边界，未用 mock/hardlink 冒充。
- 真实模型、账号分析/发布、完整 Compose、生产 Linux 仍属 5C/后续，未执行。
- 5A/Task 3/Task 4 历史截图运行后已恢复；5B R1 截图输出本组目录。
- 用户容器/卷/socket、Key/Cookie/邀请未操作；未重放 prompt、未付费、
  未发布、未全局 prune、未删除历史网络。
- 隔离测试容器/网络/卷已按标签归属清理，无残留。

## 文件

生产：

- `web/app.py`：首次/读取/临时文件/context 的 outputs 根边界；热点缓存
  stale/cached_at/updated 语义。
- `web/frontend/src/lib/api.ts`：`TrendGroup` stale/cached_at 类型。
- `web/frontend/src/lib/workspaceData.ts`：trend 解析保留 stale 元数据。
- `web/frontend/src/components/TrendsPage.tsx`：旧数据/刷新失败展示。
- `web/frontend/src/components/DashboardPage.tsx`：概览旧数据提示。

测试：

- 修改 `tests/multiuser/test_original_web_api.py`：junction 首次写入与
  合法缺失 outputs 首次创建。
- 修改 `tests/test_trends.py`：旧缓存 stale 语义与新鲜/有效缓存对照。
- 修改 `tests/frontend/workspaceData.test.mjs`：stale 字段解析。
- 修改 `tests/frontend/browser_original_web_task05b.py`：受控旧缓存浏览器场景。

证据：本目录 `probe-*.txt`、`trends-stale-response.json`、
`browser-run1..3.txt`、`task5a-run1..3.txt`、`task3-run1..3.txt`、
`task4-run1..3.txt`、`multiuser.txt`、`node-final.txt`、
`build-final.txt`、`lint-final.txt`、`worker-storage.txt`、
`static-persona.txt`、`container.txt`、`images.txt`、`*-build.txt`。
