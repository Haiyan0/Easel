# Task 8 网络 N-R2 定点复审通过

- 基线：feadfcd23e7908f7d76741e1528db934d8824967。
- 审查提交：504db8d22ebd753dc9e8dfcab8724b5260a319da。
- 结论：T8-N-R2-01 关闭，不要求 N-R3；当前已列出的网络代码返工项均已关闭。Task 8 整体仍待真实部署门槛，不进入 Task 9/10，不开放多人执行。
- 依据：[N-R2 审计单](2026-10-22-task-08-network-review-round-2.md)、[Task 8 交接单](../tasks/2026-10-21-task-08-worker-runtime.md)。本轮仅审计，没有修改产品/测试、没有提交或操作真实 Docker 及开发者持久数据。

## 关闭依据

deploy/multiuser/provisioner.py 新增 worker_confirmed_exited：仅明确 Running=false 且 Status 为 exited/dead 时允许不匹配接线实例直接删除重建。迁移分支不再以一次 lifecycle 探测是否成功决定删除权限。

仍在运行、暂停、重启、状态未知的旧实例明确返回 worker_wiring_mismatch，保留原实例，交既有控制面 drain/stop 流程收尾；不会先变更 sidecar。明确退出实例的重建和持久目录保留路径继续存在。

主审临时探针经真实 Provisioner.ensure + 现有 FakeDocker，逐项覆盖 Running+healthy、restarting、paused、空 State、State=null：均为 worker_wiring_mismatch，原 worker 对象不变，未 stop/rm，未创建/修改 sidecar。已有三类超时/网络异常/非 ready 反例通过；不存在依赖探测结果强制迁移的分支。

该结论仅关闭已知迁移缺陷，不声称所有生命周期或隔离能力已在生产环境验收。

## 独立验证

使用 C:\Users\hyh\anaconda3\envs\Agent\python.exe：

| 验证 | 结果 |
|---|---|
| tests/multiuser | 755 passed、3 skipped、2 warnings，40.18 秒 |
| Task 8 八文件回归 | 229 passed、20 skipped，21.36 秒 |
| 三个容器/网络/sidecar 集成文件（本轮未启用） | 11 skipped，0.06 秒 |
| git diff feadfcd HEAD --check | 退出 0 |

两条 warnings 为既有 websockets 弃用提示。真实 control broker 本机 sidecar socket 授权联调包含在 tests/multiuser 中并通过；供应商仍为 fake transport。

未独立执行真实 Docker 构建/网络、真实 Chromium、DSH/供应商、XFS、目标 Linux 防火墙/IPv6或宿主健康链，未运行全仓。执行 Agent 的真实网络/sidecar 2 passed、容器指定组 6 passed 作为外部交付证据保留，不写成本轮主审实测。

## Task 8 尚未完成的门槛

| 门槛 | 状态 | 需要的真实证据 |
|---|---|---|
| 硬化容器 Chromium | 已执行但未通过：No usable sandbox! | 在规定非 root、只读、cap-drop、no-new-privileges 边界内实际启用沙箱并运行临时/持久 context |
| 最终 Harness/runtime 镜像 | 未完成 | 管理员已验收 DSH_COMMIT/profile；完整构建与实际 Harness ACP 执行，不以叠加测试镜像替代 |
| Docker 拓扑中的真实 control broker | 未执行 | worker 容器→sidecar 容器→真实 control broker，合法与错误身份请求成对验证 |
| 真实公网/Task 5 live 与供应商 | 未完成 | 真实连接层出口策略及已装配 adapter 的实际调用；管理员凭证留在控制侧 |
| 目标 Linux 防火墙/IPv4/IPv6 | 未完成 | 允许及拒绝矩阵，跨空间、管理面、元数据等不可达，不能只看网络名称/argv |
| 持久区 XFS 配额 | 未执行 | 真实 10 GiB 限制与耗尽，不能以无配额测试或 Docker rootfs 限制替代 bind 配额 |
| 宿主 provisioner→worker 健康链 | 未执行 | 实际目标宿主到容器 IP 的带身份探测，不用容器内 docker exec 替代 |

因此当前状态为“已记录代码返工通过，真实隔离验收待完成”，不是 Task 8 完成，也不是又一轮同项返工。

## 可复制给执行 Agent 的下一阶段指令

基于 504db8d22ebd753dc9e8dfcab8724b5260a319da，T8-N-R2-01 已复审通过，不再为同一迁移问题追加返工，也不要进入 Task 9/10。下一阶段只补原 Task 8 的真实部署门槛与外部前提。

先做只读环境盘点，列明是否有已授权可连接的目标 Linux 测试环境、Docker/rootless 能力、XFS project quota、浏览器所需内核能力、固定镜像摘要，以及管理员已验收 DSH commit/profile。没有目标连接信息或受管 pin 时，不猜服务器、不默认使用远端 context、不伪造 pin，不把 Windows/Docker Desktop 的既有证据当目标宿主证据。明确列出需要人提供的非秘密配置/访问前提；凭证通过既有安全配置渠道装配，不输出在报告里。

环境可用后，只在自建临时测试资源中运行上述门槛，逐项报告命令、实际版本、结果与证据。尤其 Chromium 必须真实启用 sandbox；No usable sandbox! 仍为失败，不允许 --no-sandbox、privileged 或恢复 capabilities 通过。若需要改变宿主策略或全局防火墙，先准备具体可审查配置与影响范围，不擅自操作开发者宿主。

真实 control Docker 拓扑联调可以先用 fake provider 验证网络与授权，随后与真实公网/供应商门槛分别记录；不得把 fake 的成功推广成供应商或 live 出口通过。配额与宿主健康链同样必须实测。完整 Harness 构建只使用管理员验收过的 pin/profile，不追 latest。

外部前提不具备时，交付准确的待提供清单与可复现验收命令即可停止等待环境，不反复重跑同一 Windows 绿测试或重建大镜像来代替缺失门槛，不宣告 Task 8 已完成。有真实失败先定位并报告具体原因，再针对可复现问题做必要修复；不要扩大无关实现范围。任何实现/测试/部署文件仍显式路径提交，不夹带审计/任务单，不操作开发者真实账号/数据/容器。
