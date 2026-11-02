# Task 8 网络 N-R1 复审：一项 P2，需定点返工 N-R2

- 基线：d9b03304bb9cedb35a3b8b9a8e4e3e0f9ba59cdf。
- 审查提交：feadfcd23e7908f7d76741e1528db934d8824967。
- 结论：N-R1-02 已关闭，真实 control broker 本机联调通过；N-R1-01 剩下述单次探针失败导致强制重建的问题。不进入 Task 9/10。
- 依据：[网络首轮审计](2026-10-21-task-08-network-review-round-1.md)、[Task 8 交接单](../tasks/2026-10-21-task-08-worker-runtime.md)。仅审计，未修改产品/测试、未提交、未操作真实 Docker 或开发者数据。

## T8-N-R2-01 [P2] 一次 lifecycle 探测失败会删除仍在运行的接线不匹配实例

位置：deploy/multiuser/provisioner.py 第 1591–1603 行，重点第 1598–1602 行。

新增接线校验确实读取了实际 Config.Env，但保护旧实例的条件依赖一次 HTTP 探针成功。旧实例 Docker 状态为 Running=true、Health=healthy，只要 _probe 因超时、短暂不可达或非 ready 响应返回 False，代码便落入 _remove_container，执行 rm -f。

接线不匹配证明需要安全迁移，不能证明旧实例已退出、已 drain 或没有活跃任务。单次探针失败同样不能证明可以强制删除。当前分支既不确认退出，也没有控制面停止授权；会在启用/变更接线时中断可能仍在执行的 Run，与首轮要求和本次代码注释不符。

主审使用实际 Provisioner 和现有 FakeDocker、临时目录独立复现：

1. 先用无 sidecar 配置正常创建 worker，保存当时实际 Env 和健康状态。
2. 切换为当前 sidecar 配置；首次旧实例 lifecycle 探测抛 TimeoutError，之后探测正常。
3. 实际 ensure 对旧实例发送 rm -f，再创建新 worker 并返回 ready。

```json
{"old_state":{"Running":true,"Health":{"Status":"healthy"}},"probe_calls":2,"force_remove_old":true,"worker_replaced":true,"result":"ready"}
```

该探针没有执行真实容器或真实 prompt，不将“可能中断在途任务”描述为已经对真实用户任务造成损失。它直接证明了没有停止授权时删除运行中实例的真实调用路径。

### 修复与验收

接线不匹配且旧实例仍在运行时，失败关闭并保留原实例，交控制面按已有 drain/stop 顺序迁移；不要以探针失败作为强制删除授权。明确已退出的实例仍可按新接线重建，持久数据保留。若希望自动恢复运行中的不健康实例，必须先有可验证的控制面停止/任务收尾授权，不能凭单次健康请求推断。

补实际 ensure 红测试：旧实例 Running=true、原接线不匹配，分别让探针超时、抛网络错误或返回 ready=false。均应不复用错误接线、不删除/重建旧 worker、不先变更 sidecar；应返回明确错误，原实例及持久数据保留。另保留已退出实例按新 Env 重建、合法匹配实例复用、完整依赖对账通过等原回归。

## 已关闭部分

- **N-R1-01 的实际配置识别主体已补齐。** 实际 Config.Env 与批准 broker/proxy/no-proxy 配置进行比较，fake 在 docker run 时保存 env-file 内容；不再把重写宿主文件当作运行环境更新。剩余问题仅为上述不安全重建条件。
- **N-R1-02 完整依赖对账已关闭。** inspect 同时核对实际 worker 接线、internal 网络、sidecar 的归属/镜像/权限/资源/策略挂载/两条网络/健康、受控出站网络属性。缺失或篡改时不报 ready；真实 Store/RuntimeManager 的重启对账反例与合法接管均有覆盖。
- **本机真实 control broker 联调通过。** 新测试使用真实 ConfigService.publish、BrokerService.issue、AuthService/create_app 与 lifespan 专用数据库线程；uvicorn 提供真实 HTTP，独立 worker_sidecar.py 进程实际转发。fake 仅在 provider transport，不在授权路径。
- 合法 token 到达真实 broker 和 fake provider；错误 token 为 401、跨 workspace/run 为 404，未增加供应商调用；管理/未知路径拒绝。正常响应正文不含管理员 secret/broker token。不是浏览器登录/CSRF或完整泄露矩阵验收，也不是 Docker 拓扑实测。

## 独立验证与证明范围

使用 C:\Users\hyh\anaconda3\envs\Agent\python.exe：

| 验证 | 结果 |
|---|---|
| tests/multiuser | 752 passed、3 skipped、2 warnings，39.72 秒 |
| Task 8 八文件回归 | 229 passed、20 skipped，19.84 秒 |
| 容器/网络/sidecar 三集成文件（本轮未启用） | 11 skipped，0.06 秒 |
| git diff d9b0330 HEAD --check | 退出 0 |

独立复审另跑相关三个文件：159 passed、1 skipped；真实 control 联调单文件 1 passed。两条 warning 为 websockets 弃用提示，不影响本轮测试结果。

主审没有执行真实 Docker 构建、网络、sidecar 生命周期、Chromium、DSH/供应商、XFS、目标 Linux 防火墙或宿主健康探测，也未运行全仓。交付报告的容器成功结果保留为外部证据，不标为本轮主审实测。

Task 8 原有门槛仍不通过/未完成：No usable sandbox!、最终 Harness/runtime 镜像及管理员 DSH pin/profile、Docker 拓扑中的真实 control broker、真实公网/Task 5 live、目标防火墙/IPv6、XFS、宿主 provisioner 健康链。不能因代码专项关闭宣布多人正式可用。

## 可复制给执行 Agent 的 N-R2 指令

基于 feadfcd23e7908f7d76741e1528db934d8824967，仅修复 T8-N-R2-01，不进入 Task 9/10，不重做已关闭的完整依赖对账和 control broker 联调。

先补“运行中旧接线实例 + 单次探针失败”的实际 Provisioner.ensure 红测试，再最小修复迁移条件。Running 实例的接线不匹配必须保留、明确拒绝，交已授权 drain/stop 后迁移；不能让 _probe 的 false 触发 rm -f。补超时/网络错误/非 ready 三类，断言 worker/sidecar 不被删除或重建，持久状态不变；保留已退出实例重建和合法复用。

运行 tests/multiuser、Task 8 指定八文件回归、相关 broker/出口/sidecar 子集及可用集成，检查父提交到新提交 diff --check。无需为本次分支修复重建大镜像；不改浏览器安全边界、不操作开发者资源/全局防火墙。必要实现/测试显式路径提交，不夹带审计/任务单；交付父/新 SHA、逐项红绿、实际删除/重建目标与保留证据及未执行门槛，然后停止等待复审。
