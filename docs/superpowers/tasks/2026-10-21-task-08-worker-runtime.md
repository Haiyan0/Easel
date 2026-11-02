# Task 8：Worker 容器、provisioner 与生命周期

> 外部代码 Agent 按已批准 plan 和本交接单逐步执行；采用 executing-plans 工作流。完成后停止等待审计，不进入 Task 9。

日期：2026-10-21。基线：efdd4edc1dc7e3ae7f21aa12804258e329aaef8a。

**目标：** 单台 Linux 上为每个 workspace 安全创建、识别、回收独立 worker；进程重启后可对账既有实例，保留持久数据，真实隔离能力缺失时不开放多人执行。

**依据：** [spec](../specs/2026-10-16-multiuser-workspaces-design.md) 第 4、6、10 节及 [plan](../plans/2026-10-16-multiuser-workspaces.md) Global Constraints、Task 8。沿用既定设计，不新增 Kubernetes/Redis/ORM，不提前实现 Task 9 完整 SessionService 或 Task 10 relay。

## 环境与文件范围

先读 AGENTS.md、CLAUDE.md。本机 PowerShell、C:\Code\Easel，测试 Python 固定 C:\Users\hyh\anaconda3\envs\Agent\python.exe；容器集成在 Linux Docker Engine 执行，优先 rootless。记录起始分支、HEAD、git status 和可用环境，不操作开发者真实容器/账号或删除已有 workspace。

```text
Create: easel/control/runtime.py
Create: easel/worker/lifecycle.py
Create: deploy/multiuser/Dockerfile.worker
Create: deploy/multiuser/compose.yaml
Create: deploy/multiuser/provisioner.py
Create: deploy/multiuser/egress-policy.md
Create: deploy/multiuser/README.md
Create: .dockerignore（如已存在则最小修改）
Create: tests/multiuser/test_runtime.py
Create: tests/integration/multiuser/test_container_isolation.py
As needed: control Store 的生命周期持久化接口/追加迁移与旧库升级测试
As needed: CLI/worker 入口/受限 provisioner 客户端及必要测试
```

只显式路径提交本任务代码/测试/部署文件，中文提交信息；不夹带审计和任务单，不 reset 或覆盖其他修改。部署手册、策略说明属于本任务交付，审计和任务指令仍独立保留。

## 接口与状态

```python
RuntimeManager(store: Store, provisioner: Provisioner)
await ensure_ready(workspace_id: str, config_version: int) -> WorkerHandle
await drain(workspace_id: str) -> None
await collect_idle(now: float) -> list[str]

# 本任务定义 Protocol；实现通过受限本地 Unix socket，不把 Docker 能力给 control
await Provisioner.ensure(workspace_id, config_version) -> WorkerHandle
await Provisioner.stop(workspace_id) -> None
await Provisioner.inspect(workspace_id) -> dict
```

WorkerHandle 的 workspace_id、container_id、upstream、service_token、config_version 沿用已有类型，凭证不进入 repr/日志。upstream 来自 provisioner 受管结果，不允许用户指定 URL；确认 workspace/配置版本与实例身份匹配后才返回可路由句柄。

状态：absent → starting → ready → draining → stopped，另有 failed。持久化生命周期租约/所有者/代次，确保多连接或多个管理进程竞争不重复创建、旧持有者不覆盖新代次；不能只用进程内 asyncio.Lock。SQLite 不跨线程复用连接，也不在长事务里 await Docker/网络操作。新增表只能追加 migration，历史迁移不改。

就绪期限 60 秒；失败/超时返回明确 503 与 Retry-After，不路由到其他用户实例。空闲回收阈值 900 秒，必须同时无活跃 Run、无活跃请求；等待权限确认、发布或正在执行均不得算空闲。draining 时拒绝新任务，已有任务按明确策略收尾。停止仅回收可再建容器，绝不删除持久挂载。

Task 6 的签名 Run 快照与执行认领继续生效：不得把就绪探测当一次 prompt、伪造活跃 Run，或让首次快照永久授权后续任务。若需要最小 bootstrap/readiness 入口放 lifecycle 中，明确其认证和身份/版本响应；真正执行仍必须使用合法 Run 级快照。报告本任务装配路径与后续 relay 的接入边界，不能靠放宽现有启动校验换取 health=200。

## 实施与验收步骤

- [ ] **1. 先红。** fake provisioner 覆盖同 workspace 并发只启动一次、A/B 不串、ready 返回错误 workspace/配置版本、启动失败/超时、draining 期间新请求、活跃任务不回收。再补两独立 Store/manager 争用和管理进程重启后接管旧实例测试，记录失败后最小实现。

- [ ] **2. 持久生命周期。** 实现数据库认领、心跳/过期处理、状态对账和安全回收。inspect 以容器受管标签及身份响应验证实际归属，不接管任意同名容器。崩溃残留任务标 interrupted，不自动重发 prompt 或发布；后续显式重试遵循现有 Run 幂等规则。配置更新与活跃任务不互相覆盖；不用旧配置句柄冒充新版本已就绪。

