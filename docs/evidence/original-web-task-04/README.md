# 原 Web 修复 Task 4 证据：原主题登录与设置收口

日期：2026-10-30（Asia/Shanghai）。
父基线：`55501036f31ff5880db87436bd35e3c5d24653d1`（Task 3 验收）。
生产提交：`c0eadbeeed7c69fbfcafebe9bee4d21afa8dcfdc`（`feat: 原主题登录与统一账号设置`）。
证据提交：本文件所在提交。
worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。

## 结论

登录页、模式确认/身份检查/错误重试与登录后的原工作台、设置共用同一套
Easel 主题：浅色纸白/雾蓝/淡桃，深色原 slate/青蓝；不再使用另一套硬编码
紫黑背景，也不再出现 `EASEL CONTROL`。登录前后都复用原 Logo、Bodoni 品牌字、
共享按钮/输入框/卡片/圆角/阴影 token 和 `ThemeToggle`，刷新、系统主题与
跨标签偏好保持原行为。

control 设置改为复用原 `SettingsPanel` 的展示容器（`.settings-overlay` /
`.settings-panel` / `.settings-head` / `.settings-body` / `.settings-nav` /
`.settings-main` / `.settings-foot`），个人设置、全体默认/主聊天、成员/邀请
均可达；普通成员不渲染管理员入口，也不请求 `/api/admin/*`。管理员未配置
主聊天时显示明确的配置引导，只有 provider、model、凭证都已保存时才显示
已配置状态，并注明实际可用性仍需运行时验证。

真实 Easel Logo 已进入 Vite public 与 dist/static；control 固定 `/static`
路由可返回与该原图字节一致的 800×800 PNG。未使用 1px 占位图作为品牌资源
验收。

## 主题与容器复用

- 新增 `AuthFrame`：登录、模式确认、身份检查、错误重试共用居中卡片、
  品牌图和主题切换；只使用共享主题 token，不读取私人缓存。
- `LoginPage` 保留 `onAuthenticated`/`notice`、单次 POST `/api/auth/exchange`
  与原有错误类型；新增可见 label、回车、focus-visible、busy/disabled、
  重复提交保护、邀请一次性和重新获取说明。邀请明文不进入
  localStorage/sessionStorage/console。
- `AuthBoundary` 与 `App` 的状态页改用同一 `AuthFrame`；503/网络错误仍为
  服务不可用，不回落 local。
- `WorkspaceApp` 的设置改为原设置模态容器；ChatPage 不再因打开设置而卸载。
  设置分类使用原 `.snav` 样式；个人设置与管理员设置只替换内容区。
- `ControlSettingsPage`/`AdminSettingsPage` 移除 100vh 独立外壳和深色硬编码，
  改用 `--bg/--bg-elev/--surface/--border/--text/--text-secondary/
  --text-tertiary/--accent-*/--red/--amber/--green/--radius-*`。
- 没有挂载 local `SettingsPanel`，因此不会触发环境安装、CLI、`.env` 或
  宿主权限副作用；现有 identity/epoch/owner 防护与设置协议未改。
- 移动端设置面板在 720px 以下改为纵向布局、导航横向滚动；登录页在
  390×844 下无横向溢出。

## 品牌资源

- 原图：`web/static/easel-icon-transparent.png`，398752 字节，800×800，
  SHA-256 `71c260f5a28e82aced0a513186ebdcf43782b068633e3a7ca5f159d3e3606132`。
- 发布副本：`web/frontend/public/static/easel-icon-transparent.png`，与源图
  字节一致；Vite build 自动复制到 `dist/static/`。
- `tests/frontend/brandAssets.test.mjs` 校验源/发布副本哈希、PNG 尺寸，以及
  index.html、Sidebar、ChatPage 的品牌路径。
- `tests/frontend/browser_original_web_login.py` 只服务真实 `dist/`，检查
  `/static/easel-icon-transparent.png` 的 200、`image/png`、字节哈希、
  `naturalWidth/naturalHeight=800`。
- `tests/multiuser/test_control_static.py` 使用真实 `dist` 与 control 固定
  static 路由检查 200/字节/MIME、缺文件 404、`..` 目录穿越 404；临时
  fixture 也覆盖同一公开目录边界。

`deploy/desktop/Dockerfile.control` 未改：其 frontend 阶段本来执行
`COPY web/frontend/ ./` 与 `npm run build`，public 资源会进入同一 dist。
因此未重建 control 镜像，改用真实 dist + `create_app(frontend_dir=dist)` 的
TestClient 路由验证；未启动临时容器，也未接触用户容器/卷。

## 红绿

### 红（实现前，父基线 `5550103` 构建）

构建成功，产物 `index-Bf2NQlq4.js`；日志 `red-baseline-build.txt`。

Node：新增/修改的 5 项失败，其余 131 项通过（`red-node.txt`）：

```text
✖ published brand asset is byte-identical to the original Easel image
✖ login errors keep 401/403/429/503/network semantics
✖ admin defaults section guides an unconfigured main chat instead of claiming ready
✖ admin defaults section reports a real configured main chat
✖ admin members section keeps member controls without the default form
```

浏览器新增矩阵 8 个场景全部失败（`red-browser.txt`）：

