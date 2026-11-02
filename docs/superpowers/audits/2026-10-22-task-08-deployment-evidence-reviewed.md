# Task 8 环境盘点与 Docker 拓扑补充复审

- 基线：504db8d22ebd753dc9e8dfcab8724b5260a319da。
- 审查提交：7bbb0cc07e8366501b4db146286fffec4b63fdd3。
- 结论：本批授权拓扑的实现与证明范围成立，没有新的运行时代码返工项；新增测试需补齐下述资源清理及 Linux 目录准备。Task 8 整体仍未验收，等待外部环境与受管配置，不进入 Task 9/10。
- 依据：[N-R2 通过及环境阶段指令](2026-10-22-task-08-network-n2-accepted.md)、[Task 8 交接单](../tasks/2026-10-21-task-08-worker-runtime.md)。仅审计，没有修改产品/测试、没有提交，没有操作真实 Docker 或宿主环境。

## 新证据的有效范围

本批只有三个文件：新增 tests/integration/multiuser/test_control_broker_topology.py，更新部署 README 和出口策略说明，没有修改运行时代码。

测试中的容器及进程链路确实为：

```text
internal workspace 网络中的受限客户端容器
  → 真实 worker_sidecar.py 容器（internal + 出站网络）
  → 真实 create_app/Auth/Config/Broker control 容器
  → fake provider transport
```

控制数据库由真实 ConfigService 发布配置、BrokerService 签发 token；control 使用真实 lifespan/HTTP/数据库授权。fake 仅位于供应商传输层，返回成功前会验证控制侧注入的管理员测试凭证。客户端检查合法请求、缺失/错误 token、跨 workspace/run 及管理路径拒绝。

镜像 fixture 将提供的 tag 解析为实际 sha256 ID，再按该 ID 启动。每次测试创建随机命名的容器/网络及临时目录，control 持有测试数据库/master key，客户端仅挂客户端脚本，sidecar 仅挂测试策略；没有把控制秘密挂载给客户端。

执行 Agent 报告的 Docker 拓扑 1 passed 可作为本批外部交付证据保留。证明范围是 Docker Desktop 上、既有 execution 测试镜像中的真实 broker 授权拓扑；不等于管理员批准最终镜像、真实 Harness/技能执行、真实供应商或目标 Linux 隔离。

环境盘点中“未提供目标 Linux 连接、未提供管理员 DSH pin/profile、无最终 runtime 摘要”等外部前提已明确列出，没有以 WSL 盘点或测试镜像代替目标验收。没有必要为这份报告再次返工已关闭的产品代码。

## 新增测试的准备缺口

### D-R1-01 [P2] 匿名客户端在 CLI 超时后无法被 finally 清理

位置：tests/integration/multiuser/test_control_broker_topology.py 第 283–315 行。

客户端使用匿名 docker run --rm，只有容器退出时才自动删除；宿主 Docker CLI 超时/中断不保证容器立即退出。finally 只删除 sidecar/control，没有客户端标识或清理动作；残留客户端仍可占用 workspace 网络，随后的 network rm 失败又被忽略。

主审用实际测试函数、临时 SQLite/目录及 fake Docker 边界，在客户端 run 注入 TimeoutExpired，捕获：worker_named=false、worker_labelled=false，finally 仅移除 sidecar/control，没有客户端清理目标。未运行真实 Docker。

修复：为客户端及其他自建资源设置随机测试名称与本次运行标签；将客户端纳入 finally，清理前核对归属，对本次资源的失败清理给出可见结果。不能把 --rm 等同于 CLI 失败后的可靠回收。新增超时路径回归，证明客户端、sidecar/control 和测试网络都被纳入清理；不要删除其他资源。README 的“清理前核对受管标签”也应与新测试实际一致。

### D-R1-02 Linux 目录权限需在目标验收前准备并验证

位置：同文件第 249–254 行（控制容器使用 UID 10001，直接 bind pytest tmp_path）。

源码没有为该目录及测试脚本、数据库准备容器 UID 的访问权限。原生 Linux 上 pytest 临时目录通常由宿主测试用户创建为 0700，UID 10001（rootless 下还可能映射成其他宿主 UID）不一定能读取脚本或写 SQLite。Docker Desktop 的 bind 权限语义不能证明原生 Linux 可运行。

