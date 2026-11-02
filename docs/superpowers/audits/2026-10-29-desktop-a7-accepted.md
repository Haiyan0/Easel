# Task A7 验收：Desktop 运维及 R2 三项边界通过

日期：2026-10-29（Asia/Shanghai）。
原 A7：`04b5f23e36879e72fbafa345dc2ed06abd26a5ae`。
R1：`e8376753d63eb681560ae868aa2d1528b2756c85`。
最终验收提交：`1a8811096c7c86bb9079a5c88b92cd225d1907b1`。

## 裁决与适用范围

A7 的 Desktop 初始化、启停、完整备份、新目录恢复和固定镜像升级/回滚功能范围验收通过。R1 已关闭部分保留，R2-01/02/03 原确定反例全部关闭，本轮未确认新增阻塞。

本结论允许记录 A7 功能完成，不授权自动上线或进入 A8，不代表 production-release 通过。真实容器全流程采用作者提交的运行证据；主审本轮执行本机测试、真实 SQLite 探针和正式 PowerShell 函数/脚本的隔离验证，没有重跑 Docker/ngrok/真实供应商或目标 Linux。

## R2 关闭依据

### R2-01：prepare 输入完整性

`easel/control/cli.py:1324` 在 Store.open 和撤销登录/清屏障前调用完整 verify_backup；只有清单与文件仍一致时才转换并登记新摘要。

主审和独立复核分别创建临时真实 Store/v2 备份，经 restore 后修改 users.display_name，保持清单不变。prepare 返回 backup_integrity_failed，文件集合及全部文件 SHA-256 与调用前一致。workspace 同大小篡改也有新增回归覆盖。合法准备流程仍正常，源备份不变。

### R2-02：verify 不改变源树

`easel/control/cli.py:1198` 将 DB 复制到私有临时目录，在副本上执行 integrity/schema/密文检查，`:1224` 清理副本；SQLite 辅助文件不写入源树。

主审和独立复核都在合法 prepared 包上连续运行两次 verify，逐次比较源文件集合与 SHA-256，均不变，随后 materialize 成功。没有通过忽略或删除源 WAL/SHM 放宽集合校验。v1 兼容包含在回归中。

主审隔离探针实际输出：

```text
TAMPER_PREPARE=backup_integrity_failed
REJECT_TREE_UNCHANGED=True
FIRST_VERIFY_UNCHANGED=True
SECOND_VERIFY_UNCHANGED=True
MATERIALIZE=ok
TEMP_CLEANUP=ok
```

### R2-03：当前停止承诺及 key 来源

`Start-WorkspaceEasel.ps1:217`–`:222` 在 compose up 前调用预检。Common 的 `:667`–`:676` 比较备份 stop 与当前 barrier/completion 的 barrierId/project/configSha256/completedAt；`:694`–`:699` 比较当前 key 指纹。

`read_stop_state.py:49`–`:67` 复制 DB/WAL/SHM 到临时目录后直接查询 control_state；正式卷只读挂载，不通过 Store 打开正式 DB、不执行迁移。启动后的最终停止记录复核仍保留。

独立复核使用临时真实 Store 创建完整备份，运行真实 helper 读取当前 DB/key，将其真实输出接入正式 Assert-EaselUpgradeBackup。仅外部 WSL 容器调用使用替身，备份另经真实 verify_backup 验证：

```text
SAME_STOP: exit=0, ACCEPTED=True
WRONG_MASTER: exit=3, upgrade_key_source_mismatch
STALE_STOP: exit=3, upgrade_stop_commitment_mismatch
```

其中 STALE_STOP 按旧备份→reopen→新增会话→再次 Stop 复现原问题。helper 每次调用前后源 control/key 文件集合与全部摘要一致，临时目录自动清理。正式 Start 的旧 Stop 拒绝顺序另由脚本回归覆盖，未冒称本轮真实容器 Start 演练。

## 本轮主审验证

```text
C:\Users\hyh\anaconda3\envs\Agent\python.exe -m pytest
  tests/multiuser/test_operations.py
  tests/multiuser/test_operations_a7.py
  tests/multiuser/test_operations_a7_r2.py
  tests/multiuser/test_desktop_operations_a7.py
  tests/multiuser/test_desktop_operations_r1.py
  tests/multiuser/test_desktop_deployment.py
  tests/multiuser/test_desktop_workspace.py
  tests/multiuser/test_workspace_start_identity.py
  tests/multiuser/test_lifecycle_lock.py -q

153 passed, 1 skipped in 63.02s
git diff e837675 1a881109 --check：退出 0
```

独立备份复核三文件 71 passed/1 skipped（10.47 秒）、独立脚本 8 passed（10.63 秒）均与主审范围重叠，不相加。

本机五个源码文件 SHA-256 与作者 checks.json 中 source_hash_match 全部一致；没有启动镜像重验镜像内哈希，该部分仍为作者证据。

## 作者真实演练证据及未执行项

证据位于 `docs/evidence/desktop-a7-r2/`：全 multiuser/topology 1439 passed/10 skipped；真实容器全流程 1 passed/275.79 秒；新目录恢复后的旧 Cookie 401、错误 Origin 403、双用户历史隔离、sentinel、同会话新消息，以及同轮 Stop 备份升级均有记录。主审本轮审阅这些证据，未重复整套实测。

验收镜像记录：

- control：`sha256:9fc4e037f82ad16a6d0553ee208cf0b85731ed23d57532da132f628474422009`。
- worker：`sha256:e48eb63b6155b6fc14c37a2525d138e7419f7a8024f46a58d209c40d3a5bd993`。
- gateway：`sha256:c0a35aaa9f304c47f9f5c3e73add7d9d5920041baef156e2ea674f3000c5580a`。

仍保持原边界：真实 DeepSeek 供应商请求 not-run；目标 Linux/XFS/firewall/IPv6/Chromium not-run；ngrok 公网矩阵本轮未重跑；错误公钥 UI/运行期缺 broker UI 沿用 A6 未单独执行记录；production-release blocked 不变。本轮没有上线、收费请求或开发者容器/凭证操作。

上轮被自动审批阻止清理的两个假材料临时目录保持未触碰；本轮新隔离探针均自动清理。未修改产品代码，验收文档与计划更新保持未提交。
