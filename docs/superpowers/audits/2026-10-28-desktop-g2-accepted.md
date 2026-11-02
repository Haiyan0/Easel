# Task G2 验收通过

验收提交：`785f556bc81c464f59de5105a02c12305b2e6265`。
定点返工父提交：`14bfff0636caa63cccf8ec6a36b7b462a9f3e0a1`。

## 裁决

G2 通过，G2-R1-01/02/03/04、G2-R2-01、G2-R3-01 全部关闭。具备下一步 A4 公开 relay 与逐 workspace broker 装配的基础，不代表公开聊天/真实 Harness 已通过。

最终 abort 物理成功后保留 pending/completed 持久记录。相同 attempt/worker/gateway/scope/flags 的完成重试幂等成功，不重复删除；completed 写入失败保留 pending，重启后可按同一范围再核验并收口；新 attempt 替换后旧请求仍被拒绝。

先前通过的停止范围恢复、CAS/attempt fencing、refresh 恢复、worker/gateway 独立健康、创建超时追踪和每次 gateway boot 更新规则保留。

## 主审本轮实际验证

- provisioner/runtime/store/stop-recovery、G2 四文件、create recovery、文档路径：**292 passed, 1 skipped**（9.33 秒）。
- 独立复核重放原崩溃探针：DB stopping/completed → 重建 provisioner/control → ready/unavailable，原 Run running，物理 stop 一次；相关定点 **5 passed**。
- `git diff 14bfff0 785f556 --check`：退出 0。
- 本轮只核对管理记录/恢复逻辑，未重建不变的 gateway/worker 应用镜像、未重跑整套双 workspace。上一轮主审真实 Docker gateway-only 保留 worker 的结果及实施方本轮三项真实 Docker 证据保留，不冒称本轮重新执行。

## 下一阶段必须检验的接口边界

A4 需要区分“worker 身份已消失”与“仅 gateway 暂不可用/refresh”。当前公开 relay 仍是旧直连路径，不能直接把 gateway 不可用时的 None/503 当成 worker_instance_gone、自动停止实例或结算 Run。此为下一阶段的接线验收，不声称现有未装配公开路径已完成。

control→gateway 逐请求签名必须覆盖最终真实 URL/query/body；逐 Run 快照签名、用户认证/撤销窗口与 gateway 断言是不同职责，不能互相替代。

下一任务：`../tasks/2026-10-28-desktop-a4-control-relay.md`。真实 DSH/供应商、最终镜像、完整浏览器闭环和正式 Linux 门槛继续分别验收。
