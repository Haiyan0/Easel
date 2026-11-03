# Windows 验收实例全站工作区 503：只读诊断

日期：2026-11-02。用户部署源码 `C:\Code\Easel-acceptance`，HEAD `4382180862e28d43f2974e32a618093c847ec262`；工作树干净。主仓库 `C:\Code\Easel` HEAD `665f7e8`。

## 已核实现场

- 实际项目名 `easel`，不是手册示例 `easel-acceptance`。control 为 `easel-control-1`，回环端口 7863。所有后续命令必须从实际 deployment/登记参数取值，不能照抄示例项目名。
- 登记文件 `C:\Easel\acceptance-data\logs\deployment.json`：source_commit=4382180，publish_mode=manual，workspace_root=`C:\Easel\acceptance-workspaces`。
- 镜像：control `9272b5562d2d`（这里只保存 Docker 展示的短 ID）；worker `sha256:02ff3af7913a90c63677a6cfcc1602187465441fd7eb680537e74b73880c5b57`；gateway `sha256:5ea06bbef5a6c349e14c815239dc64683295c8d505d5d26fa13aa74596059eac`。
- workspace `bf8e8bb9133146cebfe193f970d8f70a`；worker `966e396e7551bb1a0bf425c0989707a30ce78ba6cb71dc28d33691e8aa022899`；gateway `0e32b01dc578436eda2cca354e1e6d52bf3467c4c29771ae4781da6cae0cf79e`。容器标签与 DB 实例 ID 相同。
- control 日志中 `/api/personas`、`/api/outputs`、`/api/accounts`、`/api/ideas`、`/api/schedule`、`/api/upload/limits` 及 POST `/api/chat` 均出现 503。worker 生命周期健康检查与曾到达 worker 的 `/api/personas`、`/api/status` 返回 200。
- 使用 control 容器内 SQLite URI `mode=ro` 直接查询，未调用 Store/迁移：worker lease 为 state=stopping、generation=2、gateway_state=staged、config_version=1；cleanup_purpose=start_abort、cleanup_scope=gateway、cleanup_worker_created=0、cleanup_gateway_created=1；attempt_id=`7ceba07272dc466cafc60e4574c6da3a`。runs 表没有记录。
- 只通过现有 provisioner socket 执行 `version=2, action=inspect`：worker_ready=true，gateway.active=true、available=true；目标 worker/gateway 身份一致。未执行 ensure/activate/abort/stop。
- 只读文件 `C:\Easel\acceptance-workspaces\.gateway\attempt-bf8e8bb9133146cebfe193f970d8f70a.json`：attempt_id=`6d66e64c3d3a4c4a95e38c1a5b701367`，worker/gateway ID 与上方一致，worker_created=false、gateway_created=false、scope=gateway、status=pending。它与 DB 清理承诺不一致，且两种 created 均为 false 的记录不符合 `_load_attempt` 重启读取校验。

## 受控复现

运行 [probe_attempt_overwrite.py](probe_attempt_overwrite.py)，分别以验收源码 4382180、根目录合并源码 665f7e8 为输入。均只在 TemporaryDirectory 使用假 ID/token，不访问 Docker：

```text
original_attempt_overwritten=true
original_abort_error=gateway_abort_superseded
record_loadable_after_restart=false
TEMP_CLEANUP=ok; LIVE_RESOURCES_TOUCHED=false
```

代码路径：`Provisioner._with_gateway` 在 attempt_id 非空时无条件 `_record_attempt`；后来的纯复用调用也可覆盖此前 pending 清理归属。`abort` 精确校验 attempt/实例/created/scope，旧承诺因此被拒；control 的 `_recover_stop` 不能凭空越过该校验。

## 裁决

已确认：实际运行态与持久清理承诺分叉、attempt 覆盖/重启无法读取缺陷；这是代码与恢复协议问题，不能只改模型配置、删卷或重启了事。导致最初进入清理的准确请求交错还需受控回归复现，不能凭当前静态现场推定完整时间线。

4382180 不含 6A 的串行/重试/status 修正；665f7e8 已包含，但本探针证明它仍有 attempt 问题，且串行锁排队预算另有待修 R1。因此不能只要求用户换根目录重建就声称修好。

另：`/api/status` 把 worker 的 gateway=false 透传、原工作台提示安装 control CLI 是已知次级误导，需随修复验证；不能代替后端恢复。真实 deepseek-flash 可用性和实际聊天尚未在本次验证；截图“主聊天已配置”只证明配置已保存。

本轮未重发用户 prompt、未发供应商请求、未操作真实平台、未写用户 DB/attempt/Key/Cookie，未重启或删除容器/卷/网络。原现场保留，交代码 Agent 按定点执行单修复后独立复审。
