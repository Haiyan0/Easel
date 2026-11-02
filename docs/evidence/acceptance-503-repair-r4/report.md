# 验收实例 503 修复 R4：交付报告

日期：2026-11-02（Asia/Shanghai）

父基线：`ac863e2`（R3 最终 tip）
分支：`fix/acceptance-503-r3`
实施 worktree：`C:\Code\Easel-acceptance`

本轮提交：

| 提交 | 内容 |
|---|---|
| `8dc45e2` | R4-01 正常 ready 刷新/显式 restore 的未知结果保留精确 attempt 归属 |

本轮只处理 R4-01。R3-01/R3-02 已关闭项未重开；未修改前端、更新脚本、
平台功能或 worker/gateway 源码；未合并、未推送、未更新用户验收实例。

## 1. R4-01 正常 ready 刷新丢回复

### 根因

`_refresh_gateway` 的未知结果分支只对 `lease.state == "starting"` 保留归属。
正常 ready 租约因 gateway 失效进入刷新时，远程 ensure 可能已替换 G2 并持久
新 attempt/pending，但控制端在收到回复前超时/断连/取消；此时旧代码只把
DB 标为 `ready/unavailable`，新 attempt 从未落盘。下一次首屏按旧 G1/旧
attempt 收口，新 ensure 又被 provisioner 的精确 pending 归属拒绝为
`gateway_cleanup_pending`，永久 503。

正常 ready 的显式 `restore_gateway` 存在同一窗口：`_restore_candidate`
的超时/连接丢失只上抛，未持久本轮 attempt。

### 实现

- 新增 `Store.fence_unknown_gateway_refresh`：当租约仍是本轮读到的
  owner/generation/attempt 且 `pending_attempt_id = ''` 时，原子写入本轮
  精确 attempt、递增代次、转为立即到期的 `starting`。ready 与 starting
  共用同一归属语义；不按目标猜测候选、不结算在途 Run、不写 `completed`。
- `_refresh_gateway` 的未知结果（`provisioner_timeout` /
  `provisioner_connection_lost` / `provisioner_invalid_response` /
  `worker_start_timeout`）与取消都走该围栏；连接阶段/确定失败仍按原逻辑
  回退 `ready/unavailable`。
- `restore_gateway` 经 `_restore_candidate_guarded` 共用同一围栏：客户端
  传输丢失码与 `gateway_recovery_timeout`（可能发生在请求发出后）都会持久
  本轮 attempt；确定失败不围栏。
- 恢复仍走既有 `_takeover_for_restore` → `_resolve_fenced_attempt` →
  `_recover_lost_attempt`：按精确 attempt 读取 provisioner 持久候选，复核
  worker/config/target 后 adopt/commit；代次递增使旧 owner 的迟到写回与
  清理 CAS 失效。

## 2. 红态证据

父基线 `ac863e2` 临时 worktree 原样运行主审探针
（`red-probe-ready-refresh.txt`，退出 0）：

```text
1 provisioner_timeout DB ready bbbbbbbb <old-attempt> remote pending ffffffff <new-attempt>
2 worker_start_failed DB ready bbbbbbbb <old-attempt> remote pending ffffffff <new-attempt>
3 worker_start_failed DB ready bbbbbbbb <old-attempt> remote pending ffffffff <new-attempt>
CONFIRMED: 普通 ready 首屏丢回复后，重试与跨租约期限重试均仍 503
```

同一基线运行本轮新增确定性测试（`red-tests.txt`，退出 1）：

```text
7 failed, 36 deselected
```

7 个失败均停在“DB 仍为 ready、未持久新 attempt”的断言。

## 3. 绿态证据

修复后原样复跑主审探针（`green-probe-ready-refresh.txt`，退出 1）。退出 1
来自探针自身编码的旧红态断言 `current.state == "ready"` 不再成立；真实
输出显示新一轮已从 `ready` 进入可接管状态，且 DB 与远程 attempt 精确一致：

```text
1 provisioner_timeout DB starting bbbbbbbb <attempt> remote pending ffffffff <same-attempt>
AssertionError: assert current.state == "ready"   # 旧红态断言失效
```

