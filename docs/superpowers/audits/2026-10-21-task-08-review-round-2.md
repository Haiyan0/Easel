# Task 8 返工 R1 复审：需要返工 R2

- 返工基线：513d08bebce138c262be9eb2f1bae03eaa3afefc。
- 本次审查提交：e845b6f811ccbfec318f3cb97e9771929dab6518。
- 结论：六项阻塞（两项 P1、四项 P2），不进入 Task 9。
- 依据：[Task 8 交接单](../tasks/2026-10-21-task-08-worker-runtime.md)、[首轮审计](2026-10-21-task-08-review-round-1.md)。本轮仅审计；没有修改产品代码/测试，没有提交，没有操作真实 Docker 容器或宿主权限。

## T8-R2-01 [P1] 安全验收仍接受明确关闭 no-new-privileges 的容器

位置：deploy/multiuser/provisioner.py 第 622–627 行。

verify_worker_inspect 只判断 SecurityOpt 中是否有字符串包含 no-new-privileges。合法 fixture 仅修改为 SecurityOpt=["no-new-privileges:false"]，实际 Provisioner.ensure → _docker → fake runner 链仍成功返回旧实例句柄，没有新建。该值明确关闭要求的安全选项；子串匹配不能证明启用。新建后检查、复用及 inspect 都使用同一个验证器。

独立探针输出：

```text
nnp_false accepted easel-worker-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
commands [['network', 'inspect'], ['container', 'inspect']]
```

修复/验收：严格识别 Docker 实际启用状态的规范值，拒绝 false、伪造包含项及冲突配置。补实际 ensure/inspect 调用链的反例，不能只检查启动 argv 中写过 true。

## T8-R2-02 [P2] 停止动作没有实例约束，迟到 stop 可以删除新代次实例

位置：easel/control/runtime.py 第 167–171 行；同类调用在 collect_idle、_upgrade；easel/control/store.py 第 588–596 行；deploy/multiuser/provisioner.py 第 1073–1086 行。

stopping 的 CAS 只保护数据库。停止认领提交后，旧管理者可能尚未把 stop 送到 provisioner；租约过期时新管理者可以启动/接管新代次。旧 stop 随后到达，协议只带 workspace，provisioner 按当前同名容器停止/删除。workspace 锁不能阻止一个尚未抵达的旧动作，最终数据库保留 ready，实际实例已经被删除。

主审复跑了两真实 Store、两个 RuntimeManager、现有 FakeProvisioner 的确定性探针：db_runner 在 begin_worker_stop 真正提交后、返回调用方之前暂停；业务时钟由 1000 推进到 1002，新 manager 完成 generation=2；再释放旧调用。

```text
paused_before_stop {state:stopping,generation:1,stop_calls:0}
before_old_stop {generation:2,state:ready,new_exists:true}
after_old_stop {generation:2,state:ready,new_exists:false,stop_targets:[新实例]}
```

现有迟到回复测试把 gate 放在 fake stop 已经取得旧实例之后，无法覆盖这一窗口。

修复/验收：停止协议与 provisioner 实际物理动作绑定不可复用的实例身份/受校验代次，停止/删除都只能作用于原实例。补“提交停止认领后、发送 stop 前暂停”的双连接测试，实际检查新实例未进入 stop/rm 目标；覆盖 drain、idle、升级。不能仅增加一次 stop 前数据库查询，仍会留下检查/使用竞争。

## T8-R2-03 [P2] 旧管理者通过查询当前 generation 续写新租约

位置：easel/control/runtime.py 第 219–226 行。

heartbeat(workspace_id) 重新读取当前租约，然后拿当前 generation 调 Store。它没有使用调用方原来持有的身份，因此旧 manager 也能给新 manager 的实例续期，甚至覆盖 active_requests。

实际探针：新 manager 的 generation=2、owner=new，先写 active_requests=7；旧 manager 调 heartbeat(workspace_id,active_requests=0)：

```text
accepted=true, generation=2, owner=new,
active_requests=0, heartbeat_at=1003, lease_expires_at=1004
```

这会破坏空闲回收依据。首轮审计已要求复核该路径，本次仍未关闭。

