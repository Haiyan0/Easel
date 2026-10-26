# 多人工作区部署、备份恢复与显式迁移手册

本文覆盖单台 Linux 主机上的 control、受限 provisioner、每 workspace worker 的
运维边界，以及 Task 13 提供的备份/恢复/本机迁移命令。**它不是发布验收报告**：
Task 8 的真实防火墙、IPv6、XFS 配额、最终 Harness/浏览器沙箱与真实供应商门槛
仍未全部通过；未装配的非聊天业务继续返回 503。没有完成这些门槛前，不得把本文
当作多用户正式上线证明。

## 1. Linux、rootless 与资源前置

- 目标为 Linux 主机与真实 Docker Engine。control 不持有 Docker socket；
  provisioner 是唯一持有宿主 Docker socket 的进程；worker 不持有 Docker 或
  provisioner socket。
- provisioner 以可操作 Docker、可 `chown` 持久目录的专用账号运行；socket
  权限 `0660`、父目录 `0750`，`--allowed-uid` 只包含 control UID。
  参见 [deploy/multiuser/README.md](../deploy/multiuser/README.md)。
- workspace 持久根必须位于支持 XFS `prjquota`/`pquota` 的文件系统；provisioner
  会实际创建并校验项目配额，默认 10 GiB。只有测试可以显式
  `--quota-mode none --allow-unquotaed`，真实部署不得使用。
- rootless Docker 只有在项目配额、UID 映射、`SO_PEERCRED` 和 bind mount 均
  实测可用时才可部署；任何一项不可用都应停止，而不是关闭配额或挂载宿主
  Docker socket 绕过。本机 Docker Desktop 的 overlayfs 结果不构成目标宿主证据。
- 网络前置：worker 只有每空间 internal 网络；sidecar 是唯一 broker/受限出口；
  control 发布端口只对反向代理/本机可见。宿主防火墙、DOCKER-USER、IPv4 与
  IPv6 的允许/拒绝矩阵必须按 [deploy/multiuser/egress-policy.md](../deploy/multiuser/egress-policy.md)
  实测，不能只依赖 Docker 发布端口。
- master key 必须是由 CSPRNG 生成的 32 字节文件，例如
  `/run/secrets/easel-master.key`，权限 `0400`/`0600`。它不能进入 Git、
  普通日志、镜像或用户可读目录。

推荐目录：

```text
/srv/easel/
  control/control.sqlite3        # control 库
  workspaces/
    .migration-backups/          # migrate-local 的默认源备份（必须在 workspace 根下）
    <32hex>/
      workspace/                 # 容器 /workspace
        outputs/                 # worker OUTPUTS_DIR：产物与会话 API 历史
        state/                   # worker EASEL_STATE_ROOT：绑定、消息、Run 状态
      home/                      # 容器 /home/easel（平台 profile）
      .credentials/              # provisioner 内部 token，备份显式排除
  backups/                       # 与 control/data 同一受管根
  run/provisioner.sock
/etc/easel/egress-policy.json
/run/secrets/easel-master.key
```

`admin backup` 要求目标在该 control 根与 workspace 根的公共受管根内，且不能
位于 control 或 workspace 根内部。若备份盘是独立物理盘，应把它挂载到
`/srv/easel/backups` 这类受管路径，而不是把目标指向任意系统目录。

## 2. 首次部署

1. 构建并固定 worker 镜像摘要（`DSH_COMMIT`、ACP profile、Chromium 与依赖
   必须已验收）。禁止用可变 tag 启动；记录 `sha256:<64hex>`。
2. 按 deploy README 启动 provisioner，确认 socket、配额、网络与 sidecar
   策略均通过；缺少任一项时 control 不得启动。
3. 启动 control：

```bash
python -m easel serve-control \
  --control-dir /srv/easel/control \
  --public-origin https://easel.example.com \
  --master-key-file /run/secrets/easel-master.key \
  --egress-proxy http://127.0.0.1:8899 \
  --egress-policy /etc/easel/egress-policy.json \
  --provisioner-socket /srv/easel/run/provisioner.sock
```

