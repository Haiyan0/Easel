# R2 证据事实清单

## 代码与镜像

| 项 | 值 |
|---|---|
| 父基线 | `43e4cd01bee0421226a193d54f6d4e09f045ff9e` |
| R2-01 提交 | `a2f8ed1` |
| R2-02 提交 | `528e41c` |
| R2-03/04/05 提交 | `5f5be50` |
| 新版 control 镜像 | `easel-control:503-r2` = `sha256:9d18ec91326e4d2500fdb5cbec52abc56e04ae5b50765050cec46b8cdfeb03c3` |
| 镜像内 `easel/control/runtime.py` SHA-256 | `dba29bb1445ff974bc43553284240de3e13ca13f212e0faaa5f97f6f4b92de23` |
| 工作树 `easel/control/runtime.py` SHA-256 | `dba29bb1445ff974bc43553284240de3e13ca13f212e0faaa5f97f6f4b92de23` |
| 工作树 `deploy/multiuser/provisioner.py` SHA-256 | `0d89fc73cdc15007d3315b4adfd66337b4043526dd2e7f07085dcdb8de62c89f` |
| 工作树 `easel/control/store.py` SHA-256 | `36641f704fffcf32314ffc4539843e1785ecba6eb62365667cc81232f16676dc` |
| 工作树 `easel/control/cli.py` SHA-256 | `6efe05ddb72f404ac9752ebd1213ae72c361b7b15a61c46ebda0f988f62e6e90` |
| 用户实际旧版本源码 | `C:\Code\Easel-acceptance`（4382180，detached） |
| 旧版本 `easel/control/runtime.py` SHA-256 | `d37efde6591d3477c0d848d798ab1eb749ea081dd93f823f98f3b122d5722368` |
| 旧 control 镜像 | `easel-control:4382180862e2` = `sha256:9272b5562d2d9bfbdeef1d60e3ccb910dc5f90ccc24e79802ad9178548d769d2` |
| 旧 worker 镜像 | `easel-worker:4382180862e2` = `sha256:02ff3af7913a90c63677a6cfcc1602187465441fd7eb680537e74b73880c5b57` |
| 旧 gateway 镜像 | `easel-gateway:4382180862e2` = `sha256:5ea06bbef5a6c349e14c815239dc64683295c8d505d5d26fa13aa74596059eac` |

## 构建

- 命令：`docker build --progress=plain -f deploy/desktop/Dockerfile.control -t easel-control:503-r2 --build-arg HTTP_PROXY=http://host.docker.internal:7897 --build-arg HTTPS_PROXY=http://host.docker.internal:7897 .`
- 退出码：0；完整日志见 `control-build.txt`。
- 构建只替换本地 `easel-control:503-r2`，未推送、未覆盖用户镜像。

## 本机验证

- 全量 `pytest tests/multiuser`：1794 passed, 7 skipped（见 `full-multiuser.txt`）。
- 隔离真实更新：项目 `easel-r2-fc0ebfac`，退出 0；旧 control
  `9272b556…` → 新 control `9d18ec91…`；旧 provisioner PID 44132 →
  新 PID 46362；首屏 6/6、短聊天、Stop→Backup→Verify 通过。
- 真实故障重试：见 `fault-retry-run.txt` / `fault-retry-result.json`。
- 清理：随机项目容器/卷/网络已删除；备份目录已手工清理。