- [ ] **3. 受限 provisioner。** 仅此进程具有容器管理权限。Unix socket 有明确所有者/权限；协议只接受结构化 workspace UUID 和受管镜像/配置版本、ensure/stop/inspect 等限定动作。服务端从批准配置构造 argv，不接受 raw docker argv、命令、volume、网络、环境或任意镜像。派生宿主绝对路径后验证落在固定工作区根内，拒绝 symlink/junction 等重定向。control、broker、worker 都不挂 Docker socket；worker/broker 不挂 provisioner socket，不开放远程 Docker API。

- [ ] **4. 固定镜像。** 锁定管理员受管 DeepSeek Harness 的版本/commit 与 ACP profile，构建时安装依赖及生成只读 profiles；启动禁止在线 npm/pip 安装。镜像含所需 Python/Node/Chromium，代码与规则位于 /opt/easel，只读；本人数据 /workspace，HOME /home/easel，state_root 显式配置。受管配置、公钥只读，控制私钥/master.key 不进镜像/worker。不得 COPY 开发者 .env、outputs、profiles、Cookie、HOME 或审计附件；.dockerignore 需有实际构建上下文验证。

- [ ] **5. 资源与挂载。** 强制非 root、read-only rootfs、cap-drop=ALL、no-new-privileges；每 worker 初始 2 CPU、4 GiB 内存、256 PIDs、1 GiB /tmp。持久区默认 10 GiB，使用经验证的项目配额或独立受配额文件系统；不能把 docker --storage-opt 对 bind mount 的无效限制当配额。能力/配额不生效时拒绝启用多人执行，不允许用静默警告或成功 inspect 占位替代。本人持久区跨容器重建保留，其他 workspace、宿主源码/秘密不可见。

- [ ] **6. 网络与浏览器。** 每 workspace 独立网络；worker 不发布宿主端口，不能经默认 bridge 互访。明确并实施 worker→必要入口/provider broker/受限公网出口允许、worker→其他 worker/宿主管理/control 管理/provisioner/元数据拒绝的矩阵，覆盖 IPv4/IPv6。broker 的受限入口不能顺带暴露 control 管理监听；沿用 Task 5 真实连接层出口策略，不用环境代理变量代替防火墙。检查已有技能 Chromium 参数，worker 不能靠 --no-sandbox、privileged 或恢复 capabilities 验收成功；local 兼容按模式区分。真实浏览器启动并执行临时 context/持久 profile 验证。

- [ ] **7. 实际 Linux 集成。** 测试自己创建的临时 A/B 容器：inspect 的用户/rootfs/capabilities/security/resource/网络/挂载；实际越界读写、只读代码、进程/内存/tmp/持久额度，以及跨 workspace、管理网、IPv4/IPv6 访问拒绝。冷启动竞争、异常退出、重建数据保留、drain/idle/重启对账同样验证。清理前核对测试容器标签和路径，只删本测试容器/网络，不删开发者持久数据。

- [ ] **8. 回归与交付。** 单元通过后运行下方回归和实际可用的 Linux 集成。缺 Linux/Docker/配额环境要明确“未执行”，提供可复现命令与失败关闭证据；本机 mock 通过仅能声称代码层验证，不能声称真实隔离已验收。不要为通过测试自动安装大体积依赖、部署到未知服务器或开公网端口。

## 必须固化的故障测试

- 两管理进程/连接同空间竞争、旧租约拥有者延迟返回、启动超时后迟到 ready，不重复创建或覆盖新状态。
- 假 ready 指向另一 workspace/版本/容器标签，明确拒绝，不缓存错误句柄。
- drain 与新请求并发、权限等待/发布中空闲时间已过，不提前停止；释放后可正常回收。
- 任意路径/volume/镜像/命令/环境字段注入、非法 UUID、socket 未授权调用，在执行 Docker 前拒绝。
- 配额/内核资源能力/网络规则不可验证，启动失败关闭；不能靠测试中仅检查 argv 判定实际生效。
- control 重启识别原实例且不重复执行任务；失败回收不删除 outputs、profile、原生会话和签名接纳记录。

## 验证命令与报告

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser -q
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/test_harness_workspace.py tests/test_agent_harnesses.py tests/test_runtime_env.py tests/test_local_write_guard.py tests/test_web_security.py tests/test_account_stats_login.py tests/test_login_start.py tests/test_output_paths.py -q
git diff efdd4ed HEAD --check
```

Linux 上使用该环境的明确 Python 运行 tests/integration/multiuser/test_container_isolation.py，并按部署手册运行真实出口/浏览器门槛。日志和交付结果不输出完整 env、服务 token、模型 Key 或 Cookie。

交付父/新 SHA、文件清单、红绿、状态转移/数据库租约、socket 协议、固定 Harness/image 版本、容器 argv 和脱敏 inspect、配额实现、网络矩阵、实际入口装配及回归结果。未执行门槛逐项列出。建议提交信息：feat(multiuser): 创建隔离工作区容器并管理生命周期。

完成后停止等待审计，不进入 Task 9，不把代码层通过表述为多用户正式发布通过。
