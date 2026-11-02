# 验收实例 503 修复 R4-R3：交付报告

日期：2026-11-03（Asia/Shanghai）

父基线：`4dd3550`（R4-R2 最终 tip）
分支：`fix/acceptance-503-r3`
实施 worktree：`C:\Code\Easel-acceptance`

本轮提交：

| 提交 | 内容 |
|---|---|
| `6ae4b84` | R4-R3-01：Stop 清理保留旧请求执行围栏 |
| `75d6912` | R4-R3-01：过滤并去重旧记录围栏 ID |

本轮只修主审第 7 轮下发的 R4-R3-01。未重开 R4-R1/R4-R2 已审行为；
未修改前端、worker/gateway、部署脚本或平台功能；未合并、未推送、未更新
用户验收实例。

## 1. 根因

`Provisioner.stop`（`deploy/multiuser/provisioner.py:2558`）在按精确实例
完成物理停止后调用 `_clear_attempt`（`provisioner.py:2372`），后者删除内存
与整个磁盘 attempt 文件。R4-R2 把“旧 attempt 永不再执行”的持久事实保存在
同一文件的 `fenced_attempts` 中，因此正常 Stop 连带删除了该事实。重启后
`_ensure_attempt_available` 看不到围栏，已被禁止的 A 重新越过准入并写出
`A/pending`。

物理资源已清理只证明记录所属 attempt 不再需要清理承诺，不能证明 A 不会
迟到；清理必须保留“执行权已失去”的持久事实。

## 2. 修复实现（最小）

只改 `deploy/multiuser/provisioner.py`：

- **`_clear_attempt`**：物理停止成功后，若记录携带 `fenced_attempts`，
  不再删除文件，而是原子改写为合法无资源语义记录：
  `scope="none"`、`worker_created/gateway_created=False`、
  `status="completed"`，保留原有围栏 ID，并把**本次被停止记录自己的
  attempt** 也加入围栏（已清理实例的 attempt 同样不得迟到复活）。无围栏
  的普通清理仍按原行为删除文件。
- 新记录仍可正常覆盖该无资源记录（`_record_attempt`/`_record_noop_attempt`
  携带围栏），因此 Stop 不影响后续重新启动。
- **`recover_abort`** 旧坏记录分支：重建记录时携带原始
  `fenced_attempts`，abort 收口为 completed 时不丢失围栏。
- 新增 `_valid_fenced_attempts` 提取、过滤并去重原始围栏 ID，避免旧坏
  记录中的重复/非法项写出不可读记录。

审计结论（所有 `_clear_attempt`、删除、覆盖、旧坏记录、物理 abort 路径）：

| 路径 | 行为 |
|---|---|
| `stop → _clear_attempt` | 有围栏改写为无资源记录；无围栏删除（本轮修复） |
| `_abort_locked` | `dict(record)` → completed，自动携带围栏 |
| `_commit_attempt_locked` | committed 复制记录；legacy 分支保留并追加围栏 |
| `_record_attempt` / `_record_noop_attempt` | 拒绝已围栏 attempt，新记录携带既有围栏 |
| `recover_abort` 旧坏记录 | 重建记录携带原始围栏（本轮修复） |
| 实例/网关错配、物理清理失败 | 不调用 `_clear_attempt` 或提前返回，记录与围栏保持 |
| 围栏写盘失败 | 保留原记录（含围栏），绝不改为删除 |

没有内存-only、TTL 或数量截断；围栏随记录持久化，跨 Stop 与重启生效。

## 3. 红态证据

父基线 `4dd3550` 临时 worktree 原样运行主审探针（未修改探针）：

```text
EXIT=0
正式 stop 后 attempt 文件及旧 A 围栏均消失
重启后迟到 A 的正式 ensure 已越过 admission，抵达 prepare_workspace 副作用
同一旧 A 再次获准 gateway.ensure，并写回 A/pending:
{attempt_id: aaaa…, status: pending, scope: gateway, gateway_created: true}
```

原始输出见 `red-probe-stop-fence.txt`；临时 worktree 已移除。

## 4. 绿态证据

所有绿色命令均在最终树 `75d6912` 上执行（证据文件首行
`TREE: 75d6912`）。

