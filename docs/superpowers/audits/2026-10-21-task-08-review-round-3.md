# Task 8 返工 R2 复审：需要返工 R3

- 基线：e845b6f811ccbfec318f3cb97e9771929dab6518。
- 本次提交：41d91af755b605ff6cd1a79a9baddf0b2a6d03f7。
- 结论：两项 P2 阻塞，继续 Task 8，不进入 Task 9。
- 依据：[Task 8 交接单](../tasks/2026-10-21-task-08-worker-runtime.md)、[R2 审计单](2026-10-21-task-08-review-round-2.md)。仅审计，未修改产品/测试、未提交，未执行真实 Docker 变更。

## T8-R3-01 [P2] 新代次复用待停止实例，旧 stop 仍会删除它

位置：easel/control/store.py 第 588–596 行；deploy/multiuser/provisioner.py 第 1068–1071 行、第 1127–1132 行；easel/control/runtime.py 第 171 行。

R2 已把不可复用 Docker ID 贯穿协议和实际 stop/rm，修复了“旧动作按名字误删另一物理实例”。但新代次不一定创建新容器：真实 Provisioner.ensure 会复用同配置、仍健康的旧物理实例。

当 stopping 认领提交后、旧 stop 尚未送达时，租约可以到期；新管理者按新代次调用 ensure，复用原 Docker ID 并写 ready。旧 stop 随后携原 ID 到达，ID 恰好仍匹配，被执行 stop/rm。物理 ID 的唯一性没有提供租约代次隔离；数据库 CAS 仍只能保护写回，不能保护物理动作。

独立确定性探针使用两个真实 Store、两个 RuntimeManager、真实 Provisioner.ensure/stop，只将外部 Docker 边界替换为 FakeDocker。gate 放在 begin_worker_stop 成功提交后、返回旧管理者前：

```text
paused: state=stopping, stop_calls=[]
takeover: generation=2, owner=new, same_instance=True, exists=True
after_old_stop: generation=2, state=ready, exists=False
physical_commands:
docker stop --time 30 <原实例 ID>
docker rm -f <原实例 ID>
```

本轮现有 FakeProvisioner 每次 ensure 都生成新 ID，迟到测试还强制新旧 ID 不同，因而没有覆盖生产复用路径。

修复/验收：已进入停止承诺的实例不能未经消解就被新代次重新授权。设计停止恢复与接管的一致规则，或在 provisioner 物理执行边界实现可信代次隔离；保留 ID 约束。不能只再查询一次数据库，也不能仅让 fake 每次创建新 ID。

补两个真实连接/manager + 真实 Provisioner fake runner 的同配置复用交错：停止认领后发送前暂停，时钟推进、另一管理者接管，旧调用恢复；验证新代次真正可用的实例未被旧 stop/rm 删除，数据库与实际实例一致。覆盖 drain、idle、升级的共用停止路径，并保留正常 ready 实例接管/重启对账能力。

## T8-R3-02 [P2] Docker stop/rm 失败仍返回成功，生命周期错误写为 stopped

位置：deploy/multiuser/provisioner.py 第 1130–1132 行、第 1271–1273 行；同文件 _inspect_raw 第 973–977 行也把所有非零 inspect 退出都当不存在。

_docker 返回 CompletedProcess，不自动检查退出码。stop 无条件 await 两个命令后正常返回，_dispatch 随后回复 ok=true。调用方 RuntimeManager 因此可写 stopped，即使容器仍在运行。

主审实际走 parse_request → _dispatch → Provisioner.stop → _docker → fake runner；初始为合法受管 Running=True 实例，stop 与 rm 都返回非零且不改变容器：

```json
{"reply":{"ok":true},"still_running":true,"commands":[["container"],["stop"],["rm"]]}
```

这是外部命令失败后的错误确认，不能用 asyncio 异常路径测试替代。类似地，Docker daemon/权限等 inspect 错误不能一概被当作目标不存在而回复成功。

修复/验收：检查实际命令结果并确认原实例的终态。确认已删除的实例可以幂等成功；无法确认、实例仍运行或 Docker 不可用必须明确失败，不写 stopped、不报告 idle 回收成功。不要在失败处理时改用同名容器删除，不破坏 R2 的 ID 约束。

补实际服务入口及 RuntimeManager 联动：stop 非零、rm 非零、两者非零、Docker inspect 失败、目标确已不存在。断言响应、错误码、数据库状态及真实 fake 实例一致；失败时保留可恢复状态，正常/幂等回收继续通过。只检查 argv 或只让 fake 抛 Python 异常不足以验收。

## 上轮六项的关闭情况

