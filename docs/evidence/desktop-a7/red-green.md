# A7 红绿记录

## 1. Python 备份/恢复/升级契约

### 红

新增 `tests/multiuser/test_operations_a7.py` 后先运行：

```text
python -m pytest tests/multiuser/test_operations_a7.py -q
ImportError: cannot import name 'materialize_restore' from 'easel.control.cli'
```

补齐 API 占位前先观察到导入失败；随后在实现前观察到 10 failed / 1 passed，
失败原因包括 `backup_requires_clean_stop`、缺签名私钥/配置、未生成 v2
manifest、`restore` 未校验派生公钥、`prepare` 未撤销登录/邀请等。

### 绿

```text
python -m pytest tests/multiuser/test_operations_a7.py -q
12 passed
```

覆盖：

- v2 必须具备签名私钥/受管配置/出口策略/部署身份；缺项不生成备份。
- manifest 记录派生公钥/部署摘要，不包含密钥内容；`.credentials`/
  `.gateway` 明确排除。
- `verify_backup` 只读、缺文件/篡改失败；恢复拒绝未来 schema、错 key、
  非空目标、路径逃逸。
- `prepare_restored_instance` 撤销旧登录/未兑换邀请/Run token、清屏障、
  保留带 `reopenedAt` 的完成记录、签发一次性管理员恢复邀请；活跃 Run
  失败关闭。
- `materialize_restore` 落到新 control/keys/egress/workspace，目标非空
  拒绝。
- `upgrade_stop_completion` 只接受匹配旧摘要 + 备份 manifest 摘要；错误
  旧摘要/非法摘要失败。
- 复制中断/磁盘失败/取消通过注入 `_copy_file` 失败验证：不留下新备份、
  不丢旧备份、源文件摘要不变。

## 2. 脚本/Compose 契约

```text
python -m pytest tests/multiuser/test_desktop_operations_a7.py -q
6 passed
```

覆盖备份要求 Stop/完整材料、verify 只读、restore 拒绝覆盖/撤销恢复凭证、
Start 受控升级/写 deployment 身份、Compose 绑定 egress CA、Status/Logs
版本摘要且不读 key。

## 3. 既有回归

```text
python -m pytest tests/multiuser/test_operations.py -q
42 passed, 1 skipped
```

v1 备份/恢复/迁移格式保持兼容；新增 v2 为显式扩展，不改旧格式含义。

```text
python -m pytest tests/multiuser/test_workspace_start_identity.py \
  tests/multiuser/test_lifecycle_lock.py -q
13 passed
```

Start 新增 deployment 身份/CA/升级参数后，既有身份核验与跨进程锁测试通过。
`test_workspace_start_identity` 的 Docker stub 增加前端哈希返回，以匹配
真实 Start 的只读版本摘要步骤。

## 4. 真实容器演练

| 场景 | 结果 |
|---|---|
| 源部署初始化/Start/Stop | `key=created,signing_key=created`；Stop `drain_verified=settled`、`stop_completed` |
| v2 备份 | 35 files、3 workspaces、`clean_stop=true`、manifest `d149016a...` |
| 只读 verify | `version=2`、`schema_version=14`、`verified_backup=...` |
| 新项目恢复 | `restored_schema=14`、`restored_workspaces=3`、`recovery_invite_file=...` |
| 恢复后 A/B | 旧 Cookie 401；新 Origin 写 403；跨空间 404；A/B 历史 marker 保留；A 不含 B |
| 恢复后新 Harness | 同会话新 Run done，`fake-provider-ok`；sentinel 保留 |
| 恢复后顺序 | 恢复副本 Stop 后源重新 Start，`admission_barrier_reopened`，新消息 done |
| 坏镜像升级 | `worker_or_gateway_image_mismatch`；屏障保留；数据/备份不变 |
| 受控升级 | 旧 worker `984bfac` → 新 worker `e48eb63`；`upgradedFrom`/`configSha256`/`backupManifestSha256` 记录正确 |
| 升级后历史/新消息 | `A7-UP-before` 保留；新 Run done；worker 容器 `b86bc397...` |
| 回滚 | 升级前备份恢复到新项目，旧 worker `984bfac`；历史/新消息 done；worker `d1a238be...` |
| 源/备份不变 | 源 sentinel 与恢复 sentinel 相同；备份 manifest SHA 未变 |

真实测试脚本：`tests/integration/multiuser/test_desktop_operations_real.py`
（需 `EASEL_RUN_CONTAINER_TESTS=1`）。首次因随机端口重复/宿主端口未就绪
失败，修正为 7800–7899 不重复端口并增加宿主健康等待后，全流程
**1 passed in 257.26s**。A7 手工演练的完整日志/摘要见
`docs/evidence/desktop-a7/real/`。
