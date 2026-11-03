# 用户聊天失败：出口 Fake-IP 诊断

日期：2026-11-03。任务：`f3820c4a9cdd47fda525688ad94ca090`。源码 HEAD：`665f7e8`。

## 已确认事实

- worker 接纳本任务返回 202；事件读取返回 200。worker 日志分类为 `stage=prompt category=unknown status=None error_type=HarnessError`。
- worker 只读运行账本显示 `failed / agent_failed`；无助手输出成功事件。受管模型为 `deepseek-flash`。
- control 日志的 `POST /internal/broker/chat/chat/completions` 返回 502。
- control 内使用实际 `EgressGateway`、实际出口代理及既有 CA，对 `https://api.deepseek.com/` 做一次无鉴权 GET，得到 `ProxyError: 403 destination_denied`。没有重放 prompt，没有发送供应商 Key，没有调用付费模型。
- egress 容器实际 DNS 将 `api.deepseek.com` 解析为 `198.18.0.25`，`is_global=false`；同容器策略检查返回 `destination_denied`。
- 本机正在运行 Clash Verge / mihomo；其生成配置 `clash-verge.yaml` 的 DNS 为 `enhanced-mode: fake-ip`、范围 `198.18.0.1/16`，TUN 已启用，未发现 DeepSeek 排除项。

## 结论

当前出口故障可稳定复现：Fake-IP DNS 地址触发非公网地址拒绝，HTTPX 代理异常由 broker 转为 `upstream_unavailable` 502，Harness 未提供可分类的结构化证据，因此 UI 使用 unknown。现有证据不能据此判断供应商 Key 是否有效、模型是否可用或真实上游能否完成聊天。

## 修复边界与后续验收

优先对 `api.deepseek.com` 配置精确 Fake-IP 排除，并按代理正式方式重载与清理 DNS 缓存；不将 `198.18.0.0/16` 加入 Easel 允许网段，不关闭私网/元数据拒绝，不更改 Key、模型或聊天记录。

Clash 属于 Easel 之外的共享代理配置，已请求用户确认后再修改。本次诊断未改代理配置、运行数据库、容器或镜像，也未重启服务。

修复后需重新检查出口容器实际解析结果及同一无凭证 HTTP/TLS 路径；通过只能证明出口连通，再由用户发起一次新聊天验证模型，不自动重试失败任务。

## 后续结果

2026-11-03 用户选择自行关闭代理，随后明确确认聊天可以使用。本会话未修改 Clash 配置或 Easel 出口白名单，未自动重试用户任务。此问题按用户实测结果关闭；不将其扩写为全部生产门槛通过。
