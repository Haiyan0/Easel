# Task G1 验收通过

验收提交：`d1fc8b16d6cb98092fa1ff9d0772c15bd40e35bd`。
返工父提交：`174af25f8b69b52e0914b091a53d486bb4f0b885`。

## 裁决

G1 通过；G1-R1-01/02/03 与 G1-R2-01 全部关闭。可进入 G2 实际容器/代次装配，尚不代表 gateway 已在 Docker 拓扑部署，也不代表 control 的公开聊天入口可用。

最终 URL 在 client 默认值合并后恢复为固定已验签 URL；元数据预验签在 body/受保护槽位之前完成，真实 body 摘要和 nonce 接纳保留。上游使用唯一关闭任务；成功才释放槽，异常/取消保留失败终态并隔离实例，重复等待不伪成功；隔离后后续合法请求 503，调用者共享 client 不被擅自关闭。

## 主审本轮验证

- gateway auth/forward/stream/TCP + 文档路径：**130 passed, 2 warnings**（2.25 秒，含真实 TCP 1 项）。其中 G1 120、文档 10。
- auth_api/policy/worker_guard/proxy/event_stream：**182 passed**（14.34 秒）。
- `git diff 174af25 d1fc8b1 --check`：退出 0。
- 独立定点复核关闭失败隔离：相关 **8 passed**；无新阻塞，未修改文件/操作 Docker。
- 两条 warning 为既有 websockets/Uvicorn 弃用信息；本轮未启动 Docker、ngrok、供应商或浏览器。

## 保留的装配门槛

G2 必须保留 A2 批准单的真实地址绑定、每次进程重启更新 boot generation、Lease/Handle 同步前未激活的要求；隔离状态必须反映到受信健康/状态报告，不能仅看 active 布尔或 worker health。

gateway 短暂不可用/隔离不等于 worker 退出，不能因此删除 worker 或把在途 Run 伪判终态。G2 生命周期/控制面恢复先于 A4 公开 relay 接线验收。

下一任务：`../tasks/2026-10-28-desktop-g2-gateway-assembly.md`。原真实 Harness、Linux/XFS/防火墙/Chromium 等门槛不变。