4. 在服务器本地引导管理员：

```bash
python -m easel admin bootstrap \
  --control-dir /srv/easel/control \
  --name "Owner"
```

命令只输出一次明文邀请。邀请在数据库只保存摘要；兑换成功后即失效，不能重放。
邀请丢失只能由管理员撤销后重新签发，不要从日志、备份清单或浏览器历史寻找旧
明文。登录后使用同源 Cookie + CSRF；不要把 token 放进 URL。
5. 为成员签发邀请：

```bash
python -m easel admin invite \
  --control-dir /srv/easel/control \
  --admin-user-id <管理员用户ID> \
  --user-id <成员用户ID> \
  --ttl-seconds 604800
```

明文只显示一次。成员首次兑换后进入自己的 workspace；管理员禁用成员会同时
撤销其全部登录和未兑换邀请。

## 3. 令牌轮换与撤销

- **邀请轮换**：撤销尚未兑换的邀请后重新签发；已兑换邀请不能恢复。
- **登录轮换**：管理员通过 `POST /api/admin/users/{user_id}/revoke-sessions`
  撤销该成员全部登录；禁用成员会产生同等效果。SSE 在撤销后按既有 5 秒边界
  关闭，不把旧流继续当作已授权连接。
- **worker/broker 令牌**：worker 服务 token 由 provisioner 生成并只进入
  0600 env 文件与数据库；drain/stop 会清空租约 token。broker token 按 Run
  短时签发并在终态撤销；重试不会复用已撤销 token。
- **master key**：V1 不提供在线轮换。直接替换 key 文件会使已有密文无法解密，
  control 启动会明确 `invalid_master_key`，恢复也会 `master_key_mismatch`；
  不得通过生成新 key 或清空配置来“修复”。需要轮换时先停服、保留旧 key 与
  完整备份，并另行完成受审查的离线重加密流程；本文不提供未实现的自动轮换。

## 4. 备份

备份包含：控制 SQLite 一致性快照、master key 文件、各 workspace 的
`workspace/` 与 `home/` 持久区，以及带 SHA-256 的清单。备份只排除
`.credentials/` 等秘密材料；用户工作区内的 `.git`、未推送提交、分支、stash、
脚本执行位及其他持久文件都保留，不能按镜像/构建缓存裁剪。快照同时清除
`worker_leases.service_token`（使用 `secure_delete` + `VACUUM` 避免空闲页
残留），因此恢复后不会复用旧的 worker 服务 token；新 worker 会重新签发。
清单不包含邀请/登录明文、模型 Key、Cookie 或 master key 内容，只记录必要的
版本与完整性摘要。

### 4.1 发版/维护窗口

1. 停止接纳新任务（关闭上游入口或停止 control），等待在途 Run 结束。
2. 使用 provisioner socket 让备份命令先 drain 所有活动 worker：

```bash
python -m easel admin backup \
  --control-dir /srv/easel/control \
  --destination /srv/easel/backups/2026-10-26 \
  --master-key-file /run/secrets/easel-master.key \
  --workspace-root /srv/easel/workspaces \
  --image-digest sha256:<worker镜像摘要> \
  --harness-revision <已验收Harness修订> \
  --provisioner-socket /srv/easel/run/provisioner.sock
```

- 活动租约存在但未提供 drain 能力时，命令直接失败
  `worker_drain_required`；drain 后仍有 starting/stopping 等中间态时失败
  `worker_drain_incomplete`。此时不得继续复制文件，应等待或排查。
- 目标必须是不存在的新目录；命令先在临时目录写入并校验，再整体提交。
  不允许覆盖已有备份或运行中的 control 目录。
- 备份目录的 `manifest.json` 记录 schema、镜像摘要、Harness 修订、受管配置
  版本、各 workspace 配置版本及每个文件的字节数与摘要。恢复时逐项复核。
- 备份/恢复遇到符号链接、Windows junction 或其他非普通文件会失败关闭；
  迁移会跳过这些条目并在 `rejected` 中列出。两者都不会跟随到受管根之外。
  若 workspace 或 profile 中确实存在此类链接，先由管理员转为普通文件或移出
  持久区，再重试；工具不会静默跳过它们。

