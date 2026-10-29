# Task A7-R1 证据：完整性、备份源根、升级前置与初始化失败关闭

日期：2026-10-29（Asia/Shanghai）
基线：`04b5f23e36879e72fbafa345dc2ed06abd26a5ae`（A7 首轮提交）
范围：仅 A7-R1-01/02/03/04 及关联手册/测试；不自动上线。

## 1. 结论

- **A7-R1-01**：`verify_backup` 对清单全部文件逐项核对普通文件/集合/大小/
  SHA-256；`materialize_restore` 对输入和实际复制结果双重复核，失败只清目标。
  `prepare_restore` 明确为受控 staging 转换：只更新 staging 清单及
  `prepared.source_manifest_sha256`，原备份不变；合法 prepare 后仍可验证，
  未经授权篡改继续失败。
- **A7-R1-02**：v2 完整备份必须有真实 workspace 根；已有 Run 历史的
  workspace 缺目录时失败。正式 `Backup-WorkspaceEasel.ps1` 以
  `deployment.json` 登记值为准，要求用户参数指向同一物理目录，并拒绝
  重解析点；错误空根在启动备份容器前失败。
- **A7-R1-03**：`Start -UpgradeFromBackup` 在 **compose up 之前**读取
  manifest、匹配旧 `deployment.json`（项目/镜像/配置/根）并用新镜像只读
  `verify-backup`；只有 manifest、缺 key/DB、损坏或错版本备份在这里失败，
  不打开/迁移正式 DB、不改完成记录、不 reopen。通过后才进入正常升级顺序。
- **A7-R1-04**：`Initialize-WorkspaceEasel.ps1` 在 chown/keygen/config copy
  前用只读容器探测 control DB 与 key 卷；已有 DB/身份缺 master 或 signing
  时拒绝且不生成新 key，不覆盖配置/公钥，不改已有数据。仅全新空部署创建
  初始材料；私钥存在而派生公钥缺失时仍安全派生。
- 真实容器全流程复跑通过：**1 passed in 258.93s**。v1 备份/恢复格式保持
  兼容；正常 v2 备份/恢复/升级/回滚继续通过。

## 2. 镜像与源码

| 项 | 实际值 |
|---|---|
| A7-R1 control | `sha256:6d12e0783526084283931da194575809750f8e4a0c50cb24d8d49cdea6fd4842`，60572259 bytes |
| 旧 worker（A6） | `sha256:984bfac19412c0c6251e93568508fc9c078970727e4a9b515d5cb455a82128e9` |
| 新 worker（A7） | `sha256:e48eb63b6155b6fc14c37a2525d138e7419f7a8024f46a58d209c40d3a5bd993` |
| gateway（未改） | `sha256:c0a35aaa9f304c47f9f5c3e73add7d9d5920041baef156e2ea674f3000c5580a` |
| 构建下载 | 局部代理 `host.docker.internal:7897`，仅构建阶段 |
| 镜像内源码哈希 | `easel/control/cli.py` `711e22a1...`、`store.py` `60c15491...`、`easel/cli.py` `25c54c4c...`、`initialize_state.py` `546ab760...`，与宿主一致 |

worker/gateway 继续使用 A7 固定镜像；前端未改，未做无意义重建。

## 3. 四项修复

### A7-R1-01 verify/materialize 完整性

- `_verify_bundle_digests` 对 manifest `files` 每一项检查 non-reparse 普通
  文件、大小、SHA-256；`_validate_bundle_tree` 继续拒绝额外文件/链接。
- `_copy_verified_file` 复制前核对源、复制后重读目标实际字节；不信任
  `_copy_file` 返回元数据。
- `_verify_materialized_tree` 对 workspace 目标树重核文件集合/大小/摘要。
- `prepare_restored_instance` 在 staging 上更新控制库摘要并写入
  `prepared` 来源块；`_read_manifest` 校验 `prepared.control` 与当前
  manifest 一致；原备份只读不变。
- 反例：workspace/HOME 同大小内容、有效 SQLite 行变化、缺/多文件、prepare
  后篡改、复制结果损坏；失败不提交部分目标。

### A7-R1-02 备份源根

- `create_backup` v2 完整模式要求 `workspace_root`；已有 Run 历史的
  workspace 任一部分缺失 → `backup_workspace_missing`；从未物化的新
  workspace 允许为空。
- `Backup-WorkspaceEasel.ps1` 从 `deployment.json` 读取登记根，与用户参数
  做物理目录比较（拒绝 junction/符号链接），错误根在备份容器前失败。
