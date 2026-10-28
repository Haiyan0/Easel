# A4 R3 红绿记录

## 红

正式 Stop + 真实命名互斥持锁探针；移除 Stop 的锁获取后，持锁期间实际出现：

```text
docker exec ... admin drain ...
wsl ... docker compose ... down
```

`test_stop_does_not_touch_topology_while_lock_is_held` 断言
`admin drain` 不在持锁期间出现，失败。

## 绿

```text
R3 生命周期锁 + Desktop 静态/身份/屏障：31 passed in 14.04s
multiuser + Desktop 拓扑静态：1365 passed, 10 skipped, 2 warnings in 194.73s
全量 pytest：2694 passed, 60 failed, 87 skipped（既有 setup/Starlette 环境）
进程门控：锁期间无 drain/down/pkill；释放后 drain -> down -> pkill
Start 门控：锁期间无 keygen/compose；释放后正常启动
真实两轮 Stop→Start：stop-f468... -> reopened -> stop-d30...
```