修复/验收：心跳携带调用者已取得的租约/实例身份并在数据库原子校验；读取当前 generation 不能授予旧持有者写权限。补旧持有者心跳在新代次 ready 后被拒绝，且所有活动计数、时间、所有者均不改变的测试；合法持有者继续可续期。

## T8-R2-04 [P1] 执行镜像仍缺少 ACP 必需的共享工作区规则

位置：deploy/multiuser/Dockerfile.worker 第 90–92 行；easel/harnesses/workspace.py 第 11–19 行；easel/harnesses/acp.py 第 104 行。

新增 COPY web/skills 补齐了一部分文件，但整个构建链没有复制公开共享规则目录 ./agents/workspace。workspace_documents 强制要求其中的 AGENTS.md；实际 ACPAdapter.run 在发送 prompt 前调用 harness_instructions，必然抛 HarnessError（缺少共享工作区规则）。临时模拟镜像目录布局已复现；不需要真实 DSH commit 或供应商凭证。

当前 smoke 只在 local 模式 import web.app，再运行自身的假 initialize 应答。没有走 ACP adapter、指令装配或 prompt，故 WORKER_SMOKE_OK 不能发现这个硬阻断。

修复/验收：把公开受管规则纳入只读镜像与文件清单。镜像内冒烟使用合法测试签名快照/受管假 ACP，经过实际 worker/adapter 指令装配，至少完成 initialize/session/new/session/prompt，验证规则、技能、路径和环境；不能只启动能打印一行 JSON 的子进程。保留真实 DSH commit/profile 未提供的独立门槛。

## T8-R2-05 [P2] Playwright 默认重新注入 no-sandbox，真实浏览器门槛是假阳性

位置：./skills/shared/scripts/runtime_paths.py 第 125–141 行；例如 ./skills/shared/scripts/douyin_publish.py 第 163–169 行；tests/integration/multiuser/test_container_isolation.py 第 185–193 行。

