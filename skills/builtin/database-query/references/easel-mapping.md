# Easel 工具映射

本技能是纯本地 CLI，不依赖平台工具层、会话授权或产物服务。Agent 在项目根通过项目 Python 调用 `skills/builtin/database-query/scripts/db_query.py`。

## 任务与本地命令

| 任务 | Easel 做法 |
| --- | --- |
| 列出项目 | `projects list` |
| 解析精确项目 | `projects resolve --project <精确 ID>` |
| 读取业务文档 | 直接读取 `projects resolve` 返回的 `document_path`；该路径已核验位于 `project_docs_root` 内 |
| 列出授权表 | `tables --run-id <RUN_ID> --project <精确 ID>` |
| 描述表结构 | `describe --run-id <RUN_ID> --project <精确 ID> --table <表名>` |
| 执行查询 | `query --run-id <RUN_ID> --request <请求文件>`（或 stdin） |
| 保存有界结果 | 请求里设 `"save_result": true`，读取 stdout 的 `saved.path` |
| Python 分析 | 直接用项目 Python 读取本地 `saved.path`，不需要准备输入副本 |
| 运行预算 | 先 `runs new`，再在 `query`/`tables`/`describe` 中复用 `--run-id` |

## 与其它 Skill 的分工

| Skill | 职责 |
| --- | --- |
| `project-docs` | 项目目录、业务口径、字段含义与表关联 |
| `database-query`（本 SKILL） | 连接、允许的 SQL、预算、截断与结果文件 |
| `data-analysis` | 统计、趋势、异常、图表与报告 |

平台侧的会话、技能启用状态与摘要授权在 Easel 中没有对应对象。本地工具只做配置校验，不是权限边界；数据库端最小权限只读账号仍然必需。

## `data-analysis` 接入示例

`query` 输出 `saved.path` 后，用项目 Python 读取完整有界结果：

```python
import json
from pathlib import Path

import pandas as pd

result_path = Path(saved_path)  # query stdout 的 saved.path，或已知的本地结果文件
payload = json.loads(result_path.read_text(encoding="utf-8"))
page = payload["page"]

if page["truncated"]:
    raise ValueError("查询结果不完整，需先确认截断影响后再计算")

df = pd.DataFrame(page["rows"], columns=page["columns"])
if not df.columns.is_unique:
    raise ValueError("重名列需按位置显式重命名后再分析")

print(df.shape)
```

注意事项：

- 保存结果时，stdout 的 `page.rows` 只是前 20 行预览；完整的有界结果在 `saved.path` 文件中。不要用预览行推算全量。
- `page.returned_rows` 是本次查询返回的总行数；`truncated=true` 时结果不完整，`truncated=false` 也不代表 SQL 覆盖了全部业务范围。
- Decimal 与大整数以字符串保存，日期时间用 ISO；不要在不确认口径与单位时静默转数值。
- 重名列按位置保留，转字典会丢失列；先按位置重命名。
- 结果文件可能包含业务信息与查询参数，按私有输出处理，不写入可公开分享的位置。

## 部署检查清单

1. 把 `project-docs` 的 `projects.yaml` 与业务文档放在配置的 `project_docs_root` 下。
2. 在配置中为每个 `connection_id` 建立 `secret_env`、`allowed_schema`、`allowed_tables`；凭据只放环境变量或显式 `--env-file`。
3. 用上表的本地命令执行；不要尝试在 Easel 中恢复平台会话或技能授权语义。
4. 准备数据库端最小权限只读账号；`readonly_account_verified` 只是部署记录。
5. 用 `doctor` 做本地自检，再用 `doctor --connect --project <ID>` 验证真实连接与 schema。
