# Task 7：隔离技能状态、浏览器账号与产物预览

> 执行方式：外部代码 Agent 按已批准 plan 逐步执行，可使用 executing-plans 工作流。完成后停止等待审计，不进入 Task 8。

日期：2026-10-20。基线：f30b611d65d8431494cb1d3b4f80109bfa06927f。

依据：[spec](../specs/2026-10-16-multiuser-workspaces-design.md) 第 4、6、9 节及 A09/A15/A16，[plan](../plans/2026-10-16-multiuser-workspaces.md) Task 7。沿用既定设计，不重新设计认证、broker、Run 或容器服务。

## 目标与范围

每个 worker 的技能凭证、缓存、浏览器 Cookie/profile 只写本人 state_root/home；共享技能代码可以只读执行。账号 profile 跨重启持久化，同账号跨进程互斥；临时浏览器 context 相互隔离并可靠关闭。产物读取不得暴露内部状态，HTML/SVG 直链也受响应头限制。

平台账号凭证与管理员模型 Key 是不同对象；本任务不得为兼容旧技能而把管理员 Key 注入 worker。只用临时目录和测试账号状态，不读取、迁移、删除开发者真实 HOME、公众号配置或浏览器 profile。

## 环境和接口

先读 AGENTS.md、CLAUDE.md。PowerShell，C:\Code\Easel，Python 固定 C:\Users\hyh\anaconda3\envs\Agent\python.exe。开始记录分支、HEAD、git status；保留他人变更，不 reset，不 git add .，只显式路径提交必要代码/测试。中文说明、注释和提交信息。不夹带审计/任务单。

沿用 WorkspacePaths(code_root, workspace_root, state_root, home)、worker.context.resolve_paths、脚本 runtime_paths.resolve_runtime_paths，不新建第二套路径猜测逻辑。Task 6 的签名、认领、过期和 local 行为必须保留。

预期文件范围：

```text
Create: easel/worker/browser.py
Create: tests/multiuser/test_skill_state.py
Create: tests/multiuser/test_browser_profiles.py
Create: tests/multiuser/test_artifact_access.py
Modify: web/app.py
Modify: ./skills/shared/scripts/runtime_paths.py
Modify: ./skills/shared/scripts/account_stats.py
Modify: ./skills/builtin/skill-wechat-publisher/scripts/config.py
Modify: ./skills/builtin/skill-wechat-publisher/scripts/wechat_token.py
As needed: 扫描发现的其他多人可调用技能的共享目录写入者及其测试
Optional: 真实浏览器集成测试独立放 tests/integration/multiuser 下
```

产出接口：

```python
# 脚本侧 runtime_paths.py，使用当前进程已固定的显式路径
state_path(kind: str, account_id: str = "default") -> Path
# kind 只接受 wechat-config/wechat-token/platform-cookie/browser-profile/skill-cache

# easel/worker/browser.py，显式 paths，不依赖宿主 HOME
BrowserProfiles(paths: WorkspacePaths)
profile_dir(platform: str, account_id: str) -> Path
persistent_profile(platform, account_id)  # 带跨进程文件锁的 context manager
```

浏览器 profile 根按 spec 使用本人 home/.easel-browser-profiles，再按平台/账号分区；其他账号配置与 token/cache 使用本人 state_root。明确并在报告中给出各 kind 的实际子布局和文件名，所有实际读写者调用同一映射，不要求每类状态都变成同一种文件格式。

账号/平台标识先验证，再派生路径；拒绝绝对路径、分隔符、点段、控制字符及会产生别名/越界的输入。worker 不允许浏览器请求或 profile-base 参数选任意宿主目录。local 保留既有兼容读取，不向新旧位置双写，也不自动导入旧开发者账号。

## 执行步骤

- [ ] **1. 扫描并建立清单。** 用 rg 搜索所有可由 Web/Agent 调用的技能脚本中 Path.home、__file__ 相邻写入、Cookie、config、token、cache、profile 及全局 os.environ 修改。报告“脚本/实际调用者、旧位置、新位置、测试”；区分只读资源与可写状态。不能只修公众号两个文件后声称全部隔离。排除项必须给出不可达或纯只读依据。

- [ ] **2. 先写红测试。** 三个新测试文件覆盖 A/B 相同平台、相同账号 ID 得到不同状态/profile；A 的登录/缓存/注销不改 B；worker 缺路径失败关闭。运行确认失败，再做最小实现。

