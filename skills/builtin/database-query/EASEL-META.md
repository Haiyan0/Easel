# Easel 迁移说明

## 来源

- 上游技能：`database-query`
- 上游平台：`smart_assistant` 本地可移植版本（其自身 `PROVENANCE.md` 记录了更早的 Smart_Assistant 来源与 revision）
- 迁移日期：2026-11-04

## 本次适配

| 项目 | 改动 |
| --- | --- |
| frontmatter | 改为 Easel 三字段：`name` / `description` / `layer: general` |
| 工具层 | 删除平台 `workspace.*` / `sandbox.*` / `artifact_id` 叙述，只保留本地 CLI 与 `saved.path` |
| 路径 | 命令统一为项目根相对路径 `skills/builtin/database-query/scripts/db_query.py` |
| 映射文档 | 删除原平台映射文档，改为 `references/easel-mapping.md`，只描述 Easel 本地等价物 |
| 配置 | 本机默认技能目录 `config.yaml`；多人 Docker 使用镜像内 `assets/config.multiuser.yaml`，凭据只走 `--env-file /run/easel-secrets/database-query.env` |
| 依赖 | `PyYAML`、`SQLAlchemy`、`asyncmy`、`sqlglot` 加入 Easel `pyproject.toml` 与 worker 固定依赖 |
| 清理 | 未复制 `manifest.yaml`、`config.yaml`、`state/`、`outputs/` 与任何缓存目录 |

## 保持原样

查询策略、SQL 白名单、预算、截断、脱敏、项目目录解析与结果 JSON 契约保持不变；技能自带的 Python 测试继续随目录提供，运行方式见 `pytest.ini`。
