# Task 7 首轮审计：需要返工 R1

- 父提交：f30b611d65d8431494cb1d3b4f80109bfa06927f。
- 被审提交：7e070acd753469c1850a480b189eb772e2fa4aae。
- 结论：四项 P2 阻塞，暂不进入 Task 8。
- 主审与独立复审只读审计；探针使用临时目录、假凭证、本地 HTTP/Chromium，不读取真实平台账号、不调用供应商。没有修改产品代码或提交文件。

## T7-R1-01 [P2] XHTML 绕过预览限制，以应用同源执行脚本

位置：`web/app.py` 的 api_media，约第 4319–4333 行。

响应限制仅按 .html/.htm/.svg 后缀施加。合法输出目录里的 .xhtml 被 FileResponse 识别为 application/xhtml+xml，却没有 CSP sandbox；浏览器正常执行其中脚本。扩展名大写测试不足以覆盖另一种实际可执行 HTML 媒体类型。

独立探针在临时 outputs/project/poc.xhtml 写入 XHTML 文档及设置 title 的脚本，真实 HTTP GET 返回 200、Content-Type=application/xhtml+xml、无 CSP。随后用真实 Chromium 按该响应的实际状态/响应头/字节履行同源页面导航，得到：

```text
browser_title=EXECUTED_SENTINEL
browser_origin=http://easel.test
```

这违反“产物不得作为应用同源可执行文档”的当前任务要求。没有实际访问公网测试域名，响应由本地路由提供。

修复要求：统一判定可执行文档响应，覆盖 XHTML 及实际会被浏览器作为文档执行的媒体类型；选择严格 sandbox 或下载策略，保留普通媒体体验。不能仅补前端 iframe 属性。按最终响应 Content-Type/媒体策略核对旁路，nosniff 与策略配套。

验收：新增 XHTML 的 GET/HEAD 真实 HTTP 测试及真实浏览器脚本不可执行证据；原 HTML/HTM/SVG、大小写、普通图片音视频回归保持。列明实际允许的内联文档类型及限制策略，避免只把当前探针文件名加黑名单。

## T7-R1-02 [P2] 产物树遍历没有路径边界，可跟随 junction 读取私有目录

位置：`web/app.py` 的 get_output_tree、_build_output_node、_read_project_meta，约第 1232–1307 行。

单文件接口已调用 _safe_output_path，但产物树沿 path.is_dir/iterdir 递归，并读取项目 .easel.json，没有同等的已解析路径边界。通过 junction 指向内部状态或 outputs 外部目录，仍会把文件名、大小等元数据及项目 JSON 展示字段返回。

独立真实 HTTP 探针：临时私有目录内放 internal-sentinel.txt 和含 title=PRIVATE_METADATA_SENTINEL 的 .easel.json，在 outputs 下创建指向该私有目录的 linked-project junction。GET /api/outputs 返回 200，同时包含私有文件名与私有 title；下载接口的拒绝并不能保护该列表入口。

修复要求：树入口、递归节点及元数据读取都遵守同一边界，拒绝/跳过越界或指向内部状态的 reparse/symlink/junction；处理递归环，不能无限遍历。不要只检查最顶层目录名，也不要先读元数据再过滤。

验收：真实 HTTP 树测试覆盖顶层和嵌套 junction/symlink 指向 outputs 外、内部系统目录以及递归环；返回不得包含 sentinel 文件名/元数据，不能 500/无限递归。普通项目树和合法元数据不退化。Windows junction 能实际执行；符号链接权限不足可明确 skipped，不能替代 junction 测试。

## T7-R1-03 [P2] 注销绕过持久 profile 锁，删除持有者正在使用的文件

位置：`web/app.py` 的 api_logout，约第 4966–4969、4982–4987 行。

登录/发布等打开 profile 的调用已加锁，但注销直接 rmtree(ignore_errors=True)，未取得同一把锁。它只处理 LOGIN_PROCESSES，不涵盖持有 profile 的统计、whoami 或发布任务。公众号分支 terminate 后还未等待退出就删除。删除锁文件所在目录也可能破坏后续互斥协议；Windows 下部分删除失败仍会报告成功。

独立真实 HTTP worker 探针：临时 xiaohongshu/default profile 由 BrowserProfiles.persistent_profile 持锁，写入 cookie-sentinel。持锁期间 POST /api/logout/xiaohongshu 使用合法内部身份：

