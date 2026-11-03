# 验收实例503修复R4-R1：远程副作用前持久归属

父基线 `34ede47`，继续 `C:\Code\Easel-acceptance`、`fix/acceptance-503-r3`。先确认HEAD/干净状态。见 [R4独立复审](../audits/2026-11-03-acceptance-503-review-round-5.md)。仅一项P1，不扩业务或部署框架。

## R4-R1-01 控制进程退出后的恢复

现有fence只在异常或取消handler执行。正常ready刷新时远端已写G2/pending、控制进程尚未保存新attempt便直接退出，重启仍永久503。先原样运行主审 `probe_r4_ready_crash.py <源码根>` 留红，不修改主审探针；修复后红态断言可能失效，以实际状态和正式正向回归判断。

在可能发生远程副作用之前，以最小、受CAS保护的持久状态保存本轮精确attempt及恢复归属；或给出等价、严格绑定精确attempt/worker/config的崩溃恢复方案。不能仅在except中补写，不能无条件覆盖pending、忽略精确ID或手工修DB。

正式测试必须使用独立控制子进程直接退出，不用CancelledError代替：

- 正常ready经ensure_ready刷新；远端写G2/pending后，在返回/staging前直接退出。重启后普通首屏成功解析/接管G2，不重复创建worker/gateway，正常Stop按G2收口。
- 显式restore同一窗口；两入口保持统一归属语义。
- 本轮attempt已持久而远程尚未执行便退出，也可恢复，不永久starting或错误删除仍可信G1。
- 保留超时/断连/协作取消、恢复再次中断、旧owner迟到写回护栏；有已接纳Run时不得误结算Run/撤销其token或破坏必要收尾。

不要为所有操作增加通用状态机；只收口此窗口，复用已审归属与恢复机制。

## 验证交付

先红→最小实现→绿。受影响attempt/runtime/store/Workspace API契约回归；R3屏障与messages/Run收尾作为护栏保持通过。不重跑无关全量环境、前端和真实平台。

控制生产源码变更后重建control镜像、逐文件核对摘要；未改worker/gateway不重建。未改正式更新流程可引用历史4382180更新/故障重试证据，明确非本轮重跑。

独立R4-R1证据目录，列正式命令/退出码、子进程退出事实与重启恢复终态、提交/镜像/源码哈希、临时资源清理、not-run。交付停止等待主审。

不合并、不推送、不更新用户实例；不操作bf8e工作区/用户DB/Key/Cookie/attempt；不调用真实模型/平台。不自动进入下一组。
