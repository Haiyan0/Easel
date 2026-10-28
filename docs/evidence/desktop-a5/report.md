# Task A5 证据：真实 DeepSeek Harness Linux runtime 与 broker 适配

日期：2026-10-28（Asia/Shanghai）
基线：`04271949a8ce2b14d6d618063b2a0eb7fb355579`（A4 验收提交）
本轮提交：见 `git rev-parse HEAD`；仅实施 A5，不进入 A6/A7。

## 1. 已核对的真实输入

| 项 | 实际值 | 核对方式 |
|---|---|---|
| Harness 本地候选 | `C:/Users/hyh/Desktop/deepseek-harness` | `git rev-parse HEAD`、`git status --porcelain` 为空 |
| 固定 commit | `687f282e8625c6b8d11ff58211a90524be2e4317` | 候选 HEAD；tag `dsh-v0.2.1-rc.2` 指向该提交，公开 `origin/master` 包含它 |
| package version | `0.2.1-rc.2` | 根 `package.json` |
| Node engines | `^22.19.0 || >=24.0.0` | 根 `package.json` |
| pnpm | `11.28.5` | `packageManager` 与 devDependency |
| 构建脚本 | `tsx scripts/build.ts`；Host 构建为 `tsc -b tsconfig.host.json` + `tsdown --env.DSH_BUILD_FACE host` | 根 `package.json`、`scripts/build.ts`、`tsdown.config.ts` |
| 公开 ACP profile | `acp` = `@deepseek-ai/dsh-base` + `@deepseek-ai/dsh-acp-app` | `packages/boot/app-boot/src/profile.ts` `PROFILE_TEMPLATES` |
| ACP CLI | `apps/cli/lib/bin.js --profile acp` | `apps/cli/package.json`、`apps/cli/tsdown.config.ts` |
| Linux 原生组件 | `native/system` 的 glibc Node-API flock；`build:native-system` 产出 `linux-x64/bin/glibc/system.node` | `native/system/scripts/build.ts`、`prebuilds.json` |

没有复制 Windows `node_modules`、开发者 HOME、私有 profile、`.env`、账号或
浏览器目录；没有读取或打印任何现有凭证。

## 2. 实际协议与最小适配

### 2.1 Harness 默认路径不是 OpenAI chat

`packages/llm/llm-deepseek/src/adapter.ts` 实际使用 Messages transport：

```text
POST {messagesApiRoot(baseURL)}/messages
accept: text/event-stream
anthropic-version: 2023-06-01
x-api-key / x-deepseek-* 头
Messages SSE 事件
```

现有 Easel control broker 的 chat adapter 只注册：

```text
POST chat/completions
GET  models
```

因此不能只把 Harness 的 baseURL 改成 sidecar；那会把 Messages 请求发到
OpenAI chat 路由上，属于协议不匹配。

### 2.2 选择 Harness 已有的 OpenAI-compatible provider

仓内已有 `@deepseek-ai/dsh-llm-pi-ai`，其 hand-declared route 支持：

```text
api: openai-completions
baseURL: <worker 可达的 OpenAI 根地址>
apiKeyEnv: <逐 Run 凭证引用>
models: [{ id, name, contextWindow, maxTokens, ... }]
```

pi-ai 的 OpenAI SDK 实际请求：

```text
POST {baseURL}/chat/completions   （stream: true, SSE chat.completion.chunk）
GET  {baseURL}/models
Authorization: Bearer <apiKey>
```

这与 Easel broker 的 `POST chat/completions` / `GET models` 精确一致。
A5 采用该路径，不修改 Harness 源码，也不新增 Messages 代理或 `/v1`
泛放行。

### 2.3 逐 Run 地址、令牌与身份头

受管 overlay 只包含公开表达式，不含秘密：

