# A4 R2 红绿记录

## 红

```text
R2-01  keygen.py: error: unrecognized arguments: --fingerprints
R2-02  compare_identity missing engineContext/engineId/socketUid mismatches
R2-03  AttributeError: 'Store' object has no attribute 'complete_stop'
       AttributeError: 'Store' object has no attribute
       'reopen_after_completed_stop'
```

真实反例：key 卷私钥 A、宿主 worker 公钥 B 时，旧实现无法区分；修复后
Start 实际返回 `public_copy_mismatch:worker`。

## 绿

```text
R2 定向：118 passed in 43.36s
multiuser + Desktop 拓扑静态：1361 passed, 10 skipped, 2 warnings in 180.74s
G1：120 passed, 2 warnings in 2.20s
真实 A4 链路：1 passed in 88.44s
真实材料隔离：1 passed in 8.84s
Desktop 两轮 Stop→Start：通过；stop_completed/reopened 正确
PowerShell Start 身份测试：9 passed（含 key、Engine、屏障场景）
全量 pytest：2690 passed, 60 failed, 87 skipped（既有 setup/Starlette 环境）
```
