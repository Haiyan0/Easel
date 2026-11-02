# 原 Web 修复 Task 2 首轮审计：模型链路通过，目录来源仍需单项收口

日期：2026-10-29（Asia/Shanghai）。
父基线：`bcf001a0ff5a9ba7242ba6f686a9ce7eb341081d`。
生产实施：`f785d444874c6a804bc27702e57cb430aeca1216`。
本次审查 tip：`68789d3a36c96b3d087774e5aafc7cd33c18e2f5`（证据说明修正）。
worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。

## 裁决

Task 2 暂不整体通过，仅 T2-R1-01 一项 P2 需修复。已验证的同事务模型固定、ACP modelId 映射、worker 签名元数据校验与 flash 受控 provider 链路结果保留，不重开已通过部分；不进入 Task 3，不切换用户实例。

本项依据原执行单“目录与实际 Harness 来自同一份固定配置、无有效模型/目录在接纳前明确拒绝”。当前实现将镜像目录和运行时目录的一致性留给部署方手填，尚未满足程序级保障；这不是将管理员配置错误扩写成新的安全威胁。

## T2-R1-01 / P2：运行时目录未绑定目标镜像目录，仍可接纳镜像不支持的模型

位置（实施 worktree）：

- `easel/control/snapshot.py:88` 起的 `_model_catalog`：只检查类型、大小、唯一性，允许任意格式正确的名称。
- `easel/control/cli.py:2397`：将 `agent.models` 直接作为 ConfigService/BrokerService 的运行时可信目录。
- `easel/control/config.py:539`、`easel/control/broker.py:692`：检查的是上述声明目录，而非已验证的固定镜像可用目录。
- 实际目录到 `easel/harnesses/acp.py:143` 的 session 目录匹配才检查，晚于接纳/派发。

镜像内 manifest/patch 已相互校验，这是正确改进；但 managed-agent.json 的 `agent.models` 与该目录没有自动关联。部署时手填一个镜像不含的模型后，两边再次分叉，发布/接纳仍被判为可用，直到执行才失败。实施 README 的部署前置明确要求人工保持这两份目录一致，证实当前没有该保证。

### 主审与独立复核

主审用正式 `load_managed_agent_config` 读取临时配置，模型目录仅含 `not-in-worker-image-catalog`；按 CLI 同源方式传入 ConfigService/BrokerService，再用真实 Store/WorkerRelay 和内存假 worker HTTP 接纳。当前固定 manifest 目录不含该模型：

```text
MODEL_IN_IMAGE_CATALOG=False
CONFIG_LOAD_ACCEPTED=True
PUBLISH_VERSION=1
PREFLIGHT_ACCEPTED=True
SUBMITTED=True
DISPATCHES=1
SIGNED_MODEL=not-in-worker-image-catalog
TEMP_CLEANUP=ok
```

探针：`docs/evidence/original-web-task-02-review/probe_catalog.py`，Agent Python 执行 exit 0。仅临时数据库/假 Key/内存 HTTP，不运行 Docker、真实 Harness 或供应商。两名独立复核分别确认同类目录漂移，且一项同样验证已派发。

### 最小收口

- 从现有受管 manifest/固定镜像的可信公共元数据取得实际可用目录，运行时配置只能声明其合法范围；允许部署方收窄策略，不允许声明镜像没有的模型。
- 在正式加载/装配或接纳前验证目录来源和目标 runtime 的绑定；目录缺失、漂移、不可验证时明确拒绝。不能仅新增一份同样靠手填的列表或 README 提醒。
- 可以复用共享 manifest/现有可信部署契约及摘要校验；具体最小实现由实施方选择，不要求新服务、另一套模型注册表或 Run 状态机。
- 红测走正式配置加载/装配到发布/接纳链路：运行配置含镜像不存在名称、镜像/公共目录版本不匹配均提前失败；没有 Run/token/prompt 派发副作用。合法相同目录及合法收窄正常；已固定旧 Run 的重试/恢复不因当前目录变化重签新模型。
- 不要求再次重做无变化的 flash/权限/停止整套演练；目录绑定影响的入口和实际构建资源需针对性补验。镜像内代码/资源改变时增量构建并核对源码，不无缓存重建全栈。

## 本轮已确认部分

- BrokerService.issue_with_route 的 token 与 EffectiveRoute 来自同一 Store 写事务；WorkerRelay 签名模型/允许列表从该 route 构造，未确认新旧混配窗口。
- 已有 Run 的幂等识别优先于最新配置读取，旧 token 的 authorize 不套用新目录；未确认跨 workspace 权限扩大或真实 Key 下发。
- ACP 按真实公布 select value 选择，模型分组、flat/旧未公布目录兼容检查通过；worker 校验 model/models/allowed_models 关系。
- 作者真实 Harness 目录探针记录实际 JSON modelId 和三次成功选择。新真实拓扑测试确实检查受控 provider 的全部 chat 请求模型及假 Authorization；两份实测日志各记录 1 passed。主审本轮审阅代码与证据，没有重跑容器。
- 证据里的 21 项源码哈希与本次源码一致（13 worker、8 control）；镜像内哈希仍作为作者实测证据，不冒称主审新读镜像。

## 主审新运行结果

在实施 worktree、指定 Agent Python：

```text
pytest tests/multiuser/test_managed_chat_model.py tests/multiuser/test_managed_profile.py tests/multiuser/test_relay_cli.py tests/multiuser/test_provider_adapters.py tests/multiuser/test_config.py tests/multiuser/test_proxy.py tests/multiuser/test_worker_runtime.py tests/multiuser/test_runs.py tests/multiuser/test_run_diagnostics.py -q
159 passed in 20.94s

pytest tests/multiuser/test_run_diagnostics.py tests/multiuser/test_runs.py tests/test_agent_harnesses.py tests/test_rpc_client_pressure.py -q
156 passed, 4 skipped in 30.44s

git diff bcf001a0 68789d3 --check：退出 0
```

两组有重叠，不合计。独立 config/broker/proxy 45 passed、ACP/worker 12 passed/15 deselected、profile 18 passed 等为重叠/相关复核，不作新的总计。四项既有平台 skip 不算真实环境通过。

作者真实 Harness/受控 provider 与浏览器权限/停止结果保留；用户真实 DeepSeek、用户实例更新、原 Web 与登录 UI 仍未验收。旧用户失败直接触发点保持 history-unavailable，不追溯伪造原因。

## 后续

只执行 [Task 2 R1](../tasks/2026-10-29-original-web-task-02-rework-r1.md)，基于 tip 继续原隔离 worktree。主审未合入 root、未操作用户容器/卷/Key。修复交付后复审，不进入 Task 3、不自动上线。
