# Task 5A R1 返工证据：配置版本、画像链接边界、流错误收尾

日期：2026-10-30（Asia/Shanghai）。
父基线：`ea3066471811daeb2f51211dea0162578cbfe483`（Task 5A 首轮交付 tip）。
修复提交：`16b634e`（`fix: 修正 Task 5A 配置版本、画像链接与流错误收尾`）。
worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。

## T5A-R1-01 配置版本统一

修复：`WorkspaceApi._revision` 只把 `workspaces.config_version` 用于确认
workspace 存在，实际请求版本改用 `build_workspace_factory(revision=...)`
传入的受管配置 revision；聊天 relay 与文件 API 因此使用同一版本来源。
`revision` 构造函数只接受非负 int。

主审探针修复后：

```text
MANAGED_REVISION=7
FORMAL_RELAY_REVISION=7
FORMAL_WORKSPACE_FACTORY_REVISION=7
WORKSPACE_RECORD_VERSION=0
LIVE_CHAT_WORKER_VERSION=7
ACTIVE_CHAT_FILE_REQUEST=ACCEPTED
AFTER_FILE_WORKER_VERSION=7
PROVISIONER_STOPS=0
PROVISIONER_ENSURES=1
TEMP_CLEANUP=COMPLETE
```

新增红绿测试 `tests/multiuser/test_workspace_revision.py`：

- 正式 `build_relay_factory` + `build_workspace_factory` + 真实 Store/
  RuntimeManager，revision=7、workspace 记录=0；
- 活跃 Run 存在时 personas 请求成功、status 为 ready、worker 保持 7、无 stop；
- Run 结束后再次读取仍为 7、无 stop/重建；
- 先文件后聊天不来回切换版本；
- 未知 workspace 仍 404，不因统一版本放松归属校验。

## T5A-R1-02 画像实际文件边界

修复：

- `easel/persona.py` 新增 `safe_profile_dir` / `safe_profile_file`：解析后
  路径必须与解析前绝对路径一致，且目录必须是 profiles 直接子目录；目录
  junction/symlink、文件 symlink 均不通过。
- `list_personas`、`profile_exists`、`load_profile_text`、Web
  `list_personas`、详情 `api_persona`、`api_persona_files` 全部使用真实
  边界；附加 Markdown 只有安全文件才列出。
- `_persona_file_path` 基于安全目录，拒绝已有文件链接；原子临时文件改用
  画像目录内随机名；删除只接受安全目录，避免沿链接写/删根外材料。

主审探针在修复后于详情处返回受控 `HTTPException 404`（探针未捕获该
预期拒绝），不再打印 sentinel。目录 junction 已在 Windows 实测；同环境
文件 symlink 返回 `WinError 1314`，如实记为 not-run。独立红绿测试
`test_persona_links_cannot_read_or_mutate_outside_profiles` 通过，覆盖
列表/详情/files/保存/删除；正常六维与附加 Markdown 仍由既有
`tests/test_persona_security.py` 覆盖。

## T5A-R1-03 流式错误响应有界收尾

修复：`GatewayClient._raise_for_status` 不再对未消费流直接 `response.json()`。

- workspace 错误只按状态映射，不读取 worker 正文；
- relay 401/403/503 才需要 `gateway_*` 错误码时，使用最多 64KiB 的有界
  `aiter_bytes`；超限立即停止读取；
- 无论成功解析、超限、异常还是取消，`finally` 都调用 shielded close；
  `WorkspaceApi` 的 lease 释放路径继续覆盖异常/取消。

主审探针修复后：

```text
stream=False type=DomainError status=404
stream=True type=DomainError status=404
NO_USER_RUNTIME_TOUCHED=True
```

新增 `tests/multiuser/test_gateway_client_errors.py`：

- 真实 `AsyncByteStream` 404/403/413/429/503：稳定 DomainError、流关闭、
  workspace 错误不读取正文；
- relay 503 `gateway_*` 有界解析并关闭；
- 70KiB 超限错误体仍关闭，不冒充解析成功；
- 流请求 DomainError/CancelledError 均释放 request lease；
- 成功 GET/HEAD/Range/304 由既有 gateway/媒体测试继续覆盖。

## 回归

| 项目 | 结果 |
|---|---|
| 定点 API/画像/gateway 测试 | 93 passed |
| `tests/multiuser` 全量 | **1572 passed, 5 skipped** |
| 全部 frontend Node | **143 passed, 0 failed** |
| `tsc -b && vite build` | 成功，`index-Bw-bcRB_.js` |
| `oxlint` | 退出 0 |
| control static + `test_persona_security` | 22 passed |
| Task 5A 浏览器 | 4/4，3 次 `FAILURES=[]` |
| Task 3 浏览器 | 33/33，3 次 `FAILURES=[]` |
| Task 4 浏览器 | 8/8，3 次 `FAILURES=[]` |
| 隔离容器链路 | `TASK5A_CONTAINER_OK=True`，`CLEANUP_ERRORS=[]` |

容器链路新增缺失媒体 404：返回受控 `not_found`，随后正常 Range 请求仍
206。镜像（本轮重建）：

- control `easel-control:task5a-20261030`：
  `sha256:3669a3accac4e869a17a1f3db633799f675ca9e469691116f0f6f778d1a2c7e0`
- gateway：`sha256:afaa29d093d93735ff08aaa9ee379978ff8b25ccf0b040c16162f910ef5d276f`
- worker `easel-worker-execution:task5a-20261030`：
  `sha256:75426efd0ee0f12bdd71d17dae30c42b37d3eacac64dd40e5fde11f53043bace`

worker 仍是基于既有 execution 镜像覆盖当前 `easel/`、`web/` 的增量构建，
未重建 Harness/Chromium；未执行真实模型、真实账号/发布或完整 Compose。

## 文件

生产：

- `easel/control/workspace_api.py`
- `easel/control/gateway_client.py`
- `easel/persona.py`
- `web/app.py`

测试：

- 新增 `tests/multiuser/test_workspace_revision.py`
- 新增 `tests/multiuser/test_gateway_client_errors.py`
- 修改 `tests/multiuser/test_original_web_api.py`
- 修改 `tests/multiuser/task5a_container_chain.py`

证据：本目录 `probe-*.txt`、`persona-link-test.txt`、`file-symlink-not-run.txt`、
`node.txt`、`build.txt`、`lint.txt`、`multiuser.txt`、`static-persona.txt`、
`browser-run1..3.txt`、`task3-run1..3.txt`、`task4-run1..3.txt`、
`container.txt`、`container-images.txt`、`control-build.txt`、`worker-build.txt`。

## 未执行与清理

- 文件 symlink 因 Windows `WinError 1314` not-run；目录 junction 已实测；
  未用 mock/hardlink 冒充。
- 未使用真实 Key/Cookie/邀请，未重放用户 prompt，未调用付费模型，未发布。
- 用户 control/worker/gateway/sidecar、原卷/socket 未操作；隔离容器资源
  按随机标签与归属校验清理，未全局 prune。
- Task 3/4 历史截图已恢复；未进入 5B/5C/6，未上线。
