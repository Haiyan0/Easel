# 验收实例503修复R3：超时未知结果与更新请求边界

继续 `C:\Code\Easel\.claude\worktrees\acceptance-503-repair`，父基线 `c9c0f0a`。仅两项，见 [R2独立复审](../audits/2026-11-02-acceptance-503-review-round-3.md)。R2-01/04/05保持关闭，不扩业务或部署框架。

## R3-01 普通网络错误的未知结果（P1）

先运行主审 `probe_r2_lost_ensure_error.py <源码根>` 保存红态，不修改主审文件。该探针断言当前缺陷，修复后可能因红态断言不再成立退出；以产品正式正向测试和实际输出确认，不能修改探针来掩盖差异。

新增真实持久化回归：远程 ensure 已完成替换G2且写pending，但回复以 timeout/EOF/连接错误丢失，control仍保存G1。关闭重开DB和manager后普通首屏能按精确attempt恢复，resolve实际被调用，不无限gateway_cleanup_pending，不重复创建worker/gateway。覆盖取消原路径、普通异常路径与重复中断。

选择最小一致方案保留或恢复未知结果归属；不能忽略精确ID/config/owner校验，也不能将未确认结果标completed。真实Unix客户端错误映射要纳入测试，避免只用CancelledError。恢复后正常Stop按真实候选收口。

## R3-02 更新屏障与请求接纳的原子边界（P1）

先运行主审 `probe_r2_update_late_request.py <源码根>`。用正式Event测试覆盖ready claim之后/请求租约登记之前，Update建屏障并观察零计数的交错。修复后结果只能是请求在屏障前被计数、更新等待它结束，或屏障后请求被拒绝且没有上游传输；不能零计数放行更新后再启动请求。

使接纳/登记与屏障检查处于正确的事务边界；区分新workspace请求和既有Run的必要收尾。保留已接纳Run的stop/events/answer/broker行为，不让更新等待它们时阻止其完成。覆盖流式成功关闭、异常、取消、请求租约释放；不等待所有静态坏worker租约。

## 验收与交付

- 先展示红绿，独立R3证据目录。受影响runtime/attempt/workspace API、request lease/stop barrier、desktop update契约回归即可；不重复无关前端/平台矩阵。
- 既有更新脚本未改可引用已通过的4382180正常更新/故障重试容器证据，明确本轮未复跑；若修改正式更新路径，则复跑相应隔离流程。生产文件变动需更新相应镜像并核对源码摘要。
- 两条新交错必须有确定性Event回归，不以反复跑全量碰运气替代。保留旧探针输出与原证据，不改主审记录。
- 交付列提交、正式测试命令/退出码、镜像与源码哈希、未执行项、临时资源清理。工作树干净后停止等待复审。
- 不合并、不推送、不更新用户验收实例；不操作bf8e工作区/用户DB/Key/Cookie/attempt；不运行真实模型/平台，不进入6B或自动上线。
