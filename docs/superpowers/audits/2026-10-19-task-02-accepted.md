# Task 2 复审通过

日期：2026-10-19（Asia/Shanghai）。

审计提交：d5a44c332d058d5cffae240de1a9743a384aff3a；父提交：64b7abd560b1db70f73d20506f12ed9271fc80b2。

结论：Task 2 通过，三项 R1 阻塞关闭。本次聚焦[上轮发现](2026-10-19-task-02-review-round-1.md)及修复引入的回归；未发现新的 P1/P2。允许按原计划进入 Task 3，不代表多人部署已通过发布门槛。

## 修复核验

| 问题 | 核验依据 | 结论 |
|---|---|---|
| T2-R1-01 并发绕过限速 | BEGIN IMMEDIATE 内完成双维限速、邀请兑换与失败记账；denied 正常提交后再映射 401。单 app、双 app、多连接竞争测试通过 | 关闭 |
| T2-R1-02 畸形 token 500 | surrogatepass 保证摘要/HMAC 编码可处理孤立代理项；非法类型、空值、超长和孤立代理项均有回归，重复失败进入 429 | 关闭 |
| T2-R1-03 HTTP/非法公共 Origin | 配置及 create_app 校验 HTTPS Origin；CLI 在打开控制库前拒绝非法配置。合法配置兑换/me/logout 回归通过 | 关闭 |

主审额外使用临时数据库和 24 个独立连接，同时从不同 source 兑换同一合法邀请。结果：1 ok、10 denied、13 rate_limited；数据库 1 个 Login、10 条失败记录。单次兑换与摘要维限速在同一竞争中共同成立。没有修改产品文件。

独立审查复核了原子事务路径及输入修复，没有发现本轮范围内的 P1/P2。

## 本机验证

使用 C:\Users\hyh\anaconda3\envs\Agent\python.exe，PowerShell，C:\Code\Easel。

```text
python -m pytest tests/multiuser -q
145 passed in 7.64s

python -m pytest tests/test_setup_auth.py tests/test_agent_harnesses.py tests/test_harness_workspace.py -q
8 failed, 121 passed, 7 skipped, 5 warnings in 17.40s

git diff 64b7abd d5a44c3 --check
退出 0，无输出
```

八项失败仍全部在 test_setup_auth.py，失败用例名与上轮已核实的 Task 1 父基线一致；本提交未修改该文件或 setup 脚本。包含 Bash 断言失败及 GBK 子进程解码警告，不能宣称指定合并回归全绿。

未运行全仓测试、前端构建、真实供应商调用或 Linux 容器发布门槛；本轮未实现这些部分。

## 非阻塞维护项

easel/control/store.py 中 check_auth_rate_limit 和 record_auth_failure 各有两处定义；后面的旧定义覆盖前面的重构定义。当前语义等价，HTTP 使用 redeem_invite_limited 原子路径，不受影响。建议后续触及该模块时合并重复定义，保留接口与既有测试，不为此再开启阻塞返工轮。

## 后续

继续[原计划](../plans/2026-10-16-multiuser-workspaces.md) Task 3，执行[交接单](../tasks/2026-10-19-task-03-policy.md)。保持 Task 1/2 通过的存储、迁移、认证与线程规则。审计及任务单不混入产品提交。
