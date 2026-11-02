# 原 Web Task 5C3 R1复审：主体关闭，仅素材意图匹配待修

日期：2026-11-02。父基线 `aef1a578185d15a90d8c46ab46ffb3adde1edfb3`；R1实现 fdb597c、补修 e8abc2c，文档tip `40fca7c4551db82aa782ef2db466080421337ed2`。

结论：R1三项主体修复成立，Stop插值和完整payload持久化也已核实；暂不整体通过，仅以下一项修复副作用按 [R2单项执行单](../tasks/2026-11-02-original-web-task-05c3-rework-r2.md)收口。不重开已通过后端/部署方案，不进入Task6。

## T5C3-R2-01 用户明确改选素材，实际请求仍静默发送旧素材（P2）

实施树 `web/frontend/src/components/PublishPage.tsx:74`–78，findPublishIntent在完整fingerprint不匹配后，仅因title/body/tags相同就复用prior.payload；没有区分刷新导致未恢复的media/persona与用户主动编辑。

主审与独立审阅者分别在当前真实build、原隔离fixture上验证：

1. 首次POST被客户端截断，明确没有forward，后台发布执行0次；原意图保存A图project/cover.png。
2. 用户不修改标题/正文/标签，但明确取消A，选中B图project/other.png；DOM选中项仅B。
3. 再次确认发布，实际第二次POST仍含A，复用旧requestId，受控publisher执行一次错误的A素材。

主审输出：first_request_never_forwarded=true、visible_media_before_second_submit=[other.png]、second_request_media=[cover.png]、second_request_matches_visible_selection=false、fake_publisher_executions=1、temporary_directory_cleaned=true，退出0。

这不是原重复发布问题重开，而是为刷新恢复加入的fallback覆盖了用户新选择。应保留原意图恢复能力，同时把显式改媒体/画像当作编辑处理，或明确阻止新稿、引导解决旧意图；不能显示B却发送A。不要求增加新后台系统。

证据：[只读复跑探针](../../evidence/original-web-task-05c3-review/probe_r1_changed_media.py)、[输出](../../evidence/original-web-task-05c3-review/probe_r1_changed_media_output.txt)。正常刷新stored-payload用例仍需保留。

## 原R1关闭证据

- **R1-01 部署策略**：Start/Compose显式传递并登记publish_mode；旧登记缺字段、登记丢失但已有控制DB要求显式迁移；大小写统一；Restore从manifest给出带策略的后续启动命令，漏参数时已有DB探测仍拒绝；旧部署/升级备份策略冲突在compose up前失败。Stop down补齐必填插值，契约测试遍历Compose必填变量。独立部署审阅者36项测试通过、CLI help通过，无新增阻塞。
- **R1-02 稳定意图与恢复**：发送前持久化请求号和完整payload，requestId状态查询只找本人原Run，未知接纳不自动换号；新增浏览器验证在途/终态丢ack、刷新、stored-payload、显式新意图与身份切换。11组本组浏览器全部通过。仅新增fallback对显式编辑的副作用按R2处理。
- **R1-03 输入绑定**：运行态复用前比对run_input_digest，relay request_conflict不再吞掉，终态沿用持久摘要。主审原真实链请求内容探针复跑：首次200，同ID改标题/正文409，清理停止200；原Run不被新输入冒充。发布/导出运行态和终态由新增测试覆盖。

## 主审本轮新运行

- workspace_publishing、真实5C3链、desktop_operations_r1/desktop_workspace/workspace_start_identity：66 passed。
- worker发布、5C2/Run/stop、Store/gateway/policy/lease/workspace/profile、原账号/分析/导出/资产：599 passed、2 skipped。两组文件无重叠，共665 passed、2 skipped。
- frontend Node173 passed；build成功index-CfTWRFga.js，lint退出0。
- 本组浏览器11/11全部true，FAILURES=[]；历史矩阵采用独立Python进程运行，5C2六组/5C1七组/5B五组/5A四组/Task3 33组/Task4八组全部通过、各脚本退出0，共74组正常场景通过。临时截图已清理，未覆盖作者或历史证据。
- 生产/测试/部署源码diff --check通过，实施树干净；既有chunk/Windows Playwright退出回调警告不扩为本轮产品阻塞。

## 主审隔离容器复跑

首次误用脚本缺省的旧control镜像标签，源码哈希预检即拒绝，没有将其计为产品失败；改用交付verification指定的R1标签后独立复跑通过。control镜像为sha256:902de6c1330d8d773dcd6e532943d5ef485e55a40344a2be34d6250af293ddee；gateway/worker保持本轮报告ID。镜像内源码与宿主逐项一致，dist SHA256=6f7123fc6617e8ef40a74200bb3ef195bdfd7339aa0fc17b2f3a85ea1668a9f3。

实际结果：manual策略拒绝、导出下载解包、一次发布/日历、短信归属、停止/超时释放、状态恢复、重启不重放、按意图查状态、运行态/终态内容绑定、真实受控runner与profile锁均true，TASK5C3_CONTAINER_OK=True、CLEANUP_ERRORS=[]。容器均是新建测试资源，未接管用户实例。

固定受控runner替代原平台脚本，真实平台与账号没有执行；seccomp=unconfined的隔离容器通过不解除生产Linux默认seccomp/网络门槛。未独立执行完整顶层pytest，不冒充作者四批全量结果。

## 边界

用户运行容器/卷/socket/Key/Cookie/邀请未动，未真实发布/短信/凭证/付费，未合并推送或上线。上一轮审批阻止清理的两个主审临时目录未接管或再次删除。本轮成功探针临时目录与测试容器清理完成；首次误用旧标签预检会在脚本创建的空临时工作目录之后退出，未产生运行卷/网络或用户数据变更。