- v1 控制库+key 备份保持原行为，不要求 workspace 根。

### A7-R1-03 升级前置

- 新增 `Assert-EaselUpgradeBackup`（Common）：manifest 形状 → 旧部署身份
  匹配 → 新镜像只读 `verify-backup`，全部在 compose up 前。
- 旧 Stop 完成记录仍在 control 健康后核对；`upgrade-complete` 仍要求旧
  屏障/项目/旧配置/备份 manifest 摘要；没有无条件 resume。
- 正式脚本反例证明只有 manifest 的备份在 compose up 前失败，调用日志无
  `compose up`；正常固定镜像升级通过。

### A7-R1-04 Initialize

- 新增 `deploy/desktop/initialize_state.py`，只读输出 DB/control 文件/key
  状态；容器 `--network none --read-only --user 10001 --cap-drop ALL`。
- 既有 DB 或 key 材料存在时，master 与 signing 必须同时存在；缺失/不可读
  在 chown/keygen/config copy 前失败。仅全空卷创建初始材料。
- 测试断言无 keygen 调用、无新 key、DB/sentinel 字节不变；既有完整身份
  幂等初始化保持 `key=exists`。

## 4. 红绿与回归

| 项 | 结果 |
|---|---|
| A7-R1-01 Python 红 | 6 failed, 2 passed（workspace/HOME/SQLite 篡改、prepare、materialize、复制损坏） |
| A7-R1-01/02 绿 | `test_operations_a7.py` 20 passed |
| 基线正式脚本红 | 隔离 worktree 中 5 failed, 2 passed（错误根、空升级备份、三类 Initialize 缺 key；正向幂等/新部署通过） |
| R1 正式脚本绿 | `test_desktop_operations_r1.py` 7 passed |
| A7 单元/契约/R1 | 38 passed |
| 定向回归 | 147 passed, 1 skipped |
| 全 multiuser + desktop topology | **1433 passed, 10 skipped**, 186.58s |
| 真实容器全流程 | **1 passed in 258.93s** |

真实脚本反例使用临时 Docker/WSL 桩执行真实 PowerShell 控制流；断言的是
没有 compose up / 没有 keygen 调用 / 数据字节不变，不是只看退出码或字符串
marker。

## 5. 真实演练复跑

项目 `easel-a7-live-b729adf1`（端口 7800）初始化/启动；A/B 真实 Harness
聊天后 Stop：

- Stop `stop-e8a22119...`，`drain_verified=settled`；
- v2 备份 `desktop-20261029-103051`，manifest
  `f98e2568...`，35 files、3 workspaces、`clean_stop=true`；
- 只读 verify `version=2 schema_version=14`；
- 新项目 `easel-a7-restore-b729adf1`（端口 7801）恢复：旧 Cookie 401、
  新 Origin 403、A/B 历史隔离、sentinel、同会话新 Harness 消息 done；
  备份 manifest 未变；恢复副本 Stop `stop-71717700...` 后源才重启；
- 坏镜像 `worker_or_gateway_image_mismatch`；匹配备份升级旧 worker
  `984bfac` → 新 worker `e48eb63`，`upgraded_from_backup=...`，
  `config_sha256=89cc1916...`，历史/新消息通过；最终 Stop
  `stop-f37cde3a...`。

日志/摘要：`real/`。

## 6. 未执行与边界

| 项 | 状态 | 说明 |
|---|---|---|
| 真实 DeepSeek 供应商请求 | **not-run** | 无批准 control 受管 channel/凭证；未读取开发者 `.env`/HOME |
| Linux 目标宿主/XFS/firewall/IPv6/Chromium | **not-run** | Docker Desktop 结果不替代 |
| ngrok 公网浏览器矩阵 | 未重做 | A6 原证据保留；本轮未改前端 |
| 错误公钥/运行期缺 broker UI 反例 | 未单独复跑 | 沿用 A6 边界 |
| 生产发布检查器 | blocked | 未修改检查器绕过 |

## 7. 手册与清理

- Windows 手册更新备份源根绑定、verify 全量摘要、prepare/materialize 关系、
  升级前置顺序、Initialize 缺 key 失败关闭；修复恢复变量示例换行。
- Linux 手册更新基线、完整备份源根、verify 全量摘要、prepare/materialize、
  升级命令顺序与 Initialize 说明；继续明确 Linux 目标未执行。
- `deploy/desktop/README.md` 更新 A7-R1 入口说明与证据链接。
- 本轮真实项目容器/卷/网络/provider 全部删除；无 provisioner socket；
  未全局 prune，未触碰开发者资源；审查镜像保留。
