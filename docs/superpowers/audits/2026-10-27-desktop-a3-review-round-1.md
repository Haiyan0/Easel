# Task A3 主审 R1

基线：`81589c0f24f3507b954e46b37ce85d181c619520`。
审查提交：`b88d236`（交付时须保留完整 SHA）。

结论：A3 本轮不通过；保留执行入口、公钥装配与协议验证的既有成果，定点修复以下三项后复审。不进入 gateway/A4。

## A3-R1-01（P1）：正常运行实例探针失败会被强删

位置：deploy/multiuser/provisioner.py `_ensure_locked` 1692–1699，调用 `_remove_container`。

接线与静态配置符合的已存在 worker，只要 report ready 为假或固定 exec probe 失败，代码就执行按名称 docker rm -f。容器没有被确认 exited，也没有取得 drain/stop 的停止授权。正常 draining 本来会 ready=false，暂时 exec 超时/失败同样可触发强杀，直接违反 A3 执行单第 6 条。

主审使用现有 FakeDocker、临时根及真实 Provisioner.ensure；第一次 exec 返回非零时：

```text
STATE_AT_PROBE_FAILURE True
LIVE_INSTANCE_REMOVED ['easel-worker-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa']
REPLACEMENT_RUN True
```

要求：对已存在实例，单次健康失败只返回明确未就绪/503，保留 worker、凭据、sidecar 与持久目录；禁止自动 rm -f/stop 或替换。仅 Docker 明确 exited/dead 且满足既定重建条件时允许清理，并使用已核验不可复用实例 ID，避免按名称误删新代次。新建失败的本轮自有实例清理与既有运行实例必须区分。

红绿：分别覆盖 exec 非零、超时、ready=false（包含有活跃 Run 的 drain）、paused/restarting/缺状态；断言原 ID 不变、无 stop/rm/run、sidecar 未改。明确退出实例仍可按规则重建；真实 Store/RuntimeManager 的交错停止恢复回归保持。

## A3-R1-02（P1）：SIGTERM 未在 Uvicorn 停机前触发 drain

位置：easel/worker/server.py `_install_drain_signals` 与 main 中 uvicorn.run 的组合；web/app.py lifespan 的 RunManager.aclose。

在 uvicorn.run 前用 signal.signal 注册的 handler 会被 Uvicorn 的信号捕获覆盖。当前环境直到 Uvicorn 关闭完 lifespan、恢复原 handler 后才调用 begin_drain；lifespan 已先执行 RunManager.aclose 并取消在途 supervisor。

主审真实 Linux 容器子进程验证：挂载当前 easel 代码，使用已有 execution 测试依赖镜像、真实 Uvicorn、最小 FastAPI lifespan/RunManager 观测器，向进程发真实 SIGTERM：

```text
Shutting down
Waiting for application shutdown.
SHUTDOWN draining=False
Application shutdown complete.
Finished server process
BEGIN_DRAIN
```

该探针隔离验证信号顺序，不冒充完整 ACP Run 验收；随机专用容器已核对标签删除。

要求：与实际 Uvicorn server 生命周期集成信号和 drain，不依赖会被覆盖的外层 handler。首次 SIGTERM 先停止接纳新 Run，真实在途 Run 在明确的有限宽限期内继续执行；超时按 interrupted/取消收尾，最终等待任务与 ACP 子进程清理，不能无限阻塞 docker stop。重复/强制停止行为明确。关闭不能把中断任务伪标 done。

测试必须是实际 Linux worker 进程/容器 + 真正 SIGTERM：短 Run 在期限内完成且历史保留，新 Run 被拒；挂起 Run 宽限期耗尽后进程有限时间退出且持久状态可恢复。断开 SSE 不影响 active_runs/drain 判断。不能仅调用 handler 或 begin_drain 方法证明信号接通。

## A3-R1-03（P2）：exec 自检未证明待返回服务凭证有效

位置：deploy/multiuser/provisioner.py `_ensure_locked` 返回 WorkerHandle、`_inspect_locked` 及 `_probe` 的 token 检查移除。

exec 子命令从容器实际 Env 取得 token，可以认证自己；ensure 返回的是宿主 .credentials 加载/新生成的另一份 token。已有容器继续运行而宿主凭据缺失或恢复不一致时，自检成功仍返回 ready + 错误凭证。后续 control 的请求全部 unauthorized。旧宿主 HTTP probe 曾使用待返回凭证，新路径丢掉了这项核验。

主审沿用合法现有容器 fixture（token A），宿主无对应凭据，真实 ensure 生成 B 后：

```text
RETURNED_TOKEN_MATCHES_ACTUAL False
HANDLE_AUTH unauthorized
TOKEN_CASE_REMOVED []
```

HANDLE_AUTH 通过真实 verify_worker_request 校验返回句柄得到；没有访问真实账号或容器。

要求：接管/inspect ready 时将待返回/控制面使用的预期 token 与已核验实例的实际 token 明确比较，或采用等价的认证验证；失配失败关闭，不返回 ready，不自动轮换活跃实例或删除 worker，不记录 token。已有实例缺宿主凭据不得静默新建 token 后自称可接管；新建实例仍可以正常生成凭据。安全恢复方案保持现有管理权限边界。

红绿：缺凭据、错误/恢复旧凭据、大小写/重复 env 歧义拒绝；合法相同凭据复用成功。断言错误路径无容器重建、无秘密回显；返回合法 handle 必须能通过真实 worker guard。重启对账不缓存假 ready。

## 主审验证与证据边界

- server/lifecycle/runs/provisioner/stop_recovery 定向回归：**138 passed, 1 skipped**（6.11 秒）。
- 两项 Provisioner 反例由主审临时纯逻辑探针复跑，与独立审查结论一致。
- SIGTERM 顺序由主审在真实 Linux 容器/子进程复现，未发布端口，未操作开发者资源；测试容器删除成功。
- 实施方 A3 镜像摘要当前本机不可 inspect；主审未重新大镜像构建或复跑 441 秒协议测试，不以报告替代新验收。原协议成果保留，不能覆盖新增故障场景。

## 交付要求

仅修以上三项；先补确定性失败测试再实现，显式路径中文提交。修改启动/停机后需用最终固定 execution 镜像复跑真实 SIGTERM、正常提交/事件/停止/重开历史；不涉及真实 DSH/供应商。保留文件与密钥边界，保护主审文档。

交付完整父/新 SHA、逐项红绿、镜像与运行入口、信号时序/宽限期结果、实例保留与 token 一致性证据、清理结果及 pass/fail/not-run。提交后停止等待复审，不进入 gateway/A4。
