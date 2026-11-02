# Task 5 复审：返工轮 R2

日期：2026-10-19（Asia/Shanghai）。

返工基线：e264116bdd55b5f4b70c42186c5f65d5903c80c6。
复审提交：93d4df0901ecbb16b47dbb04716c7bdabbbd287c 和 764fa995c57c8cd2952296e28ec6f8e5630b7d58（当前 HEAD）。

结论：暂不通过。上轮四项中两项关闭，multipart 模型约束及租约生命周期仍各有一项 P2 残留。不进入 Task 6。

依据：[上轮审计](2026-10-19-task-05-review-round-1.md)与[Task 5 交接单](../tasks/2026-10-19-task-05-broker.md)。本次只复审四项修复及其引入的回归，不重启无关设计审查。

## 已关闭与验证

| 原问题 | 结论 |
|---|---|
| T5-R1-01 路由/凭证混配 | 关闭。BEGIN IMMEDIATE 内读取配置行、纯构建路由与凭证并写入快照；两连接 clear/重建交错回归覆盖旧/新路由与 Key 成对一致 |
| T5-R1-02 multipart 首字段校验 | 普通重复与后置字段已拒绝，但扩展参数解析仍可绕过，见 T5-R2-01 |
| T5-R1-03 到期释放在途额度 | token 时钟已分离，正常流有续期；续期失败和租约丢失仍未完整终止请求，见 T5-R2-02 |
| T5-R1-04 漏配出口策略 | 关闭。显式配置标志贯穿 CLI/ConfigService/EgressGateway，缺项在联网前 503，fake transport 保持显式 |

主审运行环境：PowerShell，C:\Code\Easel，C:\Users\hyh\anaconda3\envs\Agent\python.exe。

```text
python -m pytest tests/multiuser -q
397 passed in 12.51s

python -m pytest tests/test_settings_stale_mirror.py tests/test_web_model_config_parity.py tests/test_local_write_guard.py tests/test_web_security.py tests/test_agent_harnesses.py tests/test_harness_workspace.py -q
248 passed, 6 skipped in 22.16s

python -m pytest tests/integration/multiuser/test_egress.py -q
6 skipped in 0.07s（真实出口门槛未执行）

git diff e264116 HEAD --check
退出 0，无空白错误
```

探针全部使用临时库、假 Key、MockTransport，没有公网或供应商调用。未运行全仓、前端、Linux/容器真实门槛；6 skipped 不算出口通过。

## T5-R2-01 / P2：Content-Disposition 扩展参数造成模型解析差异

延续 T5-R1-02。位置：easel/control/broker.py 第 252–255 行、第 280–295 行附近的 disposition 正则与字段判定。

完整 spool 解决了“只看前缀”，但自写正则只识别普通 name/filename，未处理 name* 等扩展参数。broker 看到普通 name=ignored；真实下游 multipart 解析器可将同一 part 的扩展参数解释为 name=model。因此即使 broker 统计只有一个 model，上游仍可收到两个。

主审在真实 BrokerService.forward 中构造：

```text
第一 part：Content-Disposition: form-data; name="model"
值：tts-default（允许）

第二 part：Content-Disposition: form-data; name="ignored"; name*=utf-8''model
值：forbidden-expensive-model（不允许）

第三 part：正常 file 文件
```

MockTransport 用实际 Starlette Request.form 解析最终请求，结果：

```text
forward_status: 200
models: ['tts-default', 'forbidden-expensive-model']
effective_model: forbidden-expensive-model
```

这仍然是原模型允许列表缺口，不依赖真实供应商行为推测；支持该参数的下游解析即可取到未授权模型。

修复与验收：

- 消除 broker 与最终表单语义差异。可在完整头参数语法解析后拒绝不支持的扩展/连续编码/歧义参数，或安全重建只含规范字段的 multipart；不要仅为 name* 添加一条字符串包含检查后继续按不完整正则解释。
- 覆盖普通 name + name* 两种先后顺序、仅扩展参数、连续编码参数及 filename/转义歧义。对不支持的形状明确 400；不能让字段以另一名字越过校验。
- 测试必须对实际下游字节用真实 multipart 解析器检查最终字段，不只断言内部 model 计数；不允许的请求上游调用为 0。
- 保留当前正常大文件 spool、普通重复 model 拒绝、文件原始字节和 boundary 回归。

## T5-R2-02 / P2：续期失败/租约丢失后仍可能继续无额度请求

延续 T5-R1-03，包含同一生命周期问题的两个入口。

### A. 尚未收到响应头时，续期失败只是释放额度

位置：easel/control/broker.py 第 611–617 行、第 645 行、第 663 行；相关 _renew_lease_loop 第 549–557 行。

heartbeat 在发送请求前启动，但 abort_box 的实际关闭函数直到 context.__aenter__ 返回后才注册。上游正在接收上传或处理请求、尚未返回响应头时，abort_stream 是空操作；续期失败路径随后释放租约并结束心跳，原 forward 仍在等待，可以稍后成功返回。

主审和独立审查均以 async MockTransport gate 阻塞响应头，lease_ttl=3，renew_broker_lease 注入异常复现：

```text
renewal_failed: pending_forward=2 upstream_requests=2 leases=0
after_new_requests: upstream_requests=4 leases=2
returned_statuses: [200, 200, 200, 200]
```

两条旧请求没有被终止，已经失去租约却仍能 200；随后接纳了另外两条，突破每空间 2 的约束。

### B. UPDATE 零行被当作续期成功

位置：easel/control/store.py 第 933–938 行。

renew_broker_lease 不检查 rowcount。租约因过期被另一请求回收后，原流续期 UPDATE 0 行仍返回成功；heartbeat 永远续期不存在的租约，也不会关闭该流。

独立审查使用真实已打开响应、可控 lease_clock 模拟暂停/时钟推进：两个租约到期后再发两个请求回收旧行，等待下一次 heartbeat：

```text
upstream_open=4
leases=2
renewed_existing_rows=[0,0,1,1]
```

修复与验收：

- 续期失败/所有权丢失的终止机制必须从接纳请求起就覆盖：等待连接、发送请求体、等待响应头、响应流四阶段，不能只关闭已取得的 response 对象。请求仍在途时不得先释放额度；取消与关闭完成后再回收。
- 续期必须确认仍拥有有效租约。不存在或已失效的行不能静默成功，也不能不经容量重新接纳就自行复活旧租约。
- 回归使用 gate 在响应头返回前触发续期失败，验证旧请求被取消/终止且不能随后成功；关闭之前不允许新请求突破上限。
- 再覆盖已打开流的租约到期被其他连接回收、下一次续期 UPDATE 零行：应发现丢失并终止旧流，不继续无额度运行。保留 workspace/global、正常续期、token 到期不影响有效在途额度、崩溃回收及最终清理。
- 可以使用短 TTL、可注入时钟/调度或明确事件同步，避免靠长 sleep 验证。不要仅延长 TTL 或吞掉续期错误。

## 返工与交付

本轮为 Task 5 返工 R2，仅修复 T5-R2-01/02。已关闭的 T5-R1-01/04 保留回归，不重复重做。不要进入 Task 6 或扩展真实供应商/前端功能。

先补红测试再修复，运行本报告两组指定回归及 diff 检查；真实出口门槛未执行继续如实记录。必要迁移只追加，不改旧迁移。显式路径、中文信息提交，不夹带审计/任务单或其他工作。报告新 SHA、两项及租约两个阶段的红绿证据、真实结果，停止等待复审。
