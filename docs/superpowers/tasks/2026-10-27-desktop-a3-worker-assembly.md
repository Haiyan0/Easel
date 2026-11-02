# Task A3 执行单：实际 worker 与 lifecycle 装配

基线：`81589c0f24f3507b954e46b37ce85d181c619520`。实际执行前报告 HEAD 与差异。

先读：AGENTS.md、Desktop 装配计划 Task A3、`../audits/2026-10-27-desktop-a2-accepted.md` 和 deploy/desktop/topology.json。本单细化既有 A3，不重做 A1/A2。

## 目标与范围

使真实 worker 容器在单一受管进程中同时提供受认证 lifecycle health 与已有 `/internal/runs`、问题回答、事件和 session messages 接口。使用常驻 RunManager；以真实 ACP 测试夹具子进程验证执行，不接真实供应商/Harness。

本轮**不实现 gateway、control→gateway 断言、完整 A4/broker 接线或前端业务**；不添加新 Run 状态机。不因缺少 gateway 而直接把 control 加入 worker 网络。

## 必做顺序

1. **持久路径预检。** 使用随机专用测试根，从 WSL Docker CLI 以实际容器 UID/GID 10001 验证创建、SQLite/WAL、跨进程文件互斥、fsync、同目录原子替换及容器重开后的数据。先试拟用的 /mnt/c 根；权限/锁语义不满足就选择耐久 WSL ext4 专用根，记录 Windows 备份方式；不用 /tmp 保存持久数据。证明拒绝 symlink/junction 越界且不修改开发者目录。
2. **红测试。** lifecycle-only 假 ready 必须失败；缺/错误公钥、错误身份/版本/实例、未知模式不得启动或不得 ready。活动 Run 下 drain、停止恢复和旧实例接管用例先固定行为。
3. **执行入口。** 复用 web.app 常驻 worker 和 RunManager，合并已认证 lifecycle health，确保信号/drain 依据真正活跃 Run，SSE 断开不释放任务执行语义。不要用原生测试服务器冒充最终入口。
4. **最小公钥装配。** provisioner 从管理员固定参数/配置接收只读公钥路径，校验合法文件与边界后挂载到固定容器路径并注入 EASEL_WORKER_PUBLIC_KEY_FILE；不得由用户请求指定路径/argv。私钥只留在测试签发端/未来 control，绝不进入 worker。真实管理员签名配置和全链路装配留 A4。
5. **健康探测。** 对核验后的不可复用 Docker container_id 执行固定 argv，10 秒总超时；校验 workspace/config_version/container_id/ready 和执行能力标记。核验不能只依赖子命令返回 0；固定工具可输出最小 JSON 供 provisioner 再比较，不输出 token。不得发 prompt 作为常规 health，不靠伪造 handler 承诺已装配 Run 接口。
6. **迁移与重启。** 更新实际 inspect 的命令/env/公钥挂载契约。旧 lifecycle-only 或配置不符的运行实例不得直接接管，也不得因探针失败就 rm -f 活跃容器；沿用 drain/stop/CAS/实例 ID。只有明确退出且满足既定条件才重建，持久目录不删除。
7. **真实容器验证。** 测试签名快照 + 实际 ACPAdapter + harness_rpc 子进程：Run 接纳/去重、事件读取、问题回答（用夹具支持的场景）、停止、历史快照与重启恢复。测试请求在受控临时客户端中从允许的内部路径访问，明确不宣称 control/gateway 链路已完成；不得发布 worker 宿主端口。
8. **回归与提交。** 跑受影响 worker/provisioner/runtime/stop-recovery/签名/会话测试与 A1 Desktop 回归、文档路径；实际容器用显式开关运行，测试资源命名+run-id 标签+逐项清理。检查 diff，显式路径中文提交，交付后停止。

## 预期路径

- deploy/multiuser/provisioner.py、easel/worker/lifecycle.py、web/app.py；必要时创建 easel/worker/server.py，避免在两个独立服务器间分裂状态。
- deploy/multiuser/Dockerfile.worker 和 smoke_worker.py 仅为固定执行入口、必要依赖和真实冒烟而修改；不提前做 A5 Harness 构建。
- worker/runtime/provisioner 对应测试及新增专用持久目录/真实容器测试。
- Desktop README/topology.json 同步本轮已确定的路径/入口/probe 状态，gateway 保持未实现；批准依据引用主审验收单，不夹带主审文档提交。

如果代码需要越出上述业务范围，先报告精确依赖与最小必要路径；常规模块拆分和测试夹具调整可自行决定，不请求重复许可。

## 交付格式

父/新完整 SHA、精确文件清单、逐项红绿、真实镜像 ID/入口/UID、公钥挂载边界、健康 JSON 脱敏证明、持久路径探针和权限结论、停止/重开证据、清理结果、pass/fail/not-run。明确区分协议夹具与真实 DSH；不声称完整多用户聊天可用。

完成后停止等待主审，不进入 gateway 子任务或 A4。
