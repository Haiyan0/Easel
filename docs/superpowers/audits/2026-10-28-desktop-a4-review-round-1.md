# Task A4 首轮审计：暂不通过，六项定向返工

基线：`785f556bc81c464f59de5105a02c12305b2e6265`。审查提交：`81e9cc18f6ff698fdc16abff7a0418260e0217ee`。

## 裁决与范围

A4 已完成正式签名客户端、逐 workspace broker 和协议夹具真实链路的主要装配。正常路径测试通过，但下列秘密挂载、故障恢复与启停问题尚未关闭，不进入 A5/A6。本轮不重开已验收的 G1/G2/A3，也不要求无变化的大镜像重建。

主审实际运行十二文件定向回归：**410 passed, 2 skipped（57.97 秒）**；首组五文件 68 passed 包含在该范围内，不相加。独立状态审查三文件 41 passed。`git diff 785f556 HEAD --check` 退出 0。绿色用例不覆盖下面的反例。

主审未重跑 Docker/ngrok/真实供应商；作者真实容器链路证据保留为作者实测，不能替代这些反例。未修改运行时代码、开发者资源或主审以外文档。

## A4-R1-01（P1）：egress 获得 control 私钥与 master key

位置：`deploy/desktop/compose.workspace.yaml:79`、`:102`；关联 Initialize-WorkspaceEasel.ps1 的 key 初始化。

egress 与 control 同为 UID 10001，且挂载相同的 `keys:/keys:ro`。该卷含 `master.key` 与 `snapshot.pem`，均归 UID 10001。只读仅阻止修改，不能阻止出口代理读取解密密钥和 Ed25519 私钥。这直接违反 A4.3“私钥和 master key 只给 control”。当前测试只检查 egress 无端口/socket，没有检查材料隔离。

修复：egress 仅获得出口策略（独立专用卷或经归属校验的只读单文件）；不挂整个 key 卷。同步初始化、启动、诊断与说明。用测试 sentinel 在真实同 UID 容器中证明：control 能读取其必要材料，egress/worker/gateway 没有 control 私钥/master key/数据库。只输出布尔结果，禁止打印 key。

## A4-R1-02（P1）：超时对账在 gateway 503 后仍发 stop 并结算 Run

位置：`easel/control/proxy.py:1008`–`:1017`，关联 `_confirm_stop`。

代码虽然把超时前撤销 token 排除出 gateway 模式，但 events GET 报错后，只要 `timed_out`，仍进入 `_confirm_stop`。gateway 短暂恢复可接收 stop，Run 随后 cancelled、token 被撤销；未成功读到原 worker 状态。这与 A4.2 及报告“临时故障不停止、不撤 token”相矛盾。

独立复现使用真实 Store/WorkerRelay、同步记录型 broker 和受控 HTTP transport：

```text
GET /internal/runs/<id>/events?after=0&nonce=nonce -> 503 gateway_inactive
POST /internal/runs/<id>/stop -> 200 cancelled
reconcile = {checked: 1, finished: 1}
Run = cancelled; revoke_count = 1
```

已有 `test_reconciler_gateway_unavailable_keeps_run_and_token` 没有 `mark_run_dispatched`；dispatching 宽限期使测试在发 HTTP 之前返回，因此并未覆盖标题场景。新红测必须明确已派发、已过 deadline，断言至少发生 GET，且故障后没有 POST、撤销或容量释放。gateway 恢复并成功读取状态后的正常超时停止仍应保留；direct 兼容语义不扩大修改。

## A4-R1-03（P2）：旧 Run 无法恢复已过期的 refresh/受限清理

位置：`easel/control/runtime.py:449`–`:456`；关联 relay `_existing_handle` 和后台 reconciler。

`restore_gateway` 对 starting 一律 503，不检查租约是否已过期。原 control 在 refresh staged 后崩溃，新 control 只有旧 Run 的 events/stop/answer/history 时，不会调用通用 ensure_ready；这些入口永远失败，后台也只跳过。gateway-only stopping 恢复也没有在此受限路径收口。

真实 Store 复现：ready worker + running Run → begin_worker_gateway_refresh(now=1001, lease=60) → 模拟旧 owner 消失；新 Runtime 时钟分别为 1200、9999。两次均：

```text
worker_gateway_refresh_in_progress
lease = starting; Run = running; provisioner.restore_calls = 0
```

修复：未到期时继续明确忙；已到期时以持久身份、owner/generation/attempt CAS 接管，且只能恢复原 worker/config 的 gateway。gateway-only stopping 按原 scope/tombstone 消解。不得调用可冷启动 worker 的通用路径，也不得等待新 submit 来救活旧 Run。新增两个真实 Store 连接、control/provisioner 重建、迟到旧回应反例；证明旧 Run 可只靠读取/停止入口恢复，worker ID/token/容量不变，不重复 POST prompt。

## A4-R1-04（P2）：Start 盲目复用现有 provisioner，所报告参数未生效

