# 验收实例 503 修复 R4-R2：交付报告

日期：2026-11-03（Asia/Shanghai）

父基线：`188f1c8`（R4-R1 最终 tip）
分支：`fix/acceptance-503-r3`
实施 worktree：`C:\Code\Easel-acceptance`

本轮提交：

| 提交 | 内容 |
|---|---|
| `8361ce5` | R4-R2-01：未执行 attempt 的精确执行权围栏 |
| `174af97` | R4-R2-01：旧坏记录收口时同样围栏未知 attempt |
| `b686ea5` | R4-R2-01：测试断言迟到请求的确定失败结果 |

本轮只修主审第 6 轮下发的 R4-R2-01。未重开 R4-R1 已通过的崩溃恢复、
R3/R4 已审行为；未修改前端、worker/gateway、部署脚本或平台功能；未合并、
未推送、未更新用户验收实例。

## 1. 根因

主审反例（`probe_r4_r1_live_takeover.py`）的链路：

1. A 在 `_refresh_gateway`/`_restore_candidate_guarded` 远程调用前，用
   `Store.fence_unknown_gateway_refresh`（`easel/control/store.py:1486`）
   预占精确 attempt 并转为立即到期的 `starting`。
2. B 看到过期 `starting`，`_takeover_for_restore`
   （`easel/control/runtime.py:832`）把 A 写入 `pending_attempt_id`，
   再调用 `_resolve_fenced_attempt`（`easel/control/runtime.py:877`）按
   精确 attempt `commit_attempt(A)`。
3. 此时 A 尚未抵达 provisioner 工作区锁，远端没有 A 记录；
   `_commit_attempt_locked` 对 “记录为 committed/completed 或根本不存在”
   直接返回成功。B 据此清空 DB 槽位并继续。
4. A 迟到后按旧规则被放行，写出 `A/pending`；B 的新 attempt 被
   `gateway_cleanup_pending` 拒绝，而 DB 已无 A 归属，普通首屏永久 503。

根因不是“等待时间太短”，而是 **B 把“当前没有 A 记录”当成了“A 已不可
执行”**。provisioner 的每 workspace 锁是 A/B 唯一的串行化点：A 若已在
锁内执行，B 的 commit 会等它写完记录并按精确 attempt 收口；A 若尚未进入
锁，B 的 commit 必须让其之后永远不能执行，而不能无操作成功。

## 2. 修复实现（最小、可证明）

只改 `deploy/multiuser/provisioner.py` 的 attempt 归属语义，不改协议字段、
不改 runtime/store、不扩编排框架：

- **`_commit_attempt_locked`（`provisioner.py:2771`）**：当 commit 的精确
  attempt 没有任何本服务执行记录（记录不存在，或当前记录属于另一个
  attempt 且已 committed/completed）时，不再无操作成功，而是把该 attempt
  持久写入 `fenced_attempts`：
  - 无任何记录：写 completed/no-op 语义的围栏记录
    （`_persist_fenced_attempt`，`provisioner.py:2838`）；
  - 已有 committed/completed 记录：保留原记录和原 attempt，仅追加精确
    ID（`_remember_fenced_attempt`，`provisioner.py:2860`）。
  两者都在 provisioner 的 workspace 锁内原子落盘。
- **`_ensure_attempt_available`（`provisioner.py:2257`）**：在 `ensure`
  的 `prepare_workspace`/网络/容器动作之前、`restore_gateway` 的 worker
  核验之前，先拒绝 `fenced_attempts` 中的 attempt，错误码
  `gateway_attempt_fenced`。因此迟到请求不会产生任何目录、worker 或
  gateway 副作用。
- **`_record_attempt` / `_record_noop_attempt`**：同 attempt 的写盘入口
  再次失败关闭（纵深防护），并在新记录中携带既有 `fenced_attempts`，
  避免新 attempt 的正常记录把旧围栏覆盖掉。
