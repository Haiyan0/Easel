# Task G2 gateway 容器、可信握手与租约代次装配证据

日期：2026-10-28（Asia/Shanghai）

基线：`d1fc8b16d6cb98092fa1ff9d0772c15bd40e35bd`
（G1 验收提交）。

本轮仅装配 provisioner/gateway 运行时、WorkerHandle/Lease/Store v11 与
RuntimeManager 两阶段激活；未接公开 WorkerRelay/聊天入口，未进入 A4。

## 设计选择

1. **create 前固定 control IPv4**：provisioner 只读 inspect
   `easel-control` 的 IPAM/Containers，选择最低空闲主机地址；`docker
   create --network easel-control --ip <fixed>`。start 后 inspect 必须看到
   恰好一个控制网 IPv4 且等于预分配值；禁止 create 后改 Env、0.0.0.0、
   首网卡猜测。
2. **trusted local control channel**：gateway 只监听固定 control IPv4 的
   8897，容器内 `/run/easel/gateway-control.sock` 为 0600。provisioner 用
   固定命令 `docker exec -i ... configure/activate/status`，配置和激活
   payload 经有界 stdin JSON；worker service token 不进入 argv/状态/日志。
3. **boot generation 来源**：`easel.gateway.server serve` 每次进程启动调用
   `new_boot_generation()`；不读取 Docker 标签。configure 只绑定实际 worker
   目标，activate 必须精确匹配 workspace/gateway_instance_id/boot/
   worker_container_id/config_version。
4. **两阶段 CAS**：v2 `ensure` 创建/复用 worker 和 gateway，返回未激活候选；
   RuntimeManager 在 `owner/generation` CAS 中先写候选（starting/staged），
   再调用 v2 activate，验证 available 后才写 ready。数据库事务内不 await
   Docker/网络。
5. **重启/隔离**：同一 gateway 容器重启会生成新 boot、清空进程内配置；
   provisioner status 从实际 inspect + configured/active/available 计算，
   关闭失败隔离报告 unavailable，不把 active 当 ready。gateway 短暂不可用
   不删除 worker、不中断 Run。
6. **停止/清理**：v2 stop 先按精确 gateway ID 停止 gateway；失败不继续删除
   worker。v2 abort 只清理本轮 `worker_created`/`gateway_created` 资源；
   已有活跃资源不因单次 probe 失败删除。

## 接口与迁移

- `WorkerHandle`/`WorkerLease` 新增 `gateway_instance_id`、
  `gateway_generation`、`gateway_upstream`、`gateway_state`；worker
  `container_id`/`upstream` 保留不变，service token 仍 repr 脱敏。
- Store schema 10→11 只追加四列：`gateway_instance_id`、
  `gateway_generation`、`gateway_upstream`、`gateway_state`（默认
  `disabled`）。旧记录保持 disabled，不视为已完成 gateway 装配。
- 新增 Store CAS：启动阶段候选、ready→starting refresh、activation 成功
  ready、activation 失败 ready/unavailable、精确停止清空。
- provisioner 协议 v2：`ensure`/`activate`/`stop`/`inspect`/`abort`；
  v1 仅保留给未启用 gateway 的旧部署；gateway 启用时 v1 失败关闭。
- 新建 `easel/gateway/server.py`、`deploy/multiuser/gateway_runtime.py`、
  `deploy/desktop/Dockerfile.gateway`；未复制 G1 验签/转发实现。

## 真实拓扑

固定测试镜像（最终一次真实运行）：

```text
GATEWAY_IMAGE sha256:a25c259e9c09a36d902ddb9e68f28de803f30fea1636d8b794b4919ffceb5707
WORKER_IMAGE  sha256:f599b224abdbf7ad71052febad55ebba0c87513dce4053d085f84fb3e17dc3cb
```

两个随机 workspace 通过真实 `RuntimeManager.ensure_ready` 创建：

```text
workspace cc555cbee07c44cdbd169e6461a73195
  worker  419a037079bc6c726b8107100e5c4c366ae768e422a2bddbb36c2e4461ac7736
  gateway 6f637fd608c8c63ea6f449bc89d1138871b469c9310a931b906add96cbbae886
  boot    01e19760b43838b55800f5890913bb54
  upstream http://172.31.205.2:8897
workspace 40202d7519194074b3116ff922280511
  worker  5552dfb5e743883753eed46e83eb8a219f3731e3791892659080c5ebe1a0aea4
  gateway 49a7559e6e85b0d506a1c01ff2771f0962556276e966ea0542f6d3ae8d9d46da
  boot    f2a5e6b48e5589b302e007df0a8b92c2
  upstream http://172.31.205.3:8897
```

实测结果：`tests/integration/multiuser/test_gateway_runtime_real.py`
**1 passed**（377.37 秒）。

覆盖：

- 两个 workspace 的签名 health、submit/events、问题状态、messages 历史；
- 问题回答与 stop；
- 跨 workspace A audience 访问 B gateway 返回 401/403；
- control 网络上的 gateway 彼此可达（共享网络事实，未伪称物理隔绝）；
- worker 网卡访问 gateway 管理端口、另一 workspace worker 均不可达；
- 同容器 gateway restart：旧断言重配前 503、重配后 403；新 boot 经
  RuntimeManager CAS/activate 同步；worker ID 不变；已开始 hang Run 仍为
  running，未重发/伪结算，随后可 stop；
- `RuntimeManager.drain` 按精确 worker+gateway 元组停止，两个容器均消失，
  Store 写 stopped 且清空 gateway 字段。

## 测试与清理

```text
G2 新增单元（server/runtime/runtime-gateway/store）：28 passed
provisioner/runtime/store/stop-recovery：238 passed, 1 skipped
tests/multiuser + Desktop 拓扑静态：1245 passed, 9 skipped, 2 warnings
G1 auth/forward/stream/TCP：120 passed, 2 warnings
真实双 workspace 集成：1 passed（377.37 秒）
```

2 条 warning 为既有 websockets/uvicorn 弃用信息。

真实测试结束后按随机名称/精确 ID 删除两个 worker、两个 gateway、两个
worker 网络和临时 control 网络；检查 `easel-gateway-*`、`easel-worker-*`、
`easel-sidecar-*` 容器与 `easel-g2*`/`easel-ws-*` 网络均为空。未执行全局
prune，未操作开发者已有容器/卷/网络/HOME/profile/ngrok。测试镜像保留为固定
ID，供复审复跑；不属于运行资源。

## 未运行/边界

- 公开 WorkerRelay/聊天/前端入口、A4 broker URL/受管 Agent 接线未实现；
- 真实 DeepSeek Harness/供应商、ngrok、Chromium、完整多用户聊天未运行；
- 未修改用户账号/HOME/profile，未关闭 Chromium sandbox；
- 本证据只证明 Desktop 测试拓扑与 gateway 生命周期，不替代 Linux 生产、
  XFS 配额、防火墙/发布门槛。
