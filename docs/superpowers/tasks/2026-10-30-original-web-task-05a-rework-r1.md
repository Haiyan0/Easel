# 原 Web Task 5A R1：配置版本、画像链接边界与媒体错误收尾

父基线：`ea3066471811daeb2f51211dea0162578cbfe483`。
继续原 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。
依据 [首轮审计](../audits/2026-10-30-original-web-task-05a-review-round-1.md)，仅修三项 P2；保留 [5A 原执行单](2026-10-30-original-web-task-05a-workspace-api.md)已通过范围。

## T5A-R1-01：统一运行版本来源

- 正式受管配置非零 revision 时，聊天、文件请求、status 使用同一可信版本语义；workspace 归属/存在校验保留。
- 先红测正式 factory 装配 + 真实 Store/RuntimeManager：revision=7、新用户 workspace 字段=0，已有同版本活跃 Run 时文件/画像正常访问；Run 结束后同类读取不停止/重建合法 worker；反向先文件后聊天也不得版本来回切换。
- status 能识别实际合法实例；真正配置升级/不匹配仍按既有生命周期处理。不得强制所有配置为0、把数据库字段写成偶合测试值，或另建调度器。

## T5A-R1-02：画像实际文件边界

- 对公开画像列表/摘要/status、详情、files 的真实目录和 Markdown 读取实施一致边界校验。不能只限制名称字符串；目录链接及目录内文件链接都需覆盖。
- 先红测：临时 profiles 下合法名字指向根外假材料，详情/files/摘要不能读出 sentinel；正常画像的固定六维及附加 Markdown 仍可管理。
- 检查关联保存/删除路径不能沿同类链接写/删根外材料；只做必要公共路径助手收口，不重做画像 AI 或5C功能。
- Windows junction 可验证目录分支；文件 symlink 若无权限如实标 not-run，可用独立 Linux 测试环境补证。不得用 mock/hardlink 谎称软链接实测；不使用用户材料。

## T5A-R1-03：流式失败响应有界解析与必定关闭

- 对真实未消费 httpx.Response 的 404/403/413/429/503 等媒体错误，返回稳定受控 DomainError；无 ResponseNotRead，不回显原始正文。
- 若需正文错误码，应有界读取；不需要解析时可直接映射状态。响应关闭、lease 释放必须在异常/取消路径完成；不得单纯吞异常或无界 response.aread 掩盖。
- 先红测真实 AsyncByteStream 或 localhost TCP（不能预装 content 代替），断言失败映射、响应关闭、lease/连接不残留；成功 GET/HEAD/Range/304 及后续正常请求保持。

## 复跑与交付

主审探针位于 root 的 docs/evidence 下 original-web-task-05a-review，保持只读。按实际受影响范围跑新增红绿、既有5A后端/gateway与画像/附件/生命周期相关回归；frontend Node/build/lint和5A四组、Task3 33组、Task4八组浏览器保持。若无前端生产改动，不借返工重做页面。

如需重跑隔离容器，仅增量构建受影响部分，假材料/随机标签，清理前核对归属；无须无缓存重建 Harness 或全栈。用户运行实例、原卷/socket/凭证不动，不调用真实付费模型，不发布、不全局 prune。

交付父/新 SHA、精确文件清单、三项红绿、主审探针结果、回归及 not-run/清理说明。只修改生产必要文件/测试与本项证据；主审 audit/task/plan/probe 保持只读。停止等待复审，不进入5B/5C/6，不自动上线。
