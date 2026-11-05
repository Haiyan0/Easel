# Easel 迁移说明

## 来源

- 上游技能：`geo-content-sop`
- 上游平台：`smart_assistant`
- 迁移日期：2026-11-04

## 本次适配

| 项目 | 改动 |
| --- | --- |
| frontmatter | 改为 Easel 三字段：`name` / `description` / `layer: produce` |
| 路径 | `geo-output/` 改为 `outputs/<选题>/`；中间记录、样本工作副本放入 `assets/` |
| 工具层 | 平台文件、分页、Artifact、沙箱与文档工具改为本机文件、项目 Python 和运行环境检索工具 |
| 联网 | 优先用运行环境检索/抓取；新增 `scripts/web_search.py` 作为 Tavily 回退，密钥只从环境变量或 `--env-file` 读取 |
| 样本更新 | 本地 Python 确定性重算；只改任务目录内的 sample-update 工作副本，不写发布版 skill 目录 |
| 清理 | 未复制 `.secrets/`、`manifest.yaml`、`migration.json` 与任何缓存目录 |

## 保持原样

阶段 0–6 的确认顺序、EEAT 评分口径、47 篇历史基准、五模块结构与三份交付物保持不变。
