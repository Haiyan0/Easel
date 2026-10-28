# A5 红绿记录

## 红

### 1. Sidecar 只接受旧内部 token 头

在新增 OpenAI Bearer 映射前，正式 sidecar 测试：

- `Authorization: Bearer token-sentinel` 被原样转发到上游，上游没有
  `x-easel-broker-token`，`test_broker_accepts_openai_bearer_and_maps_to_internal_token`
  失败；
- `Basic`、空 Bearer、多 token Bearer 与同时携带两种 token 头的请求仍被
  转发并得到 200，`test_broker_rejects_ambiguous_or_malformed_authorization`
  六组全部失败。

### 2. A4 基线 Dockerfile 不满足 A5 构建门槛

对 `04271949a8ce2b14d6d618063b2a0eb7fb355579` 的 Dockerfile 运行 A5 静态
断言，以下 9 个标记全部缺失：

```text
FROM ${NODE_IMAGE} AS dsh-build
ARG NODE_IMAGE=node@sha256:
ARG PNPM_VERSION=11.28.5
corepack prepare "pnpm@${PNPM_VERSION}" --activate
pnpm run build:native-system
./node_modules/typescript/bin/tsc -b tsconfig.host.json
pnpm exec tsdown --config-loader native --env.DSH_BUILD_FACE host
test -f apps/cli/lib/bin.js
COPY --from=dsh-build /opt/dsh /opt/dsh
```

且基线仍执行 `pnpm install --frozen-lockfile --prod`，没有实际 Linux
构建；其 `_check_dsh` 只检查 `/opt/dsh/.git/HEAD`、`acp-profile.json`
存在与文件只读性，不能发现缺 CLI/缺 native/Node 错误。

## 绿

- sidecar 新增精确 Bearer→`x-easel-broker-token` 映射并拒绝畸形/歧义后，
  `tests/multiuser/test_worker_sidecar.py`：**16 passed**。
- 受管 manifest/patch 校验器与负例：**15 passed**。
- A5 构建阶段实际完成（`pnpm install` 2m5.5s；native flock；tsc；tsdown；
  CLI `--help`），镜像 `sha256:dafce72...`。
- 以 UID 10001、只读 rootfs 运行真实 Harness：
  `harness_acp_smoke=ok model=easel-broker/deepseek-chat`。
- 以 UID 10001 实际加载 Linux 原生 flock：`flock-ok`。
- 最终 runtime 与真实 Harness→broker 链路结果见 `report.md`。

## A5.3 补充：真实工具调用与权限回答

- fake provider 新增标准 OpenAI `tool_calls` 流（`bash` 请求
  `danger-full-access` + justification）。
- 红：该流最初漏发 HTTP 200/SSE 响应头，broker 返回
  `502 upstream_unavailable`，Harness Run 以 `agent_failed` 结束；补齐
  fixture 响应头后重跑通过。该缺陷只在测试 fixture，不涉及产品路径。
- 绿：真实 Harness 经 ACP `session/request_permission` 发出“执行授权”
  问题；经公开 question 接口回答“允许本次”后，工具实际执行
  `echo harness-tool-ok`，第二回合 provider 请求携带 `role=tool` 结果，
  Run 完成且助手消息正常。审计共 9 次 chat 请求（含 2 个工具回合）。

## 构建网络说明

- 首次 runtime 构建在 Debian 官方 CDN 的 `libz3-4` 上收到 502。
- 加入 `Acquire::Retries "5"` 后的第二次构建下载了 103 MB，但在
  `libnspr4` 上仍收到 502（同一次构建的其他包已成功）。
- 最终构建使用 Dockerfile 的可选 `APT_MIRROR` 构建参数，指向
  `https://mirrors.aliyun.com`；只改写构建期 apt 源，不修改宿主/WSL 全局
  配置，不关闭 apt 校验或任何安全检查。HTTP 镜像在 Docker 网络中超时，
  HTTPS 正常，因此实际使用 HTTPS。

