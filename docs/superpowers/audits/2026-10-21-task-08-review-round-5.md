# Task 8 返工 R4 复审：一项遗漏，需定点返工 R5

- 基线：256355814cc31f1eb2d8a7fc77fc0f2d4325fb8c。
- 本次提交：77055402d4b8b6a486e9d1c1435c66089e797b21。
- 结论：精确 absent 文本匹配已修复；成功响应的结构校验仍未实现，剩一项 P2。继续 Task 8，不进入 Task 9。
- 依据：[R4 审计单](2026-10-21-task-08-review-round-4.md)。本项是上一轮已明确要求、交付报告声称已完成的范围，未新增功能要求。本轮仅审计，未修改产品/测试、未提交、未进行真实 Docker 变更。

## T8-R5-01 [P2] inspect 退出成功但响应结构非法仍被当作对象不存在

位置：deploy/multiuser/provisioner.py 第 1012–1014 行、第 1027–1029 行、第 1042 行。

_not_found 的 kind/target/fullmatch 已关闭 context/endpoint 等错误文本误判。可是退出码为 0 的另一路径没有同步修改：

- _inspect_raw 和 _inspect_instance 对空数组、非数组或首项不是对象返回 None。
- _network_payload 对非数组返回 None。

None 在调用方表示已确认对象不存在。因此未知/非法成功响应依然能绕过终态确认。不能仅因命令退出 0，就把没有有效对象信息当成对象已消失。

主审临时探针使用实际 RuntimeManager、真实 SQLite Store、实际 Provisioner；仅外部 Docker runner 对 container inspect 返回退出 0 和以下 stdout。三种情况均复现：

```text
stdout=[]      → db_state=stopped, still_running=true, stop_calls=[]
stdout={}      → db_state=stopped, still_running=true, stop_calls=[]
stdout=[null]  → db_state=stopped, still_running=true, stop_calls=[]
```

独立复审另确认 parse_request → _dispatch → stop 仍回复 ok=true；网络 inspect 成功返回 {} 时，真实 ensure 继续发送 network create。均没有操作真实 daemon/容器。

### 修复与验收

仅明确针对本次目标的 absent 错误响应可以返回 None。退出成功但对象结构非法时抛稳定的 inspect_invalid/network_invalid（或已有等价错误），不能按 absent 继续。对单目标 inspect 验证其单对象列表结构，并保留现有实际身份/安全字段校验，不通过选择首个任意值跳过异常。

三个读取路径一起补齐，避免只修名称查询、仍让最终实例确认或网络创建吞掉未知响应。

先红后绿至少覆盖：

1. 初始名称查询收到退出 0 的 []、{}、[null]，服务返回非成功；RuntimeManager 不写 stopped，原实例不变且没有 stop/rm。
2. 最终按实例 ID 确认收到同类非法成功响应，不能报告终态已确认，数据库保留可恢复状态。
3. 网络查询收到非法成功响应，不能继续 network create/run。
4. 有效 inspect 正常执行；明确针对目标的不存在仍幂等成功；context not found 等失败测试继续通过。

必须覆盖服务入口及生命周期数据库状态，不仅单测 JSON helper；不要以更改 fake 默认响应或放宽断言消除反例。

## 独立验证

指定 Agent Python：

| 验证 | 本轮结果 |
|---|---|
| tests/multiuser | 709 passed、3 skipped，37.26 秒 |
| Task 8 八文件回归 | 229 passed、20 skipped，17.18 秒 |
| 容器隔离集成（本轮未启用） | 9 skipped，0.04 秒 |
| git diff 2563558 HEAD --check | 退出 0 |

上述反例不在当前新增三个回归中，因此现有测试通过不能证明这一分支已关闭。

未进行真实 Docker 构建/浏览器、真实 DSH/供应商、XFS、目标 Linux 网络/健康探测或全仓测试。交付报告中的既有镜像 socket 探针作为外部证据保留，不标为主审实测。

原有部署门槛不变：真实硬化 Chromium 沙箱未通过，完整执行/最终 Harness 镜像、DSH pin/profile、XFS、宿主健康链、目标网络及 worker→broker/受限出口允许路径仍未关闭。代码定点修复不等于整个 Task 8 已验收。

## 可复制给执行 Agent 的 R5 指令

基于 77055402d4b8b6a486e9d1c1435c66089e797b21，仅修复 T8-R5-01，完成上一轮遗漏的成功 inspect 响应结构校验，不进入 Task 9。

先用上述 []、{}、[null] 反例补实际服务入口/RuntimeManager 红测试，再最小修复三个 inspect 读取路径；非法成功响应明确失败，只有精确目标 absent 可返回 None。保留刚修复的 kind/target 精确匹配、真正不存在的幂等、ID 停止约束和停止承诺恢复。

运行 tests/multiuser、Task 8 指定八文件回归、可用集成及父提交到新提交 diff --check。无需为这次结构校验修复重新构建大镜像；不要改动浏览器安全参数或外部环境。显式路径提交必要实现/测试，不夹带审计/任务单，不操作开发者容器/账号/持久数据。交付父/新 SHA、逐类红绿与服务响应/数据库状态证据，然后停止等待复审。