正式正向回归（固定 Agent Python）：

| 证据文件 | 范围 | 结果 | 退出码 |
|---|---|---|---|
| `green-new-tests.txt` | R4 新增 7 项 | 7 passed | 0 |
| `green-attempt-ownership.txt` | `test_attempt_ownership_503.py` 全文件 | 43 passed | 0 |
| `green-affected-contracts.txt` | R3 受影响 12 文件契约组 | 255 passed, 4 skipped | 0 |
| `green-store-lifecycle.txt` | store/生命周期补充 7 文件 | 110 passed | 0 |

新增确定性测试：

- `test_ready_refresh_lost_ensure_recovers_through_first_screen`（参数化
  `provisioner_timeout` / `provisioner_connection_lost`）：真实 ready 经
  `ensure_ready` 刷新入口丢回复后，DB 为 `starting` 且 attempt 与远程
  pending 精确一致；重开 DB/manager 后恢复 G2，恢复阶段 `ensure=0`、
  `resolve=1`，正常 Stop 按 G2 收口；旧代次迟到写回返回 False。
- `test_ready_refresh_cancel_keeps_recoverable_fence`：远程 ensure 中被
  取消后仍保留可接管的精确 attempt，下一次首屏恢复 G2。
- `test_ready_refresh_interrupted_recovery_keeps_fenced_attempt`：恢复解析
  再次中断时保留 `pending_attempt_id`，下一 owner 继续前进为 ready/G2。
- `test_ready_restore_lost_response_recovers_exact_attempt`：正常 ready 的
  显式 `restore_gateway` 丢回复与刷新入口共用同一归属恢复。
- `test_ready_restore_unknown_result_fences_exact_attempt`（参数化
  `gateway_recovery_timeout` / `provisioner_timeout`）：显式 restore 的
  远程超时同样持久新 attempt，恢复不重复创建 worker/gateway。

R3 原子屏障、messages/既有 Run 收尾、attempt/runtime/store/gateway client
与 Workspace API 护栏均包含在受影响契约组中通过。

## 4. 镜像与源码摘要

- 镜像：`easel-control:503-r4-final`
  = `sha256:0f9c337ef926f6bb3e300b7d90301552a652b1b78b84318f6a24c01f51d6cb69`
- 构建命令（退出 0，日志 `control-build.txt`）：
  `docker build --progress=plain -f deploy/desktop/Dockerfile.control -t easel-control:503-r4-final --build-arg HTTP_PROXY=http://host.docker.internal:7897 --build-arg HTTPS_PROXY=http://host.docker.internal:7897 .`
- 镜像内与工作树源码 SHA-256 一致（`image-hashes.txt`）：

| 文件 | SHA-256 |
|---|---|
| `easel/control/runtime.py` | `c8c301d698a39f6dda00c7d08b04d35ca902237d68af71bc2e2af360a4f6b816` |
| `easel/control/store.py` | `4183d0a59520450b2ee349e75ed5d49f558f5a120cdbe6813d32f864a41eff2e` |

本轮未改 worker/gateway 源码，未重建对应镜像。镜像仅本地构建，未推送、
未覆盖用户镜像。

## 5. 未执行项与清理

- 未复跑容器级 Update / Stop→Backup→Verify：Update 脚本与部署流程未改，
  按 R4 执行单沿用既有 4382180 正常更新/故障重试证据，不冒充本轮重跑。
- 未运行全量 `pytest tests/`、前端或平台矩阵；按执行单只跑受影响
  attempt/runtime/store/gateway client/Workspace API 与补充 store/生命周期。
- 未启动真实模型、真实平台或供应商请求；未读取 Key/Cookie；未操作
  bf8e 用户工作区、DB、attempt 或验收实例。
- 未建立真实 Unix socket；探针与测试均使用临时 Store/attempt 与假远程。
- 红态临时 worktree `C:\Users\hyh\AppData\Local\Temp\easel-r4-red-worktree`
  已移除；探针临时目录自动清理；`docker run --rm` 摘要核对容器自动移除。
- `git diff --check` 无输出；工作树提交后干净。未合并、未推送，交付后
  停止等待主审。
