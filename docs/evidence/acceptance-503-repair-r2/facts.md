# R2 证据事实清单

## 代码与镜像

| 项 | 值 |
|---|---|
| 父基线 | `43e4cd01bee0421226a193d54f6d4e09f045ff9e` |
| R2-01 提交 | `a2f8ed1` |
| R2-02 提交 | `528e41c` |
| R2-03/04/05 提交 | `5f5be50` |
| 新版 control 镜像（最终） | `easel-control:503-r2-final` = `sha256:fabe6b75835a93e31112f51b5508052a391a5f0d25dd827cbe048a6f95261964` |
| 镜像内 `easel/control/runtime.py` SHA-256 | `73702e19626fcd56159af65de4cdb0f95fa8eb30743afabbc3a26e6f1164b7ab` |
| 工作树 `easel/control/runtime.py` SHA-256 | `73702e19626fcd56159af65de4cdb0f95fa8eb30743afabbc3a26e6f1164b7ab` |
| 工作树 `deploy/multiuser/provisioner.py` SHA-256 | `f229ec30e619030fe0082885d1006c36cc24b1853cc3b85a2dacb755b1042103` |
| 工作树 `easel/control/store.py` SHA-256 | `36641f704fffcf32314ffc4539843e1785ecba6eb62365667cc81232f16676dc` |
| 工作树 `easel/control/cli.py` SHA-256 | `6efe05ddb72f404ac9752ebd1213ae72c361b7b15a61c46ebda0f988f62e6e90` |
| 用户实际旧版本源码 | `C:\Code\Easel-acceptance`（4382180，detached） |
| 旧版本 `easel/control/runtime.py` SHA-256 | `d37efde6591d3477c0d848d798ab1eb749ea081dd93f823f98f3b122d5722368` |
| 旧 control 镜像 | `easel-control:4382180862e2` = `sha256:9272b5562d2d9bfbdeef1d60e3ccb910dc5f90ccc24e79802ad9178548d769d2` |
| 旧 worker 镜像 | `easel-worker:4382180862e2` = `sha256:02ff3af7913a90c63677a6cfcc1602187465441fd7eb680537e74b73880c5b57` |
| 旧 gateway 镜像 | `easel-gateway:4382180862e2` = `sha256:5ea06bbef5a6c349e14c815239dc64683295c8d505d5d26fa13aa74596059eac` |

## 构建

- 命令：`docker build --progress=plain -f deploy/desktop/Dockerfile.control -t easel-control:503-r2-final --build-arg HTTP_PROXY=http://host.docker.internal:7897 --build-arg HTTPS_PROXY=http://host.docker.internal:7897 .`
- 退出码：0；完整日志见 `control-build-final.txt`。
- 构建只替换本地 `easel-control:503-r2-final`，未推送、未覆盖用户镜像。

## 本机验证

- 全量 `pytest tests/multiuser`：1796 passed, 7 skipped（见 `full-multiuser.txt`）。
- desktop/update 专项：10 passed；连契约测试共 13 passed（`desktop-update-tests.txt`）。
- 隔离真实更新：项目 `easel-r2-15aacce1`，退出 0；旧 control
  `9272b556…` → 新 control `fabe6b75…`；旧 provisioner PID 55704 →
  新 PID 57912；首屏 6/6、短聊天、Stop→Backup→Verify 通过。
- 真实故障重试：见 `fault-retry-run.txt` / `fault-retry-result.json`。
- 清理：随机项目容器/卷/网络已删除；备份目录已手工清理。
