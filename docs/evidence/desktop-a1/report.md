# Task A1 登录管理面真实运行证据

日期：2026-10-26（Asia/Shanghai）

## 结论

登录管理面已在 Windows 11 + Docker Desktop + ngrok HTTPS + Chromium 上实际运行并完成双身份验证。以下结果只证明 Task A1 的登录/成员/会话目录管理面，不证明执行 worker、真实 Harness、聊天闭环或正式 Linux 发布门槛。

## 环境与边界

| 项目 | 实际值 |
|---|---|
| 代码父提交 | `290d94a68b9a4c1e8e3f627b5c96fcbdb8f11f1b` |
| 主机 | Windows 11 Pro 10.0.26200 |
| Docker | Docker Desktop，Engine 29.2.1，Linux containers/amd64 |
| control 镜像 | `sha256:d06578a3b011f6b06e9b3781414aae1757bbe41ffba7e7f3479e3302d4d3b49f` |
| control 容器用户 | `10001:10001` |
| 宿主端口 | `127.0.0.1:7861`（仅回环） |
| ngrok | 3.39.11，独立 HTTP 隧道到 `127.0.0.1:7861` |
| 浏览器 | Chromium 151.0.7922.34，headless，独立 context，未全局忽略证书错误 |
| worker | 未启动；聊天/历史接口预期 503 |
| 测试项目 | `easel-a1-evidence-20261026183821`（随机专用 Compose 项目） |

control 容器只挂载 `control` 数据卷与 master key 卷；没有 Docker socket、worker 根、开发者 HOME、浏览器 profile 或 outputs 目录。ngrok 只把公网 HTTPS 转发到宿主回环端口。

## 实际检查

| 检查 | 结果 |
|---|---|
| 管理员通过 ngrok HTTPS + 一次性邀请登录 | 通过 |
| bootstrap 邀请重复兑换 | 401 |
| A/B 独立浏览器 context | 不同 userId/workspaceId |
| A/B 会话目录互不可见 | 通过 |
| 跨 workspace 会话读取 | 404 |
| member 调用管理员 API | 403 |
| 未装配 worker 的聊天提交 | 503 `capability_unavailable` |
| 注销后旧登录 | 401 |
| 禁用成员后旧登录 | 401 |
| 停止 control 后重复初始化 | `key=exists`，未覆盖 key |
| 同一数据卷重开后管理员重登 | 同一 admin userId |
| 同一数据卷重开后 A 会话 | 保留并在 UI 可见 |
| 旧 ngrok 域名 Cookie 到新 Origin | 401（host-only Cookie 未发送） |
| 旧 Origin 写请求到新 control | 403 |
| 新 Origin 用新邀请登录 | 成功，userId 未变 |
| 初始 ngrok 证书 | Chromium 正常校验公开 CA |
| Origin 变更子场景证书 | 本地 TLS 代理 + Chromium SPKI pin；非公开 CA |

## Origin 变更限制

当前 ngrok 账户是免费套餐，只提供一个固定静态域名。实测第二次 `ngrok http` 被拒绝：

- `ERR_NGROK_334`：同一 endpoint 已在线；
- `ERR_NGROK_313`：免费套餐不允许自定义子域；
- 匿名隧道被拒绝（需要账户凭据）。

因此**真实 ngrok 公开地址变化标记为 not-run**。为避免把本地结果冒充 ngrok，Origin 变更子场景使用 `https://127.0.0.1:8443` 的本地 TLS 反向代理，并在 Chromium 启动参数中只 pin 该测试证书的 SPKI；没有使用全局 `--ignore-certificate-errors`，初始 ngrok 页面仍走公开 CA 校验。该子场景证明 host-only Cookie、Origin 精确校验和重新登录行为，但不证明 ngrok 域名切换。

若主审要求真实 ngrok 地址变化，需要在 ngrok 账户中准备第二个静态域名/付费自定义域，或提供第二个独立 ngrok 账户；这属于账户资源变更，未在本次自动执行。

## 最终脚本校验（归属标签）

在加入卷归属标签和 `Test-Easel` 镜像 ID 匹配检查后，重新构建同源镜像并单独实测：

| 项目 | 实际结果 |
|---|---|
| 最终脚本校验镜像 | `sha256:c4a4c7f24462addff841f9b5b360e97654bd4cebaf89c48f113bcc57f8c4e6d1` |
| 临时项目 | `easel-a1-finalcheck-20261026184907` |
| 卷标签 | `control`/`keys` 两个卷均有本项目 `easel.desktop.project` 与角色标签 |
| `Test-Easel.ps1` | 12 项通过，含 `image_matches`、UID、只读根、cap drop、挂载、无 Docker socket、回环端口、健康与 key 存在 |
| 清理 | 仅删除该随机项目容器/卷；无残留 |

镜像 ID 会因 Docker 重新构建而改变（同一源码、同层内容），因此表中同时保留浏览器证据镜像和最终脚本校验镜像；实际部署必须使用构建后 `docker image inspect` 得到的固定 ID，而不是可移动 tag。

## A1 R1 定向返工（2026-10-27）

### R1-01：Start/Test 卷归属复核

`Start-Easel.ps1` 和 `Test-Easel.ps1` 现在在任何 helper `docker run` / Compose 操作前调用共用卷契约：

- 原始 `docker volume inspect` 必须非零退出以外的合法 JSON；
- 恰好一个对象，含 `Name`/`Labels`；
- 实际 `Name` 等于本项目卷名；
- `easel.desktop.project` 等于项目名、`easel.desktop.role` 分别为 `control`/`keys`；
- 空/多对象、缺标签、错项目/角色、非法 JSON 明确拒绝，不自动 create/chown。

