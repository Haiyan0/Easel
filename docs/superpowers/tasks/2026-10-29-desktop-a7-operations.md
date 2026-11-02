# Task A7：日常运维、完整备份与可验证升级恢复

> **For agentic workers:** 使用 superpowers:executing-plans；逐项先红后绿，沿用现有部署与备份机制，真实演练后提交，停止等待主审。不要把未验证的门槛改成通过。

**Goal:** 让初级 Python 开发者按两份手册完成 Desktop 实例初始化/启停/诊断、完整备份、固定版本升级、失败回滚和新目录恢复，并能辨认实际已通过与未执行项。

**Architecture:** 复用正式 Desktop Start/Stop/Status、跨进程生命周期 Mutex、control 持久屏障/完成记录和既有备份 manifest 校验。备份是 control DB、密钥/配置及所有 workspace 私有状态的一致集合；恢复到新目标，以可信配置重建容器，不接管旧进程/旧实例，不覆盖原数据。

**Tech Stack:** PowerShell、Docker Desktop/WSL、现有 Python admin backup/restore、SQLite、固定镜像摘要。Linux 手册采用对应 Bash/权限语义，未验证的生产前提明确分层。

**Spec:** Desktop 装配计划 Task A7、原多用户 spec。基线 `d9c2f5ba960e892f198ebc4a111b7e2ad7e835af`；A6 功能验收见 `../audits/2026-10-29-desktop-a6-functional-accepted.md`。

## 文件与职责

- 修改 `deploy/desktop` 必要共用/初始化/启停/诊断逻辑；可新增职责清晰的 Backup/Restore/Update 入口，避免一脚本另造多用户运行时。
- 读取并复用 `easel/control/cli.py` 的 create_backup/restore_backup/完整性校验。既有格式只覆盖 master.key 等早期材料，不能默认已包含新增签名 key 和全部 Desktop 卷；必要时兼容扩展版本，不改旧格式含义。
- **本轮明确允许更新** `docs/windows-desktop-operations.zh-CN.md` 与 `docs/linux-docker-operations.zh-CN.md` 两份既有手册及 deploy README；这是用户要求的最终运维交付。先阅读本地现有内容，保护其它主审 README/plan/spec/审计/任务单，不把它们一起暂存。
- 新增 ops 契约/反例单元测试与 `tests/integration/multiuser/test_desktop_operations_real.py`（或职责等价文件），证据放 `docs/evidence/desktop-a7/`。测试只用本轮临时部署，不访问开发者真实账户/持久区。

## Global Constraints / Review Focus

- 主机 Python 固定 `C:/Users/hyh/anaconda3/envs/Agent/python.exe`；Windows PowerShell 与 Linux Bash 命令分别给出，不混用续行符。
- 不读开发者 .env/HOME/profile 找真实凭证，不动现有 ngrok 隧道或全局 Docker context/防火墙。服务 Key 不入 argv/日志/截图/证据或 Git。
- 生命周期保护覆盖新增变更型入口，与 Start/Stop 共用同项目互斥；避免互相嵌套持锁死锁。Force 不能绕过归属/实例代次保护。
- 既有 DB/公钥/历史存在时，缺 master 或签名私钥必须失败，不能初始化新 key“修复”。错误 Origin/镜像/卷/WSL/socket/端口给稳定且可操作的失败信息。
- 卷/路径必须真实归属核验；拒绝非空恢复目标、路径逃逸、未批准 junction/symlink，不向未知卷 chown/覆盖。root helper 只挂经验证的专用卷与本轮目标，禁止 Docker socket 下放、chmod 777/666 或全局 prune。
- 备份一致性不能依赖一开始 list 为空：先跨进程停接纳，drain 并确认 Run/请求/实例收口，再在写入者停止后复制整套状态。失败/取消不伪报完成，不无条件清屏障。
- 旧版本不能直接打开已升级 schema：回滚恢复匹配的整套备份，禁止降低 user_version/删迁移或仅换旧镜像继续写新库。
- 源部署与恢复副本共享 workspace ID；同一 daemon 上容器名按 workspace 派生。演练时源保持停机，不能让两个副本同时上线争用同名 worker。不得删除源实例来掩盖冲突。

## A7.1：清点与可靠日常入口

- [ ] 报告 HEAD/工作树，列出实际持久材料、卷/目录/权限、容器来源及完整启动命令。核对当前镜像实际代码/前端与来源，不凭 tag 当固定版本。
- [ ] 红测现有 DB 丢 master key、丢签名私钥、有公钥却无私钥、错误密钥、镜像漂移/外来卷、端口占用、Origin 改变、WSL/Unix socket 不可用；拒绝时不覆盖已有材料、不启动错误替代实例。
- [ ] 初始化只创建全新且确属本项目的材料；重复初始化与正常两轮启停不换 key。补实际 Status/日志/版本摘要命令，日志不含秘密。
- [ ] 更新 key/profile/镜像时明确新版本与活动 worker 的关系：先 drain/停止，重新核验 public 副本/Engine/config；配置摘要变化导致旧 stop 完成记录不匹配时，提供受控升级协议，不教用户无条件 resume 绕过校验。

## A7.2：完整备份与失败关闭

