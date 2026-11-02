# Task 9：会话目录、workspace 原生绑定与消息快照

日期：2026-10-22。执行代码 Agent 完成后停止等待审计，不自动进入 Task 10。

本任务直接开始，不再等待目标 Linux 服务器或管理员手工提供 Harness commit。用户已委托主审安排执行；[当前执行补充](../specs/2026-10-22-local-docker-development-gates.md)替代旧审计中阶段性的“不进入 Task 9”指令。原计划 Task 9 依赖 Task 1+6，均已完成。

## 基线与范围

- 代码基线：8ad817af072710333a52d1b669cb9a65f65a8d70；开始先核对 HEAD、分支和工作区，若已有新提交先报告关系，不重置用户改动。
- C:/Code/Easel，PowerShell；Python 固定 C:/Users/hyh/anaconda3/envs/Agent/python.exe。
- 依据：[原 plan Task 9](../plans/2026-10-16-multiuser-workspaces.md)、[spec](../specs/2026-10-16-multiuser-workspaces-design.md)。不引入新基础设施。
- 本批只做会话目录/绑定/快照及必要入口，不做完整 WorkerRelay、SSE、前端或 Docker/Harness 打包，不重做 Task 8 绿色代码。
- 仅显式路径提交实现、测试、必要说明，不夹带审计/任务单/本轮计划补充，不覆盖或暂存主审已有文档改动。

## 文件与接口

```text
Create: easel/control/sessions.py
Create: easel/worker/sessions.py
Create: tests/multiuser/test_sessions.py
As needed: tests/multiuser/test_sessions_api.py
Modify as needed: easel/control/store.py
Modify as needed: easel/control/app.py
Modify as needed: easel/harnesses/store.py
Modify as needed: web/app.py
Modify as needed: easel/control/types.py
```

```python
SessionService(store: Store)
list_conversations(actor) -> list[ConversationRecord]
create_conversation(actor, title="新对话") -> ConversationRecord
assert_run(actor, run_id) -> RunRecord

WorkspaceSessionStore(root: Path, workspace_id: str)
bind(session_id, runtime, native_id, agent_version) -> None
binding(session_id) -> dict
append_message(session_id, message: dict) -> None
messages(session_id) -> list[dict]
```

保留上述最小接口兼容；分页等可用关键字参数扩展，或新增明确页接口，不把所有历史一次加载为生产分页。沿用 Task 1 ConversationRecord/RunRecord，复用现有 Store.get_run/create_conversation/reserve_run，不复制第二套幂等/归属逻辑。

控制库只持有会话目录/归属元数据；worker 消息、附件引用和原生绑定落本人私有根。每个持久化绑定和快照有明确 workspace 身份及版本，不能把无归属旧文件默认为当前用户。

## 实施步骤

- [ ] 先读现有目录/绑定入口、policy 和 Store schema，列出本批会接入的真实调用点；不先写无调用者的平行存储。
- [ ] 写红测试：A/B 使用相同 Easel session/native ID、不同私有根，绑定和消息不串；跨空间或伪造 actor 访问目录/Run/快照/附件为404或现有等价拒绝，不泄露存在性。
- [ ] 实现 SessionService 和 Store 的带 actor 目录操作；服务端生成 ID，列表稳定分页，无跨 workspace 查询。按原 plan 明确 update/archive/delete 语义；删除目录不删除原生历史或持久产物，活跃任务存在时不破坏引用。
- [ ] 如需 schema 变更只追加迁移；用真实旧库数据升级测试保留用户、会话、Run、幂等、认证/配置/worker 记录，不改写已发布迁移或伪造 schema 指纹。
- [ ] 实现 WorkspaceSessionStore：标识不得形成任意路径，写入原子，损坏/归属或版本不符明确报错，不把坏文件当空历史。覆盖同一会话并发追加不丢消息、进程/实例重开后历史保留。
- [ ] 原生绑定 runtime/native_id/agent_version 不得静默替换。续接失败、cwd 不兼容或版本不支持时返回明确错误，不自动新建空会话并覆盖原绑定；保留原记录及可审查的失败原因。
- [ ] local 旧格式仅在 local 保持兼容；worker 对旧无归属记录默认拒绝，不自动导入/认领真实旧历史。不要修改用户现有 profiles/outputs 或本地 Harness 数据。
- [ ] 接入当前已分类的 control 会话目录路由：使用既有认证、CSRF、默认拒绝与专用数据库线程。worker 读写入口依赖固定内部身份；消息只从实际服务端记录，不接受客户端提交整段助手历史覆盖服务器。
- [ ] Task 10 relay 尚不存在时，跨进程消息读取不能走服务器任意文件路径或假成功；能在本批授权入口直接装配的装配，仍缺 transport 的显式 capability_unavailable，并报告哪些真实入口已实现。保留现有 worker Run 签名/一次执行认领，不以历史读取触发 prompt。
- [ ] 跑本任务测试及相关回归，审查 diff 后独立提交，停止等待审计。

## 必须覆盖的故障类

1. 相同会话/原生ID跨空间、外空间Run、禁用用户、伪造角色/归属。
2. 未知/归档/删除会话；分页边界和稳定排序；活跃Run与目录操作冲突。
3. worker 重开、本地旧格式、损坏或截断文件、错误 workspace/version、路径穿越及越界附件。
4. 原生绑定改变、底座版本变化、cwd 不兼容和 resume 失败，均不重置历史。
5. 两连接/两个存储对象并发追加或绑定，不能覆盖他人的更新；失败写不留下假成功或损坏文件。
6. HTTP 入口未登录401、跨空间404、非法写入缺 Origin/CSRF 拒绝、失败不写会话状态；读取/回放不会执行 prompt。

## 验证与交付

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser -q
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/test_agent_harnesses.py tests/test_harness_workspace.py tests/test_chat_question_replay.py tests/test_local_write_guard.py tests/test_web_security.py -q
git diff --check
```

不要求本任务触发大体积 Docker 构建、真实供应商、XFS 或目标服务器测试；这些不影响此会话存储任务开始，仍保留在正式发布门槛清单。

交付父/新 SHA、显式文件清单、逐类红绿、真实授权/持久化调用链、迁移及并发证据、local 回归、仍未装配入口，然后停止。建议提交信息：feat(multiuser): 新增工作区会话目录与原生历史绑定。
