# Task 5 审计：返工轮 R1

日期：2026-10-19（Asia/Shanghai）。

基线：f282843ea3416adaca34a738e50b544ff6853a67。
审计范围包含实现提交 ac763106baff65a47196e930b5b926f2b6a247c6 与追加测试提交 e264116bdd55b5f4b70c42186c5f65d5903c80c6（当前 HEAD）。

结论：暂不通过。四项 P2 需要修复；不进入 Task 6。

依据：[Task 5 交接单](../tasks/2026-10-19-task-05-broker.md)、[plan](../plans/2026-10-16-multiuser-workspaces.md) Task 5、[spec](../specs/2026-10-16-multiuser-workspaces-design.md) 第 8.2 节。

## 已验证结果及限制

主审环境：PowerShell，C:\Code\Easel，C:\Users\hyh\anaconda3\envs\Agent\python.exe。

```text
python -m pytest tests/multiuser -q
386 passed in 11.58s

python -m pytest tests/test_settings_stale_mirror.py tests/test_web_model_config_parity.py tests/test_local_write_guard.py tests/test_web_security.py tests/test_agent_harnesses.py tests/test_harness_workspace.py -q
248 passed, 6 skipped in 21.95s

git diff f282843 HEAD --check
git show --check --oneline HEAD
退出 0，无空白错误
```

另进行了令牌/存储与出口的独立审查。DNS 解析结果逐项验证、代理连接固定解析 IP、无代理时拒绝请求等已实现；此次未找到可证实的 DNS 重绑定连接绕过。但下述功能边界仍有缺口。

所有额外探针仅使用临时数据库、假 Key 和 MockTransport，无真实公网或供应商调用。本轮未执行 Linux/真实出口门槛、前端构建或全仓测试。交付报告中的全仓 42 项失败未在本轮逐项核实基线，不能推断其全部与本任务无关；指定回归结果如上。

## T5-R1-01 / P2：签发时路由与凭证非原子读取，仍可混配新 Key 与旧地址

位置：easel/control/broker.py 第 294–300 行；相关 config.py credentials_for_ref 的个人 revision 检查。

issue 先 resolve 获取路由，再 export_credentials 重新查个人覆盖行。两次读取之间没有一致的数据库快照；个人覆盖 clear 后重建会重新使用 revision=1，单靠 credential_ref 中的数字无法识别该变化。

主审与独立审查均用第二个真实 Store 连接复现：

1. 用户个人覆盖为 old.example + old-fake-key，revision=1。
2. issue 完成 resolve 后，在 export_credentials 入口暂停；第二连接 clear，再保存 new.example + new-fake-key，revision 又为 1。
3. 恢复签发，得到的 token 解密快照包含旧 URL 和新 Key。

```text
snapshot_interleave: old_url=True new_key=True
```

不可变快照只保证写入后不变，无法修复写入前已经混配的数据。随后转发会把新凭证用于旧目的地，违背完整路由不混来源的核心要求。

修复与验收：

- 在一致事务/快照下取得完整路由及对应凭证，并与 token 快照落库的版本条件保持一致；或采用不会复用的不可变配置记录。不能仅在 resolve/export 之间加进程锁，必须对多 Store/进程成立。
- 加入可确定交错的两连接测试：resolve 与凭证读取之间 clear+重建、普通更新。可以得到完整旧快照、完整新快照或明确冲突/重试，绝不能混配。
- 用实际假 transport 断言目的 URL 与认证凭证属于同一版本。保留旧 token 在后续修改/清除后仍固定的测试，不把明文 Key 复制进数据库或 worker。

## T5-R1-02 / P2：multipart 仅校验首个 model，后续字段直接透传

位置：easel/control/broker.py 第 591–594 行、第 571–575 行；相关 _find_multipart_field 第 197 行。

找到首个合法 model 后 _prefetch_multipart 立即返回；replay 原样传递后续 body。即使当前 buffer 已包含后续字段，_find_multipart_field 也在第一个 model 处返回，不检查重复字段。

主审对 audio/transcriptions 构造两个 chunk：首段 model=tts-default 及文件，后段再附 model=forbidden-expensive-model。MockTransport 用真实 Starlette multipart 解析器模拟下游读取：