- [ ] 先补缺签名 key/配置/私有 HOME 的“不完整备份不能恢复”红测。备份至少包括 control DB 的一致快照、master key、Ed25519 私钥及派生公钥、managed-agent/出口策略、各 workspace 的 workspace/state/HOME/DSH session 与本拓扑必要持久元数据。明确 .credentials/.gateway attempt/tombstone 的处理规则，不能把运行 socket/PID 当可恢复状态。
- [ ] 用清单记录文件集合/摘要、schema、源码/Harness/profile、所有镜像 digest 与配置身份；不记录密钥内容。制品写入受限目录并有原子完成标记，失败不留下看似有效的备份；提供只读 verify 命令。
- [ ] Windows ACL / Linux 0700目录与0600秘密文件保护备份，不能把含 key 的备份放 Git/公开目录。若使用打包格式，拒绝绝对路径/..、链接逃逸及清单外文件；复用已有验证，不另写无检查解压。
- [ ] 验证活动长 Run 时新提交被屏障挡住，原 Run 收尾后才备份；注入复制中断/磁盘失败/取消，不丢唯一 key/旧备份、不误 resume、不报告 success。

## A7.3：新目录/新项目恢复演练

- [ ] 在本轮真实 Desktop 测试部署创建 A/B 用户、模型配置、会话/原生历史与一个 workspace sentinel，然后停机备份。上游继续用受控 provider，Harness 必须真实 A6 runtime。
- [ ] 源部署保持停机，恢复到新的专用卷/目录、不同测试项目/端口；源与备份字节保持不变。密钥必须来自备份，public 文件可由其派生并校验，不自动换 key。
- [ ] 校验 DB integrity/schema/密文和全部摘要，检查所有还原材料。旧 leases/container ID/boot/attempt/停止屏障及原生 cwd 的迁移策略必须明确：旧实例不接管、不重放旧 Run、不仅靠手工改 SQLite 字段放行。
- [ ] 新 Windows 物理根不等于容器内 cwd 改变（仍 /workspace）；原 native session 可续接。通过真实 UI/正式 API 验证 A/B 隔离、旧消息/配置解密、sentinel、同会话新的真实 Harness prompt 成功。记录新实例 ID 与原 key/历史一致性。
- [ ] 恢复前撤销或保留旧 Cookie/邀请/Run token 的策略明确并有测试；新 Origin 不匹配写请求继续 403。未知/在途 Run 的处理不伪造成 done、不重发原 prompt。
- [ ] 错 key、缺文件、改摘要、未来 schema、非空目标、外来卷/逃逸路径等负例失败时目标不部分上线，不触碰源。完成后恢复副本先 drain/停止，才允许源再次启动。

## A7.4：升级与失败回滚

- [ ] 至少演练一次固定版本切换：旧实例记录版本→停止接纳/收尾→全套备份→核验目标镜像/profile→启动新版本→登录/历史/真实 Harness 续接检查。无应用差异的版本演练也要如实标为流程演练，不能伪造 schema 升级。
- [ ] 注入坏镜像或坏 profile/公钥，验证失败有明确诊断，原数据/Key 完整。用匹配备份恢复原版本，再验证历史和新消息，不在原卷上强行 schema 降级。
- [ ] 若当前没有新的 schema 版本，至少使用隔离迁移 fixture 验证旧程序拒绝未来版本、整套备份恢复的边界；不要为演示随意新增迁移。

## A7.5：两份新手手册与发布分层

- [ ] Windows 手册把已装配完整 Desktop 路线更新为当前事实，保留 local 单人/login-only/完整 workspace 三者区别；删改“worker 仍仅 lifecycle/聊天未装配”等过时断言。给首次配置、镜像构建/固定摘要、初始化、ngrok HTTPS、管理员引导/邀请、用户登录/聊天/权限/停止、日常 start/stop/status/logs 的完整命令与预期输出。
- [ ] 两份手册分别给变量约定、前置依赖、数据位置、备份/验证/恢复/升级/回滚、常见错误与恢复命令、哪些命令会修改数据。占位符明确需替换，不能把 shell 命令写成 Python代码；不要求用户到处找隐藏步骤。
- [ ] Linux 手册按当前代码更新 runtime/gateway/签名配置事实，但目标宿主/XFS/firewall/IPv6/Chromium 门槛未验收仍明确阻断生产。没有目标 Linux 时只交付可核查命令与 not-run 清单，不要求新购服务器，不把 Docker Desktop 结果冒充目标 Linux。
- [ ] 输出两个独立维度：Desktop 功能/运维实际通过项，以及 production-release 原检查器未通过项。真实供应商仍 not-run 时单列，不能把桌面功能状态拼成“全部验收通过”，不修改发布检查器绕过固定阻断。
- [ ] 跑 ops/Desktop/A1兼容、backup/restore/迁移/身份/会话/Run/生命周期/文档路径受影响回归；真实恢复/升级场景单独计数，前端未改不要求无意义重建。
- [ ] 交付完整父/新 SHA、必要文件、红绿、制品清单（无秘密）、固定镜像、真实恢复/回滚证据和逐项资源清理。两份手册本轮明确可提交；其它主审文档不提交。完成后停止等待 A7 终审，不自动上线或宣称生产可发布。
