# Task 8 首轮审计：需要返工 R1

- 基线：efdd4edc1dc7e3ae7f21aa12804258e329aaef8a。
- 实现提交：cfbb68eefd5fceb0ae121c51de3530a271b78f51；文档补充后 HEAD：513d08bebce138c262be9eb2f1bae03eaa3afefc。
- 结论：三项 P1、四项 P2，暂不进入 Task 9。
- 范围依据：[Task 8 交接单](../tasks/2026-10-21-task-08-worker-runtime.md)。仅审计，没有修改产品代码/提交；探针使用临时目录、假 Docker runner、真实 SQLite/FastAPI，不更改宿主权限或启动真实容器。

## T8-R1-01 [P1] 实际新建调用丢失 docker run 子命令

位置：`deploy/multiuser/provisioner.py` 第 687 行。

build_run_argv 返回以 run 开头的参数数组；_docker 自行添加 docker 可执行程序，但实际调用又使用 argv[1:]，丢掉 run。独立复审 fake runner 捕获真实 ensure 调用前缀为 [docker,-d,--name,...]，最终 worker_start_failed。任何首次创建均无法成功。

现有 Linux 集成直接调用 build_run_argv 后执行完整参数，没有走实际 Provisioner.ensure，因此不能覆盖这个装配错误。

修复/验收：修正参数传递，测试实际 ensure→_docker→runner 链，严格断言 docker/run 顺序及成功健康核验后的句柄；补实际受限服务入口新建测试。不能只更新 build_run_argv 的单元测试。

## T8-R1-02 [P1] root chown 跟随 worker 可替换路径，存在特权路径竞争

位置：`deploy/multiuser/provisioner.py` 第 243 行及第 655–658 行。

prepare_workspace 检查并返回 state 路径后，ensure_worker_ownership 在下一次 to_thread 中默认跟随链接执行 root os.chown。已经运行的 worker 拥有 workspace 目录的写权限，可以在两阶段之间替换 state；初始 resolve/is_symlink 检查不能保护后续特权操作。

主审和独立复审安全探针：prepare 后把空 state 移到同目录 saved-state，再用真实 Windows junction 将原 state 指向临时工作区根以外的 outside。模拟 geteuid=0，并把 chown 替换为只记录解析目标的函数；ownership 没有拒绝，记录包含 outside。未实际调用 chown，不将该探针表述为已经在 Linux 更改系统文件；Linux 代码中默认跟随 symlink 的特权调用及竞争窗口明确存在。

修复/验收：针对宿主特权目录操作使用可信目录句柄/不跟随链接的方式并校验类型与身份，避免“再 resolve 一次然后按路径 chown”仍有窗口；已运行 worker 可写的子路径不能被当作可信 root 操作目标。用受控 gate 在校验后替换 state，证明被拒绝或操作始终绑定已验证对象；Linux 实际 symlink 测试只对临时文件执行，外部 sentinel 的 owner/mode/内容均不变。检查同链上的 mkdir/chown/凭证写入/挂载派生是否有同类遗漏，不操作开发者文件。

## T8-R1-03 [P1] 接管旧容器只凭标签，安全配置与实际网络未强制验证

位置：`deploy/multiuser/provisioner.py` 的 sanitize_inspect、第 625–631 行、_ensure_locked 第 678–681 行。

sanitize_inspect 收集部分 security 字段却不以其决定是否可接管；_ensure_network 对已有网络只验标签，Internal=False 也通过。合法身份 health 不代表容器具有批准镜像、权限、挂载和网络限制。

独立复审用实际 ensure/probe 路径、假 Docker inspect 和合法健康响应，成功返回了来自 User=0:0、Privileged=True、可写 rootfs、未批准镜像且额外连默认 bridge 的旧容器句柄。没有实际创建不安全容器。

修复/验收：新建后、复用、inspect/reconcile 均使用同一严格的实际配置验收，核对固定镜像摘要、用户、read-only、capabilities/security、资源限额、准确挂载、无 socket/端口及仅批准网络；已有网络的 Internal/IPv6/driver 等关键隔离属性必须校验，不能只认名字/标签。逐项篡改 inspect fixture 皆拒绝；合法实际实例可复用。生产链不能用脱敏输出省略掉尚未完成的安全判断。

## T8-R1-04 [P2] 迟到 drain 无代次约束，覆盖新代次状态

位置：`easel/control/runtime.py` 的 RuntimeManager.drain，约第 131–150 行。

drain 的 mark_worker_draining 和最终 stop_worker 未传代次，也没有在 await stop 后复核所有权。增加 Store 的可选 generation 参数并没有保护这一真实调用者。

主审两独立 Store/manager、共享 fake provisioner 的确定性交错：旧 drain 的停止操作已发生但回复暂停；另一管理者完成 drain 并重新 ensure 得到 generation=2 ready；旧回复放行：

```text
before_old_reply={generation:2,state:ready}
after_old_reply ={generation:2,state:stopped}
new_container_still_exists=true
```

修复/验收：认领 drain 与停止/写回必须绑定取得的实例和租约代次，所有关键 CAS 失败要停止旧动作；迟到回复不能改写/停止新实例。复核 heartbeat 等路径不能通过“读取当前代次”让旧持有者续写新租约。补两个真实连接/manager 的上述交错测试，以及旧动作对新物理实例不执行 stop 的证据，不仅单测 Store 的 CAS。

## T8-R1-05 [P2] control 实际装配的 RuntimeManager 跨线程使用 SQLite

位置：`easel/control/app.py` 第 352–356 行；`easel/control/runtime.py` 的直接 Store 调用。

