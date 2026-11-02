# 原 Web 修复 Task 4：原主题登录与设置收口

状态：Task 3 已验收，当前仅 Task 4。代码 Agent 实施，主会话审计；完成后停止，不进入 Task 5、不切换用户实例。
父基线：`55501036f31ff5880db87436bd35e3c5d24653d1`。
继续 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。

## 必读与目标

读 AGENTS.md、[修复设计](../specs/2026-10-29-original-web-login-and-chat-repair.md)、[总计划 Task 4](../plans/2026-10-29-original-web-login-and-chat-repair.md)、[Task 3 验收](../audits/2026-10-30-original-web-task-03-accepted.md)和[用户实例保护清单](2026-10-29-original-web-running-baseline.json)。

用户要求在原 Easel Web 加登录，视觉与本身主题相似、不突兀。沿用已有 Logo、字体、主题变量、按钮/输入框/卡片/间距，不另造品牌/界面/前端构建，不安装新 UI 框架或重新设计整个工作台。

## 1. 登录与加载/错误状态

- LoginPage 使用原 Easel 图标/品牌，移除 EASEL CONTROL；沿用既有正文/品牌字体栈，无新字体服务或大图依赖。
- 使用 styles/index.css、styles/theme.css 的 bg/surface/border/text/accent/radius/shadow token。浅色纸白/雾蓝/淡桃，深色原 slate/青蓝；不保留另一套硬编码紫黑背景。
- 登录页、模式确认/身份检查/错误重试、登录后原工作台和设置之间主题连续。复用 ThemeToggle 与 easel_theme 预绘制逻辑；刷新、系统主题、跨标签偏好按原行为，不能先闪一块不相关底色。
- 邀请流程与 onAuthenticated/notice 兼容，不改认证协议、不加账号密码/SSO/公开注册。标明“一次性邀请，使用后失效；退出后需重新获取”，提供简短重新获取帮助，不把它叫可复用验证码。
- UI 不展示 Cookie/broker/Docker/env/路径等实现细节，不打印邀请、Key 或错误原文。管理员重签完整命令留现有运维手册，不在页面嵌入服务器终端操作。
- 保留可见 label、回车、加载/disabled、重复提交保护、focus-visible 和错误/429 提示。401 邀请失效、403 来源/权限、429、503/网络错误不能全部冒充“邀请过期”；原已测试安全语义保持。
- 窄屏可滚动、无横向溢出；不通过删文案、遮住焦点或页面缩放来获得截图。

## 2. 原设置容器与角色边界

Task 3 的设置桥接需融入原设置视觉和布局，个人覆盖、全体默认/主聊天、成员/邀请都保持可达。可以共享/提取原 SettingsPanel 的纯展示容器或复用其样式；不能为复用而挂载 local 的全局 env/tools/CLI 安装副作用。

ControlSettingsPage/AdminSettingsPage 的深色硬编码、100vh 独立全屏外壳要按原设置主题收口；普通成员不渲染管理员写控件，也不请求管理员 API。管理员未配主聊天时提供明确配置引导，不能以绿色状态伪装已可用。

数据/动作继续复用现有 identityFetch/useWorkspace.call、版本冲突、凭证留空复用、邀请一次性展示和过期提示；不因改样式重做协议。异步设置响应在注销/换号后不能落入新身份，取消和 pending 题卡的 Task 3 行为不动。

## 3. 真实品牌静态资源必须进入发布产物

目前 Sidebar/index.html 的 Logo 相对 URL 指向 static 下的 easel-icon-transparent.png；原图位于 `web/static/easel-icon-transparent.png`，当前 Vite public 仅 favicon.svg/icons.svg，构建 dist/static 不含该图。control 的 /static 只服务固定 frontend_root/static，不能假设本机 web/static 自动存在于控制镜像。

- 采用最小、明确的公开品牌资源打包方式，让本机 npm build 与 control Dockerfile 构建都能得到相同实际 Logo；复用原图，不生成新图或引入整套 showcase 媒体。
- 尽量单一来源；如使用 public 副本，明确源及同步/哈希校验，避免静默替换成另一个图标。
- browser fixture 必须服务真实 dist 中品牌资源，不用现有 1px 占位 PNG 替代这项验收。检查资源 200、naturalWidth/naturalHeight 正常、字节/哈希与原资产一致，无 broken image。
- 公开范围仅品牌静态资源；不能把 outputs、HOME、.env 或仓库根整目录挂为 static。保留路径穿越/越权拒绝，私有文件访问仍留 Task 5 的认证路由。

## 4. 修改范围

LoginPage.tsx、AuthBoundary.tsx、App.tsx 的模式/错误展示，ThemeToggle.tsx、Sidebar.tsx 的必要品牌/身份入口，SettingsPanel/ControlSettingsPage/AdminSettingsPage/WorkspaceApp 设置容器的最小共享展示；styles/index.css/theme.css。必要的 public 品牌资源、前端构建资源步骤及 control Dockerfile 可改。

不改 useWorkspace/Run/SSE/题卡状态或模型/profile；本项不接工作台/内容库/账号等未接 API、不编造空数据或降级 local。品牌 static 若需后端最小修复，先给实际 dist/路由证据，只改公开固定目录边界。

测试扩充 tests/frontend/multiuserAuth.test.mjs、multiuserSettings.test.mjs、原 browser_original_web_task03.py 必要兼容；新建专用登录/主题浏览器 fixture 与必要 static 回归。证据放现有 docs/evidence 下 original-web-task-04 子目录，不改主审文件。

## 5. 红绿与验收

- [ ] 先写行为/资源红测：登录前后共享主题与真实 Logo、身份错误状态区别、窄屏无溢出、成员无管理入口；不以只查 CSS 文本 marker 代替真实渲染。
- [ ] 保留原邀请 API/单次提交/401/429/503、键盘/焦点、注销及重新获取帮助；登录令牌不入 localStorage/sessionStorage/日志。
- [ ] 截图至少覆盖 desktop 1280×800 与窄屏 390×844、浅/深色登录，登录后原工作台、个人/管理员设置；检查真实计算样式、可见内容和焦点，不仅“截图文件存在”。原空白 Logo 不能作为通过证据。
- [ ] 检查真实 dist 静态资源与 control 固定公开目录：200/正确字节及 MIME、缺文件/路径穿越拒绝；可扩充 tests/multiuser/test_control_static.py。
- [ ] 运行全部 frontend Node、build、lint 和 Task 3 的完整 33 场景回归；新视觉场景单独列。无相关改动时不重复后端/真实 Harness/provider 全套。
- [ ] 若控制镜像公共资源打包改变，只增量构建 control 并验证无端口临时容器内的 dist/静态路由；不重建未变 worker/Harness/gateway，不用用户实例验收。

基本命令：

```powershell
node --test tests/frontend/*.test.mjs
npm --prefix web/frontend run build
npm --prefix web/frontend run lint
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser/test_control_static.py -q
git diff --check
```

如当前 Node 不展开 glob，显式枚举测试文件再传入；记录真实结果与命令，不改依赖版本来绕过。

## 交付

父/新完整 SHA、精确文件清单、共享样式/容器复用说明、浅深/窄屏/错误/焦点截图及行为断言、资源来源和 dist/control 验证、Node/build/lint/static/33场景结果、角色/敏感输入回归与清理。保存已审代码/数据，原用户容器继续运行不动；显式提交后停止等复审，不进入 Task 5、不自动上线。
