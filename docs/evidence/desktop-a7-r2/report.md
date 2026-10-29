# Task A7-R2 证据：准备转换、只读校验与当前停止承诺

日期：2026-10-29（Asia/Shanghai）
基线：`e8376753d63eb681560ae868aa2d1528b2756c85`（A7-R1）
范围：仅 A7-R2-01/02/03 及必要测试/手册；不进入 A8、不自动上线。

## 1. 结论

- **A7-R2-01**：`prepare_restored_instance` 在 Store.open、撤销登录/清屏障
  之前先调用完整 `verify_backup`。staging 的 DB/workspace 被篡改时拒绝，
  且 DB、manifest、文件集合均不变；只有合法输入才进入受控转换并更新
  staging 清单。原备份始终只读。
- **A7-R2-02**：`_validate_restored_database` 在私有临时副本上执行 SQLite
  integrity/schema/密文检查；合法 prepared 包连续两次 `verify_backup`
  不创建 WAL/SHM、不改变任何源文件字节或集合，随后 `materialize_restore`
  成功。
- **A7-R2-03**：升级预检在 compose up 前通过只读副本查询正式 control
  的 barrier/Stop 完成记录和 key 指纹；要求备份 `runtime_state.stop` 与
  当前同一轮 Stop 的屏障/项目/旧配置/完成时间精确匹配，key 来源一致；
  旧 Stop 备份在 verify/compose up 前失败。启动后的 `stop-status` 最终
  复核保留。
- 真实容器全流程复跑通过：**1 passed in 275.79s**；正常恢复/升级/回滚
  继续通过。

## 2. 镜像与源码

| 项 | 实际值 |
|---|---|
| A7-R2 control | `sha256:9fc4e037f82ad16a6d0553ee208cf0b85731ed23d57532da132f628474422009`，60573706 bytes |
| 旧 worker（A6） | `sha256:984bfac19412c0c6251e93568508fc9c078970727e4a9b515d5cb455a82128e9` |
| 新 worker（A7） | `sha256:e48eb63b6155b6fc14c37a2525d138e7419f7a8024f46a58d209c40d3a5bd993` |
| gateway（未改） | `sha256:c0a35aaa9f304c47f9f5c3e73add7d9d5920041baef156e2ea674f3000c5580a` |
| 镜像内源码哈希 | `easel/control/cli.py` `99f1b1da...`、`store.py` `60c15491...`、`easel/cli.py` `25c54c4c...`、`initialize_state.py` `546ab760...`、`read_stop_state.py` `cd62f2eb...`，与宿主一致 |

worker/gateway 无变化；未重复重建。

## 3. 三项修复

### R2-01 prepare 输入验证

- `prepare_restored_instance` 读取 manifest/版本后立即 `verify_backup`。
- 全文件集合/大小/SHA、DB 密文、签名材料都验证通过后才打开 Store。
- 拒绝路径只读取 staging，不改 DB/manifest/文件集合；测试逐字节比较。
- 正常 prepare 后仍更新 staging 清单和 `prepared.source_manifest_sha256`，
  源备份不变。

### R2-02 真只读 verify

- `_validate_restored_database` 先 `_private_copy` 到临时目录，再在副本上
  `PRAGMA integrity_check`、`user_version`、`ConfigService` 解密检查。
- SQLite 只可能向临时副本写 WAL/SHM；源备份/ prepared staging 树不变。
- 测试：连续两次 verify 后 file set + 全字节哈希一致，无 `control.sqlite3-*`
  辅助文件；随后 materialize 成功。

### R2-03 当前 Stop 与 key 来源绑定

- 新增 `deploy/desktop/read_stop_state.py`：复制 control DB（及可能的
  WAL/SHM）到 `/tmp`，直接查询 `control_state`；不打开 Store、不迁移。
- 输出 `stop_state` 与 master/signing/worker-public/gateway-public 的
  SHA-256，不输出 key 内容。
- `Assert-EaselUpgradeBackup` 在 compose up 前：
  1. 匹配旧 `deployment.json` 项目/镜像/配置/根；
  2. 只读读取当前 barrier/Stop 和 key 哈希；
  3. 要求备份 `runtime_state.stop` 的 barrierId/project/configSha256/
     completedAt 与当前完全一致，key 哈希与备份 identity 一致；
  4. 再执行只读 `verify-backup`。
- 旧 Stop 备份、缺 key/DB、错版本、损坏备份都在新 control 打开/迁移正式
  DB 前失败；同一轮 Stop 的正常升级在真实演练通过。

## 4. 红绿与回归

| 项 | 结果 |
|---|---|
| R2-01/02 红 | 4 failed（有效 SQLite 行变化 prepare 接受、workspace 篡改 prepare 接受、prepared 包第一次 verify 增加 WAL/SHM、materialize 二次 verify 失败） |
| R2-01/02 绿 | `test_operations_a7_r2.py` 5 passed |
| R2-03 正式脚本红/绿 | 基线旧 Stop 备份未被绑定；修复后旧备份 `upgrade_stop_commitment_mismatch`，无 compose up；R1 脚本 8 passed |
| A7 单元/R2/R1 | 37 passed |
| 定向回归 | 153 passed, 1 skipped |
| 全 multiuser + desktop topology | **1439 passed, 10 skipped**, 198.25s |
| 真实容器全流程 | **1 passed in 275.79s** |

## 5. 真实演练复跑

项目 `easel-a7-live-49078de2`（端口 7800）初始化/启动；A/B 真实 Harness
聊天后 Stop：

- Stop `stop-c69650e8...`，`drain_verified=settled`；
- v2 备份 `desktop-20261029-110801`，manifest
  `730ce9e1...`，35 files、3 workspaces、`clean_stop=true`；
- 只读 verify 连续通过（`version=2 schema_version=14`）；
- 新项目 `easel-a7-restore-49078de2`（端口 7801）恢复：旧 Cookie 401、
  新 Origin 403、A/B 历史隔离、sentinel、同会话新 Harness 消息 done；
  备份 manifest 未变；恢复副本 Stop `stop-724dd3f1...` 后源才重启；
- 坏 worker 镜像 `worker_or_gateway_image_mismatch`；同一轮 Stop 备份升级
  旧 worker `984bfac` → 新 worker `e48eb63`，`upgraded_from_backup=...`，
  `config_sha256=cc044fc9...`，历史/新消息通过；最终 Stop
  `stop-1c9f31f4...`。

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

- Windows/Linux 手册更新：verify 私有 DB 副本/连续两次只读；prepare 先校验
  staging；升级预检绑定当前同一轮 Stop 与 key 来源。
- `deploy/desktop/README.md` 增加 A7-R2 状态与证据链接。
- 本轮真实项目容器/卷/网络/provider、provisioner socket 已清理；本轮
  产生的零容器 `easel-ws-*` 网络已删除；未全局 prune，未触碰开发者资源；
  审查镜像保留。
- 上轮主审被自动审批阻止清理的两个假材料临时目录未触碰。
