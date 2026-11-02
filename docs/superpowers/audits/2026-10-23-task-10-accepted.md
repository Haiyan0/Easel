# Task 10 验收通过

日期：2026-10-23。最终提交：e4ffc83c685762b94bdc063d7a6200c35b861725。

## 结论与范围

Task 10 的聊天授权 relay、逐 Run 持久上下文、消息读取、任务收尾与可恢复 SSE 代码验收通过。首轮九项、R2 五项及 R3 四项的范围内阻塞均已关闭，本轮未发现新的重要可复现缺陷。

- 旧 pending 扫描以 status/dispatch/container/prepare owner/期限作数据库 CAS；成功才在同事务中断、释放容量与撤销 token。
- 保存失败与连续重开保留完整恢复材料；核心四时点真实进程退出恢复、runId 去重和唯一成功终态保持通过。
- 认证监视独立于发送与读取，以检查开始时刻安排单调时钟节拍，认证耗时计入窗口。
- 消息原子替换按记录流式复制；内存取决于单条记录，不再随全部历史增长。
- 正式 broker factory 已接签发，幂等/准备所有权/原实例停止确认/权限映射和终态清理保留。

本验收不等于“所有多人功能已开放”。以下仍 503：skill、profile/build、analytics 抓取、publish/manual-publish、平台登录与注销等未装配业务。Task 8 的目标防火墙/IPv6、XFS、Chromium 沙箱、最终 Harness/runtime 镜像与真实 profile/live 出口等未通过项原样保留，仍阻断正式发布。

## 本轮新鲜验证

固定 Python：C:/Users/hyh/anaconda3/envs/Agent/python.exe。

- 主审 tests/multiuser：898 passed、3 skipped、2 个既有依赖弃用 warning，79.25 秒。
- 主审指定六文件兼容回归：209 passed、6 skipped，17.57 秒。
- 独立复核 lifecycle_rework/runs/event_stream/sessions：72 passed，24.10 秒，含四时点 os._exit 恢复。
- git diff ee16f0d e4ffc83 --check：退出 0。
- 主审真实 ConfigService/BrokerService/Store CAS 探针：旧观察 CAS=False、实际 token 仍可授权、active=1；当前合法 CAS=True、重复 CAS=False、active=0、release_count=1、token revoked=1。
- 独立组合故障：助手追加失败 + failed_outputs 目录创建失败 + 连续两次重开，完整材料仍在 finalizing；恢复目录权限后移至 failed_outputs，旧消息可读。
- 独立合法历史内存探针：约 4 MB / 20 MB 历史的小追加峰值分别为 5,015,065 / 5,015,077 字节，不随总历史增长。

主审未改产品、提交、运行 Docker/真实供应商或用户 Harness，已有主审 plan/spec 改动保留。

## 下一步

允许开始 [Task 11：登录与身份隔离的前端接入](../tasks/2026-10-23-task-11-identity-ui.md)。完成 Task 11 后停止等待审计，不自动进入 Task 12，也不将本次代码验收写成 Task 8 或正式发布通过。
