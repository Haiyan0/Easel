# A7-R2 红绿记录

基线：`e8376753d63eb681560ae868aa2d1528b2756c85`。
仅修 R2-01/02/03；真实供应商、Linux 目标生产门槛保持 not-run。

## 1. R2-01 prepare 前验证

### 红

新增 `tests/multiuser/test_operations_a7_r2.py` 后运行：

```text
python -m pytest tests/multiuser/test_operations_a7_r2.py -q
4 failed
```

- restore 后对 staging `users.display_name` 做有效 SQLite UPDATE，manifest 不变：
  `verify_backup` 正确报 `backup_integrity_failed`，但 `prepare_restored_instance`
  仍成功并重写摘要；随后 materialize 接受 `CORRUPTED-STAGING`。
- 篡改 workspace sentinel 后 prepare 仍成功。
- 合法 prepared 包第一次 verify 后出现 `control.sqlite3-wal`/`-shm`，
  materialize 的第二次 verify 因额外文件失败。

### 绿

```text
python -m pytest tests/multiuser/test_operations_a7_r2.py -q
5 passed
```

prepare 在 Store.open/撤销登录/清屏障前调用完整 `verify_backup`；损坏时
DB、manifest、文件集合都不变。只有合法输入才进入 Store 转换并登记新摘要。

## 2. R2-02 verify 真只读

### 红

合法 prepared 包：

```text
FIRST_VERIFY_ADDED=["control/control.sqlite3-shm", "control/control.sqlite3-wal"]
SECOND_VERIFY=backup_integrity_failed
```

### 绿

`_validate_restored_database` 改为把主库复制到私有临时目录，再在副本上
执行 `PRAGMA integrity_check`/schema/ConfigService；临时目录关闭后源树
不变。测试连续两次 verify，逐次比较源文件集合与所有字节，随后
`materialize_restore` 成功；并确认 prepared 包没有 `control.sqlite3-*`
辅助文件。

## 3. R2-03 升级绑定当前同一轮 Stop

### 红

基线 `e837675` 的正式预检只匹配项目/镜像/配置/根并验证备份完整性；
旧 Stop 的完整备份在 reopen 增加 `AFTER-BACKUP` 后、同配置再次 Stop 时
仍被接受；真实 Store 的 `upgrade_stop_completion(new-stop, 旧备份摘要)`
也接受。

本轮新增正式 Start 反例（临时 Docker/WSL 桩，真实 PowerShell 控制流）：

```text
旧备份 stop=old-stop，当前 barrier/stop=new-stop
期望 upgrade_stop_commitment_mismatch 且无 compose up
基线得到 upgrade_backup_verify_failed（未绑定 Stop）
```

### 绿

`Assert-EaselUpgradeBackup` 在 compose up 前运行只读
`read_stop_state.py`：

- 复制 control DB（含 WAL/SHM）到 `/tmp` 后直接查询 `control_state`，
  不打开 Store、不执行迁移；
- 要求备份 `runtime_state.stop` 与当前 barrier/stop 的
  `barrierId/project/configSha256/completedAt` 精确相等；
- 同时核对当前 key 卷的 master/signing/worker-public/gateway-public
  哈希与备份 identity 一致；
- 通过后才执行只读 `verify-backup`；旧 Stop 备份在 verify/compose up 前失败。

正式脚本反例 8 passed；真实同轮 Stop 备份升级/回滚在 275.79s 容器演练中
通过。启动后的 `stop-status` 最终复核和 `upgrade-complete` 顺序保留。

## 4. 回归

```text
pytest tests/multiuser/test_operations.py \
  tests/multiuser/test_operations_a7.py \
  tests/multiuser/test_operations_a7_r2.py \
  tests/multiuser/test_desktop_operations_a7.py \
  tests/multiuser/test_desktop_operations_r1.py \
  tests/multiuser/test_desktop_deployment.py \
  tests/multiuser/test_desktop_workspace.py \
  tests/multiuser/test_workspace_start_identity.py \
  tests/multiuser/test_lifecycle_lock.py -q
153 passed, 1 skipped
```

```text
pytest tests/multiuser tests/integration/multiuser/test_desktop_topology.py \
  tests/multiuser/test_lifecycle_lock.py -q
1439 passed, 10 skipped, 2 warnings in 198.25s
```

真实容器全流程：**1 passed in 275.79s**（control `9fc4e0...`，worker
984bfac → e48eb63）。日志/摘要见 `real/`。