- **`_load_attempt`（`provisioner.py:2311`）**：严格校验并往返保存
  `fenced_attempts`；重启后仍生效。
- **旧版坏记录修复分支**：修复旧版无资源记录时，若请求收口的是另一个
  未知 attempt，同样写入围栏，避免借旧记录修复重新获得执行权。

串行化语义（不依赖时钟或短延迟）：

| A 的状态 | B 的 commit 行为 | 结果 |
|---|---|---|
| 已进入 provisioner 锁并写出记录 | 等待锁，按精确 attempt 收口 | 正常接管，不围栏 |
| 尚未抵达 provisioner 锁 | 落盘精确围栏后成功 | A 迟到时在副作用前被拒绝 |

`gateway_attempt_fenced` 不在 `_UNKNOWN_ENSURE_OUTCOME_CODES` /
`_UNKNOWN_RESTORE_OUTCOME_CODES` 中，control 按确定失败处理；A 的旧代次
回滚 CAS 必然失败，不会覆盖 B 的新代次。

## 3. 红态证据

在父基线 `188f1c8` 的临时 worktree 中原样运行主审探针（未修改探针）：

```text
COMMAND: C:\Users\hyh\anaconda3\envs\Agent\python.exe
         C:\Code\Easel\docs\evidence\acceptance-503-review\
         probe_r4_r1_live_takeover.py <临时 worktree>
EXIT=0
B first error: gateway_cleanup_pending
B retry 0 gateway_cleanup_pending
B retry 1 gateway_cleanup_pending
B retry 2 gateway_cleanup_pending
{"db_state": "ready", "pending_slot": "", "db_attempt": "<非A>",
 "remote_attempt": "<A>", "remote_status": "pending"}
REPRODUCED: A pending 归属已从 DB 丢失，连续普通首屏无法恢复
```

原始输出见 `red-probe-live-takeover.txt`。临时 worktree 已移除。

## 4. 绿态证据

所有绿色命令均在最终树 `b686ea5` 上执行（证据文件首行
`TREE: b686ea5`）；生产代码与 `174af97` 完全相同，`b686ea5` 只强化测试
断言。

| 证据文件 | 范围 | 结果 | 退出码 |
|---|---|---|---|
| `green-r4r2-tests.txt` | 本轮新增 11 项 | 11 passed | 0 |
| `green-attempt-ownership.txt` | 503/R4-R1/R4-R2 归属三文件 | 59 passed | 0 |
| `green-affected-contracts.txt` | 受影响契约 14 文件 | 271 passed, 4 skipped | 0 |
| `green-store-lifecycle.txt` | store/生命周期 4 文件 | 93 passed | 0 |
| `green-provisioner-contracts.txt` | provisioner 契约 6 文件 | 182 passed, 1 skipped | 0 |
| `green-unix-socket-contract.txt` | 真实 Unix 客户端/服务端 | UNIX_CONTRACT_OK | 0 |

`green-affected-contracts.txt` 的 3 个 warning 是
`test_original_web_api.py` 中 Windows 子进程 reader 线程解码告警，父基线
即存在，与本轮改动无关。

修复后原样复跑主审探针（限时 25s）：探针停在自身 `late_written` 屏障
（`EXIT=124`）。原因是 A 的迟到写入现在会在持久化前被拒绝，探针脚本中
“A 写入成功后才放行 B”的固定屏障不会打开。按 R4-R2 补单，这不再作为
通过依据；超时后实际状态（`green-probe-live-takeover-after-fix.txt`）：

```text
DB: starting / owner=live-b / attempt=<B> / pending_attempt_id=""
provisioner: attempt=<旧C> / status=completed /
             fenced_attempts=[<A>]
```

即 A 已进入精确围栏，DB 无 A pending，远端不是 `A/pending`，未进入红态
永久 503。完整恢复由正式正向测试证明。

