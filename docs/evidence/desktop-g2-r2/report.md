# Task G2 R2 复审返工证据

日期：2026-10-28（Asia/Shanghai）

父提交：`65cec868d933679fc850b5e7c84f38dc75bcc22a`
本轮仅修 G2-R2-01；未进入 A4，未接公开 relay/聊天。

## G2-R2-01：持久停止目的/范围与 gateway-only 恢复

- Store schema 13 只追加：
  - `cleanup_purpose`
  - `cleanup_scope`
  - `cleanup_worker_created`
  - `cleanup_gateway_created`
- 启动 abort 在 `starting → stopping` CAS 中持久化：
  - `purpose=start_abort`
  - `scope=gateway|worker|worker_gateway`
  - attempt_id、worker/gateway 不可复用目标与 created 标记。
- 业务 drain/stop 在 `begin_worker_stop` 写 `purpose=business`、
  `scope=worker_gateway`；旧 stopping 记录默认按业务完整停止解释。
- `recover_stop` 严格按持久 purpose/scope 恢复：
  - gateway-only：调用受限 abort 只删 gateway，然后
    `finish_gateway_abort` 恢复 `ready/unavailable`，保留既有 worker ID、
    service token、历史、在途 Run、容量和 broker token；
  - worker/worker_gateway：只按对应范围 abort；
  - business/旧记录：才执行原完整 worker+gateway stop。
- abort/stop 失败、超时或取消只保留原 stopping/scope/attempt，不扩大范围；
  新 owner 在停止承诺消解前不能采用实例。

### provisioner 重启恢复

- provisioner 在 `workspace_root/.gateway/attempt-<workspace>.json` 持久化
  当前 attempt 的目标 ID、created 标记与 scope（不含 secret），原子替换，
  权限 0600。
- `abort` 必须匹配该持久记录和请求 scope/flags；内存 `_attempts` 丢失后
  从文件重建。记录缺失/不匹配返回 `gateway_abort_superseded`，不回退完整
  stop。
- 业务完整 stop 成功或 abort 成功后清理匹配记录；遗留材料保留。

## 红证据

主审 R2 复现的失败终态：

```text
FIRST_ERROR gateway_cleanup_failed
BEFORE_RECOVERY stopping WORKER_CREATED False
RECOVERY_FULL_STOP [(workspace, existing_worker_id, gateway_id)]
RUN_STATE running
```

即 gateway-only abort 失败后，旧实现只留下通用 stopping；过期恢复调用完整
worker+gateway stop，删除既有 worker。

## 绿证据

```text
G2/R2 新增单元（server/runtime/runtime-gateway/store/create-recovery）
39 passed

指定 provisioner/runtime/store/stop/gateway/拓扑回归
286 passed, 6 skipped

tests/multiuser + Desktop 拓扑
1259 passed, 9 skipped, 2 warnings

真实双 workspace + create 超时 + gateway-only 保留 worker
3 passed, 76.70s
```

固定测试镜像（未变化的 gateway/worker 应用，复用固定 ID）：

```text
GATEWAY_IMAGE sha256:c0a35aaa9f304c47f9f5c3e73add7d9d5920041baef156e2ea674f3000c5580a
WORKER_IMAGE  sha256:0d8ea2c3e6f21d3ba9cb12125f80dd851eef73c013d0fb24d7a69676dfab7ef4
```

真实 gateway-only 路径（临时 Docker）输出：

```text
GATEWAY_ONLY_KEPT_WORKER
{"gateway_removed": "be8c973080d768830b1c2028905936407bb65aef983321035ebfd02f522afdd0",
 "running": true,
 "worker": "b3bbc890c802b778cefb11fd4fed0f882d301e8da741de5c8f622c00e6e60872"}
```

该路径先创建真实 worker/gateway，只删除 gateway 后重新 ensure（候选
`worker_created=False, gateway_created=True`），再执行 gateway-only abort；
真实 worker 容器仍 Running、原 ID 不变，随后业务 stop 仍可完整删除 worker。

真实双 workspace handles（同一最终复跑）：

```text
workspace 8cc09873066d46f497b2ed385da7b2b8
  worker  8a9501638362224353a9a7d586a1121bcf479bff949b3455f895c771bf31bb15
  gateway 49cfc6d1111b95fa6741f0816bcb32bed5c5c0179f37d93bdd057a4cf249cab9
  boot    c8600616fec54c5d63cf8b348c06b53e
workspace c467c00e3373411b9047e1e997657c7a
  worker  6d448fae6bb6b3c2b2f5d4e5ea6e7473503c42ad49a1dc82c56cccd7e7eca9ce
  gateway afbddd018cb3dd7fdbbab9ad44c79b3cedc40e940ad962e4aa60797be36cfde3
  boot    20d2b83c2d523d1e33b1deae1b533bd1
```

覆盖两个 workspace、签名 health/submit/events/questions/stop/messages、
跨 workspace 拒绝、worker 反向不可达、同容器重启新 boot、旧断言 503→403、
worker ID/在途 Run 保持、精确 ID drain、create CLI 超时清理，以及本轮新增的
gateway-only 保留既有 worker。

## 清理与边界

- 真实测试后按随机名称/精确 ID 删除 worker、gateway、worker 网络和临时
  control 网络；`easel-gateway-*`、`easel-worker-*`、`easel-sidecar-*`
  容器与 `easel-g2*`/`easel-ws-*` 网络检查为空。
- 未执行全局 prune，未操作开发者容器/卷/网络、HOME/profile、ngrok 或供应商。
- 未运行公开 relay/聊天、A4 broker/受管 Agent、真实 DSH、Chromium；Linux
  生产/XFS/防火墙门槛不变。
