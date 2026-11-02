# Task 4 审计：返工轮 R1

日期：2026-10-19（Asia/Shanghai）。

审计提交：b2c43f3d93a6f52aeb9a96d1f05576d95b26aaca。
父提交：dd3da720d2dfa7e10b95f155f0cbfd79798d852d。

结论：暂不通过，两项可复现 P2。修复后复审，不进入 Task 5。

依据：[Task 4 交接单](../tasks/2026-10-19-task-04-config.md)、[plan](../plans/2026-10-16-multiuser-workspaces.md) Task 4、[spec](../specs/2026-10-16-multiuser-workspaces-design.md) 第 8 节。

## 已验证部分

迁移只追加 v3，migration 1/2 与父提交保持一致。管理员角色回查、发布版本比较与写入在事务内；个人留空凭证在事务内比较 provider/base_url/protocol，不会复制管理员 Key。AES-GCM 绑定受管版本或 workspace/channel，跨作用域替换有拒绝回归。配置 API 保持认证、CSRF 与专用数据库线程边界。

主审使用 PowerShell、C:\Code\Easel、C:\Users\hyh\anaconda3\envs\Agent\python.exe 实测：

```text
python -m pytest tests/multiuser -q
297 passed in 10.07s

python -m pytest tests/test_settings_stale_mirror.py tests/test_web_model_config_parity.py tests/test_local_write_guard.py tests/test_web_security.py tests/test_agent_harnesses.py tests/test_harness_workspace.py -q
248 passed, 6 skipped in 21.06s

git diff dd3da72 b2c43f3 --check
退出 0，无输出
```

独立审查另跑配置与存储测试，88 passed；并独立复现下面两项缺陷。现有绿色测试没有覆盖这两类输入。本轮未运行真实供应商、Linux 容器、前端构建或全仓测试。

## T4-R1-01 / P2：options 中的凭证字段明文入库并向 member 返回

位置：easel/control/config.py 第 112–115 行，相关 _sensitive_option_key 与 public_settings。

当前 options 递归校验使用关键词黑名单，只识别 api_key/apikey 等拼法，无法阻止 headers.api-key 这类明确的凭证字段。options 随后原样写入 public_json，并作为公共设置返回。这违背了本任务“只允许非敏感参数、凭证进入加密 bundle、公共响应脱敏”的边界；不需要等 Task 5 发起网络请求即可发生泄露。

真实 control API 探针使用临时库、假凭证、真实管理员/成员兑换 Cookie：

```json
{
  "defaults": {
    "speech": {
      "provider": "openai-compatible",
      "model": "tts",
      "base_url": "https://provider.example/v1",
      "protocol": "openai",
      "secret": "proper-encrypted-secret",
      "options": {"headers": {"api-key": "option-secret-sentinel"}}
    }
  }
}
```

```text
POST /api/admin/managed-config = 200
member GET /api/settings/models = 200
option_secret_in_member_response = True
option_secret_in_public_database_column = True
```

secret 字段本身虽被加密，options 仍形成另一条明文凭证通道。独立审查用另一临时库确认相同结果。

修复与验收：

- 在保存前限定 options 为已知非敏感能力参数；凭证/认证头、路由地址、底座参数不得通过 options 绕过专属字段。优先明确允许参数与结构，避免只为 api-key 单独补一条黑名单后留下其他拼法/嵌套通道。
- 默认配置、个人 complete 和 managed-model 使用一致的非敏感校验。无效输入返回明确 400，不写配置、不推进版本，不记录原始秘密。
- 回归至少覆盖 headers.api-key、混合大小写及嵌套认证头/凭证结构；断言公共 JSON/HTTP 响应不出现 sentinel，失败后版本、原配置和原凭证不变。
- 保留正常 speed 等已支持非敏感参数。不能靠删除所有 options、仅在 GET 时隐藏泄露值、或把任意 options 全部当秘密加密来代替输入边界。

## T4-R1-02 / P2：非有限数字可以持久化并使设置读取持续 500

位置：easel/control/config.py 第 122–123 行，相关 _canonical；故障出口 easel/control/app.py 第 436 行。

options 的所有 float 都被接受；_canonical 默认允许非标准 Infinity/NaN。合法 JSON 数字 1e309 经 json.loads 转为 inf 后被成功发布并持久化；随后 JSONResponse 拒绝非有限数值，普通成员读取设置返回 500。错误发生在成功保存之后，重试 GET 不能恢复。

真实 API 探针在正常 speech 路由中使用：

```json
"options": {"speed": 1e309}
```

```text
POST /api/admin/managed-config = 200
managed_revisions.public_json 包含 Infinity
member GET /api/settings/models = 500
```

独立审查确认 JSONResponse 抛出 ValueError: Out of range float values are not JSON compliant。

修复与验收：

- 在持久化前递归拒绝非有限数字，可用严格 JSON 序列化作为第二道防线；错误映射为明确 400，不在 GET 时捕获后伪装成功。
- 覆盖正/负溢出数字、NaN/Infinity 表示及嵌套字典/数组；服务层公开保存方法与 HTTP 路径均不得写入毒化值。正常有限小数继续可用。
- 默认、个人 complete、managed-model 都必须失败原子：不写入新版本、不覆盖原覆盖项；后续 member 设置读取仍为 200，原有效配置保持。
- 无需迁移或自动修复生产历史数据，本次探针只使用临时库；不因此扩展到 Task 5。

## 非阻塞证据说明

tests/multiuser/test_config.py 的 make_v2_database 当前直接执行本版本导入的 MIGRATIONS[0]/[1]，并非交付报告所称的独立“冻结 v2 DDL”。本次 diff 已确认旧迁移未修改，因此不另列第三项阻塞。建议将旧库 fixture 改为独立冻结内容或旧提交实现生成，并准确描述证据，避免未来改写迁移后测试仍自洽通过。

个人覆盖清除后 revision 从 1 重启、managed-model 在新 allowlist 不再允许旧模型时 public_settings 与 resolve 状态不一致，留作后续版本引用及可用状态集成关注点；本轮不要求提前实现历史 broker 快照或新 UI。

## 返工边界

仅修复 T4-R1-01/02 及必要回归，不修改 spec 降低要求，不实现 broker/出口/容器或前端，不重构已通过的认证与存储约束。

先补红测试，再最小实现。运行上述两组 pytest 和 git diff --check，报告真实计数与红绿证据；显式路径、中文信息提交，不夹带审计/任务单或其他工作。交付新 SHA 后停止等待复审，不进入 Task 5。
