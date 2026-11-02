# Task 8 网络接线首轮审计：两项 P2，需返工 N-R1

- 基线：f644abde702c85ea4be5761c04fddcfb6a09d81e。
- 审查提交：d9b03304bb9cedb35a3b8b9a8e4e3e0f9ba59cdf。
- 结论：网络接线实现尚不能通过，需修复两项生命周期装配缺口；不进入 Task 9。
- 依据：[Task 8 交接单](../tasks/2026-10-21-task-08-worker-runtime.md)、[R5 关闭及网络后续指令](2026-10-21-task-08-r5-accepted.md)。本轮仅审计，未修改产品/测试、未提交，未操作真实 Docker、账号或持久区。

## T8-N-R1-01 [P2] 旧 worker 被复用，新增代理环境没有实际生效

位置：deploy/multiuser/provisioner.py 第 1456–1462 行、第 1473–1481 行；verify_worker_inspect 未校验运行中的接线配置。

ensure 先重写 env 文件并创建 sidecar，但遇到已有同配置、健康且符合原安全条件的 worker 时直接返回旧 ID。Docker 的 env-file 只在创建时读取；更新宿主文件不会更新现存进程的 Config.Env。

因此从 f644abd 的零出口部署原地开启 sidecar，或改变 broker/proxy 接线配置时，ensure 可以报告 ready，但实际 worker 仍没有新入口。当前测试只检查写出的 env 文件，不证明复用实例的运行环境已匹配。

主审与独立复审均用实际 Provisioner + 现有 FakeDocker、临时目录复现：预置原版健康安全 worker，实际 Env 没有 broker/proxy 配置；开启 sidecar 后调用 ensure。

```json
{"reused_old_id":true,"worker_runs":0,"sidecar_runs":1,"actual_worker_has_broker_url":false}
```

### 修复/验收

把实际接线配置纳入 worker 可复用/接管条件，核对运行中的 broker/proxy/no-proxy 等受管配置，或使用等效可信配置指纹。不能只看宿主文件/相同 workspace 配置版本。配置不符时不能返回已接线 ready；采用明确的安全重建/迁移或失败关闭策略，不盲目中断活跃 Run。

补已有零出口实例→启用 sidecar，以及运行环境与当前批准接线不符两种反例；真实 ensure/对账链不得复用错误配置。合法配置匹配的实例仍可复用；重建保留持久区；local/未启用 sidecar 模式继续兼容。测试 fake 应反映实际 docker run 时读取的环境，而不只断言 env 文件包含字段。

## T8-N-R1-02 [P2] 重启对账没有验证 sidecar，缺失出口仍被当成 ready

位置：deploy/multiuser/provisioner.py 第 1588–1605 行；easel/control/runtime.py 的 _reconcile 接管路径。

sidecar 的安全/健康验收只在 ensure 路径；inspect 只检查 worker、workspace internal 网络及 lifecycle health，没有检查 sidecar 或其出站网络。RuntimeManager 租约过期或管理进程重启时使用 inspect 对账，可直接接管，不会再调用 _ensure_sidecar。

主审及独立复审实际探针：先成功 ensure，再从 FakeDocker 删除 sidecar，调用真实 Provisioner.inspect：

```json
{"missing_sidecar":true,"report_ready":true,"sidecar_inspected":false}
```

这不仅遗漏缺失/不健康 sidecar，也绕过本次新增的 sidecar 实际安全配置验收。不能以 worker 自身 health 成功证明整个接线路径就绪。

### 修复/验收

启用 sidecar 时，对账结果必须反映完整批准依赖：worker 实际接线配置、sidecar 的归属/固定镜像/权限/资源/策略挂载/网络/健康，以及批准出站网络属性。缺失、不健康或不安全时不返回 ready、不缓存/接管为可用；根据明确策略安全修复或失败关闭。只在首次 ensure 校验不足以验收。

补真实 Store/RuntimeManager 重启或租约过期→真实 Provisioner.inspect 的回归：sidecar 缺失、不健康、安全参数或网络被篡改时不接管；合法完整依赖可接管且不重复启动/执行 prompt。禁止通过放宽严格 inspect、仅让 fixture 标 healthy 或对账时无条件返回 ready 通过。

