# 原 Web Task 5B 首轮复审：两项定点返工

日期：2026-10-30。父基线 `7b9d76f73dd730f63b74a63f3cd2e81c9436dbef`；实施 `a9c89e5`；证据 tip `38955b4c8707e07e51d6153abe6794dbb9a67ed1`。

结论：暂不通过，仅下列两项P2需返工。热点/选题/日历/概览的原组件接线与正常CRUD成立；不要求重做工作台，不进入5C/6。见 [R1执行单](../tasks/2026-10-30-original-web-task-05b-rework-r1.md)。

## T5B-R1-01：数据文件缺失时首次写入绕过父目录链接边界（P2）

实施树 `web/app.py:5998–6005` 的 _workbench_file 在目标 lstat 抛 FileNotFoundError 时直接返回 target；校验解析后的真实路径只覆盖目标已存在分支。_write_workbench_json 随后在同一个未验证的 OUTPUTS_DIR 下创建临时文件并 replace。

真实临时 junction 探针将 outputs 指向临时 private-state 空目录，分别调用选题和日历 handler：初读返回[]；首次创建返回成功，_ideas.json/_schedule.json实际写到 private-state；随后再读因目标已存在才检查解析路径，返回500。既发生首次写越界，也出现成功创建后无法读取。不声称跨容器或触及真实秘密；所有目标/记录均为临时假材料。

主审独立复跑同结果，清理完成。证据：[探针](../../evidence/original-web-task-05b-review/probe_storage_root.py)、[输出](../../evidence/original-web-task-05b-review/probe-storage-root.txt)。本机Windows真实目录junction已验证，不依赖文件symlink权限。

要求在目标不存在时也验证可信outputs根及父链，再允许空列表或创建；读、写、临时文件目录和context共用同一边界。正常未物化数据文件仍须支持首次创建，不能把所有不存在的文件一律拒绝。

## T5B-R1-02：上游失败后的旧热点被展示为刚更新（P2）

实施树 `web/app.py:5982–5990` 在过期缓存刷新失败后继续返回旧items，并报告ok=True和updated=当前时间。原执行单明确要求网络不可达时失败或标注旧数据。

受控探针缓存一小时前的假热点，令_fetch_platform返回失败空结果；响应仍返回旧标题、ok=True、updated年龄0秒且无stale字段。执行当前 TrendsPage 原TSX转译渲染确认：显示“多平台实时热搜”和当前分钟“更新”，无旧数据/刷新失败提示。不是要求公网实测，而是当前失败分支的真实语义错误。

证据：[后端探针](../../evidence/original-web-task-05b-review/probe_trends_stale.py)、[输出](../../evidence/original-web-task-05b-review/probe-trends-stale.txt)、[原组件探针](../../evidence/original-web-task-05b-review/probe_trends_view.cjs)、[渲染输出](../../evidence/original-web-task-05b-review/probe_trends_view_output.json)。前端探针默认输入首轮反例响应；R1复跑需传入修复后的实际受控响应，不能拿默认旧形状认定后端未修。

可保留旧缓存并显式标注实际缓存时间/刷新失败，也可返回明确失败；最小选一种一致语义即可。按需同步 WorkspaceData契约、TrendsPage及概览，不能只改worker添加字段却在适配层丢掉。

## 主审本轮新运行与保留结果

- original_web_api + gateway_workspace + workspace_revision：95 passed、1 skipped；skip为本机文件symlink权限。
- 全frontend Node：144 passed、0 failed；tsc/Vite build成功（index-eIaaBOlh.js）、oxlint退出0。
- 5B浏览器4/4、5A四组、Task3 33组、Task4八组均FAILURES=[]；浏览器临时截图已清理，没有覆盖tracked证据。
- 基线到tip diff --check通过，实施树干净。
- 独立前端/gateway复核确认“做内容”不自动提交、control账号/分析无旧local请求；未确认新的身份隔离或任意代理阻塞。
- 浏览器测试在主审以-X utf8启动时出现日历子进程输出编码不一致警告，矩阵仍通过；不将该主审启动参数差异误判为生产故障，也不将矩阵通过等同于额外证明摘要子进程成功。既有5A清理coroutine警告保留为测试质量项。

主审未执行Docker；读取交付隔离容器证据，不冒充独立重跑。用户容器/卷/socket/真实Key/Cookie/邀请未操作，未付费/真实发布。公网热点、生产Linux、完整Compose与5C能力保持未验收。无关既有视频脚本路径测试失败不作为本轮阻塞，也未修改该项。
