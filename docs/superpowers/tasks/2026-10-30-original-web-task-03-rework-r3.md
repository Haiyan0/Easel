# 原 Web 修复 Task 3 R3：未知问题状态不终结题卡

父基线：`8b914403dfbf7dd3758a81ee992968f2dc796cb0`。
继续原 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。
仅 T3-R3-01 / P2；依据 [R2 复审记录](../audits/2026-10-30-original-web-task-03-review-round-3.md)。

## 最小修改

verifyQuestion 的 explicit 当前把任何非空、非 pending 字符串当终态。请改为只认协议明确终态；unknown/checking/缺失/空串等保持 questionPending，不清 question、不加入 answered，不启用提交。

生产已有 resolved/expired/not_found，answered 如为既有兼容值可显式保留。无需新增状态服务、重构恢复逻辑或修改其它已通过功能。

## 验证

- [ ] 先用真实 build + 独立假 API 写红测：HTTP 200 返回非空未知状态，后台仍 pending，题卡应保留且提交禁用；不要只测缺字段/空字符串。
- [ ] 后续明确 pending 能解除待确认；明确终态正常清卡，旧连接迟到回应不改变新状态。
- [ ] 原 gate 重连、thinking 游标、401 与临时回答失败回归保持，POST chat 不增加。
- [ ] Node/build/lint、原 31 场景加新增状态行为全部通过；用户容器/凭证不接触，不构建大镜像、不做后端变更。

主审未知状态探针位于 root 的 `docs/evidence/original-web-task-03-review/probe_question_unknown.py`，保持只读。预计只需 useWorkspace 的状态判断、必要测试与本项证据；不得修改主审文档或推进 Task 4/5。

显式文件提交，交付父/新 SHA、单项红绿、原矩阵结果和清理情况，停止等复审，不自动上线。
