# 验收实例503修复R4：正常ready刷新未知结果单项收口

父基线 `ac863e2`。继续当前交付分支 `fix/acceptance-503-r3` 与实际worktree `C:\Code\Easel-acceptance`；先确认HEAD/干净状态，不切错至仍为c9c0f0a的旧repair worktree。见 [R3独立复审](../audits/2026-11-02-acceptance-503-review-round-4.md)。R3两项已通过，本轮仅处理一个残留P1。

## R4-01 正常ready的gateway刷新丢回复

先运行主审 `probe_r3_ready_refresh_loss.py <源码根>` 保存红态，不改主审探针。它编码旧缺陷断言，修复后以真实输出和正式正向回归判断，不要求继续满足红态断言。

正常ready租约因gateway故障进入刷新时，远程ensure可能已经替换G2并写新attempt/pending，而控制端尚未保存该新attempt便超时/断连/取消。要求此入口同样保留可恢复的精确归属，普通首屏重试能解析/采用真实候选，不永久gateway_cleanup_pending。

- 选择最小、自洽的归属方案，覆盖远程请求发出前后与DB staging之前的窗口。不得只去掉 `state == starting` 条件却仍未持久真实新attempt；不得忽略worker/config/instance/owner校验或无条件completed。
- 正式确定性测试从真实ready开始，通过ensure_ready实际刷新入口；超时/连接丢失、取消后重启、解析期间再次中断都可前进。验证恢复不重复创建worker/gateway，旧owner迟到清理不能破坏新代次。
- 核对正常ready下显式restore_gateway同类远程回复未知窗口；若共用归属机制一起覆盖，避免两个入口再次分叉。无需扩展其他业务。
- 重开DB/manager验证实际G2接管、下一次首屏成功、正常Stop按G2收口；不能仅把DB改成ready或用旧句柄绕过物理核验。

## 验证与交付

红→最小实现→绿，固定Agent Python/PowerShell；受影响attempt/runtime/store/gateway客户端与Workspace API回归即可，不重复无关前端/平台矩阵，不以全仓环境失败作为本轮耗时工作。

R3的原子屏障与messages/既有Run收尾测试作为护栏保持通过，不修改已验收更新脚本。若只改控制runtime/store，更新control镜像并核对源码摘要；不需要重建无变化的worker/gateway。

证据写独立R4目录，列提交、确定性红绿/退出码、镜像源码哈希、not-run与临时资源清理。已有隔离4382180更新/重试证据可按未改路径引用，不冒充本轮重跑。

不合并、不推送、不更新用户验收实例，不操作bf8e工作区/用户DB/Key/Cookie/attempt，不运行真实模型/平台。交付后停止等待主审。R3已关闭项不自动重开。
