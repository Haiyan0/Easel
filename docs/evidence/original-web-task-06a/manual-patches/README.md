# Task 6A 运维手册候选补丁与变更说明

本目录只保存候选补丁，**未修改** root 权威文件：

- `C:\Code\Easel\docs\windows-desktop-operations.zh-CN.md`
- `C:\Code\Easel\docs\linux-docker-operations.zh-CN.md`

补丁以 root 当前版本为基线生成，路径前缀为 `a/docs/...` / `b/docs/...`。
请主会话复审后，在 root 仓库用 `git apply` 应用；实施 worktree 中的旧版手册
未作为输入，也不会用来覆盖 root。

## 1. Windows 手册

补丁：[windows-desktop-operations.zh-CN.md.patch](windows-desktop-operations.zh-CN.md.patch)

| 位置 | 变更 | 原因 |
|---|---|---|
| 标题下版本行 | `1a88110…` / 2026-10-29 → `4382180…` / 2026-11-02 | 与 6A 最终输入一致 |
| 引言 | 删除“未装配的产物预览/下载/发布可能返回 503” | 5A/5C3 已接通；保留“真实供应商/平台仍需授权验证”的边界 |
| 第 3 节 managed-agent.json | 增加 `agent.models` 三项目录 | 与实际 `acp-profile.json` 的 `provider.models` 一致；解释“配置可用”不等于真实供应商已验收 |
| 第 5 节 Start 前 | 说明全新部署默认 automated、manual 需显式 `-PublishMode`、旧登记缺策略必须显式迁移 | 5C3 R1/R2 的发布策略迁移语义 |
| 第 6 节聊天配置 | 模型必须来自受管 manifest；服务端版本化路由 | 防止把浏览器本地配置误当受管模型 |
| 第 7 节 Status | `version_*` 补充 `version_publish_mode` | 实际 Status 输出字段 |
| 第 12 节错误表 | 增加 `publish_mode_migration_required`、`deployment_publish_mode_invalid` | 5C3 策略迁移失败关闭的可操作错误 |

未改：邀请一次性、管理员无单独登录 URL、Stop/备份/恢复/升级步骤、密钥与
数据保护边界。命令参数逐项对照 `deploy/desktop/*.ps1` 与实际 Status 输出。

## 2. Linux 手册

补丁：[linux-docker-operations.zh-CN.md.patch](linux-docker-operations.zh-CN.md.patch)

| 位置 | 变更 | 原因 |
|---|---|---|
| 版本行 | 更新为 `4382180…` / 2026-11-02 | 与 6A 最终输入一致 |
| 第 1 节 | 明确 Windows 隔离演练不改变 Linux 的 XFS/防火墙/IPv6/Chromium seccomp/systemd/真实供应商 not-run 结论 | 防止把 Windows 结果复制成 Linux 生产承诺 |
| 第 9.2 节环境变量表 | 补充 `EASEL_PUBLISH_MODE` | `compose.workspace.yaml` 将其设为必填；缺失会直接拒绝启动 |

未改：Linux 原生生命周期仍标注“没有等价已验收包装层”；`drain` /
`stop-complete` / `backup` / `restore` / `materialize-restore` 的未支持边界、
XFS 配额前置、rootless/firewall/IPv6 门槛保持原文。没有新增“可直接生产上线”
的命令或脚本。

## 3. 验证方式

```powershell
# 补丁可解析（在 root 仓库、干净工作树或先备份）
git apply --check docs/evidence/original-web-task-06a/manual-patches/windows-desktop-operations.zh-CN.md.patch
git apply --check docs/evidence/original-web-task-06a/manual-patches/linux-docker-operations.zh-CN.md.patch
# 手册内相对链接逐项存在
Test-Path C:\Code\Easel\deploy\multiuser\egress-policy.md
Test-Path C:\Code\Easel\docs\superpowers\audits\2026-10-29-desktop-a7-accepted.md
```

补丁内容不包含真实 Key、Cookie、邀请、用户路径或内部实现细节。
