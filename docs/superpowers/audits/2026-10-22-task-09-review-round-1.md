# Task 9 首轮复审与 R1 返工单

日期：2026-10-22。结论：暂不通过；仅修复下列四项，完成后停止等待复审，不进入 Task 10。

## 基线与范围

- 实现基线：8ad817af072710333a52d1b669cb9a65f65a8d70。
- 本轮审查 HEAD：2eec2f6bb474bb789229b0cb9d8578224605b71d（含 b4cd542）。
- 依据：[Task 9 交接单](../tasks/2026-10-22-task-09-workspace-sessions.md)。消息跨进程 relay 未装配时 control 返回 503，属于本任务允许范围，不要求本轮实现 Task 10。
- 本轮为只读代码复审、临时目录探针和独立测试，未改产品代码，未运行 Docker、真实供应商或开发者 Harness。
- 不夹带主审已有 plan/spec 改动及审计/任务文档，不修改用户历史或真实 workspace。

## T9-R1-01（P1）：真实聊天没有写入消息快照

定位：./web/app.py，第 3331–3470 行，尤其适配器调用和 finally；./easel/worker/sessions.py 的 append_message 没有产品调用者。

新存储目前仅接入 binding。独立复核在临时目录运行实际 ACPAdapter 与 tests/fixtures/harness_rpc.py 子进程：适配器返回“你好”，事件包含 thinking/token，原生绑定为 native-session，但 messages_path 不存在，messages() 为 []。另以两轮假适配器验证，旧 _save_turn 仅保存最后一轮结果，不能充当完整的服务端会话消息快照。这不是 Task 10 网络 relay 的缺失，而是 worker 本地执行路径未采集自己的消息。

返工：在真实 worker 聊天执行路径接入新存储，持久化本轮用户输入及服务端实际输出，保留多轮历史；流式/非流式使用同一事实来源。不能接收客户端整段 assistant 历史补写，也不能通过读取、回放或重复 Run 再次追加/执行。原生续接失败、取消、错误等状态应如实保存或返回错误，不伪造成功输出、不覆盖之前历史。保持 local 行为。

验收：真实 HTTP /api/chat 和流式入口配受控适配器，断言新存储实际存在消息；至少两轮、存储重开后顺序和内容仍在，读取/回放不执行 prompt、不重复追加。保留至少一项实际 ACPAdapter + 夹具子进程验证，证明不是仅测试存储 helper。

## T9-R1-02（P1）：worker 旧删除入口移除原生绑定

定位：./web/app.py，第 5691–5702 行，DELETE /api/session/{session_key}。

带合法固定 worker 身份调用旧单数删除入口，返回 200、nativeHistoryDeleted=false，但 _binding_path(...).unlink() 删除新格式绑定。独立探针中原 original-native 随即变为 binding()=={}，再次 _bind_session 可为相同 Easel ID 写入 new-empty-native。原生文件保留并不等于原生续接引用保留；已有消息时还会变成孤儿消息。

返工：worker 不能通过这个旧入口物理清除绑定或消息，绕过 control 的目录软删除语义。可在 worker 明确拒绝该旧删除能力，或实现保留不可变绑定/历史的目录语义；不得修改 local 原有删除兼容。若保留 worker 删除能力，必须与实际执行/存储锁协调，不能在并发绑定或执行时 unlink。

验收：真实 worker HTTP 删除后原 binding/native ID 和消息文件字节不变；同 ID 无法绑定另一个 native ID。验证 control 软删除仍隐藏目录且保留 Run/原生引用，local 删除回归不变。

## T9-R1-03（P2）：消息快照没有身份/版本校验，读取只检查 JSON 对象

定位：./easel/worker/sessions.py，第 123–143、297–367 行。

消息行只保存原 message，没有 workspace/session/格式版本。主审独立探针：A/B 在同根不同 workspace 各自建立合法绑定，将 A 的消息文件字节复制到 B 对应路径后，B.messages() 返回 A-private；再写入完整 JSON 的非法 role 和 ../secret 附件，读取同样接受。此证据说明落盘记录归属及格式校验缺失，不代表已证明远程用户可以复制文件或越权读取服务器。

返工：为落盘快照提供明确的 workspace、session 与格式版本（文件 envelope/header 或逐记录均可），读取/追加均校验与当前绑定及预期格式相符；格式/归属不符明确失败，不静默认领旧无身份 JSONL。读取消息也应用必要的消息与附件合法性检查。保留有界分页，不把历史整体装入内存，不引入全盘扫描。

验收：跨 workspace、同 workspace 不同 session 的文件替换，以及未知/缺失版本均拒绝；完整但非法的消息/附件记录也拒绝；正常多轮、分页、重开及并发追加仍成功。

## T9-R1-04（P2）：更新/删除在写事务内未复查 actor

定位：./easel/control/store.py，第 1531–1594 行。

update_conversation/delete_conversation 只在 BEGIN IMMEDIATE 前调用 _require_actor。主审以两个真实 Store 连接，在初次检查之后、事务开始之前由第二连接提交 disable_user，再让写事务继续：更新返回 after-disable，数据库 disabled=1；删除也实际写入 deleted_at。create_conversation/reserve_run 已有事务内重查，本轮新增写路径未沿用。

返工：更新、归档、删除的当前身份/归属校验与变更共用同一写事务；不能只依赖 HTTP 中间件或事务外预检。不改变已授权任务收尾路径允许 disabled 的既有语义。

验收：两个独立连接确定性交错，禁用在写事务前提交后，更新/归档/删除均拒绝、目录字节字段不变；合法写及活跃 Run 冲突继续通过。

## 已独立执行的验证

- Task 9 服务/HTTP + Store：92 passed。
- 全部 tests/multiuser：791 passed、3 skipped、2 个既有依赖弃用 warning，53.32 秒。
- 指定五文件兼容回归：192 passed、6 skipped，23.56 秒。
- git diff 8ad817a HEAD --check：退出 0。
- 主审两个 Store 连接禁用交错探针及消息文件替换/非法记录探针均复现以上现状；另有独立审查者完成实际 ACP 与 worker HTTP 删除探针。

绿色既有测试未覆盖这些真实入口/故障类，不能据其判定 Task 9 已完成。Task 8 未通过的真实部署门槛保持原状态，不是本轮返工的前置阻塞。

## 给执行 Agent 的指令

以 2eec2f6bb474bb789229b0cb9d8578224605b71d 为返工基线，先补能复现上述四项的红测试，再精确修改实现。先核对 HEAD/工作区，不重置主审或用户改动。Python 固定 C:/Users/hyh/anaconda3/envs/Agent/python.exe，PowerShell。

运行新增定向测试、全部 tests/multiuser，以及 Task 9 原指定五文件回归；git diff --check。不要并发跑多组共享全局夹具的 pytest。仅显式路径提交实现、测试与必要产品说明，提交信息使用简体中文。

交付父/新 SHA、文件清单、四项逐项红绿、真实调用链与失败路径、独立连接交错证据、回归结果及仍未装配项。完成后停止，不进入 Task 10，不触发大镜像构建或真实供应商调用。