反例测试覆盖 control/keys 外来标签、缺标签、错角色、实际 Name 不符、非法/空/多对象；断言失败时无 helper run、无 Compose up、key 不改变。

### R1-02：诊断实际接线

`Test-Easel.ps1` 现在校验运行容器的实际契约，而不是只比较目标路径：

- Compose 项目/service 标签；
- 合并 `Entrypoint`+`Cmd` 后的完整 `serve-control` 命令，包含实际 Origin、control/key/前端目录和 7861；
- 恰好两个 volume 挂载，实际卷 Name 正确，control RW、key 只读，拒绝 bind 替代和额外挂载；
- `NetworkSettings.Ports` 仅允许 `127.0.0.1:7861` 一个发布端口；
- key 检查使用实际 key 挂载卷；
- inspect 为空/多对象/缺结构/非法 JSON 失败。

反例覆盖错挂载来源、bind 替代、key 可写、Origin 错、命令错、Entrypoint 错、项目/service 错、额外端口、空/多/非法 inspect。

### R1 红绿与真实 Docker 复跑

| 项目 | 实际结果 |
|---|---|
| 旧脚本（`1cee445`）跑当前 44 项 Desktop 测试 | **28 failed、16 passed**，37.39 s |
| 当前脚本跑同 44 项 | **44 passed**，27.41 s（红后立即恢复并复跑） |
| R1 最终固定镜像 | `sha256:3df01c1d2072a05c757e2d00bc43a31bcff93d2db5622dd5654ff9f90975e335` |
| 真实外来卷直接 Start | exit 1 `volume_not_owned`；无容器创建 |
| 真实合法项目首次初始化/启动 | `key=created`；start `health=healthy`；20 项诊断全部通过 |
| 真实停止/重开 | 停止后无运行容器；再次初始化 `key=exists`；start healthy；20 项诊断再次通过 |
| 清理 | 删除随机测试容器/卷；无残留，未使用全局 prune |

A1 R1 只修改脚本、测试和文档，未改变镜像应用内容/依赖；审计单允许保留已有 A1 浏览器证据，因此没有重做 ngrok HTTPS 双身份浏览器场景。真实 Docker 复跑使用同一最终镜像完成了 init/start/Test/重开。逐项失败/通过测试 ID 见 [r1-red-green.md](r1-red-green.md)。

## 截图

- `01-admin-members.png`：管理员设置页（无邀请明文）
- `02-member-a-sessions.png`：A 的独立会话目录
- `03-member-b-sessions.png`：B 的独立会话目录
- `04-admin-new-origin.png`：新 Origin 管理员重新登录
- `05-member-a-new-origin.png`：新 Origin A 重新登录；聊天历史显示预期 503

截图不包含邀请令牌、登录 Cookie、CSRF、master key 或 ngrok 访问令牌。

## 启动、停止、重开

完整命令与边界见 [Windows 手册第 9 节](../../windows-desktop-operations.zh-CN.md#9-windows-上的多人验证做到哪一步)。摘要：

```powershell
# 构建
docker build -f .\deploy\desktop\Dockerfile.control -t easel-control:a1 .
$ControlImage = (docker image inspect easel-control:a1 --format '{{.Id}}').Trim()

# 初始化专用卷/key（重复执行显示 key=exists）
.\deploy\desktop\Initialize-Easel.ps1 -Image $ControlImage -ProjectName $ProjectName

# 另开窗口：ngrok 指向 127.0.0.1:7861；本机只发布回环
ngrok http http://127.0.0.1:7861 --inspect=false

# 启动（启动前校验 Origin/固定镜像/卷/key/镜像内前端/Compose）
.\deploy\desktop\Start-Easel.ps1 -PublicOrigin $PublicOrigin `
  -Image $ControlImage -ProjectName $ProjectName

# 只读诊断
.\deploy\desktop\Test-Easel.ps1 -PublicOrigin $PublicOrigin `
  -Image $ControlImage -ProjectName $ProjectName

# 停止
docker compose -p $ProjectName -f .\deploy\desktop\compose.login.yaml stop

# 重开：再次 Initialize + Start；不要重复 bootstrap 已有管理员
```

## 清理

本次证据项目 `easel-a1-evidence-20261026183821` 已按精确名称清理：

- control 容器 `easel-a1-evidence-20261026183821-control-1` 已删除；
- 专用卷 `easel-a1-evidence-20261026183821-control`、`...-keys` 已删除；
- 测试镜像 tag `easel-control:a1` 已删除（镜像 ID 保留在上表供追溯）；
- ngrok 进程已停止；临时 TLS 证书与临时 ngrok 配置目录已删除；
- 未执行全局 prune，未操作其它项目容器、卷或网络。

清理后按 `name=easel-a1-evidence` 检查，容器和卷均为空。

## 回归

见交付说明与 `docs/windows-desktop-operations.zh-CN.md` 9.8。本次实际后端回归 111 passed（Desktop 44 passed）；完整 `tests/multiuser` 1048 passed、4 skipped；前端 lint/build 和 Compose 静态校验通过。

## 不属于 A1 的剩余项

- 执行 worker、真实 Harness、真实供应商和聊天闭环（A2 以后）。
- `/api/outputs` 等工作区业务仍 503。
- 真实 ngrok 地址变化（受免费固定域名限制，not-run）。
- 正式 Linux 防火墙/IPv6、XFS 配额、最终 runtime/真实 DSH 和 Chromium 沙箱发布门槛。
