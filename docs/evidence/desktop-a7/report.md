# Task A7 证据：日常运维、完整备份与可验证升级恢复

日期：2026-10-29（Asia/Shanghai）
基线：`d9c2f5ba960e892f198ebc4a111b7e2ad7e835af`（A6-R1 终审）
范围：A7.1–A7.5；真实供应商与 Linux 生产门槛保持 not-run；不自动上线。

## 1. 结论

- 新增完整 Desktop 运维入口：备份、只读校验、新目录恢复、受控升级；
  Start/Stop/Status/Logs 补齐版本摘要与受控配置变更处理。
- 备份格式扩展为 v2：control DB 一致快照、master key、Ed25519 私钥及
  派生公钥、受管 Agent 配置、出口策略、CA、各 workspace 的
  workspace/state/HOME；明确排除 `.credentials` 与 `.gateway`。
- 真实演练通过：A/B 用户、真实 Harness 聊天、停止、v2 备份与 verify、
  新项目/目录/端口恢复（旧 Cookie 401、新 Origin 写 403、跨空间 404、
  历史/sentinel/同会话新消息）、坏镜像失败、A6 worker→A7 worker 受控
  升级、旧镜像回滚恢复、源/备份字节不变。
- 自动化真实入口 `test_desktop_operations_real.py` 已完成一次全流程
  演练：**1 passed in 257.26s**（随机项目/卷/网络/端口，finally 清理）。
- 回归：A7 定向 **115 passed, 1 skipped**；全 multiuser + desktop topology
  **1414 passed, 10 skipped**（166.66s）；v1 operations 格式兼容
  **42 passed, 1 skipped**。
- 真实 DeepSeek、Linux XFS/firewall/IPv6/Chromium、ngrok 公网复跑仍为
  not-run；不把本机 Desktop 结果拼成“生产可发布”。

## 2. 镜像与源码

| 项 | 实际值 |
|---|---|
| A7 control（当前源码） | `sha256:a5110be8a165d578b8411cce059bec8f2d5be047e63d70d1ea268faa1be9afe3`，60570843 bytes |
| 旧 worker（A6） | `sha256:984bfac19412c0c6251e93568508fc9c078970727e4a9b515d5cb455a82128e9`，2689778768 bytes |
| 新 worker（A7） | `sha256:e48eb63b6155b6fc14c37a2525d138e7419f7a8024f46a58d209c40d3a5bd993`，2689779306 bytes |
| gateway（未改） | `sha256:c0a35aaa9f304c47f9f5c3e73add7d9d5920041baef156e2ea674f3000c5580a` |
| 构建期下载 | 本机代理 `host.docker.internal:7897`；仅构建阶段 |
| 镜像内源码哈希 | `easel/control/cli.py` `240ac5cb...`、`store.py` `65e7d5a7...`、`easel/cli.py` `25c54c4c...`，与宿主一致 |

control 镜像由当前源码重建；worker/gateway 继续使用 A6 固定镜像。前端未改，
未做无意义前端重建。

## 3. A7.1 日常入口

- `Status-WorkspaceEasel.ps1` 增加 `version_control_image/worker_image/
  gateway_image/frontend_sha256/config_sha256/source_commit/harness_revision/
  schema/project/logs`，真实输出 `status=ok`、`version_schema=14`。
- `Logs-WorkspaceEasel.ps1` 输出 control/egress/provisioner 最近日志；不读
  key/邀请/Cookie。
- `Start-WorkspaceEasel.ps1` 写 `DataRoot\logs\deployment.json`（非秘密部署
  身份），并在配置变化导致旧 Stop 完成记录不匹配时，只有显式
  `-UpgradeFromBackup` 且备份/旧摘要/屏障完全匹配才调用
  `admin upgrade-complete`；否则保持 `admission_barrier_active`。
- 旧镜像/外来卷/端口/Origin/WSL socket 等既有失败关闭路径保留；新增红绿
  覆盖不完整备份、错 key、未来 schema、非空目标、路径逃逸和坏镜像。

## 4. A7.2 完整备份与失败关闭

v2 manifest 字段（无密钥内容）：

```text
version=2, complete=true, schema_version=14
deployment: project/origin/port/control_image/worker_image/gateway_image/
            frontend_sha256/config_sha256/egress_ca_sha256/engine_id/
            engine_context/source_commit/harness_revision/workspace_root/data_root
identity: master_key/signing_key/worker_public/gateway_public/egress_ca
managed_config: managed-agent.json
egress_policy: egress-policy.json
runtime_state: clean_stop/stop/excluded[.credentials,.gateway]
```

- `create_backup` 要求显式签名私钥/受管配置/出口策略成组给出；缺项失败，
  绝不生成新 key。`--require-clean-stop` 要求屏障+完成记录+drain settled。
- 写入同卷 staging，校验后整体 rename；失败不留下目标。POSIX 0700/0600；
  Windows 备份目录 ACL 仅当前用户/SYSTEM/Administrators（真实 `icacls`
  已核对）。
- `verify_backup` 只读校验清单/文件集合/摘要/SQLite integrity/schema/
  密文/签名私钥派生公钥/受管配置/出口策略；不写源、不生成 key。
- 备份一致性：先跨进程 Stop 屏障，drain 本期 Run/请求/实例，再复制；
  复制中断/磁盘失败由 staging 清理覆盖。

## 5. A7.3 新目录/新项目恢复（真实）

源部署：`easel-a7-src-09290`，端口 7871；3 个 workspace（admin/A/B）。
备份：`desktop-20261029-015521`，manifest
`d149016ae4a2455dcdef09fd4c391a71ce4f3db98c6b32851d7552928d6d26c4`，
35 个文件，3 workspaces，`clean_stop=true`。

