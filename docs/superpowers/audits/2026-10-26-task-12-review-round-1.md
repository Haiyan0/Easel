# Task 12 首轮审计与 R1 定点返工

日期：2026-10-26（Asia/Shanghai）。父基线：a6fc17713c65cd0acced72fffdccf72586878d6c。当前 SHA：359fcba90b712f43e81899d982e7a0c864e371e5。

结论：暂不通过。下面三项 P2 均已通过实际组件/真实接口验证，主审另在真实 HTTPS/Chromium 上复现。仅修本单，不进入 Task 13。

依据：[Task 12 交接单](../tasks/2026-10-25-task-12-control-settings.md)。主审及独立审查者未修改产品、未提交代码；未调用真实供应商或访问开发者账号/配置。

## 独立验证与已确认进展

- tests/multiuser：926 passed、3 skipped、2 warnings，81.27 秒。
- 全部前端 Node：109 passed。
- 八文件兼容回归：272 passed、6 skipped，21.68 秒。
- npm run build、npm run lint：退出 0；保留既有大于 500 kB 构建提示。
- browser_task12.py：退出 0，5 项结果全 true、FAILURES 为空；Chromium 151.0.7922.34。
- git diff a6fc177 HEAD --check：退出 0。
- 独立配置审查的针对性测试 12 项通过；管理员版本 CAS、事务内身份复查、AAD、未提交通道保留、空凭证作用域检查和公共投影未发现重要问题。

管理员成员创建固定为 member，禁用/启用不复活旧凭证，管理写操作在事务内重新查角色/禁用状态；已检查的前端身份隔离、409 保留表单及聊天树保持路径成立。本次不重写这些已成立的边界。

现有全绿结果未覆盖下面的受管模型编辑、多页管理和多字段凭证输入。本轮没有独立重跑全部 Task 11 浏览器脚本；交付报告中的历史脚本结果属于执行方证据，不能当成主审本轮运行结果。

## T12-R1-01（P2）：受管模型原样保存会删除个人参数并改变跟随语义

定位：./web/frontend/src/lib/controlSettings.ts 的 formFromChannel（约 393–410 行，尤其 403/407）、buildPersonalRoute；./easel/control/config.py 的 public_settings（约 850–870 行）。

managed-model 的服务端视图是生效模型和合并后的 options。前端却把生效 model 当作显式个人选择，同时将 options 固定置空。用户只是重开设置并点保存，原覆盖即被静默改写。

主审真实浏览器使用真实配置服务建立“省略 model，跟随默认，个人 speed/voice”的覆盖；不修改表单直接保存，得到：

```text
表单 options 为空；表单 model=admin-v2
保存前：{"route_kind":"managed-model","options":{"speed":1.7,"voice":"alloy"}}
保存后：{"route_kind":"managed-model","model":"admin-v2","options":{}}
```

独立真实 API/TS 转换还确认：管理员以后更新默认模型，该用户仍停在旧固定模型，个人 speed 也已经丢失。这是持久配置变化，不只是显示问题。

返工：区分“生效摘要”和“个人编辑原值”。必要时给 GET 公共视图补充白名单个人覆盖元数据：是否显式指定 model，以及个人原始非敏感 options；保留既有响应字段。不返回密文、credential_ref、凭证或数据库行。表单只编辑这份个人原值，省略 model 继续跟随；不能直接把合并 options 全复制成个人覆盖，否则会把管理员参数也意外固定。

验收：

- 已有 managed-model 带个人参数，原样保存不丢任何参数；只改模型不改其它个人参数。
- 省略 model 的覆盖保存后继续省略；管理员改默认时跟随。显式固定模型的覆盖继续固定。
- 管理员修改未被个人覆盖的参数时，个人继续继承；个人显式值恰好等于当时默认，也不能被擅自当作“未覆盖”。
- 恢复继承、完整个人路由和 A/B 隔离保持通过。失败不改原配置/密文，不把管理员凭证转成个人凭证。

## T12-R1-02（P2）：成员和邀请的第二页在界面不可达

定位：./web/frontend/src/components/AdminSettingsPage.tsx 初次读取（约 103–135 行）、refreshInvites（约 218–224 行）及成员/邀请渲染。