过滤自传 args 不等于开启 Chromium 沙箱。镜像固定的 [Playwright v1.49.1 驱动源码](https://github.com/microsoft/playwright/blob/v1.49.1/packages/playwright-core/src/server/chromium/chromium.ts#L318-L319) 在 chromiumSandbox 不为 true 时自行加入 --no-sandbox。当前技能没有显式传 chromium_sandbox=True；未修改的 doc_convert、mindmap 等无自传禁用字符串的启动点也受影响。

独立复审用真实 Playwright driver、Python 可执行文件作为无害浏览器替身，捕获最终 launching 命令仍带 --no-sandbox；没有启动真实浏览器或降低宿主权限。

新增容器测试同样没有设置 chromium_sandbox=True。即使 CHROMIUM_SANDBOX_OK 输出成功，也只能证明默认关闭沙箱时能启动，不能证明要求的沙箱能力。当前 fake kwargs 测试观察的是驱动注入之前的数据，因此全部通过仍不能关闭该项。

修复/验收：所有 worker launch/launch_persistent_context（同步、异步、渲染/转换/内置技能）显式启用沙箱并保留 local 兼容。验证最终驱动启动参数没有禁用项；修正真实容器门槛后重新执行。若 hardened 容器无法启动，就如实记录门槛未通过，不恢复 capabilities、不 privileged、不关闭沙箱。同步修正文档中“已通过”的错误证明范围。

## T8-R2-06 [P2] 已退出容器因没有运行时 IP 而无法恢复

位置：deploy/multiuser/provisioner.py 第 686–688 行、第 1026–1035 行。

verify_worker_inspect 无条件要求 IPAddress 非空。已停止/退出的容器没有活动 endpoint/IP，不代表静态安全配置违规。合法旧容器 fixture 仅改 State 为 exited/Running=False、批准网络内 IPAddress 为空，实际 ensure 抛 container_conflict，走不到后面的不健康旧容器回收重建分支。

```text
exited rejected container_conflict
commands [['network', 'inspect'], ['container', 'inspect']]
```

修复/验收：分开静态归属/安全配置验证与运行时健康/IP 检查。经验证属于本 workspace 的安全退出实例可被回收重建，未知/不安全实例仍拒绝。补真实 ensure 路径的异常退出/重启恢复，断言保留持久产物/profile/状态，新实例重新就绪；有可用 Linux 环境时测实际退出后 inspect 形态。

## 已修复部分与独立验证

- R1-01：实际调用保留 docker run，已关闭原丢子命令缺陷。
- R1-02：Linux 特权目录操作改为逐级 openat/O_NOFOLLOW、目录身份校验和 fchown，原路径 chown 跟随链接缺陷已关闭。正常 root 创建的 base 不授予 worker 写权限，未发现 worker 替换两个挂载源的同类绕过。Linux fd 分支本轮没有独立实测；交付报告的真实 Linux 证据作为外部证据保留。
- R1-03：网络及大量容器约束已经补齐，仍有 R2-01。
- R1-04：迟到写回 CAS 已修复，物理停止和旧心跳仍见 R2-02/03。
- R1-05：真实 lifespan 的短 DB 步骤已调度到专用线程，现有装配回归通过。
- R1-06/07：新增代码与测试有进展，仍有 R2-04/05。

使用指定 Agent Python 独立运行：

| 验证 | 本轮结果 |
|---|---|
| tests/multiuser | 682 passed、3 skipped，37.64 秒 |
| Task 8 八文件回归 | 229 passed、20 skipped，18.33 秒 |
| 容器隔离集成（未显式启用） | 8 skipped，0.04 秒 |
| git diff 513d08b HEAD --check | 退出 0 |

独立复审另跑 provisioner 定向 75 passed/1 skipped、runtime 28 passed、image/browser 两文件 10 passed；这些通过不能替代上述缺口的反例。

未独立执行 Docker 构建、真实 Chromium、真实 DSH/供应商、XFS 项目配额、目标 Linux 宿主网络/健康链。本轮没有重跑全仓。外部交付声称的真实结果仅作为交付证据，不写成主审实测；尤其原 Chromium 测试尚不足以证明沙箱启用。

## Task 8 尚未关闭的整体门槛

当前零出站属于安全失败关闭状态；worker→broker/受限公网出口允许路径仍未实现，宿主健康探测、配额及最终 Harness 门槛仍未通过。交接单将网络允许/拒绝矩阵列为 Task 8 范围。此次 README 把允许路径顺延为 Task 10，未经已批准任务范围变更，不能凭文档顺延宣布 Task 8 完成。修复六项代码问题后仍需逐项核对，不自动进入 Task 9；外部环境确实缺失的门槛继续如实记录，不伪造成功。

## 可复制给执行 Agent 的 R2 指令

基于 e845b6f811ccbfec318f3cb97e9771929dab6518，仅继续 Task 8，按本报告修复 T8-R2-01 至 06；不进入 Task 9。先补能在当前基线失败的真实调用链测试，再最小修复。不得仅修改 fixture、跳过或弱化验收。

重点：停止动作跨 RuntimeManager→受限协议→provisioner 物理执行全程绑定实例；心跳不能通过查询当前租约给旧持有者授权；Docker 安全选项解析要验证实际启用；静态容器安全验证不能阻断安全退出实例恢复；镜像包含公开共享规则且走真实 ACP 假 Harness 冒烟；浏览器必须显式开启 Playwright sandbox并检查最终驱动参数。补回所有遗漏的 Chromium 启动者。

保留已通过的认证、配置、broker、快照及 local 兼容。数据库只能追加迁移；不改写历史迁移。同步更新部署手册与网络策略中证明范围不准确的描述，Task 8 网络接线范围不能未经批准移动到 Task 10。没有真实 DSH pin/profile、XFS 或目标 Linux 环境时，不伪造、不放宽安全要求；可独立完成的代码与测试先完成，剩余门槛明确列出。

运行 tests/multiuser、Task 8 指定八文件回归、可用集成与修正后的浏览器/镜像冒烟，检查父提交到新提交 diff --check。只显式路径提交实现、测试及必要部署文档，不夹带审计/任务单，不操作开发者真实容器/账号/工作区。交付父/新 SHA、逐项红绿、真实入口证据及实际/未执行门槛，然后停止等待复审。
