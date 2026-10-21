# Worker 网络与出口策略（Task 8）

本文定义 worker 容器的实际网络边界。**凡是标注“未验证”的行，都不得在
审计或发布材料中表述为已隔离通过。** 真实结果以
`tests/integration/multiuser/test_container_isolation.py` 在 Linux Docker
Engine 上的输出为准。

## 已实现的基线

`deploy/multiuser/provisioner.py` 为每个 workspace 创建独立网络
`easel-ws-<uuid>`（`docker network create --internal`），并只允许通过
`build_run_argv` 构造的固定 argv 启动容器：

- 每空间独立 `--internal` 桥，不共享默认 `bridge`；
- 不发布任何宿主端口（argv 中无 `-p`/`--publish`）；
- 不挂载 Docker socket，不挂载 provisioner socket；
- 只有两个 bind：`/workspace` 与 `/home/easel`，来源在固定工作区根内；
- 唯一入站用途是宿主机 provisioner 对容器 IP:7860 的
  `/internal/lifecycle/health` 探测（带 workspace 身份头与服务 token）。

## 访问矩阵

| 源 → 目标 | 期望 | 当前实现/依据 | 本轮验证状态 |
|---|---|---|---|
| worker → 其他 worker | 拒绝 | 每个 workspace 独立 `--internal` 网络；Docker 网络间默认由隔离链阻断 | 待 Linux 集成实测（A 访问 B 的容器 IP:7860） |
| worker → 宿主管理面（docker0/宿主 IP 的 7861、2375、2376 等） | 拒绝 | `--internal` 不提供外部路由；provisioner 不发布控制端口或 Docker API | 待 Linux 集成实测 |
| worker → control 管理监听 | 拒绝 | control 不加入任何 worker 网络；worker 无默认路由 | 待 Linux 集成实测 |
| worker → provisioner Unix socket | 拒绝 | socket 位于宿主私有目录，不挂载进容器 | argv 与 inspect 断言 |
| worker → 云元数据（169.254.169.254） | 拒绝 | `--internal` 无外部/链路本地路由 | 待 Linux 集成实测 |
| worker → 公网 IPv4 | 拒绝 | `--internal` 禁止外部连通 | 待 Linux 集成实测 |
| worker → 公网 IPv6 | 拒绝 | 内部桥默认未启用 IPv6，无 IPv6 地址/默认路由 | 待 Linux 集成实测（检查地址与路由） |
| worker → 必要入口 / provider broker | **允许**（业务目标） | **尚未接线**：Task 10 relay 之前不暴露 worker 业务入口；broker 属 control 侧 | 未启用 |
| worker → 受控公网出口（egress sidecar） | **允许**（业务目标） | **尚未接线**：本轮不创建 sidecar，worker 保持零出站 | 未启用 |
| 宿主 provisioner → worker lifecycle | 允许（仅健康探测） | 宿主经内部桥访问容器 IP:7860，带身份/token 校验 | 待 Linux 集成实测 |

“允许”的两行是明确的后续接线点，不是当前能力。在接线前，worker 处于
全拒绝（fail-closed）状态；技能执行若需要外网，会明确失败，不会静默直连。

### 后续接线的约束（实现前必须逐项验证）

若要让 worker 访问 broker 或受限公网出口，必须采用独立 egress sidecar：

1. sidecar 同时接入 `easel-ws-<uuid>`（internal）和一条受控出站网络；
2. 只允许 sidecar 出站，worker 自身仍不得获得默认路由；
3. 出口使用 Task 5 的真实 CONNECT 代理与管理员策略文件，不用
   `http_proxy`/`https_proxy` 环境变量代替网络规则；
4. broker 的受限入口不得顺带暴露 control 管理监听；
5. 宿主机 `DOCKER-USER` 链显式拒绝 worker 源地址访问管理网段，并覆盖
   IPv6；Docker 的发布端口会绕过 UFW，不能只依赖 UFW；
6. DNS 重绑定必须沿用 Task 5 的“连接阶段解析、校验全部地址、按已校验 IP
   连接”机制，不做二次解析。

## IPv4/IPv6 与 DNS

- Docker 默认只为内部桥分配 IPv4；是否完全无 IPv6 地址/路由必须在真实
  Linux 上检查（`ip -6 addr`、`ip -6 route`），不能仅凭“没有写 IPv6 配置”
  断言。
- 容器内 Docker 内嵌 DNS（127.0.0.11）仍可能解析名称；无路由时连接失败。
  这不构成出口：验收必须测实际连接，不是只看 DNS 响应。
- 若宿主启用 Docker IPv6，需要重复矩阵中的 IPv6 行；未验证即视为不通过。

## 宿主防火墙要求（部署方执行）

- 控制面发布端口默认只绑 `127.0.0.1`，公网入口经 TLS 反向代理；
- 在 `DOCKER-USER` 链拒绝来自 `easel-ws-*` 网段的宿主管理/控制/provisioner
  端口（含 2375/2376、7861 管理监听、SSH 等）；
- 拒绝元数据网段与内部管理网段的转发；
- IPv6 使用对应的 ip6tables 规则；
- 规则需要重启后持久化，并由部署方实际验证。本仓库不自动安装防火墙规则，
  也不把“已写文档”当作“规则已生效”。

## 浏览器

worker 镜像包含固定版本 Chromium/Playwright。Chromium 必须使用内置沙箱；
禁止以 `--no-sandbox`、`--privileged` 或恢复 capabilities 的方式通过验收。
在只读 rootfs、`cap-drop=ALL`、`no-new-privileges` 下浏览器沙箱是否可用，
必须在目标 Linux 内核上实际启动浏览器验证；不可用即视为浏览器门槛未通过，
不得降低容器安全配置来迁就。