此项是有代码依据的可移植性风险，本轮未在原生 Linux/Docker 实机复现，不把它写成已观察到的 Linux 测试失败。目标运行前须以限定本次临时资源的方式准备目录/卷及权限并复验；不要 chmod 777、修改开发者目录或为通过测试让 control 以 root 运行。测试 key 为公开固定 sentinel，但仍不应养成把控制目录全局可写的部署方式。

## 主审本轮实际验证

使用指定 Agent Python：

- 新测试 Python AST、嵌入的 _CONTROL_APP 和 _CLIENT 编译成功。
- 新 Docker 拓扑测试关闭显式运行开关后正常收集、跳过；本机真实 sidecar→control broker 测试通过：1 passed、1 skipped、2 条既有 websockets 弃用警告，1.41 秒。
- git diff 504db8d HEAD --check：退出 0。

本轮未重跑 Docker 拓扑，不将外部 1 passed 记为主审独立实测。运行时代码未变，未重复全套 Windows 回归；交付报告的 755 passed/3 skipped、229 passed/20 skipped 保留为执行 Agent 结果。未运行全仓、真实供应商/公网、Chromium、XFS 或目标宿主网络/健康链。

## 当前状态与下一步

已记录代码返工已关闭；新增 Docker broker 授权拓扑有可复现测试和外部运行证据。下一步需要落实下面的外部前提，而不是继续重复同一批绿色单测。

1. **已授权 Linux 测试环境。** 明确连接方式/主机或专用 Docker context、可用权限和临时 workspace 根。现有 Docker Desktop/WSL 盘点不能自动视为目标宿主，也不能自行猜测远端连接或修改宿主全局规则。
2. **管理员已验收 Harness。** 提供准确 DSH_COMMIT、受管 ACP profile，以及已有最终镜像时的固定 digest。不能以 latest、任意 commit 或临时假 Harness 填补。
3. **配额与网络测试能力。** 目标持久根实际 XFS/prjquota，所需 xfs_quota 工具、测试空间及权限；目标 IPv4/IPv6 和防火墙规则的审查/实施范围明确。先准备具体变更供审查，不能擅自更改全局防火墙或开发者数据。
4. **浏览器和宿主健康链。** 在原非 root/只读/cap-drop/no-new-privileges 边界下解决 No usable sandbox! 并真实复验；宿主 provisioner 必须实际直达 worker 容器 IP，不能用容器内 exec 替代。
5. **真实公网与供应商门槛。** 管理员批准的出口 hosts/CIDRs，以及通过既有安全配置渠道装配的凭证。凭证不贴聊天、不进入报告；fake provider 的授权链成功不等于 live 出口/供应商调用成功。

这些前提没有落实前，正确状态是“Task 8 等待部署验收条件”，保持不开放多人执行、不进入 Task 9/10。无需新一轮代码返工，也不要求执行 Agent继续盲目重建镜像、自动安装 WSL 工具或配置未知服务器。

## 可复制给执行 Agent 的状态指令

基于 7bbb0cc07e8366501b4db146286fffec4b63fdd3，授权拓扑的实现与证明范围成立，先只补新增测试的 D-R1-01 资源清理及 D-R1-02 Linux 目录准备，不重开已关闭的产品代码，不进入 Task 9/10。对客户端 CLI 超时补可复现红测试；以随机名称/标签跟踪全部自建资源，finally 清理前校验归属，报告清理失败。为 UID10001 准备可用且限定于本次测试的目录/卷；有原生 Linux 时真实验证，没有则准确报告未执行，不能靠 Docker Desktop 推断。

无需为这些测试修正反复重建大镜像或重复 Windows 全量回归；运行必要定向测试、脚本编译/收集与 diff --check。显式路径提交测试及必要部署说明，不夹带审计/任务单，不操作开发者目录/容器，不改产品隔离要求。

等待人提供已授权目标 Linux 环境和管理员已验收 DSH commit/profile；只有前提落实后，才继续按原 Task 8 运行真实浏览器、最终镜像、XFS、宿主健康链、目标防火墙/IPv6及 live 出口等验收。提供的连接/策略按具体授权范围使用；账号、密钥和供应商凭证只通过安全配置渠道装配，不输出在交付报告中。没有外部前提时保持等待，不自行找服务器、改变权限边界或将未执行改记为通过。
