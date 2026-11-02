# Task 2 审计：第 1 轮

日期：2026-10-19（Asia/Shanghai）

被审提交：64b7abd560b1db70f73d20506f12ed9271fc80b2；父提交：0190780。

结论：**尚未通过。修复本轮三项 P2 后复审，不进入 Task 3。**

依据：[Task 2 执行任务单](../tasks/2026-10-19-task-02-auth.md)、[设计规格](../specs/2026-10-16-multiuser-workspaces-design.md)。只审本提交九个文件；审计未修改产品、测试、分支或暂存区。

## 已通过的复核

- 邀请/登录 token 分别生成，仅摘要入库；CSRF 单向派生，Cookie 属性和脱敏身份视图符合要求。
- 管理员邀请回查真实角色；bootstrap、邀请兑换、Login 创建在事务中完成；禁用、撤销和闲置/绝对期限的主要路径有效。
- migration 2 仅追加，migration 1 未改写。独立审查者使用 0190780 真实旧实现创建 v1 数据库，再由四个独立连接同时升级；旧会话、Run、归属及幂等记录保留，迁移记录为 1、2。
- 独立探针制造 Login 唯一键写入失败，邀请已兑换标记一同回滚；随后仍可正常兑换。先禁用再兑换被拒，先兑换再禁用后新连接认证被拒。
- API lifespan 使用独立数据库线程，未发现跨线程使用 SQLite 连接的问题；本地 CLI 不新增远程无认证 bootstrap。

## 独立测试与旧基线

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser -q
# 99 passed in 3.92s

& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/test_setup_auth.py tests/test_agent_harnesses.py tests/test_harness_workspace.py -q
# 8 failed, 121 passed, 7 skipped, 5 warnings in 15.88s

git diff 0190780 64b7abd --check
# 无输出，退出 0
```

八个失败全部位于 test_setup_auth.py。主审另用 git archive 提取父提交 0190780 的相关源码到独立临时目录，以相同解释器运行该文件：8 failed、22 passed、1 skipped、5 warnings，用例名与当前八个失败一致。此项已确认是父提交亦有的失败，不列入 Task 2 返工；不把它笼统归结成单一编码原因，也不声称整个回归全绿。

## T2-R1-01 / P2：并发兑换绕过十次失败上限

位置：easel/control/app.py 第 181–196 行；关联 Store.check_auth_rate_limit / record_auth_failure 的分离事务。

限速检查、实际兑换、失败计数分别提交到数据库执行器，中间都有 await。即使执行器只有一个线程，不同请求的三步仍会交错；多个请求可以先全部通过检查，再逐个失败并计数。单次检查内部有事务不能覆盖整个判定流程。

主审用真实 TestClient、20 个线程和 Barrier 同时发起同来源、不同错误 token 的请求，无替换服务逻辑：

```text
CONCURRENT_INVALID_EXCHANGE_STATUS {401: 20}
RECORDED_FAILURES 20
```

此时窗口内应最多允许十次失败验证，后续应为 429；现有顺序请求和多个实例轮流调用的测试未覆盖并发情况。

修复与验收：

- 将限速准入、兑换结果和失败记账做成跨连接成立的原子流程；可以新增必要的 Store/服务内部操作，但保留既有公开接口。
- 不要只加单 app 的 asyncio.Lock 或依靠单线程执行器；两个 app/进程连接同库时也必须受约束。
- 被拒的合法邀请不得被消耗；失败记账必须提交，不能因抛出 401 导致事务回滚掉计数。
- 增加同来源不同 token、不同来源同 token、多 app 并发测试；断言最多十个 401，其余 429，Retry-After 正确，窗口过后恢复。测试用同步机制制造竞争，不用 sleep 碰运气。

## T2-R1-02 / P2：畸形字符串在限速前触发 500

位置：easel/control/auth.py 第 37–39、56–62 行；入口 app.py 第 177–182 行。

JSON 中可包含孤立 UTF-16 代理项转义，例如 token 为 JSON 转义 U+D800。json.loads 得到 Python 字符串后，限速键生成直接 encode('utf-8')，抛 UnicodeEncodeError。_valid_token 只检查类型和长度，且限速摘要在 exchange 的合法性检查之前生成。

真实 HTTP 探针：

```text
MALFORMED_ESCAPED_SURROGATE_STATUS 500
MALFORMED_ESCAPED_SURROGATE_FAILURE_COUNT 0
```

这违反非法凭证统一 401 的要求，并使该类请求绕过失败计数。

修复与验收：

- 在哈希前统一处理非法 token 表示，限速键生成对所有可解析输入必须安全；不能只修 exchange，而让它之前的限速摘要继续抛异常。
- 覆盖 JSON 转义高/低孤立代理项、非法类型、空值、超长和正常错误 token。首次为统一 401，无 Set-Cookie、不创建 Login、不消耗有效邀请；重复畸形请求也受限速，达到上限后为 429。
- 服务层公开认证方法同样不得对非法凭证抛出未处理编码异常。日志不回显 token，不以一个全局捕获 500 并伪装成功替代输入处理。

## T2-R1-03 / P2：不校验公共 Origin 的 HTTPS 和合法结构

位置：easel/control/app.py 第 43–46 行；CLI cmd_serve_control 第 59 行。

ControlConfig 仅检查非空并删除末尾斜杠，接受 http://easel.test。当前控制面永远设置 Secure Cookie，结果是管理员能够启动无法完成登录的配置：

```text
HTTP_PUBLIC_ORIGIN_ACCEPTED 200
HTTP_COOKIE_AUTH_ME_STATUS 401
```

有效邀请已被兑换，但 HTTP 浏览器请求不会带回 Secure Cookie。带路径、query、fragment 等不是浏览器 Origin 的字符串也未在启动时拒绝，会导致所有正常 Origin 校验失败。Task 2 明确要求公共站点 HTTPS；这个配置应在接纳邀请前失败，而不是在成功兑换后才表现为无法登录。

修复与验收：

- 配置初始化/启动时校验公共 HTTPS Origin：有合法主机/端口，不含用户信息、路径（可规范化单个根斜杠）、query 或 fragment；不接受空白/非法 URI。
- 不通过信任请求 Host/Forwarded 头弥补配置。仍允许 TLS 在可信上游终止；验证的是公共 Origin，不要要求反向代理到应用的内部传输也必须 HTTPS。
- 增加 CLI 和配置单元用例，证明非法配置在 HTTP 服务开始处理邀请之前失败；合法 HTTPS Origin 的现有兑换/me/logout 继续通过。
- 不降低 Cookie 的 Secure/__Host- 约束，也不为 control 增加无认证 local 回退。

## 返工边界

本轮为 Task 2 修复轮 R1，只处理 T2-R1-01/02/03。保留已通过的迁移、授权、兑换原子性、撤销和线程管理。不要求本轮增加用户注册、Task 3 路由策略或后续部署功能。

允许在 Task 2 既有模块和测试中做必要改动；如 schema 需新增表/字段，追加迁移并保留 v1/v2 升级，不回写历史 migration。不要修改 spec/任务单来降低要求，不修与本次无关的八个 setup 失败，不夹带审计文档和其他工作。

先分别补失败测试，报告三项的红绿证据；运行 tests/multiuser 和指定合并回归，准确区分通过/失败/跳过与已确认基线。显式路径提交，中文提交信息，交付新 SHA 后停止等待复审。
