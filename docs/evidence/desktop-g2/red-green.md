# G2 红绿记录

## 红

### gateway server

新建 `tests/multiuser/test_gateway_server.py` 后、实现 `server.py` 前：

```text
7 failed
ModuleNotFoundError: No module named 'easel.gateway.server'
```

实现后新增关闭失败隔离状态用例，**8 passed**。

### gateway runtime

新建 `tests/multiuser/test_gateway_runtime.py` 后、实现 `runtime.py` 前：

```text
7 failed
ModuleNotFoundError: No module named 'easel.gateway.runtime'
```

实现后 **7 passed**。

### 真实集成迭代（如实记录）

首次真实运行在临时资源阶段失败：

1. 之前冒烟遗留的空 `easel-ws-*` 测试网络占满 Docker Desktop 地址池，
   `network_create_failed`；逐项核验为空后按精确名称删除，未全局 prune。
2. 客户端容器缺少 `PYTHONPATH=/opt/easel`，无法导入镜像内 `easel`。
3. 反向访问辅助函数替换遗漏，`_can_connect` 未定义（NameError）。

修复后最终 **1 passed**（377.37 秒）。

Store v11 测试首次运行有两条测试自身把旧 fixture workspace id 用于新
handle 的错误，修正测试后 4 passed；不是产品行为已通过的证据。

## 绿

```text
tests/multiuser/test_gateway_server.py
tests/multiuser/test_gateway_runtime.py
tests/multiuser/test_runtime_gateway.py
tests/multiuser/test_store_gateway.py
28 passed
```

```text
tests/multiuser/test_provisioner.py
tests/multiuser/test_runtime.py
tests/multiuser/test_store.py
tests/multiuser/test_stop_recovery.py
238 passed, 1 skipped
```

```text
tests/multiuser + tests/integration/multiuser/test_desktop_topology.py
1245 passed, 9 skipped, 2 warnings
```

```text
tests/multiuser/test_gateway_{auth,forward,stream,tcp}.py
120 passed, 2 warnings
```

```text
EASEL_RUN_CONTAINER_TESTS=1 \
pytest tests/integration/multiuser/test_gateway_runtime_real.py -q
1 passed
```

新增/修改代码与测试：`ruff check` 新文件全部通过；`git diff --check` 通过。
