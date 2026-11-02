# Task 9 验收通过

日期：2026-10-22。最终提交：9813d42511e9ca2db84de5ca4bd0463bf5a6786c。

## 结论

Task 9 会话目录、workspace 原生绑定与消息快照代码验收通过。R1 四项及 R2 输入契约问题均已闭合，未发现本轮重要可复现回归。该结论不等于多人部署或 Task 8 真实隔离门槛通过。

- control 的 actor 范围目录、软删除/归档、活跃 Run 冲突及追加 v7 迁移已接入。
- worker 真实聊天保存多轮用户/助手消息，绑定和消息校验 workspace/session/格式版本；旧无归属材料不自动认领。
- worker 旧物理删除入口拒绝，不移除原生绑定/历史；local 行为保留。
- 更新/归档/删除在同一写事务内重查 actor，禁用交错失败关闭。
- worker 上传、聊天和快照共享 100 个附件上限；输入纯校验在 Run 认领及本轮落盘前完成。
- control 消息读取暂为 capability_unavailable，后续通过 Task 10 授权内部 transport 接通；本轮不将缺失 relay 标成已实现。

## 主审本轮实际运行

固定 Python：C:/Users/hyh/anaconda3/envs/Agent/python.exe。

- tests/multiuser：807 passed、3 skipped、2 个既有依赖弃用 warning，43.81 秒。
- Task 9 原指定五文件兼容回归：192 passed、6 skipped，17.96 秒。
- git diff 53820fc HEAD --check：退出 0。

独立复核：定向 sessions_chat + sessions 为 34 passed；临时真实 HTTP 对 /api/chat 与 /api/chat/stream 分别验证 101 附件及超 4 MiB 中文消息均为 400、认领仍 accepted、适配器 0 次、已有状态/事件日志字节不变。同一 Run 改为合法 100 附件后均执行成功且只有一次适配器调用。

本轮未改产品代码、未提交、未执行 Docker/真实供应商/真实 Harness；既有计划文档改动保留。未重复尝试此前被拒绝的临时目录清理；执行 Agent 报告其清理结果仅作为其交付信息，不作为本轮独立验证证据。

## 下一步

按用户既有委托，允许进入 [Task 10 研发交接单](../tasks/2026-10-22-task-10-authorized-relay.md)。Task 8 已审查的运行时代码接口作为研发依赖，尚未通过的目标 Linux/XFS/最终 Harness/Chromium/live 网络等仍是发布阻断项。Task 10 完成后再次停止审计，不自动进入 Task 11。
