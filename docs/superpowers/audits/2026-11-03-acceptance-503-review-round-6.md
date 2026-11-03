# 验收实例503修复R4-R1独立复审

父基线 `34ede47`，生产 `8b40525`，交付tip `188f1c819ad5c5c6cfe5c3a166d8fd77c90dd02b`。源码 `C:\Code\Easel-acceptance`，分支 `fix/acceptance-503-r3`，工作树干净。

结论：原先远程写pending后控制进程退出的窗口已修复，但预占立即过期引入可稳定复现的活跃接管交错，仍有一项P1。不批准合并/更新用户实例；仅下发 [R4-R2单项](../tasks/2026-11-03-acceptance-503-rework-r4-r2.md)。

## 主审新鲜验证

- 13文件受影响契约：260 passed，4 skipped，58.92s，exit 0。
- store/lifecycle_lock/lifecycle_rework：89 passed，15.94s，exit 0，与上组不重叠；共349 passed、4 skipped。
- 首次测试命令误填不存在的test_gateway_refresh_crash.py，没有收集测试；核对实际新增文件名test_attempt_ownership_r4r1_503.py后，正式13文件命令通过。
- 原子进程崩溃探针原样复跑，触发旧红态“意外恢复成功”断言，证实原窗口已能恢复；正式子进程恢复正向测试通过。
- `git diff 34ede47..HEAD --check`无输出，实施工作树干净。
- 主审以无挂载/无网络/只读/自动删除的临时摘要容器核对镜像 `sha256:b30351b19ff72bd38b371d81c607db582c9e242581fb90776ef39564d9092ee3`：runtime/store逐文件与宿主一致，分别为 `04a9f8be595160c3df99ef727349365024eb035e7ae63620bf3543d4ede35196`、`0f9a2fa00c327a38340af3928514a17be9d941f567f79066175668dd595d34ae`。
- 未重新跑Update/生命周期容器全链、前端、真实供应商/平台，不冒充本轮通过。

## R4-R2-01 / P1：活跃预占立即到期，迟到旧请求重新产生无主pending

`easel/control/store.py:1503` 的pre-dispatch预占把lease_expires_at直接设为now。原调用仍活跃、尚未抵达provisioner工作区锁时，另一个owner即可接管。provisioner在旧attempt没有记录、当前槽已无pending时允许commit无操作成功（`deploy/multiuser/provisioner.py:2783`附近）；该成功没有阻止原请求随后执行。

独立审查者构造确定性Event交错，主审再次原样运行 [probe_r4_r1_live_takeover.py](../../evidence/acceptance-503-review/probe_r4_r1_live_takeover.py)，exit 0：

1. A正常ready刷新，预占精确attempt A后，远程请求在provisioner工作区锁前暂停。
2. B看到立即到期starting，接管并提交A。此刻A尚无远端记录，commit返回成功，B清掉A的pending槽并开始自己的attempt。
3. A请求迟到，真实attempt持久逻辑写入G2/A/pending，随后取消/丢失回复。该写入符合当前provisioner规则。
4. B远程新attempt被A/pending拒绝，回滚后DB只保留B归属。
5. 连续三次普通首屏仍gateway_cleanup_pending；终态DB ready、pending槽为空、DB attempt不是A，而远端一直A/pending。

```text
B first error: gateway_cleanup_pending
B retry 0 gateway_cleanup_pending
B retry 1 gateway_cleanup_pending
B retry 2 gateway_cleanup_pending
DB: ready / attempt != A / pending_attempt_id=""
remote: attempt=A / status=pending
```

探针使用真实Store/RuntimeManager/Provisioner commit及attempt持久化，远程用假适配器；Event发生在工作区锁前，不违反服务端串行锁。仅自动删除临时目录，无用户资源。该问题是本次“预占立即可接管”语义的直接后果；持久化时机已正确，但接管不能消解仍有权迟到执行的旧请求归属。

## 收口要求与边界

预占/活跃执行/失败可接管之间需要一致的归属约束；接管方认为旧attempt已结束时，迟到旧请求必须被明确fencing，或其真实pending仍能归属于可恢复的持久槽位。仅延长等待、吞冲突、无条件覆盖pending不构成该安全不变量的证明。

R3请求原子屏障、既有Run收尾、已完成的崩溃恢复测试保持护栏，不扩前端/平台/部署框架。未合并、推送或修改用户实例/容器/DB/Key/Cookie/attempt；未调用真实模型/平台。
