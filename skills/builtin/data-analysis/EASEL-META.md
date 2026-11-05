# Easel 迁移说明

## 来源

- 上游技能：`data-analysis`
- 上游平台：`smart_assistant`
- 迁移日期：2026-11-04

## 本次适配

| 项目 | 改动 |
| --- | --- |
| frontmatter | 改为 Easel 三字段：`name` / `description` / `layer: attribute` |
| 工具层 | 平台文件工具、沙箱与产物登记改为项目本地文件、项目 Python 与真实文件路径 |
| 数据库 | 平台数据库工具改为 `project-docs` 目录 + `database-query` 本地 CLI |
| 路径 | 中间素材写入 `outputs/<主题>/assets/`，成品写入 `outputs/<主题>/`，遵循 Easel 产物规约 |
| 依赖 | `openpyxl`（xlsx）与 `xlrd`（xls）加入 `pyproject.toml` 和 worker 固定依赖 |
| 参考文档 | 12 份 references 全部保留，只替换工具、路径与交付语义；统计与质量规则不变 |
| 清理 | 未复制 `manifest.yaml` 与任何缓存目录 |

## 保持原样

轻量/完整两档模式、数据质量检查、统计口径、图表规则、复核清单与交付结构保持不变。
