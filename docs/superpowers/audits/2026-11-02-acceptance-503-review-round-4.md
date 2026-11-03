# 验收实例503修复R3独立复审

父基线 `c9c0f0a`，交付 `ac863e22aaa2c2f27ad9e12742d653d8daf2bfca`，实际源码 `C:\Code\Easel-acceptance`、分支 `fix/acceptance-503-r3`。原 repair worktree 仍停在 c9c0f0a，未将其误当本轮源码。

裁决：**R3执行单两项通过，关闭R3-01/R3-02；整体验收与更新用户实例仍暂缓。** 报告主动延后的正常 ready 刷新 P1 由主审复现，另下发 [R4单项收口](../tasks/2026-11-02-acceptance-503-rework-r4.md)。不把已经完成的R3限定场景重新判失败，也不能把仍可稳定触发的同类永久503当作可部署状态。

## 本轮通过依据

- 主审原样运行 `probe_r2_lost_ensure_error.py`，首次超时后保留 starting，第一次重开恢复成功，resolve_calls=1。原探针编码红态断言，退出1符合预期；第二次固定超时来自其Adapter，不作为新的独立缺陷。
- 主审原样运行 `probe_r2_update_late_request.py`，登记处抛 admission_closed，未起上游媒体传输。原红态探针退出1符合预期。
- 主审实际运行作者列出的12个受影响文件：**248 passed，4 skipped，58.82s，exit 0**，包含attempt/runtime/Workspace API/artifact/gateway client/task leases/stop barrier/desktop update/start identity/proxy/messages。
- 两名独立审查者分别核对runtime未知结果恢复及请求原子屏障。屏障检查与请求登记在同一BEGIN IMMEDIATE；WorkspaceApi新请求和messages要求接纳，既有Run收尾仍可运行。生产db_runner的functools.partial支持kwargs，未发现该透传缺陷。
- `git diff c9c0f0a..HEAD --check`无输出，实施工作树干净。前端、Update脚本本轮未改，未重复构建/演练。
- 作者全multiuser的5个frontend_build_missing失败已如实记录；主审未重跑全量或逐一验证其基线归因，不写为全量通过。作者其他61项所谓无关失败未逐项核定，也不作为本轮已通过的测试。
- 镜像与4382180更新演练引用作者证据；本轮主审未运行Docker镜像核验或更新链路，不冒充独立复跑。

## 唯一保留部署阻塞：R4-01 / P1 正常ready刷新未知结果仍失去归属

位置：`easel/control/runtime.py:579` 的未知结果处理仅作用于 `lease.state == 'starting'`。正常ready租约因gateway失效走 `_ready_gateway` → `_refresh_gateway` 时，新attempt在远程ensure成功返回后才落入DB；若远端已写G2/new-attempt/pending但回复超时，`:606`仅标gateway unavailable，DB继续是ready/G1/old-attempt。

后续首屏使用旧G1/旧attempt补commit，不能收口新pending；新ensure又被provisioner精确pending归属拒绝。该问题与R3限定的过期starting不同，但属于相同用户目标的日常故障恢复，不能接受其永久503后仍建议更新。

主审独立运行 [probe_r3_ready_refresh_loss.py](../../evidence/acceptance-503-review/probe_r3_ready_refresh_loss.py)：真实RuntimeManager先把fixture收口为正常ready，再通过真实ensure_ready刷新入口模拟G2写pending后超时；只使用临时Store/attempt与假远程，不调用Docker/用户资源。

```text
1201: provisioner_timeout，DB ready/G1/old-attempt，远端pending/G2/new-attempt
1202: worker_start_failed，底层gateway_cleanup_pending，身份未变
2000: worker_start_failed，底层gateway_cleanup_pending，身份未变
```

最后一次已跨租约期限，排除仅等待即可恢复。假Adapter直接调用服务对象，所以外层泛化为worker_start_failed；探针同时断言底层异常确为gateway_cleanup_pending。临时目录自动清理。

## 边界

R3请求屏障、既有Run收尾、连接阶段与已发出阶段错误分类保持关闭；R4不重开更新脚本、前端、平台或全量环境修复。没有合并、推送、操作用户容器/DB/Key/Cookie/attempt，也没有真实模型/平台请求。