位置：`deploy/desktop/Start-WorkspaceEasel.ps1:181`–`:182`，关联 socket 探测与最终 started 输出。

只要 Unix socket 可以 connect，就完全跳过 provisioner 启动，不核验现有进程所用 worker/gateway 镜像、workspace 根、公钥/策略、控制网络或配置。重新传入新 WorkerImage/GatewayImage/WorkspaceRoot/DataRoot 时，脚本仍报告新值启动成功，实际 provisioner 继续使用旧值。任意只接受连接的服务也能通过当前探测。

独立审查通过临时 PowerShell stub 实际调用入口：镜像/归属检查正确、control healthy、socket 仅可连接；脚本 exit 0。调用日志只有目录/cryptography/socket connect/compose config/up，没有任何 provisioner 配置核对。

修复：复用前通过受信、只读、脱敏的实际配置/身份契约核验，包含实际 Docker Engine/context、镜像、根、网络、key 指纹及 socket 权限/允许 UID/GID。匹配才复用；不匹配明确拒绝并提示 drain/受控重启，不盲杀活跃实例。启动新进程后也做同样核验。补参数变化、错误服务、配置读取失败与合法重开测试。

关联诊断缺口：Status-WorkspaceEasel.ps1:94 在所有 docker/wsl 查询都失败、所有资源 missing 时仍 exit 0，且未验收实际端口/挂载/密钥/运行配置。诊断应保留有用输出并用非零状态表达必需项失败，检查实际事实，不能把健康 HTTP 当成完整装配验证。

## A4-R1-05（P1）：Stop 的 drain 与停止 control 之间仍可接纳新 Run

位置：`deploy/desktop/Stop-WorkspaceEasel.ps1:55`–`:67`、`:87`–`:94`；关联 `cmd_admin_drain` 与 RuntimeManager.drain_all。

Stop 调用独立 admin drain，公开 control 仍接受 submit。drain_all 只遍历最初快照；空间变 stopped 后立刻允许重新 ensure。返回成功到 compose down/pkill 之间的新请求可创建 worker/gateway/Run，随后 control、出口与 provisioner 被关掉，留下无人管理实例。已有 stopping 的 drain 还会直接返回；CLI 未做完成态复核。

独立复现使用同库两个真实 Store/RuntimeManager，仅 Docker 替身：

```text
drain_returned = 1; state_after_drain = stopped
worker_ensure_calls = 2; new_worker_identity = true
lease_before_compose_down = ready; new_run_status = reserved
stop_calls = 1
```

修复：停止前建立对所有接纳者生效的停止接纳屏障，再 drain；屏障不得只存在于另一个 CLI 进程的内存，也不能只给现有 workspace 打标。允许已接纳 Run 正常收尾/stop/events；复核无未完成 Run、请求及未收口实例后才 down。中间态/失败不得报告已停止；脚本取消/重开后的屏障恢复规则明确。可复用现有维护机制，不默认另建庞大运维系统。新增在 drain 完成与 down 之间受控提交、并发新 workspace、既有 stopping 反例，证明不会新建残留资源。

## A4-R1-06（P2）：平台兼容处理吞掉真实 Linux 持久化错误

位置：`easel/worker/sessions.py:414`–`:432`；关联本轮 sessions/runs 中宽泛 `except OSError`。

为兼容 Windows bind mount，目录 open/fsync 的所有 OSError 均被忽略，包含原生 Linux EIO。主审在 WSL 原生临时目录真实执行 append，仅对目录 fd 注入 EIO：

```text
HEAD: APPEND_RETURNED_SUCCESS=True; FSYNC_FAULTS=['directory_EIO']
785f556: BASELINE_ERROR=DomainError:session_write_failed
```

这不是要求 Windows 提供本来没有的能力，而是不能把真实 I/O 故障当作平台“不支持”，继续宣称崩溃持久化成功。修复应限定明确的文件系统/开发模式和已知不支持 errno；文件 fsync、真实 EIO/ENOSPC 等失败保持明确错误。不要扩大到所有 POSIX 路径。若 bind 平台无法保证目录持久化，要明确开发限制。补已知兼容错误成功、EIO 失败、旧消息/恢复输出不伪成功及临时文件清理测试。

## 返工与复审门

仅修以上六项及关联诊断测试；保留 exact audience/URL/body、默认头净化、同 worker 恢复、无 POST 自动重发、G2 scope/attempt/completed tombstone 和 A1 卷归属保护。不改主审审计/任务单。

先确定性红测试，再最小修复、定向绿与受影响回归。镜像应用未改的部分无需无缓存重建。确需真实容器时只用带随机归属的测试资源，验证材料隔离与正式启动/停止；不触碰开发者实例、供应商/ngrok，禁止全局 prune。完整提交父/新 SHA、各项红绿、真实/未执行边界；提交后停止等待复审，不进入 A5/A6。
