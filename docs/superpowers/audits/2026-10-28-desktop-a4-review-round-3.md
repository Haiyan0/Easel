# Task A4 R2 复审：并发启停还需一项收口

基线：`f5451df4a1f752605ed17fcb2f7434258ddb3011`。
审查提交：`d5e80a9b87b19a14ddf6f6939396e84b646f321b`。

## 已关闭与裁决

R2-01 的真实私钥派生公钥摘要、卷/宿主/provisioner 一致性比较已修复；R2-02 的 Windows/WSL Engine ID、WSL context 和 socket UID 比较已修复。PowerShell 不再依赖 Get-FileHash 自动加载，新增身份测试通过。

R2-03 的顺序 Stop→Start 已有匹配完成记录的原子重开，但上一轮明确要求的“并发另一 Stop”仍存在破坏性窗口。仅修下述 A4-R3-01 后复审，不进入 A5/A6，不重做已关闭项目。

## A4-R3-01（P1）：两个 Stop 共用屏障，迟到 Stop 可删除重开的 control

位置：`easel/control/store.py` 的 set_admission_barrier 已有记录直接返回；`deploy/desktop/Stop-WorkspaceEasel.ps1:119`–`:126` 的按项目名 compose down/pkill；关联 Start 成功重开屏障。

屏障 ID 只防止接纳，尚未成为整段宿主启停操作的互斥/fencing：

1. Stop A、Stop B 同时运行，二者 admin drain 都得到同一个现存屏障 ID，并完成 drain。B 在执行 compose down 前暂停。
2. A 执行 down、写 stop_completion，返回成功。
3. 用户 Start 按 A 的完成记录清除屏障、重开 control/provisioner；新的 submit 可创建 worker/Run。
4. B 恢复，仍按同一 ProjectName 执行 compose down，并 pkill 同一路径的 provisioner；这些动作指向刚重开的新进程。B 最后的 stop-complete 虽因屏障消失而失败，但破坏动作已经发生。

主审用同一临时库两个真实 Store 确认目前允许该交错：

```text
TWO_STOPS_SHARE_ID=True
START_CAN_REOPEN_WHILE_STOP_B_PAUSED=True
NEW_RUN_BEFORE_LATE_STOP_DOWN=reserved
CURRENT_BARRIER=None
```

这不是正常的新 Stop 请求重新 drain 新任务：B 依据旧 drain 结果继续破坏动作，根本未收尾新 Run，可能遗留 worker/gateway 并切断 broker。现有测试只检查完成记录/屏障不匹配时拒绝写数据库，未约束此前的 Docker/进程操作。

独立审查进一步运行正式 Stop 脚本，以临时 stub 替代 Docker/WSL 副作用、真实 Store 控制交错。在 B 请求 down 前插入 A 完成、Start 重开与新 Run 接纳，脚本实际调用顺序为：

```text
admission_barrier=stop-a
drain_verified=settled
NEW_RUN_BEFORE_LATE_DOWN=reserved
COMPOSE_DOWN_AFTER_REOPEN
PKILL_AFTER_REOPEN
stop_completion_failed
```

该探针未操作 Docker；证明的边界是正式脚本仍会发出过时的破坏命令，而非声称已删除真实容器。

## 最小修复与验收

- 为同一实际部署的 Start/Stop 建立跨进程互斥，覆盖读取旧状态、drain、down、provisioner 进程操作、完成记录及重开全过程；或提供等价的、覆盖物理副作用的操作代次保护。不能仅在 down 前查一次屏障，随后释放保护继续执行。
- 操作排队或明确 busy 均可；第二个 Stop 若等到重开后才取得权限，必须重新读取/重新 drain 当前实例，不能沿用旧快照。Force 也不能绕过这种资源代次保护。
- 锁/操作标识必须对应真实项目资源，不能通过换 DataRoot、相对路径或另一个 PowerShell 进程绕过。正常退出/异常/取消释放互斥，失败屏障继续关闭；不能为解锁而杀开发者进程或改全局权限。
- 先补确定性双 Stop + Start 进程门控红测：B 旧 drain 后暂停，A 完成后尝试 Start，随后放行 B。证明 Start 被安全串行化/拒绝，或 B 重新处理当前代次；绝不发生“新 control 启动后被旧 down/pkill 删除”。断言实际副作用调用顺序，不能只断言最后数据库 CAS 失败。
- 保留连续两轮正常 Stop→Start、失败/取消/配置漂移屏障、不同项目互不影响；不引入忙等待或无界阻塞。可使用现有部署脚本共用基础设施，通常无需新增业务迁移。

## 主审实际验证

- Desktop/identity/屏障/A1 兼容五文件：**79 passed（48.29 秒）**。
- Store/runtime/gateway relay/正式 factory/文档六文件：**160 passed（8.91 秒）**。
- 独立审查定向纯逻辑/入口检查通过；主审额外使用两个真实 Store 复现上述交错准入。
- `git diff f5451df HEAD --check` 退出 0。主审未重跑 Docker/ngrok/供应商；作者真实顺序两轮与完整链路证据保留，不冒充并发启停已通过。
- 本轮未修改运行时代码或开发者资源；原生产发布门槛保持不变。

## 给代码 Agent

以 `d5e80a9b87b19a14ddf6f6939396e84b646f321b` 为基线，只修 A4-R3-01 与对应进程门控测试。先红后绿，保留所有已验收签名、Engine、公钥、scope/tombstone 与接纳屏障保护。无需重建无变化的大镜像；交付父/新完整 SHA、确定性交错、定向/真实边界和清理，停止等待复审，不进入 A5/A6。
