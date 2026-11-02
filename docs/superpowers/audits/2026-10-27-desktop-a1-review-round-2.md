# Task A1 R1 复审：定点返工 R2

父提交：`1cee445e3b6334a1d8c3adba34f669d470d745dc`。
审查提交：`a2b83faf39e8588ecc30e27eee4d0f065a537c40`。

## 结论

上轮 A1-R1-01/02 的归属检查与诊断假绿已修复。仍有一项诊断失败路径的资源访问缺陷，修复后再关闭 A1；不进入 A2。不要求重做整轮登录验证或扩展其它功能。

## A1-R2-01（P2）：实际 key 卷来源已判失败，仍进入 helper 挂载

位置：`deploy/desktop/Test-Easel.ps1:191–200`。

`key_volume_source` 等检查仅存入 checks，真正汇总失败在 helper 执行之后。helper 的准入条件只有实际 keyVolumeName 非空，并以该 Name 执行 docker run，而不是要求来源与预期卷契约先通过。

结果：预期 control/key 卷标签合法，但实际容器的 /keys 指向另一项目卷时，诊断最终虽返回非零，仍已把未经归属检查的外来卷挂到 helper 内读取 master.key，且挂载未声明 readonly。这违反诊断不得访问其他项目持久资源的边界。已有测试只断言最终失败，遗漏失败前的 helper 调用。

主审最小反例（临时 Docker 桩 + 真实 PowerShell Test 脚本，无真实 Docker 资源操作）：

```text
DIAGNOSTIC_EXIT 1
HELPER_MOUNT ['type=volume,src=foreign-unowned-key-volume,dst=/keys']
FOREIGN_VOLUME_INSPECTED False
SOURCE_FAILURE_REPORTED True
```

独立审查确认相同调用路径。

### 修复范围

- 运行中容器的结构、身份、命令、挂载来源/类型/读写、发布端口等静态契约未通过时，先报告失败并结束，不运行 key helper。
- 静态检查通过后，helper 只挂载已验证的预期 `$volumes.Keys`，显式 `readonly`；不从未经验证的实际 Name 生成 Docker 操作目标。
- 保留实际挂载来源与预期卷一致性的检查，不能改成只验证预期卷来忽略真实容器。
- 不修改认证核心、Compose 拓扑、key 生成逻辑或其它既定任务。

### 红绿验收

1. 正确预期卷 + 实际外来 key 卷：诊断非零，调用日志没有任何 docker run、卷创建、启动/停止/删除动作。
2. bind 替代、key 可写、错误项目/service/命令/Origin 或非法容器结构：同样在 helper 前拒绝。
3. 合法容器：诊断通过；唯一 key helper 的 mount 使用预期 key 卷且带 readonly。
4. 跑 Desktop + auth/admin/config 指定回归、文档路径、diff check。
5. 使用最终固定镜像复跑合法真实 Test，确认 readonly key 检查能通过；应用/镜像内容未变化不要求再次执行 ngrok 双身份场景。

## 本轮主审验证

- Desktop + auth API + admin API + config API：111 passed，34.49 秒。
- 文档路径：10 passed，0.16 秒。
- `git diff 1cee445 a2b83fa --check`：退出 0。
- 上述新反例已实际运行；未重建镜像或启动公网隧道。实施方本轮实际 Docker init/start/Test/重开证据保留，不冒称主审重复执行。
- 原 ngrok 免费固定域名限制与 A7 密钥丢失恢复项保持原结论。

## 给代码 Agent

仅修 A1-R2-01，先补失败前调用门禁测试，再修诊断顺序与 readonly helper 挂载。保护已有主审文档。显式路径提交，报告父/新 SHA、红绿证据和最终镜像实际合法诊断结果，停止等待复审，不进入 A2。
