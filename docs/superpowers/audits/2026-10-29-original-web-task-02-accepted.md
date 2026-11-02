# 原 Web 修复 Task 2 验收：受管模型链路与目录来源通过

日期：2026-10-29（Asia/Shanghai）。
父基线：`bcf001a0ff5a9ba7242ba6f686a9ce7eb341081d`。
首轮生产：`f785d444874c6a804bc27702e57cb430aeca1216`；证据 tip `68789d3a36c96b3d087774e5aafc7cd33c18e2f5`。
最终验收：`4bb993bb353f23f5eec06442d2f6ce1c6c2e3090`。
worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。

## 裁决与范围

Task 2 验收通过，T2-R1-01 关闭。按原返工单允许的“共享受管 manifest/可信公共元数据”最小实现裁判，不追加新调度服务或额外握手要求。下一项仅 [Task 3](../tasks/2026-10-29-original-web-task-03-shared-web.md)。

通过的是同版本镜像组中的受管模型链路与隔离真实 Harness + 受控 provider 验证。用户真实 DeepSeek、用户实例更新、原 Web/UI/登录尚未验收，不自动切换用户服务。

## R1 关闭依据

- control Dockerfile 复制与 worker 同源的公共 managed manifest，不再只信运行时手填列表。
- `easel/control/cli.py:2425` 从受管 manifest 加载可信基线；`:2431` 拒绝运行目录越界，`:2436` 继承或合法收窄。
- 加载器验证 schema、Harness commit 形状、provider id/api、默认模型与有界唯一模型目录。显式传入 expected-harness-commit 时核对目标版本，缺失/无效/不匹配拒绝。
- 失败在 Store.connect/uvicorn.run 前，无控制库、Run、token 或 prompt 副作用。独立复核确认越界失败时 Store.connect 调用 0 次，master/signing 字节不变。

主审正式 CLI 探针（临时假 Key/目录、uvicorn 捕获替身）：

```text
outside_EXIT=2 DB_EXISTS=False
inherited_EXIT=0
inherited_EFFECTIVE=[deepseek-chat,deepseek-flash,deepseek-v4-pro]
narrowed_EXIT=0
narrowed_EFFECTIVE=[deepseek-flash]
TEMP_CLEANUP=ok
```

旧主审 probe_catalog.py 直接构造服务对象，未走新增的正式 CLI manifest 校验边界，不能用它绕过边界后仍成功来否定本项；本次复审针对用户实际支持的装配路径验证。

部署仍需成组使用同源 control/worker 镜像，不能把控制镜像的 manifest 视为对任意其它 worker 镜像的远程探测。跨版本目标的 expected commit 对照为可选功能，启用时失败路径已覆盖；它不代替整套镜像组版本记录。Task 6 的部署配置/文档须说明或传入目标版本，避免混用旧 worker 和新 control。此为部署前提，不新增另一项实现阻塞。

## 模型固定与兼容

首轮同事务签发 token/EffectiveRoute、签名模型固定、ACP 精确 modelId 映射和 worker 元数据校验保留。R1 不改状态机/token 事务/ACP 映射。

独立真实 Store + relay 探针：flash Run 派发后发布 v4 并收窄新目录到 v4，再次提交同请求仍为同一 Run；旧快照不变、旧 token 仍授权 flash、HTTP 派发总数 1。将新 config_provider 改成抛错也不触发它，证明既有幂等恢复未读取新配置重签。

## 本轮主审新验证

```text
Agent Python -m pytest tests/multiuser/test_managed_chat_model.py tests/multiuser/test_managed_profile.py tests/multiuser/test_relay_cli.py tests/multiuser/test_provider_adapters.py tests/multiuser/test_config.py tests/multiuser/test_proxy.py tests/multiuser/test_worker_runtime.py tests/multiuser/test_runs.py tests/multiuser/test_run_diagnostics.py -q
166 passed in 18.45s

Agent Python -m pytest tests/multiuser/test_managed_chat_model.py -k 'serve_control or load_managed_profile_models' -q
7 passed, 27 deselected in 0.92s

git diff 68789d3 4bb993bb --check：退出 0
```

两组重叠，不合计。独立 CLI/目录复核 16 passed/31 deselected（1.21 秒）及旧 Run 探针亦为补充验证，不作总数相加。

作者 R1 红 6 failed/28 passed、扩展 179 passed、Task 1 四文件 156 passed/4 skipped 保留为作者证据。Task 1 相关生产代码本轮未变，主审不无理由重跑其整套；本轮已包含 diagnostics/runs 相关项。

## 真实证据与运行边界

已审阅作者 R1 实测日志：

- 真实拓扑 `1 passed, 1 deselected / 74.92s`：受控 provider 9 个 chat 请求全为 flash，假 Authorization，resume、A/B、权限与 provider 401/429/500/drop。
- 浏览器权限/停止 `1 passed, 1 deselected / 83.66s`：allow 2、deny 2、hang cancelled、注销流撤销。
- 镜像组：control `sha256:2acb2485b07dbc65732c0c9996d987922a9d98dbe4fc9a14312217011b46e745`；worker `sha256:c44c786aa4ef70734ca878fdd9e5ca90c390acf907fc4598b42b042a62c80195`；gateway 沿用既有固定镜像。

这些是作者实测，主审本轮未运行 Docker 或重复真实拓扑。主审与独立复核只用临时存储、假凭证及内存/测试 HTTP，没有调用供应商或触碰用户实例；临时目录已清理。

root 仍在原 `1a881109`，本轮只更新主审文档，未 cherry-pick、未发布镜像。下一项继续原隔离 worktree；阶段通过不代表原 Web 已恢复或用户真实聊天已成功。
