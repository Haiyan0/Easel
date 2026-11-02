# 原 Web Task 5A 首轮复审：三项定点返工

日期：2026-10-30。父基线 `1b57e1927052d930e13779239ff2bc8907895ae5`；生产/测试提交 `8b85a8c8878acf41b756b3edc5f87f61abe7ae8b`；交付 tip `ea3066471811daeb2f51211dea0162578cbfe483`。

结论：暂不通过，仅下述三项 P2 需修复。原组件接线、精确 gateway 路由和正常文件链路成立；不要求重做底座，不进入 5B/5C/6，不切换用户实例。见 [R1 执行单](../tasks/2026-10-30-original-web-task-05a-rework-r1.md)。

## T5A-R1-01：文件访问与聊天使用不同配置版本（P2）

实施树 `easel/control/workspace_api.py:125,136–142,165` 保存正式传入 revision 后却不用它，转而查询 workspaces.config_version。真实 Store 新 workspace 此字段为 0，正式 relay 则使用受管配置 revision。

用同一份临时受管配置 revision=7 经正式 build_relay_factory/build_workspace_factory、真实 RuntimeManager 重现：聊天 worker 已为 7 且有运行中 Run 时，GET personas 得到 worker_config_busy；Run 完成后再次访问文件，停止旧 worker 并重建为 0。此时 status 也可能把合法聊天实例判为不就绪。新测及容器演练使用 revision=0，未覆盖该交错。

证据：[探针](../../evidence/original-web-task-05a-review/probe_runtime_revision.py)、[输出](../../evidence/original-web-task-05a-review/probe-runtime-revision.txt)。主审独立复跑同结果，临时资源清理完成。

修复应统一正式聊天与文件 API 的配置版本来源，保留 workspace 存在/身份验证，不以查询文件为理由停止合法活跃实例。不能通过把所有 revision 固定为 0 或改测试配置掩盖。

## T5A-R1-02：画像读取跟随链接越出画像根（P2）

实施树 `web/app.py:1783,1811–1816` 的详情和文件枚举读取调用共享助手或直接 glob/read_text；`easel/persona.py` 的 profile_exists/load_profile_text 只检查名称和 is_dir，未限制解析后的目录/文件。

本轮只在临时目录创建 profiles/public-profile 指向同一临时 workspace 的 private-state 目录链接，放入假 identity.md sentinel。画像名通过 control canonical 校验，详情及 files 均读出 profiles 根外 sentinel。Windows 实测使用真实 junction；内部 Markdown 文件 symlink 用例因本机 WinError1314 明确 not-run，不冒充已验证。此问题是 worker 可读内部材料越过公开画像边界，不声称已读取真实秘密或跨容器数据。

证据：[探针](../../evidence/original-web-task-05a-review/probe_persona_links.py)、[输出](../../evidence/original-web-task-05a-review/probe-persona-links.txt)。主审独立复跑同结果。

修复需在真实读取处检查目录和各 Markdown 文件边界，并覆盖同源列表摘要/status、详情、files 及编辑/删除的相关路径，避免只在浏览器路径名上加限制。不要扩大重构原画像功能。

## T5A-R1-03：媒体错误流未读取即解析，异常且未关闭（P2）

实施树 `easel/control/gateway_client.py` 的 request(stream=True) 在 >=400 时进入 _raise_for_status；该方法先调用 _gateway_error→response.json()，但 HTTPX 流尚未读取，抛 ResponseNotRead，未执行随后 aclose。WorkspaceApi 虽释放 lease，上游响应仍未由此错误分支收尾。

真实 localhost TCP 404 响应探针：普通请求正确得到 DomainError(status=404)，相同流式请求得到 ResponseNotRead(status=None)。缺失媒体或其它上游错误会失去稳定错误语义，并可能占用连接。已有 ASGI/预装正文响应未暴露该差异。

证据：[探针](../../evidence/original-web-task-05a-review/probe_media_error.py)、[输出](../../evidence/original-web-task-05a-review/media-error.txt)。不读取/返回上游原文；修复不能为解析错误码无界读入错误正文。

## 本轮主审新运行与保留结果

- 新增 API + gateway 测试：61 passed。
- 全部 frontend Node：143 passed，0 failed。
- tsc + Vite build、oxlint：通过；既有 chunk 大小提示不阻塞本任务。
- Task 5A 浏览器：4/4，FAILURES=[]；Task 3：33/33；Task 4：8/8。
- 浏览器截图输出到临时 .pytest_cache 子目录并清理，没有覆盖交付证据；实施 worktree 干净，基线到 tip diff --check 通过。
- 5A fixture 清理曾出现 close_clients coroutine 未 await 的 RuntimeWarning，矩阵仍成功；属测试清理质量项，不另立生产阻塞，应在涉及该 fixture 时修正并如实记录。
- 独立前端/gateway 复核未确认新的越权、主动内容执行或跨身份缓存阻塞。登录在线标志与 worker 状态展示的非确定歧义不扩为本轮返工项。

本轮未运行 Docker、未操作用户 control/worker/gateway/sidecar、未读取真实 Key/Cookie/邀请、未重放 prompt。交付的隔离容器结果作为提交证据读取，本轮不冒充独立重跑。真实供应商、完整 provisioner/Compose 及整体上线仍未验收。
