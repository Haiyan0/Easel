# Task A2：拓扑方向批准，契约与测试定点收口 R1

基线：`f1c4e5ef98ea756513071367919fe57fac7dbf17`。
范围：`52ee500`、`9291af3`，最终提交以 `git rev-parse HEAD` 完整值为准。

## 裁决

认可 A2 的探索交付和“先设计、不越权实施”流程。原则批准 **WSL 宿主 provisioner + 现有 Unix socket 授权 + 固定 docker exec 健康探测 + 每 workspace 管理 gateway** 方向。

本次是设计方向批准，不是运行时代码开工许可。以下 R1 只修契约与测试；收口复审后再下发 gateway/worker 实施任务，不直接进入 A3/A4。不要求购买服务器、额外 ngrok 域名或重新开发 A1。

批准条件：control 不进各 worker 网络、不挂 Docker socket；gateway 不具 Docker 管理权限、无 workspace/数据库挂载和宿主发布端口；management 仅绑定实际控制网络地址，不能退回 0.0.0.0。WSL 发行版/GID 按诊断配置，不将本机用户名身份硬编码为所有机器的要求。固定 exec 必须针对核验过的不可复用容器 ID、固定 argv、超时，验证身份/配置/就绪；不能把任意退出 0 当执行 worker 能力已齐备。

## A2-R1-01：修正可实施的 gateway 契约

### 路由表

当前 topology.json 中 `/question`、`/question/answer` 不对应真实 worker，且缺少历史读取。改成明确 method/path/query 结构，不再仅写“每个路径固定方法”。以现有处理器为准：

| Method | Path | Query |
|---|---|---|
| GET | /internal/lifecycle/health | 无 |
| POST | /internal/runs/{run_id} | 无 |
| GET | /internal/runs/{run_id}/events | 仅现有事件处理器支持的参数，逐项列出类型与重复参数规则 |
| POST | /internal/runs/{run_id}/stop | 无 |
| POST | /internal/runs/{run_id}/questions/{question_id}/answer | 无 |
| POST | /internal/runs/{run_id}/questions/status | 无 |
| GET | /internal/sessions/{session_id}/messages | 仅现有分页参数，逐项列出类型与重复参数规则 |

依据：web/app.py 的实际路由与 easel/control/proxy.py 的调用，不凭空新增接口。契约测试比较精确 method/path 集，未知方法/路径默认拒绝，不按前缀放行。定义 URL 编码、尾斜杠、路径 ID、查询白名单、重定向拒绝和上下游头净化要求。

### 身份、实例与恢复

- 明确 control→gateway 与 gateway→worker 是两套独立凭证；浏览器 Cookie/Authorization/外来 X-Easel-* 不传给下游，由可信侧重建身份头。
- 定义 WorkerHandle 保留 worker container_id 的不可复用实例语义，gateway upstream/身份不能替代该 ID；固定目标须绑定该 worker 实例，不能仅按 workspace 容器名解析后自动落到新实例。
- 明确配置 revision、worker 实例与 gateway 实例代次的绑定；旧 stop 不得删除新 gateway，旧 token 不得访问新实例。
- 当前“凭证不落盘”与 provisioner/control/gateway 独立重启后的合法复用尚未闭环。提交明确恢复协议：如何重新获得同一有效凭证、或如何安全撤销并重建 gateway；不能复用旧 gateway 却向 control 签发另一把不可用 token。
- 无法恢复时不得直接删活跃 worker。可以让管理面暂不可用并显式报错，或按经审查的 gateway 更换协议恢复；Run、SSE、stop 和实例代次必须有确定行为。若需要受保护管理状态持久化，应明确存放位置、权限和相对原“不落盘”提案的修订，先交设计，不自行落运行时代码。
- gateway 加入共享 control 网络后，不能声称网络上无法访问所有其他 gateway；准确描述为通过独立凭证、固定目标和协议拒绝跨 workspace 操作。实施验收要有 A 凭证访问 B 的反例。
- 写明有限请求头/请求体/并发与超时、SSE 背压和取消清理；禁止缓冲整个长流、不自动重发 POST。数值应参考现有 relay 的真实上限，若新增取值给出明确值及理由。
- management bind 解析需由可信实际网络配置确定，网络地址缺失/歧义/变更失败关闭，不依赖“第一张网卡永远正确”。

本轮只把上述决策固定到 README/topology.json 和契约测试，不实现 gateway。

## A2-R1-02：临时资源超时与清理不能假成功

位置：test_desktop_topology.py `_cleanup`、两个 bind 测试的临时 docker run、socket 测试 docker run。

主审纯逻辑反例（未操作 Docker）：

```text
DELETE_FAILURE_SWALLOWED=True
DELETE_CALLS 3
TIMEOUT_ABORTED_REMAINING_CLEANUP=True
LATER_CONTAINER_INSPECTED False
```

当前清理对非零删除不报错，删除超时则抛出并中断后续项；临时客户端没有固定名称，只靠 --rm 无法在 Docker CLI 超时后精确清理。

要求：所有临时客户端显式随机名称 + run-id 标签，启动前登记资源；CLI 超时后仍按名称检查归属并清理。每项独立捕获错误、继续后续资源、汇总报告；精确不存在才幂等成功，结构异常/daemon 失败不能当 absent。测试已成功而清理失败必须失败；已有测试异常保持原异常，同时报告清理错误。可复用既有 tests/multiuser/topology_support.py 的能力，避免重新写一套弱清理。

新增纯逻辑反例：单项删除非零、超时、inspect 异常、归属不符、删除后已不存在、CLI 超时留下命名客户端；均验证后续项仍处理且最终不会假绿。不操作其他项目资源。

## A2-R1-03：网络阻断证据排除基础设施失败

- `_wsl_http` 的阻断结果只接受约定的 returncode=3 且输出 BLOCKED；WSL 发行版/解释器启动失败、命令失败、探针脚本失败应报基础设施错误，不能因非零而通过。
- 独立 bridge 隔离测试先验证两个容器内各自回环 HTTP 200，再测试跨网络；连接超时前确认目标服务确实健康，避免“服务没启动”等于“隔离成功”。
- socket 实测与真实 provisioner unauthorized 的区别如实记录；未进入自动测试的手工证据不声称由 pytest 全部覆盖。
- 持久路径目前仅证明 marker 可读。A3 使用前补 UID 10001 的真实创建/SQLite/锁/fsync/原子替换与重开探针；若 /mnt/c 不满足权限语义，则选择耐久 WSL ext4 路径并记录 Windows 备份方式，不把 /tmp 当持久根。本项此轮记录进契约即可。

## 本轮验证边界

主审实际运行：拓扑静态 + provisioner + lifecycle + 文档路径，共 **118 passed, 6 skipped**（1.23 秒）；5 项显式 Docker 拓扑测试未启用，另 1 项既有环境 skip。`git diff f1c4e5e HEAD --check` 退出 0。

独立复核确认基础设施失败假绿与凭证恢复缺口。未启动 ngrok、未改现有容器/防火墙/全局配置。本轮不重复跑已发现清理缺陷的 live 全套，以免 CLI 超时遗留匿名资源。

## 交付

仅修以上契约与测试。先以纯逻辑故障注入证明红绿，清理与探针校验通过后再跑真实 A2 临时资源测试，逐项给出 pass/fail/not-run 和无残留证据；同步精确路由表及凭证/代次/恢复协议。提交后停下等复审，不进入 A3/A4，不实现 gateway。
