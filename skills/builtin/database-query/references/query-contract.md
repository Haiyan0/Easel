# 查询与输出契约

## 请求

`query --run-id <ID> [--request FILE]`。`--request` 缺省时从 stdin 读取；两种方式都只接受一个 JSON 对象：

| 字段            | 类型    | 必填 | 说明                                               |
| --------------- | ------- | ---- | -------------------------------------------------- |
| `project_id`  | string  | 是   | `project-docs` 目录中的精确项目 ID               |
| `sql`         | string  | 是   | 单条只读 SQL，≤ 32 KiB；使用 `:name` 命名占位符 |
| `parameters`  | object  | 否   | 与 SQL 中的命名占位符**精确一一对应**        |
| `max_rows`    | integer | 否   | 1–1000，默认 200                                  |
| `save_result` | boolean | 否   | 是否把本次有界结果写入 JSON 文件                   |

未知字段、类型不符、非法 JSON 与超过 64 KiB 的请求一律返回 `cli.invalid_request`，退出码 2。请求中不允许出现连接 URL、驱动名或任意驱动参数。

## 成功响应

```json
{
  "version": 1,
  "project_id": "…",
  "connection_id": "…",
  "queried_at": "2026-10-28T12:00:00+00:00",
  "sql": "…",
  "parameters": {},
  "page": {
    "columns": ["…"],
    "rows": [["…"]],
    "returned_rows": 1,
    "truncated": false,
    "truncation_reason": null,
    "next_cursor": null,
    "elapsed_ms": 12
  },
  "saved": null
}
```

- `columns` 按位置与每行对应；重名列各占一格，不合并。
- `rows` 是数组行而不是字典；Decimal 与大整数以字符串保留精度，日期时间用 ISO 格式。
- `returned_rows` 是返回给调用方的行数（不含截断探针多读的那一行）。
- `truncated=false` 只代表本次结果完整，不代表 SQL 覆盖了全部业务范围。
- `next_cursor` 普通查询恒为 `null`。

### 保存结果时

`save_result=true` 时：

```json
{
  "page": {"columns": ["…"], "rows": [["预览行"]], "returned_rows": 1000, "truncated": true, "truncation_reason": "row_limit", "next_cursor": null, "elapsed_ms": 12},
  "saved": {"path": "C:\\…\\outputs\\query-20261028T120000Z-….json", "bytes": 12345, "preview_rows": 20}
}
```

- `page.rows` 只含前 20 行预览；`page.returned_rows` 仍是本次查询的总行数。
- 完整的有界结果在 `saved.path` 指向的 JSON 中，字段与 stdout 的 `version/project_id/connection_id/queried_at/sql/parameters/page` 相同。
- 结果文件只允许写入配置的 `output_dir` 内；拒绝路径逃逸与覆盖已有文件，采用临时文件原子落盘。
- 保存失败不会重新执行查询。

## 截断原因

| 取值             | 含义                                   |
| ---------------- | -------------------------------------- |
| `row_limit`    | 命中 `max_rows` 行上限               |
| `output_bytes` | 命中 256 KiB 总输出上限                |
| `field_bytes`  | 单字段超过 16 KiB，已按 UTF-8 字节截断 |
| `binary_field` | 二进制字段只报告类型与长度，内容未返回 |

同时命中多条时按「行上限 > 总输出 > 单字段 > 二进制」报告。

## 退出码与错误

| 退出码 | 含义                                                 |
| ------ | ---------------------------------------------------- |
| 0      | 成功                                                 |
| 1      | 领域错误（项目、配置、数据源、SQL 策略、执行、预算） |
| 2      | 用法或请求格式错误                                   |

错误以单个 JSON 对象写到 stdout：

```json
{"error": {"code": "project.not_found", "message": "项目不存在或未建档"}}
```

stderr 只输出 `code: message`，不含 SQL 正文、参数取值、连接 URL 或口令。

稳定错误码：

- `config.unavailable`、`project.config_unavailable`、`project.not_found`
- `database.source_unavailable`、`database.unsupported_dialect`
- `database.sql_not_readonly`、`database.sql_unsupported`、`database.table_not_allowed`、`database.limit_exceeded`
- `database.timeout`、`database.connection_failed`、`database.query_failed`
- `database.run_not_found`、`database.run_expired`、`database.run_exists`、`database.budget_exceeded`、`database.budget_unavailable`、`database.busy`
- `database.result_unavailable`、`database.result_path_invalid`、`database.invalid_argument`
- `cli.invalid_request`

## 运行与预算

- `runs new [--run-id ID] [--ttl-seconds N]` 显式创建运行；不提供 ID 时生成 `run-<随机串>`。TTL 默认 86400 秒，范围 1–86400。
- 未知运行 ID 不会自动新建；`runs show` 可查看 `calls/rows/deadline/expired`。
- 每个运行最多 40 次调用（`tables/describe/query` 都计数）、累计 10000 行；`query` 按 `max_rows` 预留，执行后按实际 `returned_rows` 结算。
- 单数据源并发 2、全局并发 8；租约 30 秒到期。预算是本机协作约束，不是跨主机配额。