| 上轮项 | 本轮结论 |
|---|---|
| R2-01 安全选项启用状态 | 已关闭原缺陷：规范启用值严格校验，false/冲突/伪包含项拒绝 |
| R2-02 停止动作绑定 | ID 已贯穿物理动作；仍有 R3-01 的同实例跨代次复用窗口 |
| R2-03 旧持有者心跳 | 已关闭：显式 generation + instance_id，SQL 原子匹配；旧代次不再查询当前值授权 |
| R2-04 镜像共享规则/ACP | 已关闭原文件遗漏与假冒烟缺口：COPY agents；签名快照、worker 配置和 ACPAdapter 实际 prompt 链已覆盖 |
| R2-05 Playwright 默认禁用沙箱 | Python 生产调用点已显式开启，驱动参数缺陷关闭；真实硬化容器沙箱仍未通过，未验收部署能力 |
| R2-06 退出实例恢复 | 已关闭原静态验收强制 IP 缺陷：静态约束与运行时健康分离，合法退出实例可重建 |

心跳独立探针还覆盖相同物理 ID 被接管到新代次：旧 generation 带/不带活动计数均 False，完整租约不变，合法新 generation True。

镜像/ACP 独立验证：临时进程加载测试签名快照，实际导入 worker Web、调用 ACPAdapter.run、启动 smoke 假 ACP，完成 initialize/session/new/session/prompt，输出 REAL_WORKER_IMPORT_AND_ACP_PROMPT_OK。此项不代表真实 DSH、最终镜像从头构建或 Linux 容器实测。

浏览器独立验证：真实 Playwright driver 以 Python 为无害可执行文件替身，默认调用最终带 no-sandbox，显式 chromium_sandbox=True 后不再注入。Python 调用点已修复。交付中 No usable sandbox! 作为门槛失败保留，不能改写为通过。

非阻塞范围提示：已复制的知乎技能 references/commands.md 仍包含直接禁用沙箱的临时脚本示例，宜同步使用 worker 安全调用方式；可选小红书分析器 TS 渲染代码也保留禁用参数，但本镜像没有构建其 dist/或装配可选 Node 依赖，本轮不将其表述为已经可运行的生产调用，也不据此追加第三项阻塞。以后启用这些能力前需纳入同一浏览器门槛。

## 独立验证及门槛

指定 C:\Users\hyh\anaconda3\envs\Agent\python.exe：

| 验证 | 结果 |
|---|---|
| tests/multiuser | 696 passed、3 skipped，36.71 秒 |
| Task 8 八文件回归 | 229 passed、20 skipped，16.39 秒 |
| 容器隔离集成（本轮未启用） | 9 skipped，0.04 秒 |
| git diff e845b6f HEAD --check | 退出 0 |

独立复审补跑 runtime/provisioner：116 passed、1 skipped；image/browser：11 passed。上述探针仍能复现两项阻塞；现有测试绿不等于停止动作安全。

本轮未执行真实 Docker 构建、真实 Linux Chromium、供应商/DSH、XFS、宿主健康探测或网络防火墙，也未重跑全仓。外部交付的基础容器 6 passed、ACP 冒烟及沙箱失败如实作为交付证据，不混写为本轮主审实测。

Task 8 整体仍未完成：完整执行/最终 Harness 镜像、真实 Chromium 沙箱、XFS 配额、宿主健康链、worker→broker/受限出口允许路径及目标 Linux 网络门槛均未关闭。本轮文档已恢复正确范围，没有把未接线允许路径当 Task 10 已获准的顺延；继续保留，不能以零出站或 mock 通过宣告任务全部完成。

## 可复制给执行 Agent 的 R3 指令

基于 41d91af755b605ff6cd1a79a9baddf0b2a6d03f7，继续 Task 8，仅针对本报告 T8-R3-01/02 补红测试并修复，不进入 Task 9。不要重做已关闭的认证、配置、broker、快照或 local 行为。

R3-01 必须使用真实 Provisioner.ensure 的同配置健康实例复用路径，覆盖停止认领已提交但 stop 尚未到达、新代次已接管后旧动作恢复的窗口。解决的是停止承诺与代次接管的物理一致性，不能靠只比较实例 ID、重复数据库预检或让 fake 强制新 ID 规避。

R3-02 必须处理 Docker 非零退出与不能确认的 inspect 结果，保证不把仍存活实例报告已停止；同时保留目标确已消失的幂等成功及 ID 绑定，补真实服务入口与 RuntimeManager 数据库联动测试。

运行 tests/multiuser、Task 8 指定八文件回归及可用集成，检查父提交到新提交 diff --check；不要为了通过浏览器门槛关闭沙箱、恢复 capabilities 或 privileged。真实环境/管理员 DSH pin 缺失的项继续如实列明，不伪造、不无故重跑大体积镜像安装。可顺手更新相关技能命令文档，但不要趁本轮启用或扩展可选 TS 技能。

只显式路径提交必要实现、测试及部署说明，不夹带审计/任务单，不操作开发者真实容器/账号/持久数据。交付父/新 SHA、逐项红绿、实际调用链与实际/未执行门槛，完成后停止等待复审。
