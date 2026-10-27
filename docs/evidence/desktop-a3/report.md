# Task A3 执行 worker 装配证据

日期：2026-10-27（Asia/Shanghai）

基线：`81589c0f24f3507b954e46b37ce85d181c619520`（A2 验收通过）。
本报告只覆盖 A3 的执行 worker、lifecycle 合并、只读公钥、固定 exec 健康
探测和协议夹具；不实现 gateway/A4，不证明真实 DSH 或完整多用户聊天。

## 1. 结果摘要

| 项目 | 结果 |
|---|---|
| execution 镜像 | `sha256:45022743e7d378c516e2b03ddf5a6c64c42e8029bb7d17c42416279ad3d25da8` |
| 容器入口 | `python -m easel.worker.server serve --host 0.0.0.0 --port 7860` |
| 容器 UID/GID | `10001:10001` |
| worker 宿主端口 | 无（`PortBindings` 为空） |
| 公钥 | 只读 `worker.pub` → `/keys/worker-public.key`，`EASEL_WORKER_PUBLIC_KEY_FILE` 指向该路径；私钥不进入 worker |
| 健康探测 | provisioner 对不可复用 container_id 执行固定 `docker exec ... easel.worker.server healthcheck`，10 秒超时 |
| 真实协议 | 1 passed（签名测试快照 + ACP 夹具；提交/幂等/事件/问题/停止/重启历史） |
| 持久路径 | `/mnt/c` 与 WSL ext4 临时根均由 UID 10001 通过创建/SQLite/WAL/flock/fsync/原子替换/重开 |
| gateway/A4 | 未实现、未运行；A3 不把它们写成通过 |

## 2. 持久路径预检

脚本：[`deploy/desktop/persistence_probe.py`](../../deploy/desktop/persistence_probe.py)。
在两个随机专用根上以容器 UID 10001 运行，不访问开发者 HOME 或既有工作区：

- Windows 挂载根：`/mnt/c/Code/Easel/.scratch/a3persist-...-c`
- WSL ext4 临时根：`/tmp/a3persist-...-e`

首次/新容器的实际输出（节选）：

```text
{"atomic_replace":true,"create":true,"fsync":true,"gid":10001,"lock_cross_process":true,"phase":"first","root":"/persist","sqlite_read":true,"symlink_escape_detected":true,"uid":10001}
{"atomic_replace":true,"gid":10001,"phase":"verify","result_present":true,"root":"/persist","sentinel":true,"sqlite_read":true,"symlink_escape_detected":true,"uid":10001}
```

`/mnt/c` 与 WSL ext4 两组合格项相同；`/mnt/c` 因此保留为 Desktop 持久根，
不需要退回 ext4 备用根。测试创建符号链接到 `/etc` 并验证解析越界，不写
`/etc`；临时根随后按名称清理。

## 3. 单一 worker 入口与公钥

`easel/worker/server.py` 复用 `web.app` 的常驻 `RunManager` 和既有
`/internal/runs` 路由，把 lifecycle health 挂到同一个 FastAPI app；没有
第二个 Run 状态机。新容器实际 inspect（脱敏）：

```json
{"cmd":["python","-m","easel.worker.server","serve","--host","0.0.0.0","--port","7860"],"entrypoint":null,"env_public_key":true,"image":"sha256:45022743e7d378c516e2b03ddf5a6c64c42e8029bb7d17c42416279ad3d25da8","mounts":[{"destination":"/keys/worker-public.key","rw":false,"source_basename":"worker.pub","type":"bind"},{"destination":"/workspace","rw":true,"source_basename":"workspace","type":"bind"},{"destination":"/home/easel","rw":true,"source_basename":"home","type":"bind"}],"networks":["easel-ws-..."],"port_bindings":{},"user":"10001:10001"}
```

固定 exec 健康输出（不含 token）：

```json
{"config_version":1,"container_id":"easel-worker-...","execution_capable":true,"ready":true,"workspace_id":"..."}
```

`Provisioner._probe` 只对 inspect 得到的不可复用容器 ID 执行固定 argv；
校验 workspace/config_version/container_id/ready/`execution_capable=true`，
不是任意退出 0。 `lifecycle`-only 入口的 healthcheck 因缺少
`execution_capable` 返回失败；旧两挂载/无公钥实例也会被 inspect 拒绝。

## 4. 真实容器协议验证

测试：[`tests/integration/multiuser/test_worker_execution_real.py`](../../tests/integration/multiuser/test_worker_execution_real.py)。
使用 execution 镜像、内部网络、无发布端口、签名 v2 测试快照、真实
`ACPAdapter` 和 `harness_rpc` 夹具子进程；客户端是单独临时容器，从允许的
内部路径访问，不是宿主直连或 fake handler。

实际结果：

```text
EASEL_RUN_CONTAINER_TESTS=1 python -m pytest tests/integration/multiuser/test_worker_execution_real.py -q
1 passed in 441.60s
```

覆盖：

- 提交/事件：正常 Run 进入 done，事件含 token/done；
- 幂等：相同签名/请求重复提交返回 `duplicate=true`；
- 历史：完成后读取 session messages，包含 user/assistant；
- 重启：删除 worker 容器后用同一持久 workspace 新建容器，历史仍可读；
- 问题回答：permission 夹具进入 `awaiting_permission`，人工回答后继续；
- 停止：hang 夹具由显式 stop 收敛为终态。

该结果是 ACP 协议夹具验收，不是真实 DeepSeek Harness/供应商验收。

## 5. 红绿

| 阶段 | 结果 |
|---|---|
| lifecycle payload 未支持 active_runs/execution_capable | TypeError（先红，后实现） |
| RunManager 未支持 active_runs/drain | AttributeError（先红，后实现） |
| 持久探针首次运行 | `lock_cross_process:false`（子进程 argparse 失败）；修正后 true |
| 当前静态/纯逻辑（A2+A3） | 6 passed, 5 skipped |
| 当前 affected unit suite | 299 passed, 1 skipped |
| A1 Desktop + 文档 + A2 静态 | 61 passed, 5 skipped |
| 真实 worker 协议 | 1 passed |
| 真实容器隔离（排除 Chromium） | 7 passed, 2 deselected |

容器隔离测试只运行非 Chromium 子集；Chromium 沙箱门槛仍按原结论未通过。
全部真实临时资源按 run 标签/名称清理，未使用全局 prune。

## 6. 清理与边界

- worker/客户端/网络使用随机名称和 `easel.a3.execution`/项目标签；清理后
  `docker ps -a` 与 `docker network ls` 无本次资源；WSL `/tmp/a3*` 为空。
- 未操作开发者容器、卷、网络、HOME、微信配置、浏览器 profile 或 ngrok。
- `easel.worker.server` 的公共 key 只读；master/私钥没有进入 worker。
- A3 不实现 gateway、control→gateway 断言、A4 broker 或真实 DSH。
- 不宣称完整多用户聊天可用；A1 登录/浏览器证据不因 A3 重跑而改变。

## 7. 未执行/剩余

- 真实 DeepSeek Harness/供应商请求：not-run；此处仅为 ACP 夹具。
- gateway、A4 control 签名/broker 接线：not implemented，A3 后停止。
- 最终 runtime/DSH 镜像、目标 Linux XFS/防火墙/Chromium 沙箱：仍为发布门槛。
- 宿主到 worker 容器 IP 的 HTTP 仍不可用；已改用固定 docker exec。