服务端支持 nextOffset，但组件仅保存第一页数组，丢弃两个分页游标；所有读取都是 limit=50&offset=0，没有加载更多/下一页。成员按创建升序，超过前 50 项的新成员在重开管理页后无法禁用、撤销登录或选择签发邀请；较旧待兑换邀请也无法从列表撤销。

主审真实浏览器建立超过 50 个成员/邀请，并经同一登录的真实 HTTP 验证第二页数据存在：

```text
第二页 API 含末尾成员：True
末尾成员禁用按钮数：0
末尾成员邀请 option 数：0
加载更多/下一页按钮数：0
第二页 API 含旧待兑换邀请：True
该邀请在页面中：False
```

返工：成员和邀请分别保存/消费分页游标，提供有界可继续的 UI；邀请目标选择也能到达后续成员。刷新/管理动作不能仅保留第一页而悄悄丢掉已选目标。不得改成一次无界加载所有记录，也不靠临时前端 append 新成员代替重开后的可达性。继续沿用身份/请求所有权与服务端分页上限。

验收：至少 51/101 个成员与邀请，首尾及后续记录均可管理；后页成员可禁用、重新启用、撤销登录和签发邀请，后页待兑换邀请可撤销。刷新、创建和撤销之后分页与目标选择稳定；未知目标、越权、禁用 actor 仍明确拒绝。邀请列表不返回/缓存 token。

## T12-R1-03（P2）：完整个人路由无法提交多字段凭证

定位：./web/frontend/src/lib/controlSettings.ts 的 RouteForm.secret 和 completeRoute（约 105/370 行）；./web/frontend/src/components/ControlSettingsPage.tsx 的单个 API Key 输入（约 363–369 行）。后端契约位于 ./easel/control/config.py 的 _normalize_secret。

前端永远把 secret 作为字符串发送，但已有 registry/API 的多字段凭证需要对象，例如 MiniMax 的 MINIMAX_API_KEY + MINIMAX_GROUP_ID、Kling 的 Access/Secret。普通 Key 或把 JSON 粘进现有 password 输入，都不会变成合法 bundle。

主审真实浏览器在个人完整路由表单填入 MiniMax 凭证 JSON 文本，保存返回 invalid_secret_bundle；随即使用同一 Cookie/CSRF、相同路由和字段值，以真正 secret 对象提交既有 API，得到 200。独立审查亦确认字符串/JSON 文本都是 400，对象为 200。

返工：按已有供应商凭证结构表示和提交必要字段，输出后端已经支持的 typed bundle；单 Key 兼容保留。全部留空时沿用“仅同作用域复用本人原凭证”的契约；缺少必需字段明确拒绝，不把部分新输入与未知旧密钥任意拼接。字段来自公共 schema/registry 元数据，不从服务端读回旧值。

这不要求新增 provider adapter、真实供应商测试或开放收费调用。尚无 adapter 的供应商仍显示不可用状态，管理员默认发布仍受既有 adapter 检查；后端个人多字段测试通过不能代替 GUI 新建/轮换验收。

验收：

- 单 Key、多字段 Key、Key + 必填非秘密字段的实际页面输入，能形成准确 payload；不使用 options/env/headers 旁路。
- 个人多字段路由新建、合法全量轮换、全部留空复用、换目的地址但缺字段拒绝；失败前后原密文/配置不变。
- 公共响应、URL、缓存、日志和截图无凭证 sentinel；离开页面/退出清理输入。未装 adapter 的状态不得伪装为真实连接成功。

## 执行与交付

从 359fcba 开始，仅修上述三项及必要测试，保持既有管理员事务授权、If-Match、密文作用域和 Task 11 聊天生命周期。先为每项补当前提交会失败的实际组件/HTTP/浏览器反例，再实现；不仅测试纯 helper。

完成后运行全部 Node、tests/multiuser、交接单八文件兼容回归、构建/lint和补强的 Task 12 浏览器脚本；按原交接单复跑 Task 11 系列，记录真实结果。报告父/新 SHA、显式文件、逐项红绿、API 形状变化及未执行项。

主审 plan/spec、审计和任务单保持不暂存、不提交；停止等待复审，不进入 Task 13。Task 8 正式部署门槛和非聊天业务未开放范围保持原状态。
