# 项目数据库分析

1. 同时加载 `data-analysis`、`project-docs` 与 `database-query`。`projects list` 返回的公司、名称和别名仅辅助匹配；目录只接受精确项目 ID。重名（尤其生产/测试）必须请用户选择，不能先试库。
2. 用 `projects resolve --project <精确 ID>` 核验绑定与文档。`connection_id` 为空、未知项目或文档不可用时停止数据库分析；不能仅凭表名回退到其他项目。按返回的 `document_path` 完整读取业务说明。
3. 确认指标、粒度、键关系、时间/时区、金额单位、退款口径。文档规则优先于表注释；未填规则标记未知。`tables` 只列配置允许的表；`describe` 核对实际结构。文档 SQL 是示例，须适配真实字段及权限，不能扩大白名单。
4. 用 `query --run-id <RUN_ID> --request <请求文件>` 执行参数化只读查询。请求 JSON 为 `{project_id, sql, parameters, max_rows, save_result}`；用命名绑定参数代替字符串拼接；查汇总优先库内 SUM/COUNT/GROUP BY。默认 200 行、上限 1000 行，每次取额外一行判断行截断。每次运行最多 40 次调用、累计返回 10000 行；忙或额度不足不自行无限重试。运行必须先用 `runs new` 创建。
5. 检查 `page.truncated` / `page.truncation_reason` / `page.returned_rows`。行、输出字节、字段字节或二进制字段的截断均保留记录。字段截断或二进制占位不能转为精确金额；缺失值不能补零。显式 LIMIT 只限定 SQL 的结果范围；即使 `truncated=false`，也必须结合 WHERE、聚合语义和时间覆盖说明范围，不能把一个明细值称总额。需要全量明细超过当前范围时如实说明尚无完整扫描。
6. 需要 Python、图表或报告时设 `save_result=true`，取得本次有界结果的 JSON 文件，包含 SQL、参数、来源和截断信息。直接读取 stdout 的 `saved.path`，不传地址、账号、密码或无关宿主机路径。保存失败不得重新查询来“补生成文件”。
7. 独立核验关键数字，检查连接放大、分项与总额、统计分母和时间边界。沿用 `references/output-spec.md` 与 `references/validation-checklist.md`，区分事实/解释/假设，为每个结论记录可交付状态和限制。

项目根命令：

```bash
python skills/builtin/database-query/scripts/db_query.py --config skills/builtin/database-query/config.yaml runs new
python skills/builtin/database-query/scripts/db_query.py --config skills/builtin/database-query/config.yaml query --run-id <RUN_ID> --request request.json
```

## JSON 输入代码

将 `input_path` 设为 `query` 输出中的 `saved.path`。本例保留原值；只有确认金额字段与单位后才显式转数值。重名列先按位置重命名，不可转字典丢失列。

```python
import json
from pathlib import Path
import pandas as pd

payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
page = payload["page"]
if page["truncated"]:
    raise ValueError("查询结果不完整，需先确认截断影响")
df = pd.DataFrame(page["rows"], columns=page["columns"])
if not df.columns.is_unique:
    raise ValueError("重名列需按位置显式重命名后再分析")
print(df.shape)
```

`truncated=false` 不替代 SQL 范围审核。Decimal 和大整数保存为字符串，日期保存 ISO；不自行使用本机时区改写，也不根据占位文字猜值。分析输出保存新文件，核对脚本退出码、超时与错误输出，并确认文件真实存在后交付。
