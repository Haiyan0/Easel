# 原 Web Task 5A 验收：基础工作区与三项 R1 通过

日期：2026-10-30。R1 父基线 `ea3066471811daeb2f51211dea0162578cbfe483`；修复提交 `16b634e`；证据 tip / 下一项父基线 `7b9d76f73dd730f63b74a63f3cd2e81c9436dbef`。
实施 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。

## 裁决

Task 5A 通过。[首轮三项](2026-10-30-original-web-task-05a-review-round-1.md)均关闭，未确认新增阻塞。原组件通过 control/gateway 访问本人状态、已有画像、内容/媒体和附件的基础组成立；不代表 Task 5 或整体恢复原 Web 完成。

下一项仅 [Task 5B 工作台数据](../tasks/2026-10-30-original-web-task-05b-workbench-data.md)。账号/分析/发布/技能与画像 AI 构建仍在5C，用户实例保持原状。

## 三项关闭证据

1. **T5A-R1-01 配置版本**：WorkspaceApi 使用正式 factory 传入的受管 revision，并保留 workspace 存在校验。主审原正式 factory + 真实 Store/RuntimeManager 探针新跑：受管/relay/文件版本均7，DB字段仍0，活跃 Run 下文件请求 ACCEPTED；结束后 worker仍7，停止0次、创建1次。先文件后聊天、未知 workspace 由新增回归覆盖。
2. **T5A-R1-02 画像边界**：共享 safe_profile_dir/safe_profile_file 检查解析后目录和每个 Markdown；列表摘要/status、详情、files、保存和删除复用。独立真实 junction 探针确认列表排除链接画像，详情/files/保存/删除均404，根外假 sentinel 不变；正常六维及附加 Markdown 保持可读写。Windows 文件 symlink 受权限限制仍 not-run，未冒充实测；实现逐文件解析拒绝链接别名。
3. **T5A-R1-03 媒体错误流**：workspace 错误直接按状态映射，不读正文；relay 必要错误码有界读取并 finally 关闭。主审原真实 TCP 探针新跑，普通和流式404均 DomainError(status=404)，不再 ResponseNotRead。新增真实 AsyncByteStream 测试覆盖业务错误、超限正文和关闭；异常/取消释放 lease。

原探针保持只读，位于 docs/evidence 下 original-web-task-05a-review。交付 R1 的红绿、镜像和容器证据位于实施 worktree 的 docs/evidence 下 original-web-task-05a/r1。

## 主审本轮新运行

- workspace_revision、gateway_client_errors、original_web_api、gateway_workspace、persona_security：93 passed。
- gateway_client、proxy、runtime_gateway、control_static：53 passed。
- 全 frontend Node：143 passed、0 failed；tsc/Vite build 与 oxlint通过，构建 index-Bw-bcRB_.js。
- 5A 原页面浏览器4/4、Task3 33/33、Task4 8/8，均 FAILURES=[]。
- 原正式配置版本与真实 TCP 媒体反例、新的 junction 拒绝探针通过。
- 实施树干净，ea306647..HEAD diff --check通过；浏览器证据输出临时目录并清理，未覆盖交付截图。

5A 浏览器 fixture 仍报告 close_clients coroutine 未 await 的清理 RuntimeWarning，功能矩阵成功且进程退出；保留为测试清理质量项，不改判生产功能。既有构建 chunk 提示同样不构成本组阻塞。

本轮主审未运行 Docker，交付的隔离容器结果作为已有证据读取，未称独立重跑。未操作用户容器/卷/socket、未读真实 Key/Cookie/邀请、未调用真实模型或发布。真实供应商、完整 Compose 与最终部署仍待后续验收；本记录不授权自动上线。