- [ ] **3. 接入实际技能读写。** 微信配置、token 缓存、Web 保存/注销、account_stats 及扫描发现的写入者统一使用私有路径。只读模板/规则/图片样式继续从 code_root 读取。worker 不从共享技能目录回填旧账号秘密；缺个人配置正常要求配置/登录。检查按账号缓存和进程内活动账号，不能串 workspace/账号；复用进程须显式传配置，不能靠改全局 os.environ 把上一任务秘密留给下一任务。用两个账号交替调用真实函数证明，而非仅比较 helper 返回路径。

- [ ] **4. 接入浏览器生命周期与锁。** 所有打开持久 profile 的实际调用点使用同一套跨进程锁；同 workspace/平台/账号排队，其他 workspace/账号可独立运行。锁覆盖浏览器拥有 profile 的完整生命周期，浏览器/context 关闭完成后才释放；异常和取消也可靠清理，取消等待锁不能释放别人的锁。使用独立进程证明互斥，不能只用 asyncio.Lock。临时任务不共享默认 context/Cookie，退出时关闭，不关闭 Chromium 自带沙箱来绕过问题。

- [ ] **5. 真实浏览器验证。** 用隔离的本地测试页面和临时 profile，不登录真实平台：两个临时 context 的 Cookie 隔离；持久 profile 关闭重开仍保留 Cookie；同 profile 并发串行、异常/取消后可重新获取。mock 可验证错误路径，不能替代真实 Chromium 的状态/关闭证据。若环境缺浏览器，明确记录集成门槛未执行并提供可运行测试，不用 mock 宣称通过。

- [ ] **6. 收紧产物边界。** 检查 outputs 树、文本读取、media/下载、archive 索引及相关现有可达入口，禁止通用产物接口读取平台凭证、broker/config、内部事件和下划线系统状态。保留专用授权接口按用途访问状态。验证路径穿越、绝对路径、符号链接和 Windows junction 不越界；上传/删除同样使用既有边界，worker 的 upload/local 仍拒绝。

  HTML/HTM/SVG 的预览/直链响应设置 CSP sandbox，不含 allow-scripts、allow-same-origin 或顶层跳转许可；添加 nosniff 等与实现配套的响应约束。也可选择下载而不执行的策略，但必须明确响应头和媒体行为，不只在前端 iframe 上加属性。覆盖大写扩展名、直接 GET/HEAD 和已有预览别名，不能存在另一条无保护原样响应入口。普通图片/音视频与合法输出仍可访问。A/B 以各自 worker 内部身份访问，不通过新增任意路径参数选择别人的工作区。

- [ ] **7. 回归和提交。** 新测试通过后运行下方命令，记录真实结果和未执行门槛。只读技能目录验证不能只在 Windows 设置一个无效只读位：使用真实权限环境，或记录拒绝源码写入的探针与 Linux 挂载未验收的区别。不为本任务提前做 Docker/provisioner、完整 SessionService/relay、前端登录壳或真实发布。

## 必须覆盖的失败条件

- 非法账号标识、越界 profile-base、缺少 worker 根不得回退共享路径，拒绝前无持久写入。
- 同账号等待锁时取消，不影响实际持锁执行；持锁者取消要等浏览器关闭完成再释放。
- 共享代码目录不可写时，真实配置/token/缓存操作仍落私有根；本地旧配置读取不双写。
- 私有状态经输出树、下载、archive 或符号链接不得泄漏；直接打开 HTML/SVG 也不能同源执行。
- A 注销/清缓存不删 B；重复任务或不同账号不会继承前一次进程环境中的凭证。

## 验证命令

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser -q
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/test_account_stats_login.py tests/test_login_start.py tests/test_output_paths.py tests/test_web_security.py tests/test_local_write_guard.py tests/test_harness_workspace.py tests/test_agent_harnesses.py tests/test_runtime_env.py tests/test_web_model_config_parity.py tests/test_settings_stale_mirror.py -q
git diff f30b611 HEAD --check
```

另实际运行新增真实浏览器集成测试，报告命令/浏览器版本/结果；缺环境则明确 skipped/未执行。不要默认安装或下载大体积运行时。既有失败需逐项基线对照，不把新增失败统称环境限制。

## 交付与停止点

报告父/新 SHA、文件清单、红绿证据、完整写入者迁移清单、state_path 布局、实际持久/临时浏览器调用与锁证据、产物 HTTP 响应头和拒绝证据、local 回归、真实浏览器与只读目录门槛状态。提交示例：fix(multiuser): 隔离技能凭证与浏览器状态。

不自动迁移旧数据，不修改历史 schema；确有新持久化需求只能追加迁移并验收旧记录保留。完成后停止等待审计，不进入 Task 8，也不宣称多用户正式发布已通过。