```text
forward_status: 200
downstream_models: ['tts-default', 'forbidden-expensive-model']
effective_model: forbidden-expensive-model
```

因此 token 的 allowed_models 没有约束真实上游模型；后置的禁用字段也绕过前缀检查。把文件内容里的伪字段排除，并不能覆盖真实重复 part。

修复与验收：

- 对真实 multipart part 做完整、安全的解析与约束，保证上游只收到一个已授权的 model；后置 model 或禁用路由/凭证字段不能原样越过校验。
- 覆盖同 chunk 和跨 chunk 的重复 model、首个合法/后个非法、后置禁用字段、filename 与 name 的歧义、文件内容含伪字段。无效请求明确 4xx，不发出可被上游解释为越权模型的请求。
- 可以使用受限临时 spool 或安全重建表单，保持有界内存和正常大文件传输；不要简单扩大前缀缓冲上限或只强制 model 在第一段。保留正常 multipart 文件字节、合法 boundary/类型及模型契约验证。

## T5-R1-03 / P2：token 到期使仍在途的请求提前释放额度

位置：easel/control/store.py 第 880 行、第 895–899 行；相关 broker 流生命周期。

broker_leases.expires_at 直接等于 token.expires_at；新请求会删除到期租约，但已打开的上游流没有相应的关闭机制，HTTPX read timeout 为 None。token 过期不代表该请求已经结束。

主审在真实 BrokerService.forward + MockTransport 上复现，而非仅调用租约辅助函数：

1. clock=1000，签发 token 并打开两个上游响应，均不关闭。
2. clock=2800，旧 token 到期；为同一仍活跃 Run 签发新 token，再打开两个响应。

```text
lease_expiry: open_upstream_streams=4 counted_leases=2
```

每空间上限 2 已被突破，同类问题也影响整机上限 8。

修复与验收：

- 区分“token 不再接纳新调用”和“在途请求已完成”。在确认关闭上游前，不得释放其实际并发占用。
- 可采用请求租约续期/受控截止等明确生命周期，兼顾崩溃后的回收；不能仅延长固定 TTL 而保留同一缺口。若到期要释放，必须先终止对应在途流。
- 用可控时钟与 gate 保持真实上游响应未关闭，跨 token 到期后尝试新调用；仍在途时应 429，或旧流已确认关闭后才允许新请求。覆盖 workspace/global、SSE/慢响应及异常/取消，最后额度归零。

## T5-R1-04 / P2：漏配出口策略仍创建生产客户端

位置：easel/control/cli.py 第 86–92 行；easel/control/egress.py 第 232–246 行。

省略 --egress-policy 时 CLI 自动构造 EgressPolicy()；只要有 proxy_url，EgressGateway.client 就返回真实 AsyncClient。任务单及 deploy/multiuser/egress-proxy.md 明确要求缺少代理或策略均失败关闭，当前未保留“策略未配置”状态。

独立审查无网络探针：

```python
gateway = EgressGateway(proxy_url="http://127.0.0.1:8899")
client = gateway.client()  # 当前成功创建生产 AsyncClient
```

此项不是声称已绕过代理的 IP 检查，而是部署约束和缺省配置处理未兑现。

修复与验收：

- 生产代理与策略必须显式装配；缺策略应在启动时明确失败，或 broker 请求在联网前返回 503 egress_unavailable。可以显式提交空策略文件选择默认公网规则，不能用隐式默认对象冒充管理员已配置策略。
- 保留显式 fake transport 的本地测试路径。覆盖 CLI 只有代理无策略、只有策略无代理、文件非法、二者合法四类；缺项时断言没有真实连接/transport 调用。
- 继续保证 ConfigService 与 broker 使用同一策略，不能为修此项让保存、签发及实际请求各自采用不同例外集合。

## 返工边界

本轮仅修复上述四项与必要回归，保持已通过的认证、存储迁移、加密、首批 adapter、响应流式和 local 兼容。必要 schema 变化通过追加 migration，不改历史迁移。不要因真实 Linux 环境暂缺而降低约束或用 mock 宣称出口验收通过。

先补确定可复现的红测试，再修复；运行本报告两组指定回归及 git diff --check。显式路径、中文信息提交，不夹带审计/任务单或其他工作。交付新 SHA、文件清单、四项红绿证据、真实结果与未执行门槛后停止，不进入 Task 6。