```text
baseURL = process.env.EASEL_BROKER_URL + "/internal/broker/chat"
apiKeyEnv = EASEL_BROKER_TOKEN
headers.x-easel-workspace-id = process.env.EASEL_WORKSPACE_ID
headers.x-easel-run-id = process.env.EASEL_RUN_ID
model = deepseek-chat
retryPolicy.maxRetries = 0
```

worker 侧 sidecar 只对已批准的 broker 路径把标准 OpenAI
`Authorization: Bearer <opaque>` 映射为 control 内部的
`x-easel-broker-token`，并删除 Authorization；同时出现两种 token 头时
400，畸形/多值 Bearer 也 400。这样：

- Harness 使用标准 OpenAI SDK，不需要知道 Easel 内部 token 头；
- control broker 仍只接受自己的 opaque token 头和 workspace/run 范围；
- 供应商 Key 从未进入 worker；Authorization 也不会转发到上游；
- token 不写入镜像、profile 或命令。

### 2.4 ACP 与原生续接

`dsh-acp-app` 声明：

```text
sessionCapabilities: { close, list, resume }
```

Harness 的 session persistence 根是 `$DSH_HOME/sessions`；worker 的
`DSH_HOME` 是每个 workspace 私有可写 HOME 下的 `.dsh`。ACPAdapter 在
相同 conversation 的进程签名变化（逐 Run token/run id 变化）后关闭旧
Harness 子进程，再用既有 native session id 调用 `session/resume`；历史
从私有 session store 恢复，不读取其他 workspace 的材料。

### 2.5 文件职责

| 文件 | 性质 | 职责 |
|---|---|---|
| `deploy/multiuser/managed/acp-profile.json` | Easel manifest | 固定 commit/Node/pnpm/bundle/argv/broker/model |
| `deploy/multiuser/managed/acp.patch.yml` | Harness 原生 patch | 选择 `easel-broker` OpenAI-completions 路由与默认模型 |
| `deploy/multiuser/verify_managed_profile.py` | 构建/运行校验 | manifest、patch、checkout、CLI、Node、flock |
| `deploy/multiuser/smoke_harness.py` | 构建期真实 ACP 冒烟 | initialize/session new/close，确认默认模型路由 |
| `deploy/multiuser/worker_sidecar.py` | 信任边界适配 | Bearer → `x-easel-broker-token` 精确映射 |

## 3. 构建与运行

### 3.1 构建阶段实测

固定 Node 镜像：`node@sha256:be8204d4bd327363357f532187b752ff62759c31bedf98e9e18fc08c9cbd605c`
（Node `v24.22.1`）；pnpm `11.28.5` 由 corepack 精确激活。

`dsh-build` 阶段实际执行成功：

```text
Scope: all 366 workspace projects
Lockfile is up to date, resolution step is skipped
Done in 2m 5.5s using pnpm v11.28.5
build: built linux-x64/bin/glibc/system.node
tsc -b tsconfig.host.json
tsdown --config-loader native --env.DSH_BUILD_FACE host
node apps/cli/lib/bin.js --help
```

固定输入/产物摘要：

```text
pnpm-lock.yaml     4637cca3b89c4c8d5a147321710e597aa62ec2444a3772ab542be5690893dfe4
apps/cli/lib/bin.js 80112efe368efb1a39720c7ada0af00442e5131fb2744edaaff8a3e8bb499e6f
dsh-build image    sha256:dafce72f323ec4719b2f8fafdeb0922856862aa6f3153bd6c2688ac78cd1844b
dsh-build size     1051123543 bytes
```

镜像内核对：

```text
v24.22.1
11.28.5
687f282e8625c6b8d11ff58211a90524be2e4317
/opt/dsh/apps/cli/lib/bin.js
/opt/dsh/apps/cli/lib/profile-boot.js
/opt/dsh/native/system/packages/linux-x64/bin/glibc/system.node
```

以 UID 10001、只读 rootfs、临时 HOME 运行真实 Harness ACP 冒烟：

```text
harness_acp_smoke=ok model=easel-broker/deepseek-chat
```

Linux 原生 flock 以 UID 10001 实际加载并取得锁：

