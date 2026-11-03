# 验收实例503修复R4-R2独立复审

基线 `188f1c8`，交付tip `4dd3550`，生产 `8361ce5` / `174af97`。源码 `C:\Code\Easel-acceptance`、分支 `fix/acceptance-503-r3`，工作树干净。

结论：原活跃接管反例已由provisioner锁与精确执行权围栏处理；但正常Stop会删除围栏，仍有一项P1，暂不批准合并或更新用户实例。仅下发 [R4-R3清理一致性单项](../tasks/2026-11-03-acceptance-503-rework-r4-r3.md)。

## 主审实际验证

- 14文件受影响契约组：271 passed，4 skipped，61.54s，exit 0。
- store/生命周期4文件与provisioner6文件合并运行：275 passed，1 skipped，20.39s，exit 0。与前组不重叠，共546 passed、5 skipped。
- 在无网络/只读源码挂载/只读根文件系统、临时tmpfs的自动删除容器中，运行交付的真实Unix客户端/服务端契约：`UNIX_CONTRACT_OK`，exit 0。确认commit围栏跨独立连接生效，新attempt正常准入。
- 未再次运行会卡在旧探针late_written屏障的原反例，以正式Event正向回归验证迟到A为确定失败、B恢复ready；没有把探针超时当作通过。
- `git diff 188f1c8..HEAD --check`无输出。生产仅改宿主/WSL启动的provisioner.py，不需要为不存在于镜像内的该文件重建control镜像；runtime/store未变。
- 本轮主审未重跑Update/Stop→Backup→Verify完整容器演练、前端或真实平台，不宣称这些本轮通过。

## R4-R3-01 / P1：Stop清理记录时撤销了旧请求执行围栏

`deploy/multiuser/provisioner.py:2574` 的正式Stop在物理停止后调用 `_clear_attempt`，`:2384`–`:2386`删除内存与整个磁盘attempt文件。其中携带的 `fenced_attempts` 随之丢失。物理资源已清理，并不能证明之前已围栏的请求不会迟到；清理B不应恢复A的执行权。

独立审查者提供 [probe_r4_r2_stop_fence.py](../../evidence/acceptance-503-review/probe_r4_r2_stop_fence.py)，主审再次原样运行，exit 0：

1. commit(A)持久执行围栏；B正常创建/提交，其记录继承A围栏。
2. 调用真实 `Provisioner.stop(B)`，假远程只模拟资源已停止；真实 `_clear_attempt`删除记录和A围栏。
3. 新建provisioner对象模拟重启。正式 `ensure(A)`越过准入检查，到达prepare_workspace；探针在首次目录副作用处以sentinel截断，无实际目录/容器动作。
4. 单独调用真实 `_with_gateway`验证gateway准入，fake模拟gateway新建，真实持久化写出A/pending。

```text
正式stop后attempt文件及旧A围栏均消失
重启后迟到A的正式ensure已越过admission，抵达prepare_workspace
gateway.ensure_calls=1
record.attempt_id=A，record.status=pending
```

仅临时材料和假远程，自动清理，无Docker/用户资源。第三步直接证明正式外部入口的准入失效；第四步是独立验证内部gateway及落盘路径，不冒充完整真实容器创建。

## 处置范围

保留旧attempt失去执行权的持久事实，同时允许B的物理资源清理和新attempt正常创建。核对所有记录删除/覆盖路径，不能只改某次Stop测试，也不能靠任意时间剪裁围栏来假定迟到请求已经消失。

原崩溃恢复、锁内活跃执行收口、两入口接管、R3接纳边界保持关闭项护栏。主审未合并、推送、修改用户实例/容器/DB/Key/Cookie/attempt，未调用真实模型/平台。
