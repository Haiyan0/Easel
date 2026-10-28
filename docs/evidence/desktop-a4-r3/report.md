# Task A4 R3 定点返工证据

日期：2026-10-28（Asia/Shanghai）

基线：`d5e80a9b87b19a14ddf6f6939396e84b646f321b`（A4 R2 提交）。
返工提交：本次提交（`git rev-parse HEAD`）；仅修 A4-R3-01 及进程门控测试，
未进入 A5/A6，未重开 G1/G2/A3 或已验收 R2 项。

## 1. 修复

- Start/Stop 在完成参数解析/Docker CLI 检查后、读取卷/网络/control/
  provisioner 任何状态之前，获取跨进程 Windows 命名互斥：
  `Global\Easel.Desktop.Lifecycle.<ProjectName>`。
- 锁名只由真实 Compose 项目派生，不含 DataRoot、相对路径或时间戳；同一
  项目的不同 PowerShell 进程、不同 DataRoot、`-Force` 都使用同一把锁。
- 锁覆盖读取旧状态、drain、compose down、pkill、完成记录和 Start 重开
  全过程。锁等待有界（默认 60 秒，参数 `-LockTimeoutSeconds`），超时返回
  `desktop_lifecycle_busy`，不执行物理动作。
- 第二个 Stop 取得锁后会重新读取当前 control/socket/Store 并重新执行
  drain；不会把旧 drain 快照带到新代次。若先取得锁的是重开后的 Start，
  迟到的 Stop 只能处理当前已接纳任务，且不会在锁释放前发出 down/pkill。
- `finally` 释放互斥；`exit 0/1`、异常和取消都会执行；`AbandonedMutexException`
  视为取得锁（OS 已释放崩溃进程的所有权），不遗留永久锁。
- 不同项目锁名不同，互不影响；未修改全局 context、权限或开发者进程。

## 2. 红证据

先用真实命名互斥持锁、正式 Stop 入口和 Docker/WSL stub 复现：移除 Stop
的 `Enter-EaselLifecycleLock` 后，持锁期间 Stop 实际发出：

```text
docker exec control ... admin drain --control-dir /srv/easel/control ...
docker inspect --format {{.Image}} control-id
wsl ... docker compose ... down
```

测试断言 `admin drain` 不在持锁期间出现，失败。恢复锁代码后同一测试通过。

## 3. 绿证据

```text
R3 生命周期锁 + Desktop 静态/身份/屏障
31 passed in 14.04s

tests/multiuser + Desktop 拓扑静态
1365 passed, 10 skipped, 2 warnings in 194.73s

全量 pytest
2694 passed, 60 failed, 87 skipped
（失败仍为未改动的 setup/bash 编码、Starlette TestClient/httpx2 环境）
```

进程门控实际调用顺序（正式 Stop 入口 + 真实命名互斥，stub 只替代 Docker/WSL
副作用）：

```text
保持锁期间：无 admin drain / 无 compose down / 无 pkill
释放后：docker ps -> wsl test -S -> admin drain -> docker inspect -> compose down
        -> pkill -> stop-complete -> stopped
```

Start 门控：持锁期间无 keygen / 无 compose；释放后才读取 key/engine 并
启动。该测试与 Stop 共用同一 `Global\Easel.Desktop.Lifecycle.<project>` 锁。

## 4. 真实两轮启停（锁加入后）

使用正式 Initialize/Start/Stop、真实 control/egress 容器和 WSL provisioner，
临时项目 `easel-r3-smoke-7f2c`：

```text
Start 1: started project=... port=17866
Stop 1:  admission_barrier=stop-f468beb0938546f1adfd25d7581d8f58
         stop_completed=stop-f468beb0938546f1adfd25d7581d8f58
         stopped ... drained=True
Start 2: admission_barrier_reopened=stop-f468beb0938546f1adfd25d7581d8f58
         started project=...
Stop 2:  admission_barrier=stop-d30c69c51c3148a18cd3c5fd1410b503
         stop_completed=stop-d30c69c51c3148a18cd3c5fd1410b503
         stopped ... drained=True
```

所有临时容器、网络、卷、WSL socket 和临时目录已清理。

## 5. 固定镜像与边界

- control：`sha256:1d9c559e97f172381da28dbbec4e1afeb008405cddbea972c0f2baecff7deb16`
- gateway：`sha256:c0a35aaa9f304c47f9f5c3e73add7d9d5920041baef156e2ea674f3000c5580a`
- worker：`sha256:39f7c847d931c91f486c06e867eab799f2db38ef72854a4f0564b8fcc61f4f24`
- 未新增 schema 迁移；继续使用 v14 `control_state`。
- 未运行 ngrok 公网浏览器、真实 DeepSeek Harness、真实供应商计费、A6
  双用户浏览器闭环；未构造第二个真实 Docker daemon，Engine drift 继续由
  R2 的真实 Store/客户端与 PowerShell 入口反例覆盖。
- 未操作开发者容器/卷/网络/HOME/profile，未执行全局 prune。
