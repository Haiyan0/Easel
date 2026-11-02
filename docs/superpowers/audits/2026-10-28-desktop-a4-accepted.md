# Task A4 验收：公开 relay、逐空间 broker 与 Desktop 启停装配

原基线：`785f556bc81c464f59de5105a02c12305b2e6265`。
最终验收提交：`04271949a8ce2b14d6d618063b2a0eb7fb355579`。
包含 A4、R1、R2、R3；本轮相对 `d5e80a9b87b19a14ddf6f6939396e84b646f321b` 定点复审。

## 裁决

A4 在既定开发装配范围内通过，可以进入 A5。可宣称正式公开 control 已装配签名 gateway、逐 workspace sidecar/broker，并由真实 ACP 协议夹具子进程完成模型协议请求；仍不能宣称真实 DeepSeek Harness、真实供应商或生产发布通过。

R3 的 Windows 命名 Mutex 只按项目派生，不依赖 DataRoot；Start/Stop 在读取实例状态前取锁，覆盖 drain、down、pkill、完成记录及重开，Force 也受保护。等待有界；finally 释放；异常持有者退出后的接管显式处理。取得锁后从头读取状态，旧 Stop 不再携带旧快照跨过重开动作。

原 R1 六项和 R2 三项的已验收修复保留：私钥仅 control 可见、503 对账不误停止、同 worker 过期恢复、可信公钥/策略摘要、同 daemon/实际 context/socket 身份核验、跨进程接纳屏障与成功停止记录、原生 Linux 持久化错误传播。

## 主审证据

- R3 lock/Desktop/身份/屏障/A1/文档七文件：**93 passed（55.21 秒）**。
- 独立审查真实 PowerShell 进程门控及 Start 回归：**13 passed（16.20 秒）**。持锁期间没有 drain/down/pkill 或 keygen/compose，释放后才读取当前状态。属于正式入口 + 无 Docker 副作用的受控调用验证，不冒充真实容器并发测试。
- `git diff d5e80a9 HEAD --check` 退出 0。
- 前轮主审 Desktop 79 passed 与 Store/runtime/relay/factory/文档 160 passed 保留；本轮未改应用代码、协议或镜像内容，不重复重建/跑供应商。
- 作者提交的正式两轮 Docker/WSL Start/Stop、A4/R2 完整链路和材料隔离证据保留为作者实测；本轮主审未重跑 Docker/ngrok。无开发者资源操作。

## 验收边界与后续

- A5：真实 Harness 的 Linux 可复现构建、公开 ACP profile、实际 provider 协议及 broker 适配；固定本地候选源码，不再要求用户找服务器或 pin。
- A6：真实 Harness、双用户浏览器、ngrok 与真实供应商完整闭环；不能用 A4 协议夹具证据替代。
- A7：备份/升级/回滚/新目录恢复等运维验收。
- Linux 防火墙/IPv6、XFS、硬化 Chromium sandbox、最终 runtime/真实供应商等原生产门槛未因此关闭。未装配业务继续明确 503。

下一执行单：`../tasks/2026-10-28-desktop-a5-harness-runtime.md`。仅实施 A5，交付后停止复审，不自动进入 A6/A7。
