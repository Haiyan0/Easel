# Task 8 返工 R3 复审：需要一次定点返工 R4

- 基线：41d91af755b605ff6cd1a79a9baddf0b2a6d03f7。
- 审查提交：256355814cc31f1eb2d8a7fc77fc0f2d4325fb8c。
- 结论：R3-01 已关闭；R3-02 只剩下述一项 P2。继续停在 Task 8，不进入 Task 9。
- 依据：[上一轮审计](2026-10-21-task-08-review-round-3.md)、[Task 8 交接单](../tasks/2026-10-21-task-08-worker-runtime.md)。本轮没有修改产品或测试、没有提交。真实 Docker CLI 验证只使用不存在的 context，不连接 daemon、不操作容器或持久数据。

## T8-R4-01 [P2] Docker context 不存在被误判为容器不存在，仍会错误写 stopped

位置：deploy/multiuser/provisioner.py 第 973–980 行 _not_found；调用者 _inspect_raw、_inspect_instance、_network_payload；停止入口第 1146–1148 行。

新增的错误判断仍使用任意子串匹配：只要 stdout/stderr 包含 not found，就表示目标不存在。这个判断不能区分“请求的容器不存在”和“Docker context/endpoint 不存在”。

独立执行真实、无副作用的命令：

```text
docker --context codex-review-absent-context-20261021 container inspect easel-worker-codex-review-absent
```

退出 1，实际错误包含：

```text
Failed to initialize: unable to resolve docker endpoint:
context "codex-review-absent-context-20261021": context not found:
open .../meta.json: The system cannot find the path specified.
```

这代表无法访问 Docker，不代表任何容器已不存在。

主审将真实 CLI 捕获的 stderr 送入实际 parse_request → _dispatch → stop → _docker 的 fake 外部边界，并由真实 Store/RuntimeManager.drain 验证状态变化：

```json
{"real_docker_exit":1,"context_not_found":true,"direct_reply":{"ok":true},"db_state":"stopped","still_running":true,"stop_calls":[],"rm_calls":[]}
```

全程没有执行 stop/rm，却回复成功并写 stopped。独立复审还把 runner 直接接到上述真实 CLI 的不存在 context，实际服务入口和 RuntimeManager 同样复现，因此不是仅凭构造错误文本推测。

修复/验收：只有明确、针对本次请求对象的 Docker absent 响应才能映射为 None。错误识别携带期望的对象类别/标识；context、endpoint、daemon、权限、CLI 初始化及未知错误必须失败关闭。避免用任意 not found 或其他宽泛片段作为对象不存在依据；复核共用该判断的三个 inspect 路径。成功但结构非法的响应也不能作为成功消失证明。

新增回归必须覆盖真实 CLI 这种 context not found 文本，在服务入口和 RuntimeManager 联动中断言非成功、租约仍 stopping、没有 stop/rm、不能宣称回收完成；也覆盖 _inspect_instance 的最终终态确认与网络检查。保留真正请求对象不存在的幂等成功及 stop/rm 失败确认测试。测试不需要真实 daemon 或操作容器。

## 本轮已关闭部分

- **R3-01 停止恢复顺序已关闭原缺陷。** stopping 过期返回 recover_stop，不直接建新代次；先按旧 ID 确认清理，再 CAS stopped，才可重新认领。正常 ready 接管仍可复用健康实例。
- 独立复核覆盖多恢复者交错：慢恢复在 stop 发送前暂停，快恢复删除旧 ID 并建立新 ID；慢恢复迟到时不会删除新 ID，CAS 不覆盖新 ready。迟到调用可能返回一次 503，但不破坏新实例或数据库，不列为本轮阻塞。
- **R3-02 命令退出码主体处理已补齐。** stop/rm 非零不再无条件成功，最终按实例 ID 确认；一般 daemon/权限错误能够明确失败。剩余问题仅为本报告中对象不存在分类过宽，不要求重做整套生命周期。

## 独立验证与范围

使用指定 Agent Python：

| 验证 | 本轮结果 |
|---|---|
| tests/multiuser | 706 passed、3 skipped，36.51 秒 |
| Task 8 八文件回归 | 229 passed、20 skipped，16.06 秒 |
| 容器隔离集成（本轮未启用） | 9 skipped，0.04 秒 |
| git diff 41d91af HEAD --check | 退出 0 |

独立复审另运行 provisioner/runtime/停止恢复定向：126 passed、1 skipped。现有回归通过，但未覆盖上述真实 CLI 错误类别。

本轮未进行真实 Docker 构建、真实 Chromium、真实 DSH/供应商、XFS 配额、宿主健康链、网络防火墙或全仓测试。外部交付的 Linux 容器证据保留为交付证据，没有外推为主审实测。

Task 8 原有部署门槛仍不通过/未执行：硬化容器 Chromium 为 No usable sandbox!；完整执行/最终 Harness 镜像尚未完整验收；真实 DSH pin/profile、XFS、宿主健康探测、目标 Linux 防火墙/IPv6 及 worker→broker/受限出口允许路径仍未关闭。关闭本次代码项不自动代表整个 Task 8 完成。

## 可复制给执行 Agent 的 R4 指令

基于 256355814cc31f1eb2d8a7fc77fc0f2d4325fb8c，仅修复 T8-R4-01，不进入 Task 9。先补对象不存在与 Docker 不可用的分类反例，再最小修复。

重点使用本报告真实 context not found 错误，验证实际服务入口、RuntimeManager 数据库状态及原实例终态确认；不要只测试 _not_found 返回值。确认三个 inspect 调用者不吞掉 context/endpoint/未知错误。保持真正目标不存在时的幂等成功、旧 ID 停止约束、停止承诺恢复顺序与普通回收不变。不要扩大到无关重构。

运行 tests/multiuser、Task 8 指定八文件回归、可用集成与父提交到新提交 diff --check。环境门槛沿用真实记录，不关闭沙箱、不恢复 capabilities、不为了本次分类修复重建大体积镜像。只显式路径提交必要实现/测试，不夹带审计或任务单。交付父/新 SHA、红绿、真实入口状态证据与实际/未执行项，然后停止等待复审。