```text
dist/static 未包含品牌图
.login/auth-brand-icon 不存在，无 ThemeToggle
403 仍返回“邀请无效、已使用或已过期”
.settings-panel 不存在，成员/管理员设置仍为旧桥接
```

Static：`1 failed, 2 passed`，真实 dist 品牌图 404（`red-static.txt`）。

### 绿（生产提交 `c0eadbe` 构建）

```text
npm --prefix web/frontend run build
tsc -b + vite build 成功（index-CK2tbBN-.js）

node --test tests/frontend/*.test.mjs
136 passed, 0 failed

npm --prefix web/frontend run lint
oxlint 退出 0

python -m pytest tests/multiuser/test_control_static.py -q
3 passed
```

日志：`green-build.txt`、`green-node.txt`、`green-lint.txt`、
`green-static.txt`。

真实构建浏览器矩阵：

```text
tests/frontend/browser_original_web_login.py
8/8 场景通过，FAILURES=[]；连续 3 次均通过
```

场景与断言：

- `login_desktop_theme_brand`：1280×800 浅/深色计算样式（`--bg`/`--surface`/
  卡片背景）、真实 Logo 200/哈希/800×800、无 `EASEL CONTROL`/Cookie/
  Docker/broker/路径文案、初始焦点、空值 disabled、主题切换、登录后工作台
  与注销后的主题连续。
- `login_mobile_theme_brand`：390×844 浅/深色，无横向溢出，卡片在视口内，
  帮助与按钮可见。
- `login_system_and_cross_tab_theme`：系统深色、无存储时跟随系统；切换后
  写 `easel_theme`、刷新保持、同 context 新标签共享；另一标签切换会同步
  当前页。
- `login_keyboard_busy_once`：Tab 到提交按钮、focus-visible、回车提交、
  busy 文本/disabled、持中再次回车不增加兑换请求；邀请明文不进 storage/
  console；注销后复用同一邀请返回已使用/已过期。
- `login_error_semantics`：401 为邀请无效/已使用/已过期；403 为来源/权限，
  不含邀请失效；429 为尝试过多；503 为服务暂不可用；输入被清空。
- `identity_and_mode_status_theme`：身份检查持中显示同一品牌容器并可切主题；
  身份 503 显示重试；模式 503 显示重试且主题连续。
- `member_settings_theme`：原设置模态、个人设置内容；无全体默认/成员与邀请
  入口或写控件；无 `/api/admin/*` 请求；浅/深色面板与卡片计算背景正确；
  390×844 设置面板无横向溢出。
- `admin_settings_theme_guidance`：三个设置分类可达；未配置主聊天显示 amber
  引导和“配置主聊天”，点击后主聊天通道激活；成员/邀请可达；配置后显示
  “主聊天已配置”和模型名，深色主题正确。

截图：`browser/01..16-*.png`（桌面/窄屏浅深色登录、登录后工作台、跨标签、
错误页、成员/管理员设置、移动端设置）。

原 Task 3 完整回归：

```text
tests/frontend/browser_original_web_task03.py
33/33 场景通过，FAILURES=[]；连续 3 次均通过
```

日志：`task3-final-run1/2/3.txt`。Task 3 fixture 仅做必要兼容：移除 1px
占位 PNG，改由真实 dist 提供品牌图并断言 800×800；设置改为模态后，在
注销前先关闭设置。Task 3 历史截图已恢复，不覆盖已验收证据。

## 文件

生产：

- 新增 `web/frontend/src/components/AuthFrame.tsx`
- 新增 `web/frontend/public/static/easel-icon-transparent.png`
- 修改 `web/frontend/src/components/LoginPage.tsx`
- 修改 `web/frontend/src/components/AuthBoundary.tsx`
- 修改 `web/frontend/src/App.tsx`
- 修改 `web/frontend/src/components/WorkspaceApp.tsx`
- 修改 `web/frontend/src/components/ControlSettingsPage.tsx`
- 修改 `web/frontend/src/components/AdminSettingsPage.tsx`
- 修改 `web/frontend/src/lib/auth.ts`
- 修改 `web/frontend/src/styles/index.css`

测试：

- 新增 `tests/frontend/browser_original_web_login.py`
- 新增 `tests/frontend/brandAssets.test.mjs`
- 修改 `tests/frontend/browser_original_web_task03.py`
- 修改 `tests/frontend/multiuserAuth.test.mjs`
- 修改 `tests/frontend/multiuserSettings.test.mjs`
- 修改 `tests/multiuser/test_control_static.py`

未改 `useWorkspace`、Run/SSE、题卡状态、模型/profile、认证协议、后端
路由或 control Dockerfile。未接工作台/内容库/账号等 Task 5 API。

## 边界与清理

- 未修改主审 audit/task/spec/plan/probe；未进入 Task 5；未自动上线。
- 未执行 Docker，未启动/停止/重启/替换用户容器，未接触用户 Key/Cookie/
  卷/邀请；浏览器只使用随机 localhost 假 API、假邀请和独立 Chromium。
- 未读取或复制真实 `.env`/`.dsh`/凭证；登录令牌与设置密钥未落盘。
- Task 3 自有 33 场景回归日志在 Task 4 证据目录，历史截图已恢复。
- `git diff c0eadbe^..c0eadbe --check` 无空白错误。