```text
flock-ok
```

最终 runtime 构建成功，镜像：

```text
sha256:6830c2b4b2f3424dde6fff492803c05cbe77cc03d4e5921a14e26e3a7d738434
size 2689765163 bytes
```

最终 `--require-dsh` 门槛实际输出：

```text
DSH_PROFILE_OK commit=687f282e8625c6b8d11ff58211a90524be2e4317 node=24.22.1 patch_sha256=8ebd72c06981bd099fd3d8e1da1370c61e8b4826bf0972cc0ffc528b1254ad09 model=easel-broker/deepseek-chat
WORKER_SMOKE_OK
```

该门槛在最终镜像中以 UID 10001 运行，检查固定 checkout、CLI 构建产物、
Node 引擎、Linux 原生 flock、profile/patch 只读性，并启动真实 Harness
完成 ACP initialize/session new。

### 3.2 构建期网络处理

首次 runtime 构建在 Debian 官方 CDN 的 `libz3-4` 上收到 502；加入
`Acquire::Retries "5"` 后第二次构建已下载 103 MB，但在 `libnspr4` 上仍
收到 502。尝试的镜像站也出现 HTTP 超时/Tsinghua `bookworm-updates` 403。

最终构建使用本机已有的 `verge-mihomo` 代理（`host.docker.internal:7897`，
无凭证），通过 Docker 预定义 proxy build args 注入构建容器；Debian 源保持
官方 CDN，并由 `Acquire::Retries` 兜底：

```bash
--build-arg HTTP_PROXY=http://host.docker.internal:7897 \
--build-arg HTTPS_PROXY=http://host.docker.internal:7897 \
--build-arg http_proxy=http://host.docker.internal:7897 \
--build-arg https_proxy=http://host.docker.internal:7897 \
--build-arg NO_PROXY=127.0.0.1,localhost,host.docker.internal
```

代理只作用于构建期，不写入最终镜像，不修改宿主/WSL 的全局 Docker 或系统
代理设置；未关闭 apt 校验、TLS 校验或任何安全检查。

## 4. 真实 Harness 链路

执行：

```text
python -m pytest tests/integration/multiuser/test_harness_runtime_real.py -q -s
2 passed in 65.93s
```

测试使用 A4 正式 control/gateway/provisioner/sidecar/broker 装配，worker
使用上面固定构建的 runtime 镜像；上游是受控 TLS fake provider。

```text
A5_IMAGES {"control":"sha256:1d9c559e97f172381da28dbbec4e1afeb008405cddbea972c0f2baecff7deb16",
"gateway":"sha256:c0a35aaa9f304c47f9f5c3e73add7d9d5920041baef156e2ea674f3000c5580a",
"worker":"sha256:6830c2b4b2f3424dde6fff492803c05cbe77cc03d4e5921a14e26e3a7d738434"}

A5_REAL_PASS {"pids_a1":[26,39],"pids_a2":[51,64],
"provider_requests":9,
"token_rows":[{"revoked":true,"run":"644cc17afd3f41fe9d8dab14d185cdca"},
{"revoked":true,"run":"d545ff911f064ac986c19b55cd407483"}],
"worker_a":"1206a6b045826d829f3abcd274575d208c87e61bf40fc68c2677fccb9e627b19",
"worker_b":"1ac191c9e70128c0ff71378f801b935553750b6ac080cde82ac12feed9b26fd0"}
```

Provider 审计（`requests.jsonl`）实测：

```text
chat_requests 9          # 3 个成功 prompt + 2 个工具回合 + 4 个失败模式
models ['deepseek-chat']
stream_flags [True]
auth_headers ['Bearer sentinel-provider-key-a4']
harness-one 2            # A1 首轮 + A2 resume 历史
harness-two 1
harness-b 1
harness-tool 2           # tool_calls 首回合 + 工具结果续答回合
harness-401 1
harness-429 1
harness-500 1
harness-drop 1
```

