# 原 Web Task 5C2 首轮复审：四项修复与交付证据待收口

日期：2026-11-01。父基线 `654305a69c1d68d6b157b608dbd2c7d5f96d6298`；实施 `0801343ddcdc4111af111108a5f15897ae1f1b43`；tip `a7c8ce253905918987f2a35fc269a1936a93dd6f`。

结论：暂不通过。原账号页面接入、基础登录/分析链路成立，保留已通过内容；仅按 [R1执行单](../tasks/2026-11-01-original-web-task-05c2-rework-r1.md)修复以下四项P2并补齐原任务要求的验证证据，不重写已审架构，不进入5C3/6。

## T5C2-R1-01：旧平台状态提前结算新登录 Run（P2）

实施树 `easel/control/workspace_accounts.py:464` 附近，_login_view 在 Run 尚为运行态时，凭共享平台status的success/expired/error直接finish_run；该状态不携带当前Run归属。新任务接纳与新脚本清理旧状态之间存在窗口。

主审与独立审阅者分别跑真实control→gateway签名→worker RunManager链，预置旧success、仅暂停新browser executor：HTTP200/success，NEW_LOGIN_SCRIPT_STARTS=0；control Run已done、active_runs=0，而worker Run仍running。既误报成功，也提前释放容量。修复须让当前Run的执行结果/状态驱动终态，旧状态不能推进新Run；不能仅靠前端隐藏二维码修饰。

证据：[生命周期探针](../../evidence/original-web-task-05c2-review/probe_worker_lifecycle.py)（--case stale-login）、[输出](../../evidence/original-web-task-05c2-review/probe-stale-login.txt)。主审复跑退出0，临时资源已清理。

## T5C2-R1-02：跨站顶层 GET 能启动分析任务（P2）

`easel/control/app.py:1366` 的GET /api/analytics/{platform}直接调用analytics接纳新Run，而会话Cookie为SameSite=Lax。主审与独立审阅者在真实Chromium登录后，从受控data:外站文档点击普通链接导航到该GET：Sec-Fetch-Site=cross-site、mode=navigate，无Origin/CSRF，Cookie仍随顶层GET发送，实际执行analytics一次。

本项是未授权启动浏览器/分析工作，不主张跨站脚本能读回数据。读取已有缓存应保持只读；主动刷新应有明确动作、CSRF/来源校验和稳定任务归属。force whoami等其它会起任务的GET也应同口径检查，不能只保护前端按钮。

证据：[跨站导航探针](../../evidence/original-web-task-05c2-review/probe_analytics_navigation.py)、[输出](../../evidence/original-web-task-05c2-review/probe_analytics_navigation_output.txt)。主审独立复跑同结果，退出0并清理。

## T5C2-R1-03：旧停止回应覆盖新登录弹窗（P2）

`web/frontend/src/components/AccountsPage.tsx:446` handleStopLogin等待请求后，未按qrRequest/platform/run归属校验就setQr；关闭和新登录虽递增序列，旧stop回包仍修改当前弹窗。

真实构建浏览器：小红书停止请求实际成功但挂住回应；关闭其弹窗，打开抖音并看到新二维码，再放行小红书旧回应。抖音弹窗变为“已停止登录”、二维码数0，实际后台仍qr_ready、Run仍running。错误和finally等迟到分支应同样受归属约束；不要求重做页面或自动停止另一个任务。

证据：[迟到停止探针](../../evidence/original-web-task-05c2-review/probe_late_stop_modal.py)、[输出](../../evidence/original-web-task-05c2-review/probe_late_stop_modal_output.txt)。主审独立复跑同结果，后台新假Run随后正常停止、清理完成。

## T5C2-R1-04：凭证 Run 取消后仍迟到保存（P2）

`web/app.py:5287` api_save_credentials将校验和保存整体放入to_thread，`_verify_and_save_credentials:846` 在验证后直接落盘。Run等待槽取消/移除不会取消这个线程，也没有提交前归属/取消栅栏。

真实签名链中，暂停假的凭证验证，调用正式stop得到200/cancelled，worker也已cancelled且配置尚无；放行验证后，原凭证请求502，但配置写入、configured=true并产生success登录标记。主审独立复跑同结果。必须保证取消先完成时迟到验证不能提交文件/成功标记，且Run终态、HTTP结果与存储一致；仅禁止future.set_result不能解决已发生的写入。

证据同[生命周期探针](../../evidence/original-web-task-05c2-review/probe_worker_lifecycle.py)（--case credentials-stop）、[输出](../../evidence/original-web-task-05c2-review/probe-credentials-stop.txt)。未使用真实凭证/供应商，临时资源已清理。

## T5C2-R1-05：原要求的容器闭环与可移交日志缺失

原任务明确要求隔离control/gateway/worker容器任务闭环。交付只跑ASGI与前端Chromium；平台脚本函数为替身，没有证明镜像内固定脚本、HOME/profile、依赖/浏览器、子进程取消与停止释放可用。此项是验收缺口，不伪称已证明镜像存在某个具体故障。

verification.md引用的红绿、回归日志为.log，当前被gitignore排除；git ls-files本组目录只有文档/截图/JSON，chain_test.log等在磁盘存在却未进入tip。因此换干净checkout无法获得这些证据。需脱敏后显式纳入提交或改成受跟踪文本扩展名，验证提交内引用完整。

## 主审新运行与边界

- 新增四个5C2后端文件：32 passed；相关账本/策略/workspace/gateway及原账号/登录/分析/profile：329 passed、1 skipped。两组文件无重叠，共361 passed、1 skipped。
- frontend Node154 passed；build成功（index-CEZvVApz.js）、lint退出0。
- 当前真实构建浏览器：5C2四组、5C1七组、5B五组、5A四组、Task3 33组、Task4八组，共61组，全部FAILURES=[]，各脚本退出0。截图写独立临时目录并清理，未覆盖提交证据。
- 生产/测试源码diff --check通过、实施树干净。既有chunk提示、Windows Playwright退出Win10054、5A清理coroutine警告不扩为生产阻塞。
- 四条反例均由主审独立复跑；独立审阅者从后端/前端两侧复核。未以测试总数代替交错与来源校验。

本轮主审未跑Docker、完整顶层pytest或真实平台操作。作者父基线顺序失败证据仅作说明，不在此扩查。未修改用户容器/卷/socket/Key/Cookie/邀请，未重放prompt、未付费/发布、未合并或上线。
