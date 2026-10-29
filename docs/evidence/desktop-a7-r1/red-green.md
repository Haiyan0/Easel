# A7-R1 红绿记录

基线：`04b5f23e36879e72fbafa345dc2ed06abd26a5ae`。
本轮只修 A7-R1-01/02/03/04；真实供应商、Linux 目标生产门槛保持 not-run。

## 1. A7-R1-01 全量摘要与 materialize

### 红

新增测试后先运行：

```text
python -m pytest tests/multiuser/test_operations_a7.py -q \
  -k "every_manifest_file or staging_manifest or tampered_prepared or copied_bytes"
6 failed, 2 passed
```

失败包括：

- 修改 `workspaces/<id>/workspace/sentinel.txt` 为等长内容：旧 `verify_backup` 接受。
- 修改 HOME `.dsh/session.json`：旧 verify 接受。
- 用合法 SQLite 更新改 `users.display_name`（库仍完整）：旧 verify 接受。
- `prepare_restored_instance` 后没有 `prepared` 来源块，且 staging 清单未更新。
- 篡改已 prepare 的 staging workspace 后 `materialize_restore` 仍复制成功。
- 注入 `_copy_file` 返回错误目标字节后 materialize 未发现复制结果损坏。

缺文件/多文件在旧 `_validate_bundle_tree` 已能被集合比较拒绝；本轮保留该行为。

### 绿

```text
python -m pytest tests/multiuser/test_operations_a7.py -q
20 passed  # 当时；加入 A7-R1-02 用例后该文件最终 24 passed
```

覆盖：全清单大小/SHA、workspace/HOME 同大小篡改、有效 SQLite 行变化、缺/多文件、
prepare 只改 staging 清单并记录 `source_manifest_sha256`、源备份字节不变、
materialize 输入与复制结果双重复核、失败只清目标。

## 2. A7-R1-02 备份源根绑定

### 红（当前实现）

```text
python -m pytest tests/multiuser/test_operations_a7.py -q \
  -k "real_workspace_root or missing_materialized or new_workspace"
3 failed
```

- v2 完整备份未传 workspace 根仍成功（`complete=true`）。
- 已有 Run 历史的 workspace 数据目录被删除后仍被记为 false 并成功。
- 正向“新 workspace 无材料”测试初次因测试断言变量写错而失败；修正断言后
  用于确认合法空 workspace 不被误拒。

### 红（04b5f23 基线的正式脚本）

在隔离 worktree 中保留基线代码，只复制新测试与只读探测 helper，运行：

```text
python -m pytest tests/multiuser/test_desktop_operations_r1.py -q
5 failed, 2 passed
```

- `Backup-WorkspaceEasel.ps1` 对未登记的空 `-WorkspaceRoot` 没有绑定检查，
  测试未得到 `backup_workspace_root_mismatch`。
- `Start-WorkspaceEasel.ps1` 对只有 manifest 的升级备份继续进入启动流程
  （测试得到后续 `frontend_hash_unavailable`，而不是前置校验失败）。
- `Initialize-WorkspaceEasel.ps1` 在已有 control DB、缺 master 或 signing 时
  仍 `exit 0` 并输出 `key=created` / `signing_key=created`。

### 绿

```text
python -m pytest tests/multiuser/test_operations_a7.py \
  tests/multiuser/test_desktop_operations_r1.py -q
31 passed  # 其中 R1 脚本 7 passed、A7 Python 24 passed
```

覆盖：缺 workspace 根失败；有 Run 历史但缺目录失败；尚未物化的新 workspace
允许为空；`Backup` 在任何备份容器前拒绝错误根/重解析点；`Start` 在任何
compose up 前拒绝只有 manifest 的备份；`Initialize` 缺 master/signing/整套
身份时拒绝且不生成新 key，原 DB/sentinel 字节不变，全新空部署和既有完整身份
幂等初始化继续通过。

## 3. A7-R1-03 升级前置顺序

`Start-WorkspaceEasel.ps1` 在镜像/卷/网络检查之后、compose up 之前调用
`Assert-EaselUpgradeBackup`（`EaselDesktop.Common.ps1`）：

1. 解析并读取完整 manifest，`clean_stop=true`；
2. 匹配旧 `deployment.json` 的项目、control/worker/gateway 镜像、配置摘要、
   workspace/data 根；
3. 在新 control 未启动时，以 `--network none --read-only --cap-drop ALL` 运行
   新镜像的 `admin verify-backup`；
4. 失败即返回，不执行 compose up、不打开正式 DB、不改 stop 完成记录、不 reopen。

只有通过后才进入后续启动；正式 DB 的 Stop 完成记录仍在 control 健康后核对，
`upgrade-complete` 仍只接受旧屏障/项目/旧配置和备份 manifest 摘要。

## 4. A7-R1-04 Initialize 既有身份失败关闭

`Initialize-WorkspaceEasel.ps1` 在 chown/keygen/copy 之前运行只读
`initialize_state.py`（control/keys 卷只读挂载，网络 none，非 root）：

- 无 control DB、无任何 key 文件：全新部署，允许创建；
- 有 DB 或任一身份材料：master 与 signing 私钥必须同时存在；
- 任一项缺失/不可读：`existing_identity_incomplete` / `identity_probe_failed`，
  不创建 key、不覆盖配置/公钥、不改已有数据；
- 私钥存在但派生公钥缺失：仍由 `keygen --ensure` 从现有私钥安全派生。

脚本反例断言的是“没有 keygen 调用、master/signing 未生成、DB 与 sentinel 字节
不变”，不是只看退出码。

## 5. 回归与真实演练

```text
pytest tests/multiuser/test_operations.py \
  tests/multiuser/test_operations_a7.py \
  tests/multiuser/test_desktop_operations_a7.py \
  tests/multiuser/test_desktop_operations_r1.py \
  tests/multiuser/test_desktop_deployment.py \
  tests/multiuser/test_desktop_workspace.py \
  tests/multiuser/test_workspace_start_identity.py \
  tests/multiuser/test_lifecycle_lock.py -q
147 passed, 1 skipped
```

真实容器全流程（控制镜像 `sha256:6d12e0...`，worker 984bfac → e48eb63）：

```text
EASEL_RUN_CONTAINER_TESTS=1 python -m pytest \
  tests/integration/multiuser/test_desktop_operations_real.py -q
1 passed in 258.93s
```

日志见 `real/`；摘要见 `real/summary.json`。