恢复：新项目 `easel-a7-dst-09290`、端口 7872、新 DataRoot/WorkspaceRoot；
源保持停止。

实测（`real/drill_verify.json`）：

```text
old_cookie_status=401
evil_origin_status=403
recovery_admin=true
a_workspace_same=true, b_workspace_same=true
a_history_has_marker=true, b_history_has_marker=true
a_does_not_contain_b=true, cross_session_status=404
new_run_state=done, new_assistant_ok=true
sentinel=a7-source-sentinel, source_sentinel=a7-source-sentinel
backup_manifest_sha256=d149016ae4a2455dcdef09fd4c391a71ce4f3db98c6b32851d7552928d6d26c4
control_state=[restore_info, stop_completion]（无 admission_barrier）
active_broker_tokens=0
```

- 旧登录/未兑换邀请/Run token 在恢复准备中撤销；恢复副本签发一次性管理员
  恢复邀请，测试中通过 CLI 重新签发用户邀请后才访问历史。
- 旧 lease 句柄规范化；旧实例不接管、旧 Run 不重放；新 Run 使用同一
  workspace ID 和同一 native session 续接（容器内 cwd 仍 `/workspace`）。
- 恢复副本验证后先 Stop；源随后重启成功（`admission_barrier_reopened`，
  config_sha 不变），再发新消息 done，证明“恢复副本停止后才允许源再次启动”。

## 6. A7.4 升级与失败回滚（真实）

升级项目：`easel-a7-up-09290`，端口 7873；旧 worker `984bfac`。
升级前备份：`desktop-20261029-020128`，manifest
`605a3ac65cd100e14780a05e6555203b4bd23e2b425631908b4e3bced213a138`，
21 个文件，2 workspaces。

1. 坏镜像启动：`worker_or_gateway_image_mismatch`；control 未启动，屏障
   仍为 `stop-b9c5...`，数据/Key/备份不变。
2. 用新 worker `e48eb63` + `-UpgradeFromBackup` 启动成功：
   `upgraded_from_backup=...`、`admission_barrier_reopened=stop-b9c5...`；
   `stop_completion` 记录 `upgradedFrom=85cb1b5e...`（旧）→
   `configSha256=73561e21...`（新），
   `backupManifestSha256=605a3ac6...`。
3. 升级后同会话历史保留（`A7-UP-before`），新 Harness 消息 done
   （`fake-provider-ok`）；worker 容器
   `b86bc3970f59...`，镜像 `e48eb63`。
4. 回滚：用升级前备份恢复到新项目 `easel-a7-rb-09290`、端口 7874，旧
   worker `984bfac` 启动；历史 marker 保留，新消息 done；worker 容器
   `d1a238be0db9...`，镜像 `984bfac`。
5. 没有新 schema 版本，本轮是流程 + worker 代码版本切换演练，不伪称
   schema 升级。未来 schema 拒绝由隔离用例覆盖。

## 7. A7.5 手册与发布分层

- `docs/windows-desktop-operations.zh-CN.md` 更新基线，重写第 9 节为完整
  Desktop 路线：变量/前置、镜像固定、managed config/egress/CA、初始化、
  Start/ngrok、管理员/邀请/登录/聊天/权限/停止、Status/Logs、备份/verify、
  新目录恢复、升级/回滚、数据位置、错误码、两个验收维度。
- `docs/linux-docker-operations.zh-CN.md` 更新基线/当前事实，新增第 13 节：
  Linux 可核查命令、恢复/升级命令模板、not-run 清单和两个验收维度；
  目标 Linux/XFS/firewall/IPv6/Chromium 仍明确阻断生产。
- `deploy/desktop/README.md` 增加 A7 入口表与当前状态说明；历史 A2/A4
  契约标注为历史记录。

两个维度：

| 维度 | 结果 |
|---|---|
| Desktop 功能/运维（Windows + Docker Desktop） | 初始化/启停/Status/Logs、真实 Harness 聊天、权限/停止/续接、v2 备份/verify、新目录恢复、A/B 隔离、升级、坏镜像失败、回滚：**真实通过** |
| production-release | 真实 DeepSeek、Linux XFS/firewall/IPv6/Chromium、ngrok 公网复跑：**not-run/blocked** |

## 8. 未执行与边界

| 项 | 状态 | 说明 |
|---|---|---|
| 真实 DeepSeek 供应商请求 | **not-run** | 无批准 control 受管 channel/凭证；未读取开发者 `.env`/HOME；fake provider 不冒充真实供应商 |
| Linux 目标宿主/XFS/firewall/IPv6/Chromium | **not-run** | Docker Desktop 结果不替代 |
| ngrok 公网浏览器矩阵 | 未重做 | A6 原证据保留；A7 未改前端 |
| 错误公钥/运行期缺 broker UI 反例 | 未单独复跑 | 沿用 A6 边界，不写成通过 |
| 生产发布检查器 | blocked | 不修改检查器绕过 |

## 9. 清理

- 本轮四个项目 `easel-a7-src/dst/up/rb-09290` 的容器/卷/网络全部删除；
  fake provider 容器全部删除；WSL provisioner socket 目录删除。
- 未执行全局 `docker system prune`，未触碰开发者容器/卷/网络/ngrok 隧道。
- 测试产生的备份/恢复包只保留在临时测试根目录，未进入仓库；证据目录
  只保留不含密钥/邀请/Cookie 的日志与摘要。
- 保留供复审的镜像：A7 control `a5110be8...`、旧 worker `984bfac...`、
  新 worker `e48eb63...`、gateway `c0a35aaa...`。