## 5. 覆盖矩阵

| 要求 | 正式回归 |
|---|---|
| A 预占后、锁前暂停；B 接管/commit；A 迟到执行 | `test_live_takeover_fences_late_ensure_before_side_effects`：A 恢复时 B 已清空槽位，A 得到 `gateway_attempt_fenced`，B 进入 ready；随后连续 3 次普通首屏成功；A 的僵尸请求仍被拒绝 |
| 显式 restore 共用安全语义 | `test_live_takeover_fences_late_restore_before_side_effects`：同一确定性交错走 `restore_gateway` 入口 |
| A 确定失败 | 迟到请求的 `gateway_attempt_fenced` 按确定失败处理；A 的旧代次回滚 CAS 失败，B 的代次/状态不变（上述两项测试断言 B ready 与旧槽位为空）；R4-R1 两项确定失败回滚测试保持通过 |
| A 未知结果 | `test_unknown_result_before_takeover_fences_late_attempt`：A 只看到 `provisioner_timeout`，B 接管后 A 的僵尸请求在副作用前被拒绝 |
| A 取消 | `test_cancel_before_takeover_does_not_resurrect_late_attempt`：A 取消后 B 接管，取消的请求不得复活写 pending |
| B 在交接/收口中再次中断 | `test_interrupted_takeover_commit_fences_both_attempts`：B 的 commit 已在 provisioner 落盘围栏但响应丢失，下一 owner 在租约到期后按精确 attempt 收口，A 与 B 的 attempt 都被围栏并继续 ready |
| 活跃执行与未知/退出结果的区分 | `test_active_attempt_record_is_committed_not_fenced`：A 已在锁内写出真实记录时 commit 只收口、不围栏 |
| 围栏不因记录覆盖而丢失 | `test_fence_survives_new_attempt_record`；重启后仍生效见 `test_commit_without_record_persists_execution_fence` |
| 旧版坏记录修复路径同样围栏 | `test_legacy_record_commit_fences_different_attempt` |
| 围栏发生在任何 gateway 副作用之前 | `test_fenced_attempt_cannot_create_gateway_side_effects`：`gateway.ensure_calls == 0` |
| 已提交记录不被未知 attempt 覆盖 | `test_commit_of_unknown_attempt_keeps_committed_record`：保留原 committed 记录，只追加围栏；重复 commit 幂等 |
| 旧代次迟到写回/清理不能破坏新代次 | 受影响组内的 `test_ready_refresh_lost_ensure_recovers_through_first_screen`（旧代次写回 False）、`test_ready_refresh_definite_failure_rolls_back_reservation`、`test_restore_takeover_fences_old_cleanup_before_commit`（旧 cleanup False、迟到 abort 被拒）全部通过 |
| 3 项真实子进程 `os._exit` 恢复 | `test_attempt_ownership_r4r1_503.py` 3 项通过；恢复阶段 `ensure=0、restore=0、resolve=1`，G2 正常 Stop |
| 既有 Run/收尾/屏障 | runtime、messages、lifecycle、stop barrier、proxy 等受影响契约组通过 |

测试使用 `asyncio.Event`/显式释放构造确定性交错，没有用 `sleep` 或短延迟
让反例暂时消失；迟到请求同时覆盖租约窗口内（takeover 后 expiry 1321、
时钟 1201）和期限后（`lease_expires_at + 1`）。

## 6. 真实 Unix socket 契约

本机 Windows Python 不支持 `AF_UNIX`，因此在无网络、只读挂载源码的 Linux
容器中运行真实 `UnixProvisionerClient` 与 `Provisioner._handle_client`：

```text
docker run --rm -i --network none \
  -v C:/Code/Easel-acceptance:/src:ro \
  easel-control:503-r4-r1-final python3 - \
  < provisioner_socket_contract.py
EXIT=0
{"attempt_a": "...", "attempt_b": "...",
 "fenced_attempts": ["<A>", "<B>"], "record_status": "completed",
 "new_attempt_allowed": "<C>"}
UNIX_CONTRACT_OK
```

