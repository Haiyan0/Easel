# Task A3 验收通过

验收提交：`8a5c16be2b80328f234ad4693b00b6795a23a12f`。
返工父提交：`a384d2a0048eb80c786d8d1b0356cd6712146a95`。

## 裁决

A3 通过。A3-R1-01/02/03 与 A3-R2-01 关闭；执行入口、公钥装配、exec 健康检查、drain/停机、凭证一致性及启动失败清理达到本任务范围要求。

本轮 R2：run_task 受 shield 保护，取消后清理等待 CLI 终结再读 cidfile；按本轮不可复用 ID 删除并确认，检查无同名后继实例后才停止 sidecar，成功最后删 cidfile；失败保留追踪材料和仍有使用者的 sidecar。独立复核无新增阻塞。

## 主审本轮实际验证

- server/lifecycle/runs/provisioner/stop-recovery/sidecar + 文档路径：**201 passed, 1 skipped**（9.57 秒）；其中原指定回归 191、文档 10。
- 显式开启真实 Docker，`test_provisioner_cleanup_real.py`：**1 passed**（10.28 秒）。覆盖真实 docker run 创建与 CLI 超时、cidfile 定向清理；不冒称本轮重跑真实 ACP/SIGTERM。
- 实测后按 easel-worker-* 名称与 easel.workspace-id 网络标签检查，输出为空。
- `git diff a384d2a 8a5c16b --check`：退出 0。
- 独立复核：5 项定点测试通过，另用纯逻辑探针确认正常及删除失败时 sidecar/cidfile 的顺序。

R1 的真实 SIGTERM、正常协议/历史证据已审阅并保留；R2 未改应用代码，不要求重复整套。主审没有重新构建最终 runtime/真实 DSH 镜像。

## 交付边界

- 当前完成的是实际 worker 执行能力与容器协议，不等于 control 已可通过 Desktop 拓扑派发聊天。
- gateway 尚未实现，control→gateway 断言与 A4 broker 接线尚未装配，真实 Harness/供应商仍待 A5/A6。
- 公钥只进 worker，真实私钥/供应商秘密不进入 worker 的边界保留。
- 正式 Linux/XFS/防火墙/Chromium 发布门槛不变。

## 下一步

按 `../tasks/2026-10-27-desktop-g1-gateway-core.md` 仅实施 G1 独立 gateway 核心。G1 不修改 provisioner/WorkerHandle/数据库与现有 control 装配；验收后再单独下发 G2 生命周期和 control 接入。执行时同时读取 A2 验收单的地址绑定与重启防重放附加约束。