| 证据文件 | 范围 | 结果 | 退出码 |
|---|---|---|---|
| `green-r4r3-tests.txt` | 本轮新增 7 项 | 7 passed | 0 |
| `green-attempt-ownership.txt` | 503/R4-R1/R4-R2/R4-R3 四文件 | 66 passed | 0 |
| `green-affected-contracts.txt` | 受影响契约 15 文件 | 278 passed, 4 skipped | 0 |
| `green-store-lifecycle.txt` | store/生命周期 4 文件 | 93 passed | 0 |
| `green-provisioner-contracts.txt` | provisioner 契约 6 文件 | 182 passed, 1 skipped | 0 |
| `green-unix-socket-contract.txt` | 真实 Unix 客户端/服务端 | UNIX_CONTRACT_OK | 0 |

- 修复后原样复跑主审探针（`green-probe-stop-fence-after-fix.txt`）：探针在
  自身旧缺陷断言 `stop 后 _load_attempt is None` 处失败（`EXIT=1`），说明
  记录与围栏不再被删除。按 R4-R3 补单，以实际状态和正式正向测试判断。
- 实际状态诊断（`green-diagnostic.txt`，未修改主审探针）：

```text
before_stop : attempt=B / committed / fenced=[A]
after_stop  : attempt=B / completed / scope=none /
              worker_created=false / gateway_created=false / fenced=[A,B]
late_ensure : gateway_attempt_fenced   prepare_workspace_called=false
late_gateway: gateway_attempt_fenced   gateway.ensure_calls before fresh=0
fresh C     : admitted; record C/completed; fenced=[A,B];
              gateway.ensure_calls after fresh=1
```

即 Stop 后迟到 ensure/restore 在 `prepare_workspace`/`gateway.ensure` 之前
被拒绝，新 attempt 正常准入并完成，G2 物理清理仍由 `gateway.stop` 执行。

## 5. 覆盖矩阵

| 要求 | 正式回归 |
|---|---|
| A 被围栏 → B 正常记录/commit 继承 → Stop(B) → 重启 → 迟到 ensure(A)/restore(A) 在副作用前拒绝 | `test_stop_preserves_fence_and_blocks_late_ensure_after_restart`：Stop 后无资源记录带 `[A,B]`；`ensure(A)` 在 `prepare_workspace` 前 `gateway_attempt_fenced`（sentinel 未触发）；`restore_gateway(A)` 同码；`_with_gateway(A)` 同码且 `ensure_calls==0`；无 A/pending |
| Stop 后新 attempt C 正常准入并完成，围栏不阻塞重启 | `test_stop_fence_does_not_block_new_attempt_after_restart`：C 通过准入，`gateway.ensure_calls==1`，记录 C/completed 继承 `[A,B]` |
| G2 的实际清理仍正确 | 上述测试与 `test_recovered_gateway_is_used_by_normal_stop` 断言 `gateway.stops` 精确指向被 Stop 的实例；记录改写只发生在物理停止成功之后 |
| 无围栏普通清理保持幂等 | `test_stop_without_fence_still_deletes_record`：普通记录 Stop 后删除，重复 Stop 仍为空 |
| Stop 围栏记录幂等 | `test_stop_fence_record_is_idempotent`：连续两次 Stop 记录逐字段一致 |
| 精确实例错配拒绝 | `test_stop_instance_mismatch_keeps_fence_record`：worker ID 不匹配时不改写记录，围栏保留 |
| 物理 abort 路径携带围栏 | `test_abort_completion_preserves_fence`：abort completed 后 `[A]` 仍在，A 被拒 |
| 旧坏记录恢复携带围栏 | `test_recover_abort_preserves_fence`：原始记录含重复/非法围栏项时仍写出可读记录，recover_abort completed 后 `[A]` 仍在，A 被拒 |
| 记录覆盖不丢围栏 | R4-R2 `test_fence_survives_new_attempt_record` 及本轮新 attempt 测试，新记录始终携带旧围栏 |
| R4-R2 活跃/迟到 Event 矩阵 | `test_attempt_ownership_r4r2_503.py` 11 项全部通过 |
| R4-R1 真实子进程退出 | `test_attempt_ownership_r4r1_503.py` 3 项通过；恢复阶段 `ensure=0、restore=0、resolve=1` |
| Run 收尾 / R3 屏障 | runtime、messages、lifecycle、stop barrier、proxy 等受影响契约组通过 |

