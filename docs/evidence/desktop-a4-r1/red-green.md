# A4 R1 红绿记录

## 红

```text
R1-01  基线 egress volumes = keys:/keys:ro；同 UID 可读 master/signing key
R1-02  assert ['GET', 'POST'] == ['GET']  # 503 后仍发 stop
R1-03  RuntimeUnavailable: worker_gateway_refresh_in_progress
        RuntimeUnavailable: worker_stopping
R1-04  AttributeError: 'Provisioner' object has no attribute 'identity'
        FileNotFoundError: deploy/desktop/provisioner_identity.py
R1-05  AttributeError: 'Store' object has no attribute 'set_admission_barrier'
R1-06  ImportError: cannot import name 'fsutil' from 'easel.worker'
```

真实链路额外暴露：并发 refresh 轮询把暂态 503 当实例消失；Windows
PowerShell 5.1 无 BOM 中文注释吞掉 Stop 的变量赋值。均已修复并有复跑。

## 绿

```text
R1 定向：120 passed in 10.66s
multiuser + Desktop 拓扑静态：1350 passed, 10 skipped, 2 warnings in 156.73s
G1：120 passed, 2 warnings in 2.10s
真实 A4 链路：1 passed in 76.84s
真实材料隔离：1 passed in 4.89s
WSL 文件系统：6 passed
Desktop 脚本 smoke：init/start/status/stop 通过；drain barrier verified；
stop drained=True；临时资源清理
全量 pytest：2677 passed, 62 failed, 87 skipped（既有 setup/Starlette 环境）
```
