# Task A6 R1：取消收尾中的同会话互斥

基线：`acef78b3ae3b1b1f318a297bf2c5cb06200aa76b`。

仅修 [A6-R1-01](../audits/2026-10-29-desktop-a6-review-round-1.md)。Run 持久状态 terminal 但 holder task/ACP 取消清理仍在进行时，不得清掉活会话锁并启动下一轮。

先写真实 RunManager 慢取消 gate 红测：第一轮 stop 未结束，第二轮不执行/不追加消息/不改 native binding；gate 放行后新 Run 正常续接。保留真正 task done 的陈旧锁自愈与启动失败恢复。用真实 ACP 子进程或现有 RPC fixture 的受控慢取消补齐物理关闭验证，不用固定 sleep 冒充确定性交错。

修改仅必要 worker/测试与证据，保护主审文档。跑 runs/ACP/session/relay/stop/SSE 回归及 UI stop/同会话续接、启动失败定向场景；不需要重做无变化的 ngrok/前端/全套镜像基础层。按当前源码更新受影响 worker 镜像并记录摘要，测试资源随机归属且精确清理。

交付完整父/新 SHA、逐项红绿和执行次数、真实/未执行边界；真实供应商继续如实 not-run。提交后停止等待复审，不进入 A7。