两个独立 Unix 连接完成真实 JSON 行请求/响应；服务端持久围栏可跨连接
复核，新 attempt 仍可进入。协议字段未变。

## 7. 镜像与源码摘要

- **未重建任何镜像**。本轮唯一生产变更
  `deploy/multiuser/provisioner.py` 不被任何 Dockerfile 复制：`grep -rn
  provisioner --include='Dockerfile*'` 只命中注释与
  `provisioner_identity.py`。provisioner 由
  `deploy/desktop/Start-WorkspaceEasel.ps1` 从 WSL 仓库
  `deploy/multiuser/provisioner.py` 直接启动，属于宿主/WSL 源码，不是镜像
  内容。worker/gateway 未改，按补单不重建。
- 现有 control 镜像 `easel-control:503-r4-r1-final` =
  `sha256:b30351b19ff72bd38b371d81c607db582c9e242581fb90776ef39564d9092ee3`
  仍与当前工作树一致（`image-check.txt`）：

| 镜像内文件 | 镜像 SHA-256 | 工作树 SHA-256 |
|---|---|---|
| `/opt/easel/easel/control/runtime.py` | `04a9f8be...ede35196` | 同左 |
| `/opt/easel/easel/control/store.py` | `0f9a2fa0...595d34ae` | 同左 |

  镜像内不存在 `/opt/easel/deploy/multiuser/provisioner.py`（`ABSENT`），
  与上述判断一致。
- 生产源码摘要（`source-hashes.txt`）：
  - `deploy/multiuser/provisioner.py` 工作树字节（CRLF）：
    `6f54486b3f31097e5e97fb504dbb825c9480ab7aaa1702b6bc2b5c3fa7e19ee2`
  - 同文件 LF 规范化（git blob 内容）：
    `768d886491fa11724898f87d4b64d4bc05ead8d8f0a6df3e592ff478308996a4`
  - 本机 `core.autocrlf=true`；WSL 部署身份
    （`Get-WslFileSha256`）按实际 WSL 仓库文件字节计算。本轮未更新 WSL
    部署副本，因此未给出其线上摘要，也未重启 provisioner。
- `git diff 188f1c8 --check` 无输出；生产 diff 仅
  `deploy/multiuser/provisioner.py`（+93/−6）与测试文件。

## 8. 未执行项与临时资源

- 未运行全量 `pytest tests/`、前端、真实模型/供应商/平台矩阵；按补单只跑
  受影响 attempt/runtime/store/Workspace API、provisioner 契约与
  R3 屏障 / messages / 既有 Run 收尾护栏。
- 未复跑容器级 Update / Stop→Backup→Verify：更新脚本与正式更新流程未改，
  沿用 R4 报告中的历史 `4382180` 证据，明确非本轮重跑。
- 未更新 user 验收实例的 WSL 仓库、provisioner 进程、DB、Key、Cookie、
  attempt 或工作区；未合并、未推送。
- 红态临时 worktree `C:\Users\hyh\AppData\Local\Temp\easel-r4-r2-red`
  已移除；主审探针的临时目录已清理；容器均 `--rm`，无残留镜像/容器。
- 修复后主审探针的限时复跑在临时目录自动删除前被强制终止，已确认无
  `easel-r4r1-live-*` 残留。

## 9. 交付状态

- 生产提交：`8361ce5`、`174af97`
- 测试提交：`b686ea5`（生产代码与 `174af97` 相同）
- 证据目录提交：`7a0ce2e` 及其后的文档提交
  （`docs/evidence/acceptance-503-repair-r4-r2/`）
- 分支：`fix/acceptance-503-r3`（未合并、未推送）；交付 tip 为本分支 HEAD
- 等待主审独立验证；不自动推进其他组。
