# Task A3 R1 定点修复证据

日期：2026-10-27（Asia/Shanghai）

A3 基线：`81589c0f24f3507b954e46b37ce85d181c619520`；审查提交：
`b88d236`。本轮只修复主审 A3 R1 的 A3-R1-01/02/03；不实现
gateway/A4，不宣称真实 DSH 或完整多用户聊天。

## 1. 结果摘要

| 项目 | 结果 |
|---|---|
| 最终 execution 镜像 | `sha256:970e730af21c1f08647567854dceec61ca542c8d5b0ff30f8417a2690210d62b` |
| 入口 | `python -m easel.worker.server serve --host 0.0.0.0 --port 7860` |
| R1-01 | 活跃/非明确退出实例探针失败只返回 not-ready，不 stop/rm/run；退出实例按不可复用 ID 重建 |
| R1-02 | 真实 Uvicorn `handle_exit` 先 drain；默认 15 秒宽限；短 Run 完成、挂起 Run 有限取消 |
| R1-03 | 返回句柄 token 与容器实际 Env 常量时间比较；缺失/错误 token 不新建、不删除、不 ready |
| gateway/A4 | 未实现、未运行 |

## 2. A3-R1-01 实例保留

`Provisioner._ensure_locked` 对已存在 worker 先做静态/接线核查：

- worker 仍为 `Running`、`Status=running` 且探针成功时才复用；
- exec 非零、exec 超时、`ready=false`（包括 drain 中 `active_runs=1`）、
  `paused`、`restarting`、缺失 State 均返回 `worker_not_ready`，原实例 ID、
  凭据文件、sidecar 与持久目录保持不变，不发送 stop/rm/run；
- 只有 Docker 明确 `Running=false` 且 `Status` 为 `exited/dead` 的实例，
  才按已核验的不可复用实例 ID 清理重建；
- 新建实例通过 `--cidfile` 取得本轮 Docker 容器 ID；启动失败/健康超时只
  清理该 ID，不按同名容器删除。启动前既有实例与本轮新建实例的清理路径
  已分开。

反例覆盖：exec 非零、exec 超时、ready=false + active Run、paused/
restarting/缺 State、接线不符、sidecar 不存在；均验证无 stop/rm/run，
sidecar 不被创建或改动。明确 exited 实例反例验证 `removed == [旧实例 ID]`，
不是容器名。

## 3. A3-R1-02 真实 SIGTERM

`easel/worker/server.py` 使用 `uvicorn.Server` 子类，把 drain 接入 Uvicorn
自己的 `handle_exit`，不再依赖会被覆盖的外层 `signal.signal`。`shutdown`
在 Uvicorn 关闭监听 socket 之前先等待 RunManager 的有限宽限期；因此
SIGTERM 后 health 仍可返回 `ready=false`，新 Run 得到 503
`worker_draining`，在途 Run 可继续完成。

- 默认宽限：`EASEL_WORKER_DRAIN_TIMEOUT=15` 秒（可在受限范围内配置）。
- 第二个 SIGTERM：跳过剩余宽限，强制取消。
- 宽限耗尽：取消在途 supervisor，关闭 workspace ACP/CLI 会话，清理动作
  有界；最终状态为 cancelled/interrupted，不伪标 done。
- 正常关闭先 RunManager.aclose，再关闭全局 RPC 池；lifespan 再次调用
  aclose 为幂等收尾。

真实 Linux 容器实测（随机 workspace、随机网络、无宿主端口）：

```text
SIGTERM_SHORT {"active_runs_at_drain": 1, "drain_ms": 296, "exit_ms": 8125}
SIGTERM_HANG {"drain_ms": 328, "exit_ms": 15563}
```

短 Run 在宽限内完成，重启后事件为 `done`；挂起 Run 在宽限期后退出，
重启后事件为非 done 的终态；SIGTERM 后新 Run 返回 503。测试使用真实
容器进程和真实 SIGTERM，不是直接调用 handler 或 `begin_drain()`。

## 4. A3-R1-03 凭证一致性

- 既有实例接管只读宿主 `.credentials/<workspace>.token`，不调用
  `_load_or_create_token`；缺凭据返回 `worker_credentials_missing`，错误/
  大小写不符返回 `worker_credentials_mismatch`。
- 返回句柄前用 `hmac.compare_digest` 比较宿主 token 与 inspect 实际
  `Config.Env` 的 `EASEL_SERVICE_TOKEN`；重复 Env 键、非法值或缺失均失败。
- `inspect` 只读；错误/缺失 token 只把 `ready` 置 false，不自动轮换、
  删除或重建实例；恢复正确 token 后真实 inspect 再返回 ready。
- 新建实例仍由 provisioner 生成 token，并在健康循环中核对新容器实际 Env；
  合法 handle 通过真实 `verify_worker_request`。
- token 不进入错误文本、日志、inspect 白名单响应或报告。

真实容器反例：错误 token 时旧实现返回 `ready=true`（本轮先红）；修复后
`inspect` 为 `ready=false`，容器 ID/Running 不变；删除宿主 token 后
`inspect` 不创建 token，`ensure` 返回 `worker_credentials_missing`，原容器
仍在运行。测试未打印 token 值。

## 5. 红绿与回归

| 检查 | 结果 |
|---|---|
| A3-R1-01 反例（当前实现） | 新增测试失败：实例被按名 `rm -f` 后启动替代实例 |
| A3-R1-02 RunManager 宽限测试 | 先 `TypeError: unexpected keyword argument 'shutdown_grace'` |
| A3-R1-02 WorkerServer 信号测试 | 先 `ImportError: cannot import name 'WorkerServer'` |
| A3-R1-03 真实错误 token | 先 `inspect ready=True`；修复后 false 且实例保留 |
| A3-R1-03 真实缺失 token | 先自动生成新 token；修复后 missing、无新建、实例保留 |
| affected worker/provisioner 定向 | **184 passed, 1 skipped** |
| 多用户全量单测 + Desktop 静态 | **1084 passed, 9 skipped** |
| 真实 SIGTERM + 协议回归 | **2 passed**（短 Run 8.125s，挂起 Run 15.563s） |
| 真实容器隔离（排除 Chromium） | **7 passed, 2 deselected** |
| ruff（worker/server/runs/lifecycle） | All checks passed |

## 6. 清理与边界

- 真实测试结束后无 `easel-worker-*`、`easel-a3-*` 容器/客户端、无
  `easel-ws-*` 测试网络；测试标签查询为空。
- 镜像 tag 在证据采集后删除，摘要保留在报告/checks.json。
- 未执行全局 prune；未操作开发者容器、卷、网络、HOME、微信配置、浏览器
  profile 或 ngrok。
- gateway、control→gateway、A4、真实 DeepSeek Harness/供应商、Chromium
  沙箱、完整多用户聊天均未运行/未实现。
