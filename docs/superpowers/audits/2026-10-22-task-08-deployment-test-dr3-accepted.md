# Task 8 D-R3-01 测试挂载隔离复审通过

- 基线：77079bea09a217fda85adcf9fedeab1f1810d76a。
- 审查提交：8ad817af072710333a52d1b669cb9a65f65a8d70。
- 结论：D-R3-01 关闭，无需 D-R4。当前已列出的测试装配返工项均已关闭；Task 8 整体仍等待真实部署门槛，不进入 Task 9/10。
- 依据：[D-R3-01 审计单](2026-10-22-task-08-deployment-test-review-round-3.md)。本轮仅审计，没有修改产品/测试、未提交，未运行 Docker 或更改宿主权限。

## 关闭依据

新增 component_mounts 在实际拓扑函数中使用，三个组件的挂载来源分别为：

| 组件 | 挂载 | 初始化内容 |
|---|---|---|
| control | control_volume:/srv/easel | 控制脚本、master.key、control.sqlite3 及附属文件 |
| sidecar | sidecar_volume:/etc/easel:ro | 仅 policy.json |
| worker 客户端 | worker_volume:/check:ro | 仅 client.py |

初始化助手的 /control、/sidecar、/worker 分别绑定独立卷；key/数据库仅复制到 /control，策略仅复制到 /sidecar，客户端脚本仅复制到 /worker。三个卷先校验本次运行标签，再初始化；全部加入既有归属校验及清理列表。

0750/0400/0600 权限和正式测试组件 UID10001、只读 rootfs、cap-drop、no-new-privileges 保留。初始化助手及权限检查助手属于一次性受控准备阶段，不是正式 worker/sidecar 获得控制卷。

实际 Docker 测试已加入 control 必要材料可读/数据库可用检查、sidecar 与 worker 无 key/数据库/控制脚本检查，然后继续执行合法/缺失/错误/跨 workspace/run 的授权矩阵。检查仅输出状态，不输出凭证内容。原 UID10001_ACCESS_OK 现在仅挂 control 卷，不再以共享控制材料作为下游可用条件。

主审临时探针驱动实际拓扑测试函数，以 fake Docker 捕获 argv：control/sidecar/worker 使用三个不同来源卷，清理列表含三个卷。不是只验证 helper 返回值，也没有把模拟结果描述为真实 Docker 运行。

## 本轮主审实际验证

指定 C:\Users\hyh\anaconda3\envs\Agent\python.exe：

- topology_support 纯逻辑回归及本机真实 sidecar/control broker 联调通过，Docker 拓扑文件未启用而跳过：10 passed、1 skipped、2 条既有 websockets 弃用警告，1.47 秒。
- 新增/修改的 control、client、sidecar 不可见性及访问检查嵌入脚本均 compile 成功。
- 实际拓扑函数的 argv/清理资源探针通过：三个独立卷，三个卷均纳入清理。
- git diff 77079be HEAD --check：退出 0。

本轮未独立执行 Docker 拓扑或原生 Linux 权限验证，没有重复全套 Windows 回归。执行 Agent 的 Docker 1 passed、764 passed/3 skipped、229 passed/20 skipped、Ruff 与无残留结果保留为外部交付证据，不记为主审独立实测。

## 当前状态与下一步

已记录的运行时代码、网络接线及测试装配定点问题均保持关闭。无需继续同项返工、重做绿色单测或反复重建测试镜像。

Task 8 尚未完成的外部门槛保持不变：

1. 已授权目标 Linux 环境，含防火墙/DOCKER-USER、IPv4/IPv6矩阵的实际验证；Docker Desktop 结果不能替代。
2. XFS 真实10GiB持久区配额及耗尽测试。
3. 管理员验收的 DSH_COMMIT、ACP profile 与最终 runtime 镜像构建/执行。
4. 硬化容器真实 Chromium 沙箱，目前仍为 No usable sandbox!，未通过。
5. 真实公网/Task5 live 与供应商门槛、宿主 provisioner→worker 健康链。

继续使用[部署证据记录](2026-10-22-task-08-deployment-evidence-reviewed.md)及[N-R2通过记录](2026-10-22-task-08-network-n2-accepted.md)中的外部前提清单。测试装配已修复，不会自动使这些真实门槛通过。

## 可复制给执行 Agent 的状态指令

8ad817af072710333a52d1b669cb9a65f65a8d70 的 D-R3-01 已复审通过，无需再做同项测试返工，也不改已通过的运行时代码。停止无外部前提的重复回归/镜像重试，不进入 Task9/10。

保留当前测试与证据，等待已授权目标 Linux 环境和管理员验收的 DSH pin/profile；只有这些前提落实后才继续原 Task8真实部署验收。仍缺前提时准确报告等待项，不自行寻找服务器、改变宿主全局权限/防火墙、关闭浏览器沙箱或以 mock 代替真实门槛。后续验收仅使用授权范围内的临时测试资源，凭证通过既有安全配置渠道装配，不输出在报告中。
