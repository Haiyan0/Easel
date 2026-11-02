# Task 8 D-R2 测试装配复审：权限与清理已补齐，剩一项挂载隔离回归

- 基线：04cf86fcbf25c4da36e050f106cd89a871c743f8。
- 审查提交：77079bea09a217fda85adcf9fedeab1f1810d76a。
- 结论：上一轮全局可写权限与清理要求已补齐主体实现；新卷装配使控制材料暴露给下游测试容器，需定点修复一项 P2。只涉及测试，不重开运行时代码，不进入 Task 9/10。
- 依据：[测试装配上一轮审计](2026-10-22-task-08-deployment-test-review-round-2.md) 和原 Task 8 控制密钥不进入 worker/sidecar 的隔离边界。仅审计，未修改产品/测试、未提交、未运行 Docker 或更改宿主权限。

## D-R3-01 [P2] 含控制 key/数据库的整卷同时挂给 sidecar 和 worker

位置：tests/integration/multiuser/test_control_broker_topology.py 第 331 行、第 360 行；tests/multiuser/topology_support.py 第 34–42 行。

初始化助手将 control_app.py、master.key、policy.json、client.py、control.sqlite3 及附属文件复制到同一个卷。随后：

```text
control → <同一卷>:/srv/easel
sidecar → <同一卷>:/etc/easel:ro
worker → <同一卷>:/check:ro
```

所有容器均使用 UID10001，卷内 key 为该 UID 所有的0400，数据库为该 UID 所有的0600。因此 sidecar 和 worker 都能读取控制 key、加密数据库和相关文件。只读挂载限制写入，不限制读取；缩小文件 mode 不能隔离拥有相同 UID 且挂载同一卷的容器。

主审以实际拓扑测试函数、临时 SQLite/文件和 fake Docker 捕获创建 argv，确认三个组件挂同一卷；并确认 volume_init_script 将 master.key 与 control.sqlite3 放入该卷。独立复审核对同一链路得到相同结论。本轮没有执行真实容器读取，不将该结论冒充 Docker 实测。

本测试使用公开固定测试 sentinel，不涉及已观察到真实管理员密钥泄漏，也不改变此前运行时代码安全结论。问题是权限修复引入了与被验收架构相反的测试挂载，使下游容器拥有本应仅控制侧可见的材料；授权转发断言仍可通过，却不再提供符合隔离边界的拓扑装配。

### 修复/验收

拆分控制数据、sidecar 策略和 worker 客户端输入的挂载来源。control 独占 key/数据库；sidecar 仅看到所需策略；worker 客户端仅看到测试脚本和明确允许的调用参数。可用多个本轮专用、标签化且受限权限的卷，或满足原生 Linux 权限的单文件挂载，不重新引入0777/0666。

权限初始化助手可以处理这些经本轮标签校验的专用资源，正式 control/sidecar/worker 的非 root/只读/cap-drop/no-new-privileges 边界保持不变。所有新增卷继续纳入已有归属校验和清理。

补两层回归：

- 纯逻辑验证组件挂载清单，断言 worker/sidecar 没有控制数据卷或其他包含 key/DB 的共享来源。
- 可用 Docker 时在 sidecar/worker 实际检查控制 key、control.sqlite3 及附属文件不可见/不可读，随后继续运行合法与非法 token 授权矩阵；control 仍能读 key、写数据库。检查结果仅输出布尔/状态，不输出任何凭证内容。

保留现有 UID10001_ACCESS_OK，但它只能证明该组件对其必要输入有权限，不能以“所有组件都能读同一私有卷”作为隔离验收。

## 上轮要求的关闭情况

- **全局可写权限**：宿主0777/0666方案已删除，改用本轮专用卷、0750目录及0400/0600文件；root 初始化助手仅用于准备标签校验的测试卷，正式组件仍是UID10001。原权限放宽问题可关闭；新的挂载隔离问题见本报告。
- **资源清理**：自建容器/网络/卷均带 run ID 标签，删除前读取 inspect 核对归属。单项异常捕获并继续，非零删除可复核不存在，错误汇总；正常测试清理失败会 fail，已有测试异常时报告清理错误而保留原失败。原匿名资源定位、缺标签、清理中断/静默失败主体要求已补齐。

## 本轮独立验证

指定 Agent Python：

- tests/multiuser/test_topology_support.py + 本机真实 sidecar/control broker 联调 + Docker 拓扑文件（未启用Docker）：8 passed、1 skipped、2条既有websockets弃用警告，1.44秒。
- 主审临时 argv/卷内容探针，结果如上。
- git diff 04cf86f HEAD --check：退出0。

本轮未独立执行 Docker、原生 Linux 权限、全套 Windows 回归、供应商或其他部署门槛。交付中的7条纯逻辑测试、Docker拓扑1 passed、762 passed/3 skipped等结果保留为执行Agent证据，不混写为主审实测。

仍保留原外部前提：目标 Linux、防火墙/IPv6、XFS、宿主健康链、管理员DSH pin/profile及最终镜像、真实Chromium沙箱和Task5 live。报告中的“原生Linux复验未执行”与“按原生Linux环境实跑”存在文字歧义，后续准确区分 Docker Desktop 内Linux容器与原生目标宿主，不将两者混记。

## 可复制给执行 Agent 的定点指令

基于77079bea09a217fda85adcf9fedeab1f1810d76a，仅修复D-R3-01测试挂载隔离，不改运行时代码，不进入Task9/10。上轮权限与标签化清理主体已经补齐，不再重做或降低它们。

先补挂载隔离红测试，再最小拆分输入：控制key/DB只给control，sidecar只给策略，worker只给客户端脚本/允许参数。不要以:ro或相同UID的0400来证明不可读。新增专用资源均加run标签并纳入归属校验清理，保留UID10001实际访问检查与合法/非法token矩阵。

运行纯逻辑回归和本机可用Docker拓扑，实际检查下游看不到控制材料；没有原生Linux则继续标记未执行，不修改宿主目录或提升正式容器权限。无需重建大镜像或反复跑无关全量测试。显式路径提交必要测试及说明，不夹带审计/任务单；交付父/新SHA、红绿、各组件挂载和不可见性证据、清理结果及diff --check后停止。