### 4.2 验证备份

```bash
python -m easel admin restore \
  --source /srv/easel/backups/2026-10-26 \
  --destination /srv/easel-restore/2026-10-26
```

恢复先验证清单文件集合、路径（拒绝 `..`/反斜杠/绝对路径/符号链接、junction）
和每个摘要；再在新目录中校验 SQLite `integrity_check`、schema 版本和 master
key 能否解开已有密文。缺 key 返回 `backup_master_key_missing`，key 不匹配返回
`master_key_mismatch`；不会生成新 key、重置秘密或留下部分目标目录。

## 5. 恢复演练与切换

恢复目录布局与备份一致：

```text
<restore>/
  manifest.json
  control/control.sqlite3
  master.key
  workspaces/<uuid>/{workspace,home}/...
```

演练应使用**新目录、新 worker 根和新监听端口**：

```bash
python -m easel serve-control \
  --control-dir /srv/easel-restore/2026-10-26/control \
  --public-origin https://restore.example.com \
  --master-key-file /srv/easel-restore/2026-10-26/master.key \
  --provisioner-socket <新的 provisioner socket> \
  --host 127.0.0.1 --port 7862
```

新 provisioner 的 `--workspace-root` 指向
`/srv/easel-restore/2026-10-26/workspaces`。逐项验证：

- 管理员/成员仍能登录；登录 Cookie 若已过期，正常重新邀请或走现有登录流程，
  不把 401 当作数据丢失。
- 会话目录、消息快照与原生绑定可读；个人覆盖可用恢复后的 key 解密，管理员
  默认版本没有回退。
- `home/` 下平台 profile 文件完整；平台 Cookie 失效时正常要求重新登录。
- 新 worker 不继承旧 `.credentials`；旧宿主目录没有被恢复命令改写。
- 恢复源只读：恢复后备份目录的文件摘要保持不变。

禁止把 `--destination` 指向正在使用的 control 目录或以任何方式清空它；
非空目标会返回 `restore_target_not_empty`。确认演练通过后，再按维护窗口切换
反向代理/端口并把旧目录保留为只读回滚副本。

## 6. 升级与 drain

1. 宣布维护窗口，停止新任务入口；等待在途任务进入终态。
2. 用第 4 节命令备份；有活动 worker 时提供 provisioner socket，确认租约已到
   `stopped`/`failed`。
3. 固定新镜像并更新 provisioner；不要删除 `<workspace-root>/<uuid>` 持久目录。
4. 重启 provisioner 与 control，检查启动时的 schema、key、egress 与 socket
   装配；失败关闭时不降级到无隔离模式。
5. 抽查登录、会话续接、个人覆盖、sidecar broker 与产物访问。任何 drain 超时
   或中间态都保持原状，不自动重发用户 prompt，不丢弃已持久任务。

## 7. 显式本机迁移

迁移只接受管理员显式给出的源、目标 workspace 和 workspace 根；不会读取开发者
`.env`、HOME、浏览器目录或自动认领旧历史。源可以是本机安装根（含 `outputs/`）
或 `outputs/` 本身。

### 7.1 先 dry-run

```bash
python -m easel admin migrate-local \
  --source /home/operator/easel/outputs \
  --workspace <32位小写workspaceID> \
  --workspace-root /srv/easel/workspaces \
  --dry-run
```

输出 `imported=`、`non_resumable=`、`rejected=` 计数及逐项清单。dry-run 不创建
目标、不创建备份，也不修改源。确认目标 workspace 属于预期用户；命令不会根据
邀请或浏览器缓存推断跨用户归属。

### 7.2 实际迁移

```bash
python -m easel admin migrate-local \
  --source /home/operator/easel/outputs \
  --workspace <32位小写workspaceID> \
  --workspace-root /srv/easel/workspaces \
  --backup-destination /srv/easel/workspaces/.migration-backups/<workspaceID>-<时间戳>
```

