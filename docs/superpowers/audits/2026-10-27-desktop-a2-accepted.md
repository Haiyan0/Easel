# Task A2 验收与拓扑设计批准

验收提交：`81589c0f24f3507b954e46b37ce85d181c619520`。
返工父提交：`9291af3f1a23597976833997db1f7d3c1ddfce6c`。

## 裁决

A2 探索、契约与测试验收通过，A2-R1-01/02/03 关闭。批准 WSL 宿主 provisioner + 受限 Unix socket + 固定 docker exec 健康检查 + 每 workspace gateway 的设计方向。以下附加约束是主审对契约的明确修订，实施时优先于 topology.json/README 中对应的旧表述，不再退回整轮 A2。

批准设计不等于 gateway 已实现、完整 control→worker 路径已通过或多用户聊天已可用。本轮自动测试验证基础拓扑反例/绑定/清理，不替代未来 gateway 的真实认证、转发和恢复验收。

## 主审实际验证

1. 拓扑静态/纯逻辑 + provisioner + lifecycle + topology_support + 文档：**132 passed, 6 skipped**（1.25 秒）。其中 5 项真实拓扑默认开关未启用，另 1 项既有环境 skip。
2. 显式 `EASEL_RUN_CONTAINER_TESTS=1` 运行 `tests/integration/multiuser/test_desktop_topology.py`：**11 passed**（26.74 秒，无 skipped）。
3. 实测后 `docker ps -a --filter name=easel-a2-` 与 `docker network ls --filter label=easel.test.run-id` 输出为空。
4. `git diff 9291af3 81589c0 --check`：退出 0。
5. 独立复审：Ed25519 分域断言与现有 control 签名基础兼容；无额外阻塞，确认下列两条附加约束。

本轮未构建最终镜像、启动 ngrok、运行 gateway 或真实 Harness，不以实施方历史报告冒充主审新执行。

## gateway 实施时的两条附加硬约束

### 1. 可执行的地址绑定流程

现有“docker create → inspect IP → 通过 env 在 start 前注入”不能直接照做：已创建容器的 Config.Env 不能通过 docker start 修改。

实施可选择 create 前可信分配/固定 IPv4（Docker 实际分配校验与冲突失败关闭），或受信启动握手：仅接控制网络启动、确定唯一控制网络 IPv4 并绑定，经 provisioner inspect 对照验证后才接 workspace 网络及签发可用句柄。不得用 0.0.0.0、随意选择第一张网卡或把尚未验证的 endpoint 标为 ready。具体固定选择须在对应实施提交同步契约和真实测试，不需另等用户批准。

### 2. gateway 进程重启也必须更换认证代次

同一 Docker 容器重启会丢失内存 nonce 集合。若 ID/generation 不变，30 秒内的旧断言可能再次通过，故覆盖契约中“同容器同代次直接继续”的表述。

gateway 每次进程启动建立新 boot generation，经受信启动/恢复握手，与 control 的 WorkerLease/WorkerHandle 原子同步；同步前拒绝管理请求。不得只沿用旧 Docker 标签 generation。旧断言对同容器重启、新容器或旧 worker 实例均失效；control 对 POST 不自动重发，GET/事件可按游标恢复。测试须包含“有效断言使用过→同容器重启→旧断言拒绝”。不要求 gateway 为 nonce 持久化增加可写用户卷。

## 下一步实施顺序

先执行 A3：持久路径预检、真实执行 worker 与 lifecycle 合并、受管公钥入口、固定 exec 健康检查。A3 使用签名测试快照/真实 ACP 夹具证明 worker 协议，明确不是最终 DSH。

gateway 运行时代码在 A3 验收后单独下发实施子任务，再接 A4 control 签名/broker 装配。当前只授权代码 Agent 实施 A3，不将 A2 的设计批准理解为可同时做 A3/A4/gateway 全套。

## 保留边界

- UID 10001 持久目录创建、SQLite、进程锁、fsync/replace 和重开必须在 A3 真实探测，不以只读 marker 文件替代。
- WSL 普通 UID 的 Docker 操作权限不代表拥有任意 fchown/项目配额权限。专用目录初始化需明确运行身份与权限；只作用于本项目已核验根，不放宽全局权限或改变开发者路径。
- Desktop 无 XFS 配额时仅显式开发配置允许，原生产强制配额/发布检查不降级。
- 共享控制网络的 gateway 之间网络可达，不承诺物理隔绝；跨 workspace 管理操作由独立断言 audience、固定实例、白名单拒绝并实际验收。
- A1、ngrok 域名限制及 Linux 生产门槛维持原结论。
