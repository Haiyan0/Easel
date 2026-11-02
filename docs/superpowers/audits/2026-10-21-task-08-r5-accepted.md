# Task 8 R5 定点复审通过；整体验收仍待完成

- 基线：77055402d4b8b6a486e9d1c1435c66089e797b21。
- 审查提交：f644abde702c85ea4be5761c04fddcfb6a09d81e。
- 结论：T8-R5-01 关闭，不要求 R6。本次关闭的是已记录的代码返工项，Task 8 整体尚未通过，不进入 Task 9、不开放多人执行。
- 依据：[R5 审计单](2026-10-21-task-08-review-round-5.md)、[Task 8 交接单](../tasks/2026-10-21-task-08-worker-runtime.md)。本轮仅审计，没有修改产品/测试或提交，没有进行真实 Docker 变更。

## 关闭依据

deploy/multiuser/provisioner.py 新增 _single_object，要求成功响应为恰好一个对象的数组。名称查询、最终实例查询和网络查询均走同一校验。非法成功响应明确抛 inspect_invalid/network_invalid，不再返回表示目标不存在的 None。

已核对精确 kind/target absent 匹配没有退化；真正目标不存在仍可幂等成功，context/endpoint/daemon 错误继续失败关闭。停止承诺恢复及按实例 ID 执行 stop/rm 的约束保留。

主审临时探针使用实际 RuntimeManager、真实 Store、实际 Provisioner，Docker 边界返回退出 0 的 []、{}、[null]、[{},{}]：四种情况均抛 inspect_invalid，租约保留 stopping，原对象仍运行，未执行 stop/rm。

独立复审另以内存传输驱动实际 _handle_client → parse_request → _dispatch，结合真实 RuntimeManager/SQLite，确认：

- 初始名称查询：四种非法响应均非成功，原实例不变，没有 stop/rm，数据库不写 stopped。
- 最终按实例 ID 确认：四种非法响应均非成功；即使 stop/rm 已执行，无法确认终态时数据库仍保留 stopping。
- 网络查询：四种非法响应均 network_invalid，不执行 network create/run。

Windows 内存传输覆盖真实服务处理逻辑，不等同于本轮已执行 Linux Unix socket 集成。

## 独立验证

指定 C:\Users\hyh\anaconda3\envs\Agent\python.exe：

| 验证 | 结果 |
|---|---|
| tests/multiuser | 718 passed、3 skipped，37.42 秒 |
| Task 8 八文件回归 | 229 passed、20 skipped，16.33 秒 |
| 容器隔离集成（本轮未启用） | 9 skipped，0.07 秒 |
| git diff 7705540 HEAD --check | 退出 0 |

独立复审另跑 provisioner：102 passed、1 skipped。未运行全仓、真实 Docker 构建/浏览器、真实 DSH/供应商、XFS 或目标宿主网络/健康探测。交付报告的既有镜像 socket 探针保留为外部交付证据，不标为主审实测。

## Task 8 剩余验收状态

| 项目 | 当前状态 | 后续要求 |
|---|---|---|
| 本轮 inspect/生命周期定点代码项 | 已通过 | 保留回归，不再重做同一返工 |
| worker→broker/受限出口允许路径 | 尚未实现 | 在 Task 8 内补齐，不能把全拒绝当允许路径通过 |
| 真实硬化 Chromium | 已执行但未通过：No usable sandbox! | 在满足安全边界的目标 Linux 环境解决并复验，不关闭沙箱或增加特权 |
| 完整 execution/最终 Harness 镜像 | 未完成验收 | 完整重建；真实 Harness 需管理员已验收 DSH commit/profile |
| XFS 持久区配额 | 未执行 | 真实配额生效与耗尽验证，不以 argv/mock 或无配额测试代替 |
| 宿主 provisioner→worker 健康链 | 未执行 | 真实宿主调用链，不能用容器内 docker exec 探测代替 |
| 目标 Linux 防火墙/IPv4/IPv6 矩阵 | 未完成 | 在接线后同时验证允许与拒绝，保留跨 workspace/管理面隔离 |

因此下一批是 Task 8 的网络实现与部署门槛，不是 Task 9，也不是继续为已关闭项目追加返工。

## 可复制给执行 Agent 的下一批指令

基于 f644abde702c85ea4be5761c04fddcfb6a09d81e，T8-R5-01 已复审通过，不再修改已通过的 inspect/停止恢复实现。继续原 Task 8 第 6–8 步，优先完成目前尚未实现的 worker→provider broker/受限出口允许路径，不进入 Task 9 的会话服务或 Task 10 的完整 relay/SSE。

按现有 spec、plan 和 deploy/multiuser/egress-policy.md 的独立 sidecar 约束实施，先补红测试再最小接线：每空间隔离网络，worker 无直接公网默认出口，仅经批准入口访问 broker/受限代理；复用 Task 5 真实连接层出口策略。broker 入口不能同时暴露 control 管理接口，sidecar 不得成为任意内网/其他 workspace 的通用代理。凭证绑定沿用已有 workspace/run/token 授权，不下发管理员 Key，不放宽受限 provisioner 协议为 raw Docker 参数。

同时覆盖 sidecar/网络的创建、复用、安全 inspect、失败清理与停止重建，保留本任务已通过的路径、配额、实例与代次约束。拒绝与允许必须成对验收：合法 broker 调用/受限公网访问，跨 workspace、宿主管理/control 管理、provisioner、元数据、非法目的地址拒绝；覆盖 IPv4/IPv6 与 Task 5 DNS 重绑定防线。不得仅设置环境代理后宣称网络隔离已实现。

可用 Linux/Docker 环境内使用本测试自建资源验证；不要操作开发者容器/账号/持久区，不自动部署未知服务器或修改宿主全局防火墙。真实目标环境或管理员 DSH pin/profile 不可用时，明确列为外部前提，先完成不依赖它们的网络代码与测试；不要为填绿降低浏览器/容器安全边界，不用 mock 代替真实门槛。

更新部署说明，给出准确拓扑、固定入口、允许/拒绝矩阵及实际与未执行结果。运行 tests/multiuser、Task 8 指定回归、相关 broker/出口与可用 Linux 集成；未执行项明确标记。必要源代码、测试、部署文件显式路径提交，不夹带审计/任务单。交付父/新 SHA、红绿、网络真实装配证据、已用环境与仍缺的外部前提，然后停止等待审计。