lifespan 在数据库专用线程打开 Store，却在 ASGI 事件循环内构造 RuntimeManager；其 async 方法直接调用该连接，没有 broker 所用的 db_runner 装配。当前没有业务端点调用只能掩盖问题，不能证明可供后续调用。

主审真实 create_app(store,auth,provisioner=fake)+TestClient lifespan，通过 ASGI portal 在应用事件循环调用 app.state.runtime.ensure_ready，立即得到 ProgrammingError：SQLite objects created in a thread can only be used in that same thread。

修复/验收：将短数据库步骤调度到拥有连接的专用线程，网络/容器等待保留在适当异步层；不可设置 check_same_thread=False 绕过约束，也不在数据库线程/事务里等待整个 Docker 操作。通过实际 app lifespan 测试 ensure_ready/drain/heartbeat/collect_idle，与认证请求并发仍正常；不能只测与 Store 同线程创建的孤立 RuntimeManager。

## T8-R1-06 [P2] 最终 runtime 镜像缺少 Web/技能代码及运行依赖

位置：`deploy/multiuser/Dockerfile.worker` 第 40–47 行及 runtime 阶段。

镜像仅 COPY easel、项目元数据和 managed 配置，没有 COPY web、skills/shared 或内置技能；安装也仅覆盖生命周期服务的 FastAPI/uvicorn/httpx/pydantic 与后续 Playwright，没有装配原运行入口所需的 cryptography、SSE、multipart 等依赖。即使管理员提供 DSH_COMMIT/profile，当前最终镜像仍不能运行 Task 6/7 的已实现 worker Web/ACP/技能入口。

这与“未提供已验收 Harness commit，所以真实最终镜像暂未构建”的外部条件不同，是 Dockerfile 已有的内容遗漏；不要求伪造 commit 或供应商凭证。

修复/验收：明确 base/chromium/runtime 各阶段职责，最终阶段包含实际 worker 执行代码、技能/规则与必要受管依赖，在构建期安装并固定版本。增加镜像内文件/模块/脚本及最小假 Harness 启动冒烟，不把 base 的 health 成功当最终 runtime 可用。扩充构建上下文排除和检查，避免补 COPY skills 后把旧公众号 YAML、token/cache、Cookie 等开发者秘密带入镜像。缺管理员已验收 commit/profile 的真实 DSH 门槛仍如实记录。

## T8-R1-07 [P2] worker 技能仍明确禁用 Chromium 沙箱，本任务要求尚未实现

位置示例：`skills/shared/scripts/account_stats.py` 第 95 行、douyin_publish 第 113 行、web_publisher 第 278 行、weixin_mp_stats 第 38 行、xhs_publish 第 100 行；另有报告列明的其他启动者。

交接单第 6 步要求按模式区分，worker 不得用 --no-sandbox 换取可运行。当前只把遗漏写入部署手册，没有修改参数或阻止这些 worker 调用；这不是缺真实平台账号造成的未验证，而是已知参数与任务要求相反。

修复/验收：扫描所有实际 Chromium 启动路径，包括共享脚本、临时渲染和内置技能，worker 模式不得禁用沙箱；local 兼容按模式处理。补实际启动参数捕获和缺少沙箱能力时明确失败的测试，真实容器使用保持沙箱的启动方式验证；不能删除一处字符串后仍由其他 helper/默认参数注入。若受内核限制无法通过，记录真实门槛未通过，不降低容器权限。

## 独立验证与未执行边界

使用 C:\Users\hyh\anaconda3\envs\Agent\python.exe：

- tests/multiuser：642 passed、3 skipped，35.92 秒。
- 任务单指定八文件回归：229 passed、20 skipped，15.52 秒。
- tests/integration/multiuser/test_container_isolation.py：5 skipped，未设置启用变量；本轮未运行真实容器。
- git diff efdd4ed HEAD --check：退出 0。
- 主审实际复现特权路径检查/使用脱节（模拟 chown）、双连接 drain 竞争及真实 lifespan 跨线程错误；独立复审复现 ensure argv 和不安全旧实例接管。

执行 Agent 的 Docker Desktop 5 passed 作为交付证据保留，不标为本轮独立实测，也不外推到最终 runtime、真实 XFS 配额或宿主健康探测链。未提供真实 DSH commit/profile、Linux XFS 环境不构成要求伪造结果的理由。

当前零出站是安全的失败关闭状态，但 worker→broker/受限出口允许路径、宿主健康可达与防火墙仍未装配/验收；部署文档应继续明确，不能说完整网络矩阵已通过。Task 8 要求的允许/拒绝路径与资源能力门槛仍须在后续本任务验收中完成或明确保留为未通过，不能用 mock 或随意移动到 Task 10 消去要求。quota-mode none/allow-unquotaed 不能作为生产启用的证明。

## 可复制给执行 Agent 的 R1 指令

基于 513d08bebce138c262be9eb2f1bae03eaa3afefc，按本报告修复 T8-R1-01 至 07。先补真实调用链/装配红测试，再最小修复；重点是实际 Provisioner.ensure、接管验证、特权文件操作、RuntimeManager 的真实 ASGI 线程与双管理者交错，不仅比较 argv/SQL helper。

不进入 Task 9，不重做已通过的认证/快照/broker，不改写历史迁移，不操作开发者真实工作区或容器。没有管理员提供的 Harness commit/profile 和真实 XFS 环境时，不伪造、不降低安全参数；继续完成不依赖它们的实现和测试，并精确报告剩余门槛。

运行 tests/multiuser、任务单指定八文件回归及可用集成测试，检查父提交到新提交 git diff --check。显式路径提交实现/测试/必要部署文档，不夹带审计/任务单。交付父/新 SHA、文件清单、逐项红绿、真实入口装配及实际/未执行门槛，然后停止等待复审。
