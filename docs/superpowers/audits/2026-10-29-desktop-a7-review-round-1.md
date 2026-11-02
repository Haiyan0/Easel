# Task A7 首轮终审：完整性、备份来源和升级前置仍需返工

基线：`d9c2f5ba960e892f198ebc4a111b7e2ad7e835af`。
审查提交：`04b5f23e36879e72fbafa345dc2ed06abd26a5ae`。

## 裁决

暂不通过 A7。正常备份/恢复/升级演练已有作者证据，主审定向绿色测试也通过，但下面四项确定反例不满足完整备份、可验证回滚和缺 key 失败关闭要求。仅修四项及关联手册，不推倒重做、不扩大到真实供应商或生产部署。

## A7-R1-01（P1）：verify 漏掉全量文件摘要，materialize 可恢复篡改内容

位置：`easel/control/cli.py:1145` 的 verify_backup，关联 materialize_restore 的 verify_backup 调用。

`_validate_bundle_tree` 只比较文件集合/链接，verify 随后只做 SQLite 结构/密文校验及部分签名配置文件摘要。没有逐项比较所有 files 的大小和 SHA-256，workspace/HOME 和有效 SQLite 内容的篡改可通过。materialize_restore 依赖该 verify，又没有重核全部输入摘要，可把已被篡改的文件复制到目标。

主审与独立审查分别用真实 Store、v2 完整备份和临时目录复现：

```text
修改备份 workspaces/<id>/workspace/sentinel.txt，保持 manifest 不变
TAMPER_VERIFY_ACCEPTED=True
TAMPER_MATERIALIZED=CORRUPTED!
```

同一源交给已有 restore_backup 则正确报 backup_integrity_failed，证明新增路径的校验弱于已有路径。

修复：公共只读 verify 对清单全部文件检查普通文件/集合/大小/SHA-256；materialize 的输入及实际复制结果也要有完整性保证，不能只信一次调用成功。增加 workspace/HOME、同大小内容、有效 SQLite 行变化、缺/多文件、错误 key/签名材料反例，失败不提交部分目标。

注意正确恢复流程先 restore_backup，再 prepare_restored_instance 修改 staging DB，目前 staging manifest 仍是原清单。修复不能因此跳过 DB 哈希，也不能直接改只读原备份；应明确受控准备转换的 staging 产物/清单/来源关系，让合法准备后 materialize 可验证，未经授权篡改仍失败。源备份始终不变。

## A7-R1-02（P1）：错误 workspace 根静默产出“完整”但无历史的备份

位置：`deploy/desktop/Backup-WorkspaceEasel.ps1:64`–`:77` 的 WorkspaceRoot/deployment 读取与后续挂载；`create_backup` 的缺目录分支及 complete=True。

正式脚本只检查用户传入 WorkspaceRoot 存在，未与 deployment.json 的真实已启动 workspace_root 绑定。传入另一个存在的空目录后，底层把已有用户的 workspace/home/state 全记为 False，却仍输出 version=2/complete=True/clean_stop=true，verify 也通过。看似完整的备份恢复后永久缺少私有文件与原生历史。

主审复现（原目录真实含 sentinel 和 HOME/.dsh 历史，已干净 Stop）：

```text
WRONG_ROOT_COMPLETE=True
MEMBER_PARTS={'home': False, 'state': False, 'workspace': False}
WRONG_ROOT_VERIFY_ACCEPTED=True
```

修复：备份源以可信部署身份和实际路径/挂载为准，用户参数只允许规范化后匹配已登记根；不同物理根、重解析/路径别名、采集失败明确拒绝。v2 完整模式不能在未提供 workspace 根时默认产出 complete。区分尚未执行过的新 workspace（可合法没有材料）与已经物化后数据根缺失，不能一律静默按空处理。

红测必须走正式 PowerShell 入口的错误空根/已有历史场景及底层真实 Store；不能只把字符串 marker 加进脚本。合法首次无执行数据、正确根的正常备份/v1兼容保持。

## A7-R1-03（P1）：只有 manifest 的空备份即可授权升级，且校验晚于新 control 启动

位置：`deploy/desktop/Start-WorkspaceEasel.ps1:496`–`:507`、后续 upgrade-complete；关联前面的 compose up。

