# Task A1 主审 R1

基线：`290d94a68b9a4c1e8e3f627b5c96fcbdb8f11f1b`。审查提交：`1cee445e3b6334a1d8c3adba34f669d470d745dc`。

结论：本轮不通过，修复以下两项后复审，不进入 A2。核心登录证据与部署方向保留，不重做 A1。

## A1-R1-01（P1）：启动与诊断未复核卷归属

位置：`deploy/desktop/EaselDesktop.Common.ps1:150–155`；调用方 Start/Test。

Initialize 校验了项目和角色标签，但 Start/Test 共用的 Test-EaselImagePin 只检查 volume inspect 退出码。操作者可以跳过 Initialize 或在初始化后替换卷，直接 Start；只要同名卷存在且 key 长度有效，就会进入 Compose 并使用不属于本项目的数据库/密钥。

主审使用独立临时目录复用测试 fake Docker、真实 PowerShell 脚本，预设 `foreign_volume` 标签和有效 key：Start exit=0，`compose up=True`，输出 health=healthy。没有启动真实 Docker 资源，也未读取用户数据。

要求：提取公共卷检查，Start/Test 在任何 `docker run` / Compose up 前复核两个卷的实际名称、项目及角色标签和合法单对象结构。非零 inspect、空/多对象、缺标签、错项目或角色均明确拒绝；不得通过自动 create/chown 接管。保留 Initialize 的独立检查。

测试：分别覆盖 control/keys 外来标签、缺标签、错角色、非法 inspect。断言拒绝时无 helper run、无 up、无新 key；合法卷仍能启动与重复使用。

## A1-R1-02（P2）：运行中实际接线错误时诊断全绿

位置：`deploy/desktop/Test-Easel.ps1:71–118`。

仅比较挂载目标路径，不核对卷 Name/类型/RW；也不核对实际 serve-control 参数中的 Origin 和控制/key/前端路径。master key helper 检查的是预期卷，不能证明运行中容器挂载了该卷。端口检查只看 7861/tcp，不能发现其它意外发布端口。

主审给真实脚本返回固定镜像正确、health=healthy 的容器 inspect，但两个挂载来源均为 foreign 卷、key 挂载 RW=true、命令为 wrong_entry.py、实际 Origin 为另一个地址：Test exit=0，12/12 green。

要求：诊断实际容器和期望部署契约：Compose 项目/service 身份、Config.Cmd（合并 Entrypoint 后的有效启动契约）、实际 `--public-origin` 与目录/端口参数；Mounts 恰为预期两个 volume，控制卷可写/key 卷只读、来源 Name 正确；仅允许预期端口发布，无额外发布端口。合法 inspect 结构缺失/歧义应失败，不回显秘密或完整 env。不要简单依赖固定检查数量。

测试：来源错、bind 替代、key 可写、Origin 错、命令错、附加发布端口分别失败；正确容器实际 Docker 诊断通过。无需为本项修改认证核心代码。

## 验证与边界

- 主审复跑新增 Desktop + auth API + admin API + config API：82 passed（10.79 s）。
- `git diff 290d94a 1cee445 --check`：退出 0。
- 两项新反例在提交现状均成功触发，不以已有测试全绿覆盖。
- 本轮没有重建镜像、启动 ngrok 或复做浏览器登录；交付报告的真实运行结果是实施方证据，未冒称主审重新执行。
- 允许 ngrok 免费单固定域名导致真实公网地址变化 not-run；真实 ngrok 初始登录 + 局部 SPKI TLS Origin 变更的分层说明可以接受。不要求额外购买账户/域名。
- 不要求本轮为无运行时代码变更重跑所有浏览器场景；完成脚本修复后至少用最终固定镜像复跑 init/start/Test 与合法重开。若改变镜像应用内容/依赖，则补真实 HTTPS 双身份验证。

## 已记录、在 A7 收口的恢复风险

独立审查另复现：已有控制卷但 key 文件丢失时，再次 Initialize 会生成新随机 key；已有密文随后使 control 以 invalid_master_key 拒绝启动。该问题不覆盖现存 key、不改写旧 DB，但不应作为恢复办法。A7 已明确要求“缺 key 不创建替代 key”，保持该验收项；在完成前，密钥丢失必须停止并从配套备份恢复，不按重开流程继续初始化。此项不扩入本轮两项定向返工。

## 交付要求

只修 A1-R1-01/02，必要路径限共用脚本、Start/Test、对应部署测试与手册/证据。保留已有主审文档。提交后报告父/新 SHA、逐项红绿、实际最终镜像诊断/重开结果和剩余限制，停下等复审，不进入 A2。
