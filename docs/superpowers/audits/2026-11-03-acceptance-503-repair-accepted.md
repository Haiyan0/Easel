# 验收实例503修复组：代码验收通过

日期：2026-11-03（Asia/Shanghai）。最终交付 `6dde13e`，本轮生产 `6ae4b84`、`75d6912`，父基线 `4dd3550`。源码 `C:\Code\Easel-acceptance`，分支 `fix/acceptance-503-r3`。实施工作树干净。

## 裁决

R4-R3-01通过，关闭正式Stop删除旧执行围栏的P1。本组已下发的503修复阻塞全部关闭，**代码验收通过，可准备受控维护切换和用户验收**。本审计没有执行合并、推送或更新用户实例，也不代替切换后的真实页面/聊天验收及总计划中剩余6A事项。

## 主审新鲜验证

- 15文件受影响契约：278 passed，4 skipped，59.81s，exit 0。包括503/R4-R1/R4-R2/R4-R3、runtime、Workspace API、媒体/请求租约、屏障、desktop update、proxy/messages等护栏。
- store/生命周期4文件与provisioner6文件合并运行：275 passed，1 skipped，20.47s，exit 0。文件与上组不重叠，合计**553 passed、5 skipped**。
- 原Stop红态探针未改，主审复跑在旧的“Stop后记录必须为None”断言退出1，证明已不再删除；由正式正向回归核验合法无资源记录、重启后A拒绝、新C正常进入，不将单纯断言失败写为产品验证通过。
- 在无网络、只读源码挂载和只读根文件系统、临时tmpfs的自动删除容器中，主审运行真实 `UnixProvisionerClient` 与 `Provisioner._handle_client` 的Stop→重启契约：`STOP_KEPT_FENCE`、`RESTART_LATE_A_FENCED`、`UNIX_CONTRACT_OK`，exit 0。新C的 `prepare_workspace reachable` 异常是夹具确认准入成功的预期sentinel，不是产品失败。
- 独立审查者只读审阅生产diff、精确实例/失败路径及新增测试，另跑新增7项通过，结论无本组确证阻塞。
- `git diff 4dd3550..HEAD --check`无输出，实施工作树干净。
- 主审核对provisioner工作树SHA-256为 `7164c0c0066a5f477530c8350ae4b655ef4aa10055c18ceb77b586e282d95bee`。LF规范化为作者记录的 `860a3f148a7365cb3ec12bb5b860e9a68256ee6071cc1573abf8760f19901b03`；正式运行身份以实际WSL源码字节为准。

## 关闭依据

`_clear_attempt`仅在正式Stop完成精确实例物理清理后执行。有围栏记录原子改写为completed/scope=none/未创建资源，保留A并围栏已停止的B；重启后的ensure/restore在副作用前拒绝旧A/B。新C能正常进入并继承围栏。

无围栏的普通清理保持既有删除/幂等行为；错实例或物理停止失败不触碰围栏。`recover_abort`旧坏记录分支过滤并去重合法围栏；abort完成保留围栏。此前接管、真实子进程退出、未知回复恢复、旧代次迟到写回及请求原子屏障回归均保持通过。

旁支已有旧坏记录commit恢复分支过滤未统一去重，仅记录为非本轮新增的维护事项；未发现真实旧部署无fenced字段输入因此受影响的证据，不据此扩大本组阻塞。

## 维护切换边界

- 使用正式 `Update-WorkspaceEasel.ps1`，不能只替换磁盘源码再运行普通Start，以免继续复用旧provisioner进程。
- 本组control镜像沿用 `sha256:b30351b19ff72bd38b371d81c607db582c9e242581fb90776ef39564d9092ee3`；runtime/store未再变。新provisioner从实际WSL源码启动，该文件不在control镜像内。维护时必须同时核验控制镜像和运行中provisioner源码身份。
- Update/Stop→Backup→Verify完整容器链本轮未复跑，按任务范围引用R2的4382180隔离正常更新/故障重试历史证据；不把它写为最新树重新演练通过。
- 维护前应重新读取实际deployment登记参数/运行镜像/WSL路径/GID，沿用正式更新屏障、活动工作等待、精确身份核验与失败保留屏障流程。不得仅凭文档示例覆盖实际登记。
- 切换后再验收首屏6接口、短聊天、gateway恢复、正常停止与备份；真实模型/平台仍由用户自行验收。旧镜像不保证可打开已迁移schema，不宣称旧镜像回退通过。

本轮未读取Key/Cookie、未修改用户DB/attempt或bf8e工作区容器，未调用真实模型/平台。除无挂载摘要核验/只读源码Unix契约临时容器外，未更新或重启任何用户运行资源。
