# Task G1：独立 gateway 认证与转发核心

基线：`8a5c16be2b80328f234ad4693b00b6795a23a12f`。

先读 AGENTS.md、Desktop 装配计划、`../audits/2026-10-27-desktop-a2-accepted.md`、`../audits/2026-10-27-desktop-a3-accepted.md`、deploy/desktop/topology.json 的 gateway 契约。批准单附加条款优先于快照中的旧流程。

## 本轮目标与范围

实现可独立测试的 gateway：验证 control 的逐请求 Ed25519 断言，只转发固定 workspace/worker 的精确内部路由，重建 worker 服务身份，保留响应语义并正确关闭连接/流。

仅开发独立核心及签名工具、测试。**不修改 provisioner、WorkerHandle/WorkerLease、schema、现有 WorkerRelay 装配或 A1 Compose；不进入 A4。** G2 再接 Docker 创建/启动握手、地址绑定、公钥挂载、实例代次持久化与恢复。这样 G1 可单独审查，不以一次大提交掩盖协议与生命周期缺陷。

## 文件和公开边界

- 新建 `easel/gateway/__init__.py`、`easel/gateway/auth.py`、`easel/gateway/app.py`：签名格式/校验、固定配置与 gateway ASGI 应用。现有目录不存在时按此建立，不复制 worker RunManager。
- 新建 `easel/control/gateway_auth.py`：control 端逐请求签名工具；本轮不自动接入已有控制路由。
- 新建 `tests/multiuser/test_gateway_auth.py`、`test_gateway_forward.py`；新增实际 TCP 测试可放 `tests/multiuser/test_gateway_tcp.py`。
- README/topology.json 同步本轮协议的精确字段与实际行为；不要改主审文档，不宣称全链路运行时启用。

接口要求：固定配置包含 workspace_id、gateway_instance_id、每次启动新生成的 boot_generation、worker_container_id、config_version、固定 worker origin 和 worker service token（repr 隐藏）。应用工厂 `create_gateway_app(...)` 接收该固定配置、只读 Ed25519 公钥、可注入时钟/HTTP transport；这些仅是受信部署配置，不来自普通请求字段。签名工具输入匹配的实例 audience、method、规范 path/query 和实际 body 字节，输出单个内部断言头。类型/参数名由实施者在第一步固定到代码与测试，保持职责清楚。

## 必须先固定的协议规则

1. Ed25519 独立域 `easel.desktop.gateway.v1`，canonical JSON、无歧义 base64url，所有字段类型/大小/编码、内部断言头名称固定并测试。字段绑定 workspace、gateway 实例/boot generation、worker 实例、配置版本、method、path、canonical query、实际 body SHA-256、iat/exp、nonce、request_id；有效期最多 30 秒，未来 iat 容忍值明确且有限，拒绝未知字段/歧义字段。先完成契约校验再对 worker 发请求。
2. 有界、并发原子的 nonce 单次接纳；不能依赖进程内分步 check/add 的竞态。达到缓存容量时失败关闭，不提前驱逐仍有效 nonce。重启更换 boot_generation；旧断言即使未过期也拒绝。G1 不承担 G2 的 Lease 同步，但配置/激活接口必须支持“实例已配置而尚未激活则拒绝管理请求”，不能默认用旧 Docker 标签当新 boot generation。
3. 身份字段从已校验断言和固定配置取得；先读取并验证可信 control 断言，再清除其头与外来 Cookie/Authorization/Forwarded/X-Forwarded-*/X-Easel-*；最终下游请求（包含 HTTP client 默认头/cookie/auth 合并之后）只重建 worker workspace/service token。签名私钥只在签名端，gateway 仅公钥，错误/日志不含断言、服务 token、完整 env/body。
4. 七项精确 method/path/query 以 topology routes 和真实 web.app/proxy 调用为准。ID/百分号编码/重复未知参数/尾斜杠/绝对 URI/重复斜杠默认拒绝，不按前缀放行。依赖 worker 当前状态的范围（如最大 event id）继续交 worker 现有校验，不另建事件状态或为 gateway 随意增加查询路由。
5. 固定 worker origin 由受信部署提供，普通请求不能提供目标/Host/URL。禁用重定向和任何 POST 自动重试；上游 3xx 按契约失败，清除响应凭证/逐跳头。正常非 2xx 错误码和 JSON 不伪造成功。
6. 按已批准的请求头 64 KiB、body 64 MiB、连接 10 秒、I/O/流 idle 120 秒、并发上限 8 实施；检查重复 Content-Length/TE 等 framing 歧义，body 上限按实际读取累计，不只相信声明。并发饱和行为明确；长流占槽不能使 stop/权限回答永久饥饿，提供可测试的有限拒绝/重连策略。
7. 当前 worker `/events` 实际返回持久事件 JSON；gateway 原样保留该协议，不强行改成 SSE。若上游是 SSE/流式响应，逐块转发与背压、不缓存整段；下游断开/超时必须关闭读取任务和上游响应。control 的外层 SSE 保留现有职责。

## 实施与红绿验收

- [ ] 先写认证反例：改 method/path/query/body/任一 audience、过期/未来/超长/畸形断言、错误公钥、重复 nonce、并发相同 nonce、未激活、重启旧 generation；均在上游调用前拒绝。同 workspace 不同 worker 代次也拒绝。
- [ ] 写路由反例：七项合法路径正例（含历史/权限问答）、未知/重复参数、编码斜杠/反斜杠、重定向、用户指定目标；完整捕获实际下游 URL/头/body。
- [ ] 写转发反例：client 默认 Authorization/Cookie/auth 恢复注入、伪造内部头；body 超限/断连、普通错误和 JSON 事件、SSE gate/背压/取消、读超时、并发额度释放。断言无遗留读取任务、响应或未回收槽。
- [ ] 跑红测试并记录精确失败，再最小实现；不要在看到不便后放宽固定来源、Origin 或 worker guard。
- [ ] 除注入 transport 单测，运行至少一个真实 TCP gateway→独立临时 worker HTTP 服务，后者使用真实 worker guard 校验头，验证合法请求与跨 workspace/错误断言、停止/历史路由。临时返回的业务体可用夹具，但不得称为真实 Harness；不需 ngrok/供应商。
- [ ] 跑新模块 + 受影响认证/策略/worker guard/代理/事件回归和文档路径；新增/修改代码 ruff、diff check。真实测试使用随机资源、严格归属和逐项清理。
- [ ] 显式路径中文提交，报告完整父/新 SHA、协议接口、红绿、真实 TCP 边界、资源清理、剩余未实现。提交后停止，不接 G2/A4。

## G2 留待后续的硬门槛

G1 工厂的配置不能假设能在 docker create 后修改 Env。G2 决定可信固定 IP 或受信启动配置/握手，通过实际 inspect 验证 management 仅绑定控制网络地址，再接 workspace 网络。每次进程启动的新 boot generation 须经可信管理路径与 Lease/Handle 原子同步，同步前不激活管理路由；不得因 control/gateway 短暂不可用删除 active worker。此处列出约束，不授权本轮提前实现其持久化与容器管理。