```text
http=200
result={ok:true,deleted:[default]}
holding_profile_lock=true
cookie_file_deleted=true
```

修复要求：profile 删除/注销同样参与统一互斥。等待、明确 busy，或先可靠终止持有者并确认浏览器关闭后再删除；不能持锁时部分删除后返回成功。锁文件自身的删除/位置要保证等待者与新请求始终竞争同一互斥对象，避免删除重建导致两把锁。保留 workspace/账号隔离。

验收：真实 HTTP 注销与独立进程持锁并发时，在所有权释放前账号 sentinel 不变；释放后注销完成或返回明确可重试结果。公众号终止等待、普通平台、删除失败不伪报成功，以及 A 注销不影响 B 均有测试。

## T7-R1-04 [P2] 实际技能异常路径未关闭包装器，浏览器退出后锁仍被持有

位置：`skills/shared/scripts/browser_state.py` 的 _LockedContext/launch_persistent_locked，以及 zhihu_comments_fetch.fetch_comments 的浏览器主体（约第 44–57 行）。同类调用方包括 zhihu_answer 与 web_publisher 的部分分支，需逐个核对。

包装器只在显式 close 时释放自建 fd，真实调用方并未全部使用 with/finally。页面导航抛错后，外层 sync_playwright 退出会关闭 driver/browser，但不会调用包装器的 close。只比较 profile 路径、正常关闭或使用测试代码自行 finally 的集成测试无法证明实际调用者可靠清理。

主审和独立复审分别用真实 Chromium、临时 PROFILE_DIR 调用 zhihu_comments_fetch.fetch_comments('bad://invalid')：导航抛 Error，退出 sync_playwright 后，再获取相同 ProfileLock(timeout=.3) 仍超时。

影响范围要准确：CLI 整个进程退出后操作系统会释放 fd；稳定复现的是同一解释器捕获异常后重试/复用函数，不宣称所有 Web 子进程重试都会永久锁死。

修复要求：所有实际持久浏览器调用点在导航/预检/交互/启动后异常及取消路径中可靠关闭 context，确认结束后释放锁；不得只修测试 helper 或成功路径。避免未完成底层关闭就提前释放；关闭包装器应具有可重复清理的明确语义。

验收：对真实调用函数注入导航错误并在同一解释器重试获取锁；覆盖代表性预检错误/取消，验证锁释放与浏览器关闭先后关系。真实 Chromium 正常隔离、持久 Cookie、跨进程互斥测试保持；不能用进程退出掩盖函数清理缺失。

## 独立验证

使用 C:\Users\hyh\anaconda3\envs\Agent\python.exe：

- tests/multiuser：551 passed、1 skipped，34.67 秒。
- Task 7 指定十文件回归：291 passed、20 skipped，22.74 秒。
- tests/integration/multiuser/test_browser_profiles_real.py：3 passed，1.59 秒。
- git diff f30b611 HEAD --check：退出 0。
- 上述四组探针全部实际复现，junction 为 Windows 真实 junction；XHTML 和异常锁探针使用真实 Chromium。

现有路径迁移、私有状态布局、正常浏览器使用及 HTML/SVG 响应测试有实现与绿证据，但没有覆盖以上遗漏。没有独立复跑全仓 42 项基线失败；执行 Agent 的全仓/ACL 证据作为交付报告保留，不标成本轮独立验证。Linux 挂载、真实平台登录/发布、真实 DSH/供应商、前端仍未执行。

## 可复制给执行 Agent 的 R1 指令

基于 7e070acd753469c1850a480b189eb772e2fa4aae，仅修复 T7-R1-01/02/03/04。先增加当前提交失败的真实入口/真实调用者测试，再最小修复；不重做已通过的认证、签名、broker、正常状态路径，不实现 Task 8。涉及 profile 生命周期的两项可以共用清理/锁机制，但分别提供注销并发和函数异常的验收证据。

运行 tests/multiuser、任务单指定十文件回归和真实 Chromium 集成测试，再检查父提交到新提交的 git diff --check。显式路径提交，不夹带审计/任务文档或其他工作，不接触开发者真实账号目录。交付父/新 SHA、文件清单、四项红绿证据、实际测试计数及未执行门槛，完成后停止等待复审。
