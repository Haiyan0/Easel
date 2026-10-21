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
- worker 只有两个 bind：`/workspace` 与 `/home/easel`，来源在固定工作区根内；
- 唯一入站用途是宿主机 provisioner 对容器 IP:7860 的
  `/internal/lifecycle/health` 探测（带 workspace 身份头与服务 token）；
- 启用 sidecar 时，provisioner 另建同权限边界的
  `easel-sidecar-<uuid>`，同时接入 `easel-ws-<uuid>`（internal）和已存在
  的非 internal 受控出站网络；worker 不加入出站网络。

## 访问矩阵

| 源 → 目标 | 期望 | 当前实现/依据 | 本轮验证状态 |
|---|---|---|---|
| worker → 其他 worker | 拒绝 | 每个 workspace 独立 `--internal` 网络；Docker 网络间默认由隔离链阻断 | 本机 Docker Desktop linux/amd64 实测通过（A 访问 B 的容器 IP:7860 失败） |
| worker → 宿主管理面（docker0/宿主 IP 的 7861、2375、2376 等） | 拒绝 | `--internal` 不提供外部路由；provisioner 不发布控制端口或 Docker API | 本机实测通过（网关 7861/2375 连接失败） |
| worker → control 管理监听 | 拒绝 | control 不加入任何 worker 网络；worker 无默认路由 | 本机实测通过（无 IPv4 默认路由） |
| worker → provisioner Unix socket | 拒绝 | socket 位于宿主私有目录，不挂载进容器 | 严格 inspect 实测仅两个批准挂载；无 socket |
| worker → 云元数据（169.254.169.254） | 拒绝 | `--internal` 无外部/链路本地路由 | 本机实测通过 |
| worker → 公网 IPv4 | 拒绝 | `--internal` 禁止外部连通 | 本机实测通过（1.1.1.1:53 失败） |
| worker → 公网 IPv6 | 拒绝 | 内部桥默认未启用 IPv6，无 IPv6 地址/默认路由 | 本机实测通过（检查地址/路由并实际连接失败） |
| worker → 必要入口 / provider broker | **允许**（仅白名单路由） | worker 仅经 sidecar:8899 的 `/internal/broker/<channel>/<relative>` 精确路由表转发到 control broker | 本机 Docker 真实网络接线通过（fake broker；真实 control broker 联调未执行） |
| worker → 受控公网出口（egress sidecar） | **允许**（仅策略批准目标） | worker 仅经 sidecar:8898 CONNECT，复用 Task 5 解析后固定 IP/拒绝私网/策略例外 | 本机 Docker 接线通过（批准测试网段与私网拒绝；真实公网出口未执行） |
| worker → sidecar broker 入口的非白名单路径/方法 | 拒绝 | sidecar 只转发 `BROKER_ROUTE_TABLE` 精确 route；其余 404，不转发 control 管理接口 | 单测与真实接线测试通过 |
| 宿主 provisioner → worker lifecycle | 允许（仅健康探测） | 宿主经内部桥访问容器 IP:7860，带身份/token 校验 | 待 Linux 集成实测 |

允许路径通过每空间 sidecar 实现：sidecar 同时接入 internal workspace 网络
和受控出站网络，worker 自身仍无默认路由。broker 入口只接受精确白名单路由，
出口使用 Task 5 的连接层策略；环境变量只用于客户端指向 sidecar，不代替
网络规则。

上表“本机实测”来自 Docker Desktop 29.2.1 / linux-amd64 容器网络，不能
替代目标 Linux 宿主（含 Docker IPv6、宿主防火墙与 DOCKER-USER 规则）的
复验；部署方仍须按本文矩阵逐行实测。

### sidecar 接线的固定约束

1. sidecar 同时接入 `easel-ws-<uuid>`（internal）和一条已存在、非 internal
   的受控出站网络；worker 不加入后者。
2. 只允许 sidecar 出站，worker 自身仍不得获得默认路由。
3. 出口使用 Task 5 的真实 CONNECT 代理与管理员策略文件，不用
   `http_proxy`/`https_proxy` 环境变量代替网络规则。
4. broker 的受限入口只暴露精确 broker route；不能顺带暴露 control 管理
   监听。worker 只能访问 sidecar 的固定端口。
5. 宿主机 `DOCKER-USER` 链显式拒绝 worker 源地址访问管理网段，并覆盖
   IPv6；Docker 的发布端口会绕过 UFW，不能只依赖 UFW。
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

**参数层已修复：** 所有实际 Chromium 启动点统一调用
`runtime_paths.chromium_launch_args()` 和
`runtime_paths.chromium_sandbox_options()`。worker 模式会移除
`--no-sandbox` / `--disable-setuid-sandbox`（含 `=value` 形式），并显式
传入 Playwright `chromium_sandbox=True`，避免驱动默认重新注入
`--no-sandbox`。local 仅为开发机兼容保留原参数。测试捕获真实 launch
kwargs 与最终驱动命令行，并验证缺少沙箱能力时异常向上传播，不会静默
回退。

**真实门槛未通过：** 本机 Docker Desktop linux/amd64 的硬化容器内，
`chromium_sandbox=True` 启动明确失败：`No usable sandbox!`。因此当前
**浏览器隔离门槛不通过**，不得开放多人执行。目标 Linux 内核需提供可用的
用户命名空间或 suid 沙箱；不得以关闭沙箱、`--privileged` 或恢复
capabilities 的方式迁就。
