# Easel 迁移说明

## 来源

- 上游技能：`project-docs`
- 上游平台：`smart_assistant`
- 迁移日期：2026-11-04

## 本次适配

| 项目 | 改动 |
| --- | --- |
| frontmatter | 改为 Easel 三字段：`name` / `description` / `layer: general` |
| 工具层 | `project.list` / `project.resolve` 改为读取 `references/projects.yaml` 或调用 `database-query` 的 `projects list/resolve` |
| 文档读取 | 直接按 `document_path` 读取本地文件，不再走平台资源分页接口 |
| 隐私 | references 目录下的「元梦空间」私有业务文档只在本机保留，已加入仓库 `.gitignore`；`projects.yaml` 与 `TEMPLATE.md` 可入库 |
| 依赖 | 无新增依赖；查询运行时依赖 `database-query` 技能 |

## 保持原样

项目目录结构、精确 ID 规则、业务口径优先级、文档与结构冲突处理、模板字段保持不变。
