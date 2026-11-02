# 原 Web 修复 Task 3 R2：待确认题卡与游标原子恢复

父基线：`1b461bac6199ed7d2766489c847ebeaf4f1b1d1e`。
继续 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。
仅 T3-R2-01 / P2，依据 [R1 复审记录](../audits/2026-10-29-original-web-task-03-review-round-2.md)。

## 问题

onQuestion 先消费/持久化游标，再等待 questionStatus 才保存题卡。若状态未回、流先断、用户重连，旧请求被取消/归属检查拒绝，新流从问题之后继续，后台 pending 但卡片丢失。

主审真实浏览器输出：cards_before=0/cards_after=0、after=[0,3]、question_status_calls=1、backend_pending=true、chat_posts=1。root 的 probe_question_gate.py 可复跑，不修改主审探针。

## 最小目标

- [ ] 先写真实 build 浏览器 gate 红测，不先等题卡可见：挂起 questionStatus，断流后重连，再放行旧回应；确认卡片仍能被新连接恢复/核对并最终回答。
- [ ] question 被算作已消费时同步保存待确认 payload/恢复状态；重连后以新 owner 核对，不依赖旧请求一定成功。可以选择等价的最小一致性方案，不建立新状态机/服务。
- [ ] pending 可恢复；明确已回答/已过期则正确消除；旧连接的迟到 pending/answered 不回写新连接。必要时显示确认中并禁用提交，不能把未知状态当成已授权。
- [ ] 同页重连仍从最后消费位置继续，思考/正文不重复；刷新仍可完整重建。连续重连、权限查询 401/503/断网、回答重试的已关闭结果保留。
- [ ] POST /api/chat 数量不增，不自动批准、不重发用户 prompt；用户真实容器/Key/登录态保持未触碰。

预计只改 useWorkspace 的 question/commit/reconnect 相关提交与必要浏览器 fixture/测试及本项证据。不要重做原布局/认证/其它已通过功能，不做 Task 4 登录视觉、Task 5 后端。

先红后绿，运行 Node/build/lint、原 28 场景加新 gate 场景的完整矩阵、原主审 probe 的对应情景。无须构建 Harness/Docker 镜像或重复后端测试。测试仍用随机端口假 API 和独立 context，清理只针对本轮资源。

交付父/新 SHA、精确文件清单、gate 红绿、新旧连接/游标/题卡状态证据、请求次数、完整矩阵与清理情况。保护主审文件，显式提交后停止复审，不进入 Task 4/5、不自动上线。