旧测试 `test_recovered_gateway_is_used_by_normal_stop` 原断言“Stop 后记录
必须为 None”已按补单更新：现在验证留下的记录是无待清理资源
（`scope=none`、未创建资源），且其围栏中的 attempt 仍被拒绝。未把旧删除
断言机械当作产品要求。

## 6. 真实 Unix socket 契约

本机 Windows Python 不支持 `AF_UNIX`，在无网络、只读源码挂载的 Linux
容器中通过真实 `UnixProvisionerClient` 与 `Provisioner._handle_client`
执行：

```text
docker run --rm -i --network none \
  -v C:/Code/Easel-acceptance:/src:ro \
  easel-control:503-r4-r1-final python3 - \
  < provisioner_socket_contract.py
EXIT=0
STOP_KEPT_FENCE {"fenced_attempts": ["<A>", "<B>"],
                 "status": "completed", "scope": "none"}
RESTART_LATE_A_FENCED {"rejected": "<A>",
                       "fresh_reached_boundary": true,
                       "fenced_attempts": ["<A>", "<B>"]}
UNIX_CONTRACT_OK
```

真实 Stop 经 JSON 行协议调用；重启后迟到 A 的正式 `ensure` 在
`prepare_workspace` 前被拒绝，新 attempt 能越过准入（sentinel 证明抵达
副作用边界）。协议字段未变。

## 7. 镜像与源码摘要

- **未重建任何镜像**。本轮唯一生产变更
  `deploy/multiuser/provisioner.py` 不被任何 Dockerfile 复制：provisioner
  由 `deploy/desktop/Start-WorkspaceEasel.ps1` 从 WSL 仓库直接启动，属于
  宿主/WSL 源码，不是镜像内容。worker/gateway 未改，按补单不重建。
- 现有 control 镜像 `easel-control:503-r4-r1-final` =
  `sha256:b30351b19ff72bd38b371d81c607db582c9e242581fb90776ef39564d9092ee3`
  仍与当前工作树一致（`image-check.txt`）：

| 镜像内文件 | 镜像 SHA-256 | 工作树 SHA-256 |
|---|---|---|
| `/opt/easel/easel/control/runtime.py` | `04a9f8be…ede35196` | 同左 |
| `/opt/easel/easel/control/store.py` | `0f9a2fa0…595d34ae` | 同左 |

  镜像内不存在 `/opt/easel/deploy/multiuser/provisioner.py`（`ABSENT`）。
- 生产源码摘要（`source-hashes.txt`）：
  - `deploy/multiuser/provisioner.py` 工作树字节（CRLF）：
    `7164c0c0066a5f477530c8350ae4b655ef4aa10055c18ceb77b586e282d95bee`
  - 同文件 LF 规范化（git blob 内容）：
    `860a3f148a7365cb3ec12bb5b860e9a68256ee6071cc1573abf8760f19901b03`
  - 本机 `core.autocrlf=true`；WSL 部署身份（`Get-WslFileSha256`）按实际
    WSL 仓库文件字节计算。本轮未更新 WSL 部署副本，未重启 provisioner。
- `git diff 4dd3550..HEAD --check` 无输出；生产 diff 仅
  `deploy/multiuser/provisioner.py`（+45）。

## 8. 未执行项与临时资源

- 未运行全量 `pytest tests/`、前端、真实模型/供应商/平台矩阵；按补单只跑
  受影响 attempt/runtime/store/Workspace API、provisioner 契约与
  R3 屏障 / messages / 既有 Run 收尾护栏。
- 未复跑容器级 Update / Stop→Backup→Verify：更新脚本与正式更新流程未改，
  沿用历史隔离更新证据，明确非本轮重跑。
- 未更新用户验收实例的 WSL 仓库、provisioner 进程、DB、Key、Cookie、
  attempt 或工作区；未合并、未推送。
- 父基线临时 worktree `C:\Users\hyh\AppData\Local\Temp\easel-r4-r3-red`
  已移除；主审探针/诊断临时目录自动删除；容器均 `--rm`，无残留镜像/容器。

## 9. 交付状态

- 生产提交：`6ae4b84`、`75d6912`
- 证据目录：`docs/evidence/acceptance-503-repair-r4-r3/`
- 分支：`fix/acceptance-503-r3`（未合并、未推送）；交付 tip 为本分支 HEAD
- 工作树干净；等待主审独立验证，不自动推进其他组。
