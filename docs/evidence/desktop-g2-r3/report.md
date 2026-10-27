# Task G2 R3 复审返工证据

日期：2026-10-28（Asia/Shanghai）

父提交：`14bfff0636caa63cccf8ec6a36b7b462a9f3e0a1`
本轮仅修 G2-R3-01；未进入 A4，未重做 G1/A3 或整套镜像构建。

## G2-R3-01：物理 abort 成功后的完成 tombstone

- attempt JSON 增加 `status: pending|completed`，保留 version 兼容：
  无 status 的旧记录按 pending 读取。
- 物理 abort 成功后不再删除 attempt 文件，而是原子持久化同一记录的
  `status=completed`（tombstone），然后才返回成功。
- 同一 attempt/同一 worker/gateway ID/同一 scope 的重试：
  - 记录为 completed 时直接幂等成功，不再执行 gateway stop / worker stop；
  - 记录为 pending、但物理资源已不存在时，按原 scope 做幂等终态核验后
    再写 completed。
- 记录缺失、attempt/ID/scope/flags 不匹配，或新 attempt 已取代旧记录时，
  仍返回 `gateway_abort_superseded`，绝不把所有缺记录情况当成功。
- completed tombstone 写入失败返回 `gateway_abort_record_failed`，保持
  pending 记录；重试可有限完成，不假报已收口。
- 新 ensure 写入新的 pending 记录并取代旧 tombstone；旧 completed 重试
  不能作用于新资源。业务完整 stop 仍清理匹配记录。
- Store v13 的 cleanup purpose/scope 与 gateway-only `finish_gateway_abort`
  保持不变：恢复只删本轮 gateway，既有 worker/凭证/历史/Run/容量/token
  保留并回到 ready/unavailable。

## 红证据

主审 R3 复现：

```text
FIRST_ABORT_SUCCESS True ATTEMPT_RECORD_EXISTS False
RECOVERY_RETRY gateway_abort_superseded
RECOVERY_RETRY gateway_abort_superseded
PHYSICAL_GATEWAY_STOP_CALLS 1
```

即物理清理成功、control 未提交 DB，恢复重试永久卡在 stopping。

## 绿证据

```text
G2 新增单元（server/runtime/runtime-gateway/store/create-recovery）
39 passed

指定 provisioner/runtime/store/stop/gateway/拓扑回归
288 passed, 6 skipped

tests/multiuser + Desktop 拓扑
1261 passed, 9 skipped, 2 warnings

真实双 workspace + create 超时 + gateway-only 保留 worker
3 passed, 78.08s
```

新增恢复测试：

1. `test_completed_abort_recovers_control_crash_before_db_commit`
   - 两个真实 Store 连接 + 实际 `Provisioner.abort`；
   - 物理 abort 成功但故意不执行 `finish_gateway_abort`/`stop_worker`；
   - 重启 provisioner 后 `_recover_stop` 重试同一 attempt，幂等成功；
   - DB 从 stopping 回到 ready/unavailable，worker ID/token/Run 保持，
     物理 stop 调用仍只有一次。
2. `test_completed_record_write_failure_keeps_pending_and_retry`
   - 物理完成后 completed 写盘失败返回 `gateway_abort_record_failed`；
   - 文件保持 pending，恢复后重试写 completed 成功。
3. `test_gateway_only_abort_survives_provisioner_restart`
   - completed tombstone 重启后可读；重复 abort 不再物理停止；
   - 不匹配 attempt 和新 attempt 取代旧记录均拒绝旧请求。

固定测试镜像：

```text
GATEWAY_IMAGE sha256:c0a35aaa9f304c47f9f5c3e73add7d9d5920041baef156e2ea674f3000c5580a
WORKER_IMAGE  sha256:0d8ea2c3e6f21d3ba9cb12125f80dd851eef73c013d0fb24d7a69676dfab7ef4
```

真实 gateway-only 路径（复用未变化的固定镜像）：

```text
GATEWAY_ONLY_KEPT_WORKER
{"gateway_removed": "7a95f45eb3a1dfe30aacdda22b152bc7ddf16c3895ce3d74a82f1727a15d636c",
 "running": true,
 "worker": "3f87209c183c40a306717d3be105620df1fe0915ec99d061bdea0964ab6e4b68"}
```

真实双 workspace handles（本次最终复跑）：

```text
workspace 5fc472e16f9c4cf79296acff37cbb63e
  worker  8e3f1b40d28a405241d29d948709c5abf6266483bd846fc329dae30b5d456704
  gateway e85c5506aee0197f61d71a363879eaf91aa00f4f0fbed64594985bac58205db3
  boot    df5b11382a906431fdf1f55a4d93073f
workspace 7722cc724f6e4478848d02e665d16440
  worker  4461d7d590298938d68aa233f1ef341fc8bdb763643374753968290ed4d2e446
  gateway f90a659322b91b22d089f297519775ac237cfff61e45c951f828ae8faf6e4981
  boot    298640e780199e0ecd4b04f0e5beda2c
```

## 清理与边界

- 真实测试后按随机名称/精确 ID 删除 worker、gateway、worker 网络和临时
  control 网络；容器与 `easel-g2*`/`easel-ws-*` 网络检查为空。
- 未执行全局 prune，未操作开发者资源、HOME/profile、ngrok 或供应商。
- 未实现 A4 公开 relay、broker URL/受管 Agent 接线、真实 DSH、Chromium
  或完整多用户聊天。
