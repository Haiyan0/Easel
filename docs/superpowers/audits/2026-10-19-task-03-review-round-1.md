# Task 3 审计：返工轮 R1

日期：2026-10-19（Asia/Shanghai）。

审计提交：472872f7a8ec8764cc3f7877026ca6f8e7543153。
父提交：d5a44c332d058d5cffae240de1a9743a384aff3a。

结论：暂不通过。发现两项可复现 P2，修复后复审，不进入 Task 4。

依据：[Task 3 交接单](../tasks/2026-10-19-task-03-policy.md)、[plan](../plans/2026-10-16-multiuser-workspaces.md) Task 3、[spec](../specs/2026-10-16-multiuser-workspaces-design.md) 第 4、5、9 节。

## 已验证部分

control 私有请求已实际接入认证、Origin/CSRF 和路由授权；inspect 避免失败写请求更新 last_seen。该新增接口以及未知路由测试预期调整符合任务需要，不因超出初始文件列表要求回退。

worker 的固定身份与服务 token 校验已接入 HTTP 入口，缺启动配置失败，不回落 local。敏感受管接口在 worker 被拒；未装配 control 业务明确 503。此结论不代表已实现后续的资源授权、配置签名或容器隔离。

独立运行（PowerShell，C:\Code\Easel，C:\Users\hyh\anaconda3\envs\Agent\python.exe）：

```text
python -m pytest tests/multiuser -q
260 passed in 9.27s

python -m pytest tests/test_local_write_guard.py tests/test_web_security.py tests/test_agent_harnesses.py tests/test_harness_workspace.py -q
186 passed, 6 skipped in 15.98s

git diff d5a44c3 472872f --check
退出 0，无输出
```

独立审查另跑策略、worker、auth API 三文件，162 passed，并复核下面的转发缺口。绿色测试未覆盖两项发现。

## T3-R1-01 / P2：问题状态查询的 HTTP 方法登记错误

位置：easel/control/policy.py 第 132 行。

权限表将 /api/chat/question/status 登记为 GET；实际 web/app.py 第 3927 行注册 POST，现有前端 web/frontend/src/lib/api.ts 的 question 状态查询也发送 POST JSON。

使用真实 web.app 和合法 worker 固定凭证的 TestClient 探针：

```text
authorize(member, POST, /api/chat/question/status).kind = unknown
authorize(member, GET, /api/chat/question/status).kind = task
worker POST /api/chat/question/status = 403
worker GET  /api/chat/question/status = 405
```

因此现有问答状态查询没有可用的方法；事件重放时的已解决问题过滤请求被守卫挡住。该问题属于本任务 method+path 登记，不应等完整 relay 实现后再修。

修复与验收：

- 按现有后端/前端契约登记 POST，去掉没有真实处理器的 GET 放行；不能通过扩大整个 chat 前缀解决。
- 增加策略测试：POST 分类为 TASK 且 worker_allowed 为真；GET 仍默认拒绝。
- 增加真实 worker HTTP 测试：合法内部凭证 POST 进入既有处理器并返回正确 questions 内容。可用 harness_ 前缀的临时问题状态，避免真实 gateway/Harness 调用；缺凭证仍为 401。
- control 当前尚未装配该业务，合法 Cookie + Origin + CSRF 的 POST 应通过分类后返回 503 capability_unavailable，而非 403。缺失 CSRF 仍被拒绝。

## T3-R1-02 / P2：HTTP 客户端默认状态绕过转发头清洗

位置：easel/worker/guard.py 第 124–128 行。

forward_to_worker 先清洗显式 headers，再调用 client.request；httpx 随后会合并客户端默认 headers、cookie jar 和 auth。实际发出的请求重新带上了应被剥离的凭证与身份头。现有 MockTransport 测试使用没有默认状态的空客户端，未覆盖该路径。

主审使用 MockTransport 捕获真实 Request，全部使用假 sentinel、没有联网：

```text
客户端配置：默认 Authorization、X-Easel-Role、Forwarded、X-Forwarded-Proto，加 Cookie jar
实际下游仍含：authorization, cookie, forwarded, x-easel-role, x-forwarded-proto

客户端配置：Basic auth
实际下游仍含：authorization
```

即使显式传入的 Authorization、Cookie、角色头被正确删除，也不能阻止默认值被重新注入。固定 workspace/token 正确并不意味着整个清洗边界成立；默认 Cookie/Authorization 仍可能把控制侧凭证发送给用户 worker，默认身份/转发头也违背任务要求。独立审查使用另一组默认状态复现相同结果。

修复与验收：

- 保证最终发送到 transport 的请求经过清洗，不能只检查 build_worker_headers 的返回值。处理客户端默认 headers、cookie jar 和自动 auth 的合并/注入顺序，再从服务端固定身份重建内部头。
- 保留连接复用能力和已有业务头/请求体/参数；不得通过临时清空共享 client 的 headers/cookies/auth 再恢复解决，避免并发请求相互污染。
- MockTransport 回归至少覆盖默认 Authorization、默认 Cookie 头、cookie jar、全部约定剥离类别（含混合大小写）、客户端 Basic auth。断言实际下游没有被禁头，内部头恰为固定值；显式正常 Content-Type/Accept、body/params 仍正确。
- 检查调用后原 client 默认状态未被修改，不依赖调用方“务必使用空 client”的未声明前提。无需为此提前实现 Task 10 完整 relay。

## 返工边界与交付

仅修复 T3-R1-01/02 及必要回归。保持 Task 1/2 已通过的 schema、事务限速、认证及 SQLite 线程边界；保留本轮正确的默认拒绝和 local 兼容。不扩大公开路由、不修改 spec 降低要求、不提前实现后续任务。

先补可复现红测试，再最小修复。运行上述两组 pytest 与 git diff --check，报告实际计数；显式路径提交，中文提交信息，不夹带审计/任务单或其他改动。交付父 SHA/新 SHA、文件清单、红绿证据和最终实际转发头断言后停止，等待复审。