## 本轮其他复核结果

- 每空间 sidecar、双网络、固定入口、CONNECT 固定 IP 连接和资源约束已实现；不是只配置代理环境变量。
- HTTP 专项未发现有实证的请求走私：重复 CL、TE+CL、绝对 URI 被拒绝且未连接上游；合法请求后拼接的管理请求没有转发。内嵌 CR/NUL/非法头由实际 h11/httptools 拒绝。
- CONNECT 调用 Task 5 的全地址校验和固定 IP 连接，未发现新增二次 DNS 解析。
- broker 仍按自己的 workspace/run/token 数据库授权；sidecar 不替换凭证。专项结论不等于已完成真实 control broker 联调。
- worker ID 不符的迟到 stop 被拒绝，没有证据表明它误停新 worker 的 sidecar；sidecar 停止失败会报错，worker 已删除后重试能清残留。未把无 worker 时清理 orphan 本身列为代次缺陷。

## 独立验证与未执行范围

指定 C:\Users\hyh\anaconda3\envs\Agent\python.exe：

| 验证 | 结果 |
|---|---|
| tests/multiuser | 735 passed、3 skipped，38.73 秒 |
| Task 8 八文件回归 | 229 passed、20 skipped，15.95 秒 |
| 容器/网络/sidecar 三个集成文件（本轮未启用） | 11 skipped，0.06 秒 |
| git diff f644abd HEAD --check | 退出 0 |

独立复审另跑 sidecar+egress：54 passed；sidecar provisioner+provisioner：109 passed、1 skipped。上述新建/正常路径的绿测试没有覆盖旧实例装配与对账缺口。

主审没有执行真实 Docker 构建/网络/sidecar 生命周期或浏览器。交付的 MANUAL_NETWORK_WIRING_OK、REAL_SIDECAR_LIFECYCLE_OK、WORKER_SMOKE_OK 作为外部交付证据保留，不能外推为最终镜像、真实 control broker 或目标 Linux 网络矩阵通过。

真实 control broker 联调、真实公网出口、目标 Linux 防火墙/IPv6、硬化 Chromium、最终 Harness 镜像/管理员 pin/profile、XFS 配额、宿主健康链仍未关闭。测试 HTTP 上游证明传输可达，未证明与真实 control broker 的授权/协议实际装配。

## 可复制给执行 Agent 的 N-R1 指令

基于 d9b03304bb9cedb35a3b8b9a8e4e3e0f9ba59cdf，只继续 Task 8 网络接线，修复 T8-N-R1-01/02；不重开已关闭的 R1–R5 inspect/停止恢复项，不进入 Task 9/10。

先补旧 worker 的实际 Config.Env 不匹配和重启对账缺 sidecar 的红测试，再最小修复。将批准接线配置纳入实际 worker 可复用条件；确保对账验证完整 sidecar 依赖。不要仅更新 env 文件或 fake 数据。保留活跃任务不被盲目重建、持久数据保留、严格安全验收、ID/代次约束与零出口模式兼容。

本批补一组可不依赖真实供应商凭证的实际 control 联调：使用真实 create_app/Auth/Config/Broker 与 fake provider transport，经过实际 sidecar socket 转发验证合法 token 正常到达 broker，错误/跨 workspace/run token 被真实 broker 拒绝，管理路径不进入 control。不要继续只用无授权的测试 HTTP 上游替代 broker 入口装配。

运行 tests/multiuser、Task 8 八文件回归、相关 broker/出口和可用集成；父提交到新提交 diff --check。可用 Docker 测试只使用自建测试资源，不操作开发者容器/全局防火墙。真实环境不具备的门槛继续明确记录，不用 mock 或宽松安全参数代替。显式路径提交必要实现、测试、部署说明，不夹带审计/任务单；交付父/新 SHA、逐项红绿、实际运行环境/对账及真实 broker 联调证据，然后停止等待复审。
