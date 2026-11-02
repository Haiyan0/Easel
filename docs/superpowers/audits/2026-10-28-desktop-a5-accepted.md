# Task A5 验收：真实 Harness Linux 构建与受控 provider 链路

基线：`04271949a8ce2b14d6d618063b2a0eb7fb355579`。
审查提交：`1e70afab0a55bee4cf41f985b691b6ebbf82c604`。

## 裁决与边界

A5 的可复现 Linux 构建、真实 Harness ACP、逐 Run broker 适配及受控 fake provider 集成通过，可以进入 A6。**真实 DeepSeek 供应商请求尚未执行，不属于本次已通过结论**；该子门槛继续保留，A6 有批准受管 channel 时补测。不能宣称全部真实供应商或生产上线通过。

实际选择 Harness 自带 llm-pi-ai 的 openai-completions 路由，避免把默认 Messages 协议硬套到 OpenAI broker。未修改 Harness 源码、未扩大 broker 路由。sidecar 只在精确允许路径上把标准 Bearer 映射为内部 opaque token；重复/混用来源拒绝，由 control 继续校验 workspace/run/channel，供应商 Key 不下发 worker。

构建实际固定公开 commit、Node digest 与 pnpm，frozen 安装、Linux native-system/host tsc/tsdown；最终非 root 镜像加载原生 flock 并运行真实 ACP initialize/new/close。公开 manifest 与 Harness 原生 patch 分开，逐 Run URL/token/身份只从受管 env 获取。

## 主审实际验证

- managed-profile / worker-image / sidecar：**37 passed（1.38 秒）**。
- broker / broker-forward / egress / worker-runtime / runs / ACP / workspace：**223 passed, 6 skipped（35.43 秒）**。
- 使用作者保留的固定最终 runtime 镜像复跑 `tests/integration/multiuser/test_harness_runtime_real.py`：**2 passed（76.79 秒）**。以进程限定环境变量指定镜像，未重新构建大镜像、未改全局环境。
- 真实集成经过正式 control/gateway/provisioner/worker/sidecar/broker；真实 Harness 两轮重建进程并续接历史、A/B 空间分离、旧 token 撤销、工具权限回答与实际 echo 工具结果、401/429/500/断流单请求失败语义均通过。上游始终为测试 TLS provider，没有真实计费。
- 独立构建/profile审查 37 passed；独立 sidecar/broker审查 41 passed，均未确认阻塞问题；计数与主审范围重叠，不合计。
- 复跑结束后 A5 标签容器、A5 网络/卷查询为空，WSL A5 provisioner PID 列表为空；只使用本次测试资源，未操作开发者数据/全局 prune。测试镜像保留。

固定 runtime：`sha256:6830c2b4b2f3424dde6fff492803c05cbe77cc03d4e5921a14e26e3a7d738434`。
Harness：`687f282e8625c6b8d11ff58211a90524be2e4317`，Node 24.22.1，pnpm 11.28.5。

## 非阻塞说明与未执行

- `git diff 0427194 HEAD --check` 报告 `docs/evidence/desktop-a5/red-green.md:70` 多余的 EOF 空行；不涉及运行时，后续正常文档编辑时整理，不因这个单独要求产品返工。
- 主审未做无缓存重建；固定镜像来源与构建日志由作者提供，主审实际运行该镜像的完整真实 Harness 集成。不能把“主审复跑”描述成主审重新构建。
- 真实供应商、A6 ngrok 双用户浏览器、A7 运维尚未通过。
- Chromium 容器沙箱、XFS 配额、目标 Linux 防火墙/IPv6 等原生产门槛不变。文本工具成功不等于浏览器技能或生产隔离验收。

下一步仅执行 `../tasks/2026-10-28-desktop-a6-browser-closure.md`；提交后停止，不进入 A7。
