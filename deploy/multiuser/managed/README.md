# 受管 ACP profile 目录（Task 8）

最终 worker 镜像（`Dockerfile.worker` 的 `runtime` 阶段）要求本目录存在
`acp-profile.json`，且构建时显式传入已验收的 DeepSeek Harness commit：

```bash
docker build -f deploy/multiuser/Dockerfile.worker \
  --build-arg DSH_COMMIT=<已验收 commit> -t easel-worker:<tag> .
```

缺少 `acp-profile.json` 或 `DSH_COMMIT` 时构建直接失败，不会生成未固定版本
的镜像。本仓库**不放入**未经实际验收的 ACP profile 占位文件，也不放入任何
模型 Key、账号凭证、开发者 `.env`、outputs、浏览器 profile 或 HOME。

管理员提供该文件时必须满足：

- 内容来自实际验收通过的 ACP/DeepSeek Harness 配置，而非模板或伪造值；
- 只包含受管公共配置，不含私钥、master key、服务 token 或用户 Cookie；
- 构建时被 `COPY` 到 `/opt/easel/managed` 并设为只读；
- 记录对应的 Harness commit 与验收日期，供审计复核。

运行阶段不在线安装 npm/pip 依赖；Harness 依赖在构建时锁定并安装。
