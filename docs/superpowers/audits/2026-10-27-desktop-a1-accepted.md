# Task A1 验收通过

验收提交：`f1c4e5ef98ea756513071367919fe57fac7dbf17`。
定点返工父提交：`a2b83faf39e8588ecc30e27eee4d0f065a537c40`。

## 裁决

A1 登录管理面装配通过，A1-R1-01/02 与 A1-R2-01 关闭。允许进入 A2 拓扑可行性与接入契约验证；不代表完整多人聊天或生产部署通过。

- Start/Test 共用卷归属与结构复核，外来卷不得直接启动使用。
- Test 校验运行中容器的命令、Origin、身份、挂载来源/类型/读写及端口，异常不得报全绿。
- 静态契约失败先退出，不运行 key helper；合法路径固定预期 key 卷并 readonly。
- 独立复审确认 R2 差异收敛，无新增发现。

## 主审本轮实际验证

```text
Agent Python -m pytest tests/multiuser/test_desktop_deployment.py tests/multiuser/test_auth_api.py tests/multiuser/test_admin_api.py tests/multiuser/test_config_api.py tests/test_document_paths.py -q
122 passed in 36.35s
git diff a2b83fa f1c4e5e --check
exit 0
```

其中 Desktop 45、指定 auth/admin/config 67、文档路径 10。主审没有在本轮重建镜像、启动 ngrok 或再次执行浏览器登录；实施方提交的真实 Docker 最终固定镜像合法诊断/readonly helper 证据已审阅，旧 ngrok 双身份证据按此前裁决保留。

实施方本轮固定镜像：`sha256:0f63a617aa1f95090edbc3073e6447dbfd1c2bc6b15e95f0c7e193e789bc917f`。不以该镜像摘要替代当前源码 SHA 或完整 runtime 验收。

## 保留边界

- A1 交付登录、成员/设置与会话目录；缺少 worker 装配时聊天/历史继续明确 503。
- ngrok 真实公开地址切换受免费固定域名限制，维持 not-run；局部 SPKI TLS Origin 变更不冒充公开域名变化。
- 已有数据库丢失 master key 后不得盲目初始化；配套备份恢复与初始化防替换门禁按 A7 收口。
- A2 仍须实测 WSL/Desktop socket、路径与 control→worker 网络，不预设 Linux 宿主容器 IP 可达。
- 原 Linux/XFS/防火墙/Chromium/最终 Harness 等生产门槛不变。

## 下一任务

按 `../plans/2026-10-26-desktop-multiuser-assembly.md` 仅实施 A2。盘点与临时资源测试可进行；若需要变更宿主 provisioner 的信任边界、增加管理中继或改变网络授权，先交设计与实测证据，主审通过后再实现，不直接进入 A3。
