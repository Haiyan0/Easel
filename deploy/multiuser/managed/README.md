# 受管 ACP profile 目录（Task A5）

本目录是最终 worker 镜像（`Dockerfile.worker` 的 `runtime` 阶段）唯一允许
放入的 Harness 公共配置。它**不含**供应商 Key、broker token、账号、`.env`、
浏览器 profile、开发者 HOME 或 outputs。

## 文件职责

| 文件 | schema | 职责 |
|---|---|---|
| `acp-profile.json` | **Easel manifest** `easel.managed-acp-profile/1` | 固定 Harness 仓库/commit/版本、Node/pnpm、公开 profile bundle、精确 ACP argv、broker 目的地和模型路由 |
| `acp.patch.yml` | **Harness 原生 cordis patch** | 覆盖 `dsh-acp-app` 的 provider/model，并启用仓内已有的 `dsh-llm-pi-ai` OpenAI-completions provider 指向本空间 sidecar broker |

两者不能混用：JSON 只是 Easel 的受管清单；Harness 只读取 YAML overlay。
`acp-profile.json` 中的 `patch` 字段与 `command` 里的 `--patch` 必须指向
YAML 文件。

## 固定输入

- Harness：`687f282e8625c6b8d11ff58211a90524be2e4317`
  （`0.2.1-rc.2`，公开仓库 `deepseek-ai/deepseek-harness`）。
- Node：`24.22.1`，满足 Harness `^22.19.0 || >=24.0.0`；镜像按 digest 固定。
- pnpm：`11.28.5`；构建期 `corepack prepare`，frozen lockfile 安装。
- 公开 profile：`acp` 模板，bundle 为
  `@deepseek-ai/dsh-base` + `@deepseek-ai/dsh-acp-app`。
- 默认模型：`deepseek-chat`，经 `easel-broker` 的
  `openai-completions` 路由；模型白名单仍由 control broker 的受管 chat
  路由执行。

## 构建

```bash
docker build -f deploy/multiuser/Dockerfile.worker \
  --target runtime \
  --build-arg DSH_COMMIT=687f282e8625c6b8d11ff58211a90524be2e4317 \
  -t easel-worker:<tag> .
```

构建阶段实际执行：

1. 从公开仓库取固定 commit（按 SHA；必要时回退到固定 tag，随后仍核对完整 SHA）；
2. `pnpm install --frozen-lockfile` 安装完整构建依赖；
3. `pnpm run build:native-system` 构建 Linux 原生 flock；
4. `tsc -b tsconfig.host.json` + `tsdown` Host 构建，产出
   `apps/cli/lib/bin.js` 与各 workspace 运行产物；
5. 最终镜像只复制已构建 `/opt/dsh` 与固定 `node`，运行阶段不联网安装。

`runtime` 阶段以 UID 10001、只读 rootfs 方式运行：

```bash
python deploy/multiuser/smoke_worker.py --check-files --require-dsh
```

它实际校验固定 checkout、CLI 产物、Node 引擎、Linux 原生 flock、profile/
patch 只读性，并启动真实 Harness 完成 ACP `initialize` 与 `session/new`。
`acp-profile.json` 或 patch 缺失、commit 不匹配、模型路由未选中
`easel-broker/deepseek-chat` 都直接使构建失败。

## 运行期

每个 workspace 的 worker 使用自己的可写 HOME；Harness 首次启动时在
`$DSH_HOME/profiles/acp` 自动物化公开 `acp` profile，并加载只读
`/opt/easel/managed/acp.patch.yml`。每次 Run 的受管快照注入：

- `EASEL_BROKER_URL`：`http://easel-sidecar-<workspace_id>:8899`；
- `EASEL_BROKER_TOKEN`：逐 Run opaque token；
- `EASEL_WORKSPACE_ID` / `EASEL_RUN_ID`：令牌范围复核身份。

Sidecar 只对精确 broker 路径把标准 `Authorization: Bearer <token>` 映射
为 control broker 的 `x-easel-broker-token`；供应商 Key 始终只在 control
的加密配置中，不进入 worker、profile 或镜像。

## 升级要求

升级 Harness 时必须同时更新：

1. `acp-profile.json` 的 commit、版本、Node/pnpm；
2. 重新核对 `acp.patch.yml` 引用的 row id 与 provider 字段；
3. 重新构建并保存新的镜像 digest、patch SHA-256 和真实 ACP 冒烟结果。

未完成以上步骤的 commit 或本地 `node_modules` 不得作为镜像输入。