UpgradeFromBackup 仅调用 Assert-EaselBackupManifest 形状校验、hash manifest，并匹配旧 deployment/完成记录；没有运行完整 verify。独立审查运行正式 Start + 临时 Docker/WSL stub：备份目录只有 manifest.json、完全没有 DB/key/workspace/files 清单，仍执行 upgrade-complete/reopen 并 exit 0：

```text
upgrade_complete_sent=true
verify_backup_sent=false
reopen_count=2
输出 upgraded_from_backup 和 started
```

此外升级分支在新镜像 compose up/打开正式 DB 之后才查备份；若新程序发生迁移，此时才发现无可用回滚备份已太晚。

修复：任何升级镜像可能打开/迁移正式 DB、修改升级完成记录或放开接纳之前，先对真实完整备份执行只读全量校验，并精确匹配当前项目/原版本/停止承诺/原配置及必要 key 来源。只有 manifest 的目录、缺 key/DB、损坏数据、错项目/错版本备份必须在这些副作用之前拒绝。复用修复后的 verify，保留生命周期 Mutex，不改成无条件 resume。

新增正式脚本反例断言没有 compose up/数据库可写 helper/upgrade-complete/reopen 等提前副作用；正常固定镜像升级和匹配备份回滚继续通过。无 schema 变化也要测命令次序，不能以当前两镜像都是 v14 忽略未来升级安全。

## A7-R1-04（P2）：已有数据库丢 key 后 Initialize 仍创建替代 key

位置：`deploy/desktop/Initialize-WorkspaceEasel.ps1:145`–`:155` 的 keygen --ensure；A7 未对该入口补充状态检查。

初始化 helper 只挂 key 卷、不查询 control 数据库/已有身份，因此 master.key 缺失时仍无条件生成新 key；签名私钥及其公钥同时丢失时也可重新创建一套。Start 后续失败不能补偿“初始化已成功生成替代 key”的错误行为。

独立审查正式 Initialize + fake Docker 反例：首次成功后保留已有 control DB 标记、删除 master，再次 Initialize 仍 exit 0/key=created，新 key != 原 key。

修复：显式区分全新部署与既有数据库/身份材料。既有部署任何必需 key 缺失/错误时初始化拒绝、不创建新 key、不覆盖已有配置/公钥，也不 chown 接管外来卷。仅全新空部署可创建初始材料；key 卷部分状态应失败关闭或按已存在私钥安全派生公钥，不能换私钥。支持从已验证备份恢复材料，保持 A1/login-only 兼容与既有合法幂等初始化。

补真实 DB/专用卷 sentinel、master 丢失、签名材料整套丢失、部分材料丢失及新空部署正例；关键断言是无新 key/原数据字节不变，而不是仅检查最后退出码。

## 手册关联修订

两份手册的 Verify/完整备份/升级安全声明要与上述修复实际一致。Linux 手册开头仍有“provisioner 固定 lifecycle、尚未启动 Run worker”的旧断言，Windows 前言也应与完整 Desktop 第9节统一；更新当前代码事实，同时保留生产 Linux/XFS/firewall/IPv6/Chromium/真实供应商的未通过状态。勿把本轮演练描述成目标 Linux 实测。

## 本轮主审验证与边界

- operations_a7 / desktop_operations_a7 / v1 operations / PowerShell identity / lifecycle lock / 文档六文件：**84 passed, 1 skipped（16.81 秒）**。
- 独立服务审查 12 项契约测试通过，另行复现 integrity 与错误根反例；独立部署审查用正式脚本 stub 复现升级和初始化问题。计数不重复相加。
- 主审两项真实 Store 故障探针使用 TemporaryDirectory，结束后自动清理；没有 Docker 或开发者资源操作。
- `git diff d9c2f5b HEAD --check` 退出 0。
- 作者 1 passed 的真实全流程与手册正常路径证据保留为作者实测，主审本轮未重跑 257 秒容器演练。它们不覆盖上述确定性故障路径。
- 真实供应商、生产隔离与其它既有 not-run 保持原状。

## 给代码 Agent

仅修 A7-R1-01/02/03/04 及关联手册/测试；以 `04b5f23e36879e72fbafa345dc2ed06abd26a5ae` 为基线，逐项先红后绿。不要通过放宽完整性、生成 key、删除历史、清屏障或迁移降级使测试变绿。保护主审文档，显式路径提交，交付完整 SHA/反例/正常恢复回归/真实与未执行边界，停止等待终审，不自动上线。
