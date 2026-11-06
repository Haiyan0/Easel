# Windows 多人版局域网访问

同一局域网用户可经主机 IP 的 HTTPS 入口访问 Easel，无需经过 ngrok。继续使用原来的账号、工作区和数据，新地址首次需要重新登录。

`0.0.0.0` 仅用于监听配置，不能发给同事作为访问地址。局域网入口形如 `https://192.168.14.118:7861`，后端继续只发布到回环地址。代理不改写 Origin，不降低 Cookie 或权限要求。当前仅支持一个登录来源，切换后原 ngrok 地址不再支持登录与写操作。

## 首次准备

已有可运行的 Desktop 多人部署，包括 `logs/deployment.json`、`logs/provisioner-expected.json`。不要重新 Initialize 或创建新数据卷。

在主机准备原生 Windows Caddy。本功能使用 **2.11.9 windows amd64**，可从 [官方发布页](https://github.com/caddyserver/caddy/releases/tag/v2.11.9) 下载压缩包；使用同版本 `checksums.txt` 中的 **SHA512** 校验后解压，将 `caddy.exe` 放到 `C:\Easel\tools\caddy.exe`，或通过 `-CaddyExe` 指定路径。

先只读生成配置，再启动（以下为当前主实例数据目录）：

```powershell
cd C:\Code\Easel
powershell.exe -NoProfile -ExecutionPolicy Bypass -File deploy/desktop/Start-LanEasel.ps1 -DataRoot C:\Easel\main-data
powershell.exe -NoProfile -ExecutionPolicy Bypass -File deploy/desktop/Start-LanEasel.ps1 -DataRoot C:\Easel\main-data -Apply
```

自动选择带网关的实体网卡 IPv4；多网卡存在歧义时显式加 `-LanAddress 192.168.14.118`。只接受 RFC1918 私网地址，不接受 VPN 的 `198.18.*`、回环或监听通配地址。HTTPS 默认端口 7861、代理管理端口 17861 必须与 deployment.json 中后端端口各不相同；可用 `-HttpsPort`、`-AdminPort` 调整。管理接口始终只监听本机回环。

脚本会按原来的固定镜像、数据目录、发布策略和数据库查询接线调用现有启动入口。切换会重建 control，请在无正在执行的任务时运行；不会升级镜像或迁移账号。首次来源保存在 `DataRoot\lan\previous-origin.txt`。

## 主机防火墙

在**管理员 PowerShell** 中执行：

```powershell
cd C:\Code\Easel
.\deploy\desktop\Set-EaselLanFirewall.ps1 -StateFile C:\Easel\main-data\lan\state.json
```

规则只允许当前网卡、指定局域网 IP、HTTPS 端口、Caddy 程序与当前 IPv4 子网。Windows 将办公网标为 Public 也可使用，无需修改整台电脑的网络类别。IP 或网卡变化后须重新执行该命令，旧规则会被同名专用规则替换。可先加 `-WhatIf` 预览。后端 7863 与代理管理端口不对局域网开放。

## 每台客户端首次信任证书

给同事分发以下两个文件：

- `DataRoot\lan\Easel-LAN-Root.crt`：公开根证书。
- `deploy\desktop\Import-EaselLanCertificate.ps1`：导入脚本。

不要分发 `DataRoot\lan\storage`，其中包含 CA 私钥。必须长期保留并妥善保护此目录；删除或泄露后需轮换根证书并让所有客户端重新信任。这里的 Caddy 内部 CA 配置依据[官方 TLS 文档](https://caddyserver.com/docs/caddyfile/directives/tls)。

同事在普通 PowerShell 中执行（也可双击 `.crt`，选择“当前用户”→“受信任的根证书颁发机构”）：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Import-EaselLanCertificate.ps1 -CertificatePath .\Easel-LAN-Root.crt
```

Windows 可能显示证书信任确认，核对来自 Easel 主机后确认。管理员可通过脚本输出的 `certificate_thumbprint` 核对指纹，也可使用 `-ExpectedThumbprint` 参数锁定指纹。脚本仅导入当前用户，不需要管理员。Edge/Chrome 使用 Windows 证书库，必要时重启浏览器；独立维护证书库的浏览器需要在其设置中单独导入。其他操作系统需按其证书信任流程导入同一公开根证书。

然后打开启动脚本输出的 `lan_origin`，用保存的永久登录码登录。换 IP/域名后原 Cookie 不会带到新地址，但同一个登录码仍可使用，不用新建账号或重新签发。新电脑登录成功会使原电脑的登录失效。只有码遗失或被撤销时，管理员才按 [Windows 运维手册第 6 节](windows-desktop-operations.zh-CN.md#注销后怎样重新登录管理员) 重新签发。不得通过忽略证书警告作为日常使用方式。

## 电脑重启或 IP 变化

1. 启动 Docker Desktop，等待 Docker 就绪。
2. 重新运行上述 `Start-LanEasel.ps1 ... -Apply`。它检测新 IP、签发新地址证书并同步服务端来源。
3. IP 变化时，以管理员身份重跑防火墙脚本，然后把新地址发给大家。

CA 根目录保留时，同事无需重新导入证书，使用新地址并用原来的永久登录码登录即可。仅在浏览器修改 IP、却未运行主机脚本，会被来源校验拒绝。本功能不自动注册开机任务。

## 验证与回退

本机可使用带 CA 验证的 HTTPS 探针（本地 CA 没有公共吊销服务，curl 仅对吊销服务不可用作 best effort，仍校验证书链、有效期及主机地址）：

```powershell
curl.exe --noproxy "*" --ssl-revoke-best-effort --cacert C:\Easel\main-data\lan\Easel-LAN-Root.crt --fail --silent --show-error https://192.168.14.118:7861/api/health
```

预期 `{"status":"ok"}`。仍须从另一台局域网电脑验证登录、聊天、热点和保存；本机探针无法证明交换机、Wi-Fi 客户端隔离或另一台电脑证书状态。局域网入口不能消除热点外部数据源自身的延迟。

回退到首次 ngrok 来源（保留 ngrok 进程并先确认其可用）：

```powershell
$a = Import-Clixml C:\Easel\main-data\lan\start-args.xml
$a.PublicOrigin = (Get-Content C:\Easel\main-data\lan\previous-origin.txt -Raw).Trim()
& C:\Code\Easel\deploy\desktop\Start-WorkspaceEasel.ps1 @a
```

在新 PowerShell 中停止局域网代理：

```powershell
& C:\Easel\tools\caddy.exe stop --address 127.0.0.1:17861
```

如需移除防火墙规则，管理员运行 `Remove-NetFirewallRule -Name Easel-LAN-easel-main`。保留 CA 目录，便于之后重新启用。代理诊断日志位于 `DataRoot\lan\caddy.stderr.log`；启动失败应先看原始错误，不要重新初始化数据。