工具回合的第二次请求实测包含标准 `role=tool` 消息，内容为
`"harness-tool-ok\n"`；模型请求均为 `stream=true` 且只带控制面 sentinel
Key，worker 从未持有供应商 Key。

关键断言：

- 同一 session 的 A1/A2 两轮：A2 的 provider history 包含 A1 的用户消息和
  助手回复，证明 native session 历史经 Harness 私有 store 恢复；
- A1/A2 的 Harness 进程 PID 集合 `[26,39]` 与 `[51,64]` 不相交，证明逐
  Run token/run id 变化后 ACPAdapter 重建了 Harness 子进程；
- 两个 Run 的 broker token 行都存在且均已撤销（`revoked=true`）；A2 使用
  新 Run token，旧 token 不可复用；
- worker A/B 容器 ID 不同，各自 HOME/profile/session store 独立；B 的
  provider history 不含 A 的消息；
- 401/429/500/中途断流各只产生 1 次 provider 请求（`maxRetries: 0`），
  Run 进入 failed/interrupted，未写 assistant 成功消息；
- 真实工具调用/权限回答：fake provider 返回标准 OpenAI `tool_calls`
  （`bash` + `danger-full-access` 提权），真实 Harness 经 ACP
  `session/request_permission` 发出“执行授权”问题；经公开 question 接口
  选择“允许本次”后，工具实际执行并把 `harness-tool-ok` 作为 `role=tool`
  结果回传 provider，Run 完成且助手消息正常。该断言证明工具调用不是仅
  弹出授权框，也未绕过 Easel 的 question 路由；
- 缺 broker env 时 Harness 明确报告 `EASEL_BROKER_URL is missing`，且无
  `fake-provider-ok`；缺 patch 时 CLI 非零退出。Harness 的 loader 会把
  单个 entry 配置错误报告为非致命激活失败（上游行为），因此该负例断言
  显式诊断而非进程退出码。

没有使用真实 DeepSeek 凭证或真实供应商请求；上述 provider 为测试用
受控 fake，服务端 sentinel Key 只在 control 加密配置中。

## 5. 失败、跳过与未执行

### 测试计数

```text
A5 真实 Harness 集成：2 passed in 65.93s
A4 正式夹具容器回归：1 passed in 76.53s
tests/multiuser + tests/integration/multiuser/test_desktop_topology.py：
1388 passed, 10 skipped, 2 warnings in 169.47s
受管 profile/sidecar/worker image 静态：37 passed
```

A4 正式夹具容器回归使用原 `easel-worker-execution:a4` 镜像，确认本次
fake provider 扩展（新增 `harness-tool` 工具回合与模型回显）没有改变
A4 正常路径；真实 Harness 测试单独计数。

### 未执行

- 真实 DeepSeek 供应商文本请求：本轮未使用任何供应商凭证；为避免读取
  开发者私有配置，未执行，标记为待有批准受管 channel 时补测。
- A6 双用户公网浏览器闭环、A7 备份/升级/回滚/新目录恢复。
- 生产 Linux 防火墙/IPv6、XFS 配额、硬化 Chromium sandbox 门槛。
- 未以文本聊天成功宣称上述生产门槛通过。

### 清理

已核对：没有 A5 名称的容器、网络、卷；没有 WSL `/tmp/easel-a5-*`
目录或 provisioner 进程。镜像
`easel-worker-harness:a5` 和固定构建产物保留供复审；未执行全局 prune，
未触碰开发者容器/卷/网络/HOME 或现有 ngrok 隧道。

## 6. 边界

- 未进入 A6 双用户公网浏览器闭环或 A7 运维。
- 未以文本聊天成功宣称 Chromium 沙箱/XFS/Linux 防火墙等生产门槛通过。
- 未使用真实供应商计费请求，除非下文明确记录有可用凭证的有界调用。
- 未执行全局 prune、未触碰开发者容器/卷/网络/HOME/ngrok 隧道。