- 目标必须是 `<workspace-root>/<workspaceID>/workspace`。dry-run 与实际执行对
  空目标使用同一规则：不存在或只含递归空目录都允许；任何文件、链接、junction
  或预检后新增数据都返回 `migration_target_not_empty` 并保留现有数据。目标经
  junction/symlink 逃逸时返回 `migration_target_escape`。
- 实际执行前先复制源到备份目录；省略 `--backup-destination` 时写入
  `<workspace-root>/.migration-backups/`。备份目标必须同样位于
  `<workspace-root>` 内。备份包含静态内容、原始旧会话文件和用户 `.git`
  历史，排除 `_harness/`、`_browser/`、`browser_profiles/`、`.env*`、
  `.credentials/`。
- 静态内容复制到目标 `<workspaceID>/workspace/outputs/`，与 worker 的
  `OUTPUTS_DIR` 一致；源中同名的 `state/`、`sessions/` 等目录只会落在
  `outputs/` 下，不会覆盖真实状态根。静态导入保留用户 `.git` 等普通内容，
  只跳过 `_sessions`、`_harness`、`_browser`、`browser_profiles`、`.env*`、
  `.credentials`。
- `_sessions/api` 的消息写入目标 `<workspaceID>/workspace/state/` 下的
  `sessions/`（真实 `EASEL_STATE_ROOT`），与静态产物隔离。原生绑定只有在记录
  明确给出 `cwd == /workspace` 且 `native_id` 非空时才按可续接导入。缺少 cwd、
  路径不兼容或 native_id 为空时，只把消息作为静态历史写入
  `<workspaceID>/workspace/state/migrated-sessions/<session-id>/`，并在
  `non_resumable_sessions` 报告；**不会替换、补写或重新签发 native_id**。
- 源树在迁移前后做完整摘要对比；迁移中断或源被外部改动时返回
  `migration_source_changed`/`migration_write_failed`，临时目录清理，目标不出现
  半成品。
- 本命令只写目标 workspace 持久区与会话快照；控制库的会话目录/登录记录仍由
  control 正常流程管理。迁移后是否能在界面中续接，取决于绑定是否可续接以及
  control 是否已有对应会话目录；不把静态历史伪装成可续接会话。
- 源目录保持只读，不删除、不移动、不覆盖原数据。确认迁移结果后，源可继续按
  组织的数据保留策略处理。

## 8. 供应商支持矩阵

矩阵来自当前 `model_registry` 与 control 的通道映射；“有适配器声明”只代表
代码中存在 broker 转发路径，**不代表真实供应商连通、配额、计费或工具调用已
通过**。Task 13 未调用任何真实供应商；不要用 OpenAI-compatible 结果推断其他
供应商。

| control 通道 | 供应商 | broker adapter | 个人覆盖 |
| --- | --- | --- | --- |
| speech（registry voice） | dashscope | 无 | 允许 |
| speech | minimax | 无 | 允许 |
| speech | fish-audio | 无 | 允许 |
| speech | gemini | 无 | 允许 |
| speech | openai-compatible | openai-compatible | 允许 |
| image | openai | openai-compatible | 允许 |
| video | dashscope | 无 | 允许 |
| video | ark | 无 | 允许 |
| video | kling | 无 | 允许 |
| video | openai-compatible | 无 | 允许 |
| video | xhs-maas | 无 | 允许 |
| video | agnes | 无 | 允许 |
| music | dashscope | 无 | 允许 |
| music | suno-compatible | 无 | 允许 |
| chat | deepseek | deepseek-acp | 仅管理员 |

“无 broker adapter”不表示能力已可用；个人完整路由仍可保存，但真实调用前必须
逐供应商完成契约/出口/凭证验证。多字段凭证按 registry 必需字段提交，部分填写
会被拒绝；留空只复用本人同供应商、地址与协议的已有凭证，不复制管理员 Key。

## 9. 验收与未关闭门槛

本手册对应的自动化验证：

```bash
python -m pytest tests/multiuser/test_operations.py -q
python -m pytest tests/multiuser -q
python -m pytest tests/test_document_paths.py -q
```

