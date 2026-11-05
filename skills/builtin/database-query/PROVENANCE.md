# 来源与改动记录

- 上游仓库：`C:\Users\hyh\Desktop\Smart_Assistant_Agent`
- 上游 revision：`bc6787d0b7e9a3a098307500d734f4cd2a6ec5b6`
- 设计依据与实施计划：上游提供的迁移设计文档（未随技能复制）

## 移植文件

| 本技能文件 | 上游来源 | 改动 |
| --- | --- | --- |
| `scripts/dbquery/policy.py` | `backend/src/smart_assistant/database/policy.py` | 仅改 import 来源；策略、白名单、常量与函数体保持原行为 |
| `scripts/dbquery/mysql.py` | `backend/src/smart_assistant/database/mysql.py` | 仅改 import；新增 `check_connection`（doctor --connect 专用） |
| `scripts/dbquery/types.py` | `backend/src/smart_assistant/database/types.py` | 保留结果形状与数据源错误契约；移除 Settings 与平台导入 |
| `scripts/dbquery/sources.py` | `backend/src/smart_assistant/database/sources.py` | 去掉会话/技能授权，保留 URL 校验与固定驱动归一 |
| `scripts/dbquery/budget.py` | `backend/src/smart_assistant/database/budget.py` | Redis 脚本改为本机 SQLite 原子事务；语义为 40 调用/10000 行/2 与 8 并发/30 秒租约 |
| `scripts/dbquery/results.py` | `backend/src/smart_assistant/database/results.py` | ArtifactService 改为本地原子写文件；保留结果 JSON 形状 |

## 移除的平台依赖

Settings、会话与技能授权、Redis 预算、Worker 装配、ArtifactService、平台产物登记、`skill_activate`/`skill.read_resource` 工具调用。

## 新增本地模块

`config.py`（版本化本地配置与显式 env 文件）、`catalog.py`（project-docs 目录与文档路径约束）、`service.py`（本地编排）、`cli.py`（子命令与 JSON I/O）、`redaction.py`（日志脱敏）。

## 已验证依赖版本

Python 3.10.12、pydantic 2.13.4、PyYAML 6.0.3、SQLAlchemy 2.0.49、asyncmy 0.2.14、sqlglot 30.18.0。