### 9.1 Task 14 本机双用户与发布门槛记录

日期：2026-10-26（Asia/Shanghai）。本机证据父基线：
`5bebd87c65f723e3be656596a71acf1af9bc5948`。

本机可执行、不连接正式部署：

```powershell
python -m pytest tests/multiuser/test_two_users.py -q
python -m pytest tests/multiuser/test_release_checker.py -q
python -m pytest tests/multiuser -q
```

- `test_two_users.py`：真实 control app + 两个独立 worker 子进程 + 临时
  假 ACP Harness；A/B 一次性邀请、同名会话、HTTP/SSE、连续追问续接、
  worker outputs 隔离、控制库/持久区备份恢复均为本机实际执行。
- 本机结果：`test_two_users.py` 4 passed；`test_release_checker.py`
  18 passed；`test_live_assertions.py` 4 passed；`tests/multiuser`
  998 passed、4 skipped；真实 Chromium profile 测试 12 passed（含缓存值
  负向敏感性）；前端 Node 测试 118 passed，生产构建通过。另在
  本机 Docker Desktop 用已有 Task 8 执行镜像运行
  `smoke_worker.py --check-files` 得到 `WORKER_SMOKE_OK`；这不是从头
  构建、容器隔离/网络/配额或目标 Linux 证据。Windows 既有 POSIX 跳过
  不记为通过，本机 Chromium 也不等于目标 Linux worker 沙箱通过。
- 这些结果只证明本机代码路径，不证明目标 Linux、Docker 资源/网络、真实
  DSH ACP、真实供应商或 Chromium 沙箱。

正式发布门槛（缺前提必须失败，不 skip）：

```bash
python scripts/check_multiuser_release.py
python -m pytest tests/integration/multiuser/test_two_users_live.py \
  tests/integration/multiuser/test_browser_live.py \
  tests/integration/multiuser/test_restore_live.py \
  --multiuser-live -m multiuser_integration -q
```

正式检查要求 Linux、可用 Docker、XFS `prjquota`/`pquota`、受保护 master
key、固定 worker 镜像、HTTPS control/preview、A/B 一次性邀请、真实备份源、
空恢复目标和 `EASEL_TEST_EGRESS_PROXY`；检查器设置 `EASEL_EGRESS_LIVE=1`
并实际运行真实出口测试。所有选中的安全/发布门槛出现 skip/xfail/xpass、
空集合或任一失败均退出 1，没有文件级宽松开关。

检查器还区分已执行门槛与正式证据：真实出口通过才把 egress 标为已验证；
XFS 只检查挂载/工具不算 10 GiB 配额建立与耗尽；宿主 DOCKER-USER/
IPv4-IPv6 矩阵没有自动门槛；base/execution 假 Harness 容器测试不能证明
最终 runtime 镜像及真实 DSH/profile 已验收。后三项保持 `blocked`，整体
`release_status` 只能为 `blocked` 并退出 1，不能得到 `passed`。

本机 2026-10-26 实际运行检查器：

```text
release_status=failed
[fail] platform platform_not_linux
gates=not_run
evidence=not_run
```

因此 **正式发布门槛未通过、正式 live/restore/browser 未执行**。control 的
workspace-content 路由（如 `/api/outputs`）仍未装配并返回 503；live 测试
把该状态视为发布失败，不能用本机 worker 产物测试替代。

自动化测试只使用临时目录和临时数据，不读取真实 HOME、浏览器 profile、公众号
配置或供应商凭证。以下仍保持未通过/未执行，不能由本手册或测试替代：

- 目标 Linux 防火墙、DOCKER-USER、IPv4/IPv6 矩阵；
- XFS 项目配额在目标宿主的真实生效；
- 最终 Harness 镜像、真实 ACP 与真实供应商调用；
- 硬化 worker 内 Chromium 沙箱（本机 Docker Desktop 已记录 `No usable sandbox!`）；
- 全量双用户真实浏览器/恢复演练的发布验收。

在这些门槛完成前，Task 8 的发布状态不变；非聊天业务未装配者继续返回 503。
