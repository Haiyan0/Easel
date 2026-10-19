# Easel 技能加载、画像与多用户架构手册

分析日期：2026-10-13。对象：本地 `C:\Code\Easel` 当前源码。

本文区分三类信息：**源码已实现**、**规范要求**、**改造建议**。没有运行付费模型或发布流程；不把静态检查通过视为模型路由准确率或端到端执行成功的证明。本文讨论项目自带的技能库，与当前 Codex 聊天环境另行提供的 skills 清单不同。

## 1. 架构结论

Easel 是一个本地优先的社媒内容工作台：Python 后端组织文件、会话与配置，Agent 执行引擎理解任务并调用本机工具，技能以 Markdown 流程和脚本目录交付。

- 实际扫描到 **114 个 `SKILL.md`**，README 展示的 113 尚未同步。
- 技能注册主要是**目录发现**，不是为 114 个技能分别注册 Python 函数或 API 工具。
- 自然语言路由主要是 **LLM 根据技能描述和任务上下文选择**；Easel 后端没有实现专用的技能向量召回、关键词分类或 Top-K 重排服务。
- 按需加载包括技能正文和引用资料；脚本通常由工具执行，不需要整体加入模型上下文。
- “用户画像”是跨平台创作者/内容账号的人设档案，由六份 Markdown 组成，不是登录用户、租户或权限主体。
- 现有实现支持多个画像和多个会话，但**没有完整的应用用户认证与资源权限隔离**，不能直接当作多租户服务。

## 2. 路径与职责

| 路径 | 职责 | 模型如何接触它 |
|---|---|---|
| `skills/builtin/<目录>/SKILL.md` | 技能名称、触发描述、工作流 | 发现元数据后，按需读取正文 |
| `skills/builtin/<目录>/references/` | 领域知识、命令清单、细节规范 | 根据 SKILL 内链接继续读取 |
| `skills/builtin/<目录>/scripts/` | 技能专属执行代码 | 调用本机命令工具执行 |
| `skills/builtin/<目录>/assets/` | 模板、素材、静态资源 | 执行流程按需使用 |
| `skills/shared/` | 跨技能共享脚本及参考资料 | 引用或执行共享资源 |
| `agents/workspace/` | SOUL、AGENTS 等通用工作区说明 | 执行引擎注入常驻指令 |
| `profiles/<画像>/` | 六维画像及账号长期记忆 | 消息声明当前画像，Agent 读取 |
| `outputs/<主题>/` | 成品、中间素材、`.easel.json` | 工作流写入，后续步骤读取 |

当前 114 个技能的分布：

| layer | 含义 | 数量 |
|---|---|---:|
| discover | 发现 | 9 |
| plan | 策划 | 16 |
| produce | 制作 | 52 |
| publish | 发布 | 20 |
| attribute | 归因 | 11 |
| general | 画像管理等通用能力 | 6 |

在每个技能自己的 `references/` 与 `scripts/` 子目录下，递归统计到 172 个参考文件、61 个脚本目录内文件。这个统计不包含共享目录和 vendor 中另行存放的工具，也不等于可执行命令数量。

### 2.1 OpenClaw 路径：先同步，再由引擎发现

`agents/sync.sh` 将项目技能完整复制到 OpenClaw 实际工作区：

```text
项目 skills/builtin/<技能>/  →  <OpenClaw workspace>/skills/<技能>/
项目 skills/shared/          →  <OpenClaw workspace>/shared/
项目 agents/workspace/*.md   →  <OpenClaw workspace>/*.md
项目 profiles/              ←  <workspace>/easel-profiles 链接
项目 outputs/               ←  <workspace>/outputs 链接
```

这里的同步是磁盘复制，不等于把所有技能正文塞进 prompt。技能阅读副本位于工作区，脚本执行仍要求切换到项目根，使用项目源代码和 `.venv`。

工作区路径由 `easel/openclaw_workspace.py:workspace_dir()` 统一解析，优先级为：

1. `EASEL_OPENCLAW_WORKSPACE` 显式覆盖。
2. `openclaw --profile easel status --json` 中的实际 `workspaceDir`。
3. OpenClaw 配置中的 workspace。
4. 已经有技能内容的历史候选目录。
5. 按 OpenClaw 版本推断默认位置。

所以不能始终假设工作区固定在某个用户目录。代码兼容 `.openclaw/workspace-easel` 与 `.openclaw-easel/workspace` 两种历史布局。

Windows 的 `setup.ps1` 也调用同一个解析器，再复制技能、共享资源和说明文件；画像与产物使用 Junction 链接。

**维护差异：** Bash 完整同步会清理已删除技能、重建技能目录、移除旧 `USER.md`、清空全局 `MEMORY.md`；当前 Windows 安装器主要用 `Copy-Item -Recurse -Force` 覆盖技能，没有对应的旧技能目录清理，也没有相同的 USER/MEMORY 清理步骤。Windows 重装后可能残留旧技能或旧说明，不能假定两套同步逻辑完全等价。

### 2.2 Codex、OpenCode、ACP 路径：直接读取源码

`easel/harnesses/workspace.py:harness_instructions()` 读取共享工作区 Markdown，再补充实际项目根、引擎、权限、技能路径、Python 与画像路径。

- Codex：通过原生 app-server 的 `developerInstructions` 注入说明。
- OpenCode / 通用 ACP：通过 `session/prompt` 的文本前缀注入说明。
- 这些适配器直接指向项目 `skills/builtin/`，没有 Easel 自行构造的全库技能描述数组，也不需要 OpenClaw 技能副本。
- 原生引擎可能有自己的 skills 发现机制；其具体目录扫描、提示词截断与缓存规则不在本仓库实现，不能由 Easel 的规范直接推定。

纯 API 模式另有边界：它只有模型聊天能力，没有本机技能执行工具；CLI 与 Web 的指定技能入口会拒绝该模式。

## 3. 100 多个技能怎样注册

### 3.1 项目注册：目录即能力单元

Web 的 `get_skills()` 和 CLI 的 `_list_all_skills()` 都扫描 `skills/builtin/` 下一级子目录，找到 `SKILL.md` 就列入技能库。Web 解析 description、layer，附加 API 是否配置的展示状态。

```text
技能目录存在 + SKILL.md 存在
        ↓
出现在 CLI 列表 / GET /api/skills
        ↓
OpenClaw 同步后由其技能系统发现
或由原生 Agent 按项目路径查找、读取
```

`GET /api/skill/{name}` 返回单个技能正文，主要供技能详情界面使用。这个接口的存在并不表示聊天后端自动调用它来给模型做候选技能召回。

`find_skill()` / `_find_skill()` 以目录查找，支持 `quality-gate` 与 `skill-quality-gate` 这样的前缀兼容。不是完整的 frontmatter 名称索引。

现有例外：`skill-xhs-analyzer` 的 frontmatter `name` 为 `redbook`，校验器专门允许这一差异。CLI 按目录查找，所以不能假设 `easel skill redbook` 与 `easel skill xhs-analyzer` 完全等价。

### 3.2 元数据就是模型的“选择说明”

规范要求常规 frontmatter 只保留：

```yaml
---
name: skill-profile-manager
description: >-
  管理账号画像的创建、编辑、记忆更新、切换、导出与对比。
  当用户说“编辑画像、写进画像记忆、切换画像”时使用。
  首次从社媒数据生成画像用 skill-profile-builder。
layer: general
---
```

一个有效的 description 同时回答：**能做什么、用户怎样表达这项需求、与邻近技能怎样分工**。相近技能越多，边界说明越重要。

实际校验器要求描述 80–300 字符、有触发措辞、layer 合法、名称与目录匹配（允许已登记例外）、整个 SKILL.md 不超过 200 行、资源链接有效。仅在需要时允许 `metadata.openclaw` 运行时清单。

### 3.3 展示与执行是不同注册层

- 动态技能列表来自目录扫描。
- 中文展示名在 `web/frontend/src/lib/skillDisplayNames.ts`。
- 能力菜单在 `web/frontend/src/lib/capabilityMenu.ts`，包含静态分组和状态。
- 部分媒体技能的配置需求在 `web/app.py:SKILL_API_REQUIREMENTS`。

因此，新技能被扫描发现，不保证中文菜单、配置提示和能力地图也自动更新。这里存在多份派生数据需要维护的问题。

## 4. 模型如何选择性 reference

### 4.1 对话执行链

```text
用户输入 + 当前画像 + 会话上下文
        ↓
chat_turn_message：画像前缀 + 用户原文 + 每轮技能提醒
        ↓
共享 SOUL / AGENTS / 运行时路径说明
        ↓
Agent 结合平台、意图、上轮产物查找技能
        ↓
读取选中的 SKILL.md
        ↓
按步骤读取 references，执行 scripts / shared 工具
        ↓
写入 outputs/<主题>/，登记 .easel.json，交付成品
```

`AGENTS.md` 明确要求每轮先找精确技能，找不到才复用相邻技能，再没有相关能力时使用通用能力。`easel/persona.py:TURN_REMINDER` 在每条对话消息末尾重申“先查技能库”，用于抵抗长对话中的指令衰减。

**它是提示词引导的模型判断，不是确定性分发器。** 用户话术不会先被 Easel 的某个硬编码分类函数转换成固定 skill ID。前端 `titleIntent()` 的正则只用于会话标题，不能当成技能路由实现。

### 4.2 三层渐进加载

| 层 | 内容 | 设计中的加载方式 | 实现边界 |
|---|---|---|---|
| Metadata | name、description、位置等 | 小目录供发现和选择 | OpenClaw 负责原生技能发现；其它适配器没有自行注入统一目录 |
| Instructions | SKILL.md 执行步骤 | 命中后读取 | 依赖 Agent 真实调用读取工具 |
| Resources | references、模板、脚本 | 工作流中按需使用 | 资料读入上下文，脚本通常只执行并返回结果 |

示例：用户要求“从我的账号链接建立人设”。模型选择 `skill-profile-builder`，先读主文件；收集信息时读 `references/onboarding-questions.md`，分析链接时再读 `references/social-link-analysis.md`。已有画像的编辑则应选 `skill-profile-manager`。

复杂任务可以组合技能，例如策划 → 卡片文案 → 卡片渲染 → 平台检查。五层是业务分类及编排规则，不是强制每次走完五层的状态机。

跨步骤通过 `.easel.json` 中的 `steps[]`、简要摘要和产物路径衔接；具体内容放文件，决策意图可写入 `brief.md`。这样模型无需一直在对话里重复携带大段中间产物。

### 4.3 为什么这个设计能控制规模

常驻说明负责通用规则；短 description 负责区分能力；正文负责步骤；references 承载细节。114 个技能全部 frontmatter 合计约 30,020 字符，**这是文件字符统计，不是实际 prompt token 数**，引擎未必把全部字段原样注入。

目前控制的是“完整技能说明与资料的按需读取”。没有发现 Easel 自己实现的硬 Top-K 限制、最大 reference 字数预算、描述向量索引或技能路由命中率测试。增加技能仍可能带来描述重叠、选择不稳定和上下文增长。

### 4.4 指定技能与自动选择

```powershell
.\.venv\Scripts\easel.exe skill quality-gate -i "检查这段文案"
.\.venv\Scripts\easel.exe skill quality-gate -i "检查这段文案" -p "科技数码达人"
```

这条入口先校验技能目录，再构造“请执行 /<技能>”的消息交给所选 Agent，并不是后端直接 import 对应 Python 实现。自然语言聊天入口则让模型自行选择技能。

## 5. 画像是什么技术栈

### 5.1 六维 Markdown 文件模型

```text
profiles/<画像名>/
├── identity.md       定位、差异化、内容方向
├── style.md          语气、节奏、视觉、标志性表达
├── audience.md       目标受众、兴趣、痛点
├── platforms.md      跨平台账号信息和运营规则
├── preferences.md    偏好、禁区、红线
└── memory.md         可复用经验、反馈与归因
```

画像名是目录名，一个画像代表一个跨平台人设。同一人可以运营多个画像，同一画像也可以覆盖多个平台；它没有自动建立与 Web 登录用户的所有权关系。

| 部分 | 实际技术 |
|---|---|
| 画像页面 | React 19、TypeScript、Vite；组件本地状态 |
| HTTP 接口 | FastAPI、Pydantic、Uvicorn |
| 存储 | UTF-8 Markdown，本地文件系统 |
| 读取辅助 | Python `pathlib`、固定文件顺序、名称及路径校验 |
| 编辑保存 | 写临时文件后替换目标文件 |
| AI 增强 | 调用当前 Agent；Web 用后台 daemon thread，前端轮询 JSON 状态 |
| 个性化 | Prompt 中声明画像，Agent 读取后凝练；API 聊天模式直接拼接画像正文 |
| 记忆 | `memory.md` 增量更新，模型去重和总结 |
| 账号资料补充 | Playwright/平台脚本等获取数据，再回写 Markdown |

这个画像模块不依赖专用画像数据库、向量数据库、模型微调或独立特征服务。README 的 embedding 配置属于 OpenClaw 记忆检索能力，不能据此认定 Easel 使用 embedding 选择技能或检索六维画像。

### 5.2 创建流程：确定性基线 + Agent 增强

`POST /api/profile/build`：

1. 校验画像名及是否存在。
2. `_write_baseline_profile()` 将表单写成六份文件，缺项用待补充标记。
3. 立即返回，画像已经可以使用。
4. 后台线程调用 `skill-profile-builder` 分析链接、完善内容。
5. 进度写到 `outputs/_profile_build/`，界面轮询状态。

优点是画像创建不必等待长时间模型响应。边界是任务没有独立持久化队列，服务重启可能中断增强；`done` 表示调用返回，代码没有再逐项验证“链接成功抓取、六维字段已补全”。它不是质量验收状态。

### 5.3 会话中的注入和记忆

Agent 模式的消息前缀类似：

```text
我当前使用的画像是「科技数码达人」。
本会话的账号长期记忆仅使用 profiles/科技数码达人/memory.md。
```

它传递的是画像身份与读取范围，不是在每轮把六份文件全部由后端拼接进去。Agent 根据 AGENTS 读取并凝练。纯 API 聊天模式则通过 `load_profile_text()` 读取并拼接六维及其它 Markdown，因为它没有文件工具。

当前 AGENTS 要求发现、策划、发布、归因读取六维，制作凝练相关维度；`profiles/_template/README.md` 仍有“platforms 仅发布层”等旧分层表述，应以当前运行规则和代码为准。

账号长期经验先总结 1–3 条，经用户同意后由 `skill-profile-manager` 更新当前画像。没有画像时走通用模式，不借用其它画像记忆。

这个隔离目前是**路径约定与模型指令**。能够访问整个项目的 Agent，技术上仍可能读到其他画像；它不是操作系统或数据库级访问控制。

人设一致性也有明确边界：模型先给评分，`persona_gate.py` 再按阈值给 pass/warn；低于 80 分提醒，仍允许发布。该脚本不负责从内容自动计算评分，也不是权限门禁。

## 6. 现在能不能多用户访问

回答要分成两层：**多人从浏览器访问同一个实例在网络条件允许时可以实现；不同用户具有独立数据和权限的多用户产品，当前没有实现。**

### 6.1 已有能力

- 多个画像目录。
- 多个会话 ID、原生引擎会话续接。
- 同一会话的并发锁与部分跨进程锁。
- 按会话分区的附件目录。
- 前端各浏览器通过 localStorage 保存自己的会话列表。

这些能力处理“同时操作”和“会话续接”，不负责判断“谁有权访问这份资源”。

### 6.2 缺失的用户隔离

| 资源 | 当前范围 | 多用户所需 |
|---|---|---|
| 画像 | 全局 `profiles/`；接口按名字访问 | 用户/团队所有权与权限校验 |
| 产物 | 全局 `outputs/` | 租户存储空间、下载授权 |
| 模型设置 | 全局 `.env` 和 `_harness/config.json` | 独立配置及密钥范围 |
| 平台登录态 | 服务进程账户下的浏览器配置目录 | 按租户/账号隔离 Cookie 和凭据 |
| 会话 | 客户端提供 ID，后端按 ID 存取 | 服务端归属记录与每次操作授权 |
| 画像记忆 | 同一个画像共享 memory 文件 | 修改权限、版本与冲突处理 |
| Agent 工具 | 在项目根下工作 | 每租户独立工作目录和执行边界 |

`GET /api/personas` 返回所有画像；画像编辑、删除与产物列表接口没有登录用户身份过滤。`harnesses/store.py` 中的“ownership”指会话绑定到哪个执行引擎，记录 runtime/native_id，不是绑定哪个人类用户。

因此不同浏览器即使只在 localStorage 看到自己的聊天列表，后端数据依然共享。哈希会话 ID、附件分区和画像提示词都不能代替授权。

### 6.3 网络防护不等于登录认证

`web/app.py` 配置了 CORS、TrustedHost 和 `local_write_guard`。写请求按 Origin 白名单或回环连接判断，目的是防止恶意网页调用本机工作台。

源码的 Uvicorn 入口监听 `0.0.0.0`，所以绑定地址本身不限本机；是否远程可达取决于实际启动方式、防火墙、反向代理和网络。`EASEL_EXTRA_HOSTS` / `EASEL_EXTRA_ORIGINS` 可以扩展来源，但不产生用户身份或权限。

中间件注释也明确指出，原生 HTTP 客户端能伪造 Host/Origin。这套防护不是公网认证边界。平台扫码登录、模型供应商认证以及浏览器扩展的局部凭据，也不等于 Easel 应用用户登录。

**判断：当前定位是一个受信任操作者使用、可管理多个内容账号的本地工作台。**

## 7. 怎样扩展得更合理（建议，尚未实现）

### 7.1 技能治理与路由

先生成统一技能索引，字段至少包含规范 ID、目录、description、layer、资源路径与配置需求。由该索引派生中文菜单、配置提示和文档计数，减少多处漂移；统一 directory name 与 frontmatter name 的别名规则。

对所有执行引擎提供一致的小型技能目录，避免非 OpenClaw 模式仅获知一个路径，却要临场遍历大量文件。继续只按需加载正文与 references。

当实测路由开始不稳定时，再加入候选召回：用意图、平台、产物类型先缩小范围，再让模型在候选描述中选择。可以采用关键词检索或 embedding，但不要把六层标签做成唯一过滤条件，复杂任务会跨层。

补充路由评估集：精确任务、相邻技能歧义、追问、跨层工作流、无匹配任务。记录选中技能、读取资料、失败与产物结果，才能判断描述调整或 Top-K 检索是否有用。

更严格的 reference 预算与资源访问限制，需要由执行层实现；仅在 Markdown 中写“按需”无法保证模型不会过度读取。

### 7.2 少量用户：优先独立实例

低改造成本的办法是每个用户一个实例，分别隔离项目可写数据、OS 账户或容器、HOME、OpenClaw state/workspace、原生 Agent 凭据、浏览器配置和端口；外层代理提供身份验证与实例路由。

只隔离 `profiles/` 或换一个端口不够，Cookie 和 Agent 登录凭据可能仍通过同一个系统账户共享。仅让多个人通过外层认证进入同一实例，只形成共享工作台。

### 7.3 正式多用户：建立应用级资源模型

建议最小关系：

```text
User → Membership → Workspace/Tenant
Workspace → Persona / PlatformAccount / Session / Artifact / Job
Session → 当前 Persona + 当前执行引擎
Job → 发起人 + 操作账号 + 输入产物 + 审批结果
```

需要同时补齐：

1. 服务端认证与用户/团队权限；会话、画像、下载、发布、配置等每个接口校验归属。
2. 数据库保存资源关系、任务、状态与审计；Markdown 可继续作为画像正文，关系和授权应由服务端负责。
3. 租户级文件与凭据隔离；共享技能代码可只读挂载，用户数据独立存储。
4. 每租户受限 Agent 执行环境，不能只在 prompt 告诉模型不要访问别人目录。
5. 持久化任务队列、限流与资源预算，替代画像增强的进程内线程。
6. 画像版本控制或乐观锁，避免同一画像多人编辑与 Agent 回写覆盖。
7. 明确发布者权限、平台账号归属与操作审计。

关系数据可采用 PostgreSQL，后台任务可采用适合部署规模的队列，产物可放租户分区的文件系统或对象存储。这些是架构方向，不是当前代码已使用的技术。

## 8. 新增技能操作手册

1. 在 `skills/builtin/<稳定目录名>/` 新建 `SKILL.md`，遵循三字段 frontmatter。
2. 描述写清能力、用户表达、相邻技能边界；正文明确输入、步骤、输出与画像行为。
3. 主文件控制在 200 行内，长知识拆进 references，引用路径相对当前文档。
4. 优先复用 shared 工具；执行路径明确转换成项目根下的路径，使用项目虚拟环境。
5. 产物放 `outputs/<具体主题>/`，中间素材放 assets；按规范登记 manifest。
6. 发布脚本接入项目已有内容安全检查；付费、公开发布等遵守运行规则。
7. 更新展示名、能力菜单及必要的 API 配置需求。
8. 运行静态校验，随后再做实际任务与路由验证。
9. OpenClaw 模式更新实际工作区副本；原生 Agent 模式直接读取项目。不要为了同步技能盲目重跑会修改配置的完整安装器。

静态校验命令：

```powershell
Set-Location C:\Code\Easel
.\.venv\Scripts\python.exe scripts/validate_skills.py
.\.venv\Scripts\python.exe scripts/validate_skill_commands.py
```

本次输出：

```text
OK: 114 skills and publisher execution contracts conform
OK: 306 documented Python commands match their argparse schemas
```

Bash 同步入口是 `bash agents/sync.sh`；Windows 安装器中的同步代码在 `setup.ps1`。完整同步会修改引擎工作区；本次分析没有执行同步、安装或配置变更。

## 9. 源码阅读索引

以下路径相对项目根，可用函数名直接检索：

| 问题 | 源码/规范 |
|---|---|
| 技能格式与渐进加载设计 | `docs/SKILL-SPEC.md`、`docs/prompt-stack.md` |
| Web 技能发现与解析 | `web/app.py:get_skills`、`_parse_skill_md`、`find_skill` |
| CLI 指定技能调用 | `easel/commands/skill.py:cmd_skill` |
| OpenClaw 工作区解析 | `easel/openclaw_workspace.py:workspace_dir` |
| 同步与工作区链接 | `agents/sync.sh`、`setup.ps1` |
| 常驻说明与运行时路径 | `easel/harnesses/workspace.py:harness_instructions` |
| 原生引擎接入 | `easel/harnesses/codex.py`、`acp.py`、`docs/agent-harnesses.md` |
| 自动路由要求 | `agents/workspace/AGENTS.md` 核心执行规则 |
| 画像前缀与每轮提醒 | `easel/persona.py:persona_prefix`、`chat_turn_message` |
| 画像创建与编辑 | `web/app.py:api_profile_build`、`_write_baseline_profile`、`api_persona_file_save` |
| 画像页面 | `web/frontend/src/components/ProfilePage.tsx`、`OnboardingWizard.tsx` |
| 画像生成与记忆更新 | `skills/builtin/skill-profile-builder/`、`skill-profile-manager/` |
| 会话与引擎绑定 | `easel/harnesses/store.py`、`rpc.py` |
| 浏览器会话列表 | `web/frontend/src/lib/store.ts` |
| 本机来源防护 | `web/app.py:local_write_guard` 及相邻中间件配置 |
| 运行契约检查 | `scripts/validate_skills.py`、`validate_skill_commands.py` |
| Accounts 页面与按钮分支 | `web/frontend/src/components/AccountsPage.tsx` |
| 登录进程、状态、身份校验与退出 | `web/app.py:LOGIN_RUNNERS`、`_start_login`、`_check_account_whoami`、`api_logout` |
| 登录状态文件和短信回填协议 | `skills/shared/scripts/login_state.py` |
| 平台浏览器会话 | `skills/shared/scripts/xhs_publish.py`、`douyin_publish.py`、`web_publisher.py` |
| B站扫码及 Cookie 保存 | `skills/shared/scripts/bili_login.py` |
| 公众号后台扫码、取数、建草稿 | `skills/shared/scripts/weixin_mp_stats.py` |
| 前端身份缓存与后台校验 | `web/frontend/src/lib/whoami.ts` |

**未验证范围：** 实际 OpenClaw 安装版本的技能 prompt 内容、引擎自己的候选截断行为、模型实际选择准确率、多个不同用户同时执行发布的行为，以及远程代理部署。这些不能仅由本仓库静态源码确认。

## 10. 账号登录 Accounts：原理与操作手册

本章依据当前 Accounts 页面及后端代码补充。没有启动登录进程、读取已有 Cookie/密钥、扫码、退出账号或实际发布；这里描述的是源码实现，平台实时可用性需另行验证。

### 10.1 Accounts 登录的究竟是什么

Accounts 管理的是**社媒平台授权状态**：让运行 Easel 的机器能够复用你的平台会话进行发布、身份查询和数据获取。它不是 Easel 自身的登录系统，也不依赖用户画像才能使用。

登录流程由确定性的页面、HTTP 接口和 Python 脚本完成。模型不负责生成登录二维码、保存 Cookie 或判断每一个登录状态；Agent 执行后续技能时复用已保存的授权材料。

浏览器在 **Easel 后端所在机器** 上运行，手机只负责扫描前端展示的二维码并确认授权。若 Easel 部署在服务器上，登录态也保存到服务器的运行账户下，不会自动保存在访问页面的电脑里。

### 10.2 七个平台与三种接入方式

| 平台 ID | 平台 | 当前 Accounts 登录按钮 | 实现与保存位置 |
|---|---|---|---|
| `xiaohongshu` | 小红书 | 扫码 | `xhs_publish.py login`；持久化浏览器目录 `XiaohongshuProfile` |
| `douyin` | 抖音 | 扫码，必要时短信验证 | `douyin_publish.py login`；`DouyinProfile` |
| `kuaishou` | 快手 | 扫码 | `web_publisher.py login-qr`；`KuaishouProfile` |
| `weixin-channels` | 微信视频号 | 微信扫码 | 同上；`ChannelsProfile` |
| `zhihu` | 知乎 | 扫码 | 同上；`ZhihuProfile` |
| `bilibili` | B站 | B站 App 扫码 | `bili_login.py login`；项目根 `cookies.json` |
| `wechat-oa` | 微信公众号 | 公众号后台扫码 | `weixin_mp_stats.py login`；`WeixinMpProfile` |

三种接入方式是：**持久化浏览器会话、B站二维码 API + Cookie 文件、公众号 AppID/AppSecret 兼容接口**。当前页面正常的公众号登录按钮使用第一种；凭据接口仍留在代码中，不能只看旧注释就认定页面仍要求填写 AppID。

### 10.3 浏览器平台如何登录并持久化

```text
Accounts 点击登录
    ↓ POST /api/login/<平台>?restart=true
FastAPI 启动平台 Python runner 子进程
    ↓
Playwright 打开平台页面，使用持久化浏览器目录
    ↓
提取 / 截取二维码 → outputs/_login/<平台>.png
写进度 JSON → outputs/_login/<平台>.json
    ↓ GET /api/login/<平台>/status，前端每秒轮询
手机扫码 + 平台确认 + 必要的短信验证
    ↓
平台返回登录会话；浏览器持久化 Cookie、站点存储等
    ↓
runner 写 success，关闭浏览器并保存状态
    ↓
前端刷新卡片，触发 whoami 校验身份
```

脚本使用 Playwright 的 `launch_persistent_context()`，会话不是仅存在 Python 内存中。后续发布或取数再次打开同一浏览器目录，就能复用授权；平台会话仍可能过期、被撤销或触发再次验证，“本地持久化”不保证永久免登。

通用网页扫码流程使用无头浏览器。专用脚本还包含不同环境下的浏览器选择或窗口回退，不能假设每个平台始终只用一种启动方式。

后端通过 `subprocess.Popen` 启动 runner 并立即响应。Web 请求不一直等待手机扫码，而是读状态文件。状态文件原子写入，避免前端读到半截 JSON；异常退出会转成明确错误，二维码文件修改时间 `qrTs` 用于刷新图片缓存。

常见状态：

```text
starting → qr_ready → scanned → success
                            ↘ sms_required → verifying → success
                                                   ↘ sms_required（重新输入）
任意阶段也可能结束为 expired / error
```

`scanned` 只代表已扫描，不等于授权完成。还应在手机上确认，等待页面显示 `success`。

### 10.4 B站为何不打开浏览器

`bili_login.py` 调用 B站 TV 端二维码接口申请 URL/auth_code，用 `segno` 将 URL 渲染为 PNG，再轮询授权结果。成功后把 `cookie_info`、`token_info`、`sso` 写成 biliup 兼容的 `cookies.json`。

后续 B站上传入口把该文件传给 `biliup -u ... upload`；身份查询也可读取 Cookie 后调用接口。它与其它平台的 Chromium 会话目录不是同一种凭据格式。

账号列表快速判断 B站是否已登录时只检查 Cookie 文件是否存在；实际是否仍有效，需要“校验账号”调用 whoami 确认。文件存在不能保证平台继续接受它。

### 10.5 公众号：后台扫码与 AppID 凭据不能混用

当前页面在公众号卡片点击“登录”时调用：

```text
POST /api/accounts/wechat-oa/mp-login
    → weixin_mp_stats.py login
    → 管理员微信扫码并确认公众号后台授权
    → WeixinMpProfile 保存后台会话
GET /api/accounts/wechat-oa/mp-login/status
    → 读取 wechat-oa-mp.json 和二维码
```

后台会话用于身份查询、后台创作数据，以及当前 Web 发布入口的公众号草稿创建。这条路径不需要填写开发者 AppID/AppSecret。

代码同时保留 `GET/POST /api/accounts/wechat-oa/credentials`：

- AppID/AppSecret 写入 `skills/builtin/skill-wechat-publisher/wechat-publisher.yaml` 的 `accounts.web`。
- 保存后调用 token 接口验证；读取接口仅返回脱敏 AppID 和配置状态，不返回 AppSecret。
- 验证失败时凭据已经保存，接口会返回 `ok: false`，不能把“已保存”当作“已验证”。
- 账号状态存在旧凭据兜底：即使后台会话不可用，只要配置了凭据，某些状态路径也可能显示“已登录”。

**实际使用要以对应功能要求为准：** 当前 Web 公众号发布分支明确检查后台会话状态；仅有 AppID 凭据不足以满足它。另一方面，`weixin_mp_stats.py publish` 的实际动作是**建立公众号草稿**，并不等于完成公开发表或群发。操作后应到公众号后台核对草稿，再完成所需发布步骤。

### 10.6 登录态、进度标记与缓存分别保存在哪里

| 数据 | 位置 | 性质 |
|---|---|---|
| 浏览器授权材料 | `<运行账户 HOME>/.easel-browser-profiles/<平台目录>/` | 真正用于复用授权的浏览器数据 |
| B站 Cookie/Token | 项目根 `cookies.json` | 真正的授权材料 |
| 公众号兼容凭据 | 技能目录 `wechat-publisher.yaml:accounts.web` | AppID/AppSecret 本地文件配置 |
| 公众号兼容 token 缓存 | 技能 scripts 下 `.token_cache_web.json` 等 | 开发者接口调用缓存 |
| 登录进度 | `outputs/_login/<平台>.json` | `{state, message, qr, ts}`，不等于凭据 |
| 二维码 | `outputs/_login/<平台>.png` | 本轮扫码展示材料 |
| 日志 | `outputs/_login/<平台>.log` | runner 的执行信息 |
| 抖音短信回填 | `outputs/_login/douyin.code` | 一次性验证码，runner 读后删除 |
| 页面昵称/头像缓存 | localStorage：`easel_whoami_v4` | UI 展示缓存，不保存平台会话 Cookie |
| 后端身份缓存 | `_WHOAMI_CACHE`，进程内存 | 避免重复打开浏览器校验 |

公众号扫码使用文件前缀 `wechat-oa-mp`，例如 `wechat-oa-mp.json/png/log`，与平台 ID `wechat-oa` 有区别。

本机当前默认 HOME 为 `C:\Users\hyh`，因此默认浏览器会话根为 `C:\Users\hyh\.easel-browser-profiles`。部分脚本提供 profile-base 参数覆盖，但当前 Web 调用主要使用默认目录。更换服务运行账户会改变默认 HOME，可能表现为原账号“突然未登录”。

本项目没有在此流程中实现统一凭据保险库；YAML、Cookie JSON 与浏览器目录均应按授权材料管理。清空 localStorage 不会退出服务器上的平台登录，删除进度 JSON 也不等于撤销 Cookie。

### 10.7 “已登录”和“校验账号”有什么区别

页面先调用 `GET /api/accounts` 获取快速状态：大部分浏览器平台读取上次成功标记，B站检查 Cookie 文件，公众号还有凭据兜底。这让页面快速显示，但属于 last-known 状态。

随后页面对缓存缺失或过期的平台执行 `GET /api/accounts/<平台>/whoami`，脚本访问平台获取 `{loggedIn, name, avatar}`。收到可信结果后，页面以 whoami 为准；后端也回写成功标记，或清掉失效标记。

前后端默认缓存约 10 分钟；已登录但缺少昵称/头像时，缓存缩短到最多 60 秒。开页后台检查最多并发两个平台；后端合并同一平台同时到来的 whoami 请求。B站不参与这次自动后台检查，但可以手动校验。

“校验账号”按钮带 `force=true` 绕过通常的缓存读取。若发生浏览器启动失败、超时或无法确认身份，后端返回错误并保留原状态；网络故障不会直接被当作可信的“退出登录”。不过因此页面原来的“已登录”也可能暂时是旧状态。

### 10.8 普通用户操作流程

1. 打开 **账号登录 Accounts**，找到目标平台，点击“登录”。
2. 等二维码出现，用该平台要求的手机 App 扫码；视频号、公众号使用微信。公众号按管理员授权流程操作。
3. 在手机上完成确认；若抖音出现短信验证，在弹窗输入手机收到的 4–8 位验证码并提交。
4. 等待“登录成功”，再点击“完成”。仅看到“扫码成功”时应继续等待。
5. 点击“校验账号”，核对昵称与头像，确认授权的是要操作的账号。
6. 再进入内容制作、数据中心或发布流程。登录成功表示授权建立，具体操作仍受平台权限与内容要求约束。
7. 若要切换同一平台的账号，先退出，再登录目标账号；切换画像不会自动切换平台会话。

后端默认登录等待 240 秒，前端轮询最长约 360 秒；连续五次状态查询失败会停止轮询并提示连接中断。二维码还可能在平台自己的较短时限内失效，前端等待上限不是二维码有效期。

弹窗“关闭”主要停止前端轮询，没有调用统一的后台取消接口。因此关闭弹窗不等于立即终止 runner。普通平台下一次登录请求带 `restart=true`，后端会尝试结束旧登录进程；公众号入口则复用正在运行的进程。

### 10.9 退出登录实际做了什么

`POST /api/logout/<平台>` 主要删除本地授权材料：浏览器平台删除对应持久化目录，B站删除 `cookies.json`，同时清理进度、二维码和身份缓存。公众号还会删除 `accounts.web`、token 缓存、后台会话目录，并尝试终止正在运行的公众号登录进程。

它没有实现一个覆盖全部平台的远端“撤销所有设备授权”接口，因此不能推定其它设备上的平台登录也退出。重新登录仍需平台授权。

**实现边界：** 普通平台退出分支没有与公众号一样统一终止正在运行的登录子进程，也没有完整协调所有发布/校验任务；删除浏览器目录时使用 `ignore_errors=True`。不要将返回 `ok` 理解成并发情况下已经逐项确认所有文件清除。常规操作应等登录、校验或发布结束后再退出。

### 10.10 与画像、技能和多用户的关系

当前三个概念分别是：

```text
Persona：写什么、怎么写、面向谁
Accounts：后端机器当前获得哪个平台账号的授权
Session：这次对话的上下文、画像选择与执行引擎
```

平台会话目录和状态以 **platform** 为键，没有普遍引入 `user_id / tenant_id / account_id`。当前 Accounts 页面每个平台一张卡片、一个默认会话槽位，不提供同平台多个账号的完整管理和选择流程。

因此：画像 A/B 可以共享同一平台登录；切到画像 B，并不会自动改成 B 的社媒账号。多个浏览器用户进入同一实例，使用的是同一服务账户下的授权材料，也能影响同一平台的登录/退出状态。

如果扩展成多用户或同平台多账号，需要同时把浏览器目录、Cookie、进度文件、身份缓存、登录进程和任务锁的键扩展为租户/账号范围，并在发布任务中显式绑定 account_id 与操作者权限。只改 Accounts 页面或增加画像不足以实现隔离。

### 10.11 常见问题与定位入口

| 现象 | 先检查什么 |
|---|---|
| 一直“启动中”、二维码不出现 | `outputs/_login/<平台>.log` 中的浏览器启动、依赖和页面加载错误；平台页面是否能在运行机器访问 |
| 已扫码但没成功 | 手机上是否确认授权；是否正在等待短信或平台额外验证 |
| 登录成功但昵称/头像不对 | 手动“校验账号”，核对实际登录的账号；不要只看缓存 |
| 校验提示会话目录占用 | 是否有登录、发布或取数正在打开同一持久化目录；等待该任务结束 |
| 页面显示已登录，但发布要求重新登录 | 平台会话失效、状态标记陈旧；公众号尤其要区分后台会话与旧凭据兜底 |
| 换运行账户后全部未登录 | HOME 和浏览器目录是否变化；B站项目目录是否变化 |
| 公众号提示发布成功却看不到公开文章 | 检查公众号草稿箱；当前后台 publish 脚本是建草稿 |
| 改画像后仍使用原平台账号 | 当前设计如此；需显式退出并切换平台账号 |

网络处理并不统一：小红书、抖音 Web 登录调用显式直连参数，通用网页脚本也有自身代理设置；公众号扫码会读取配置的正向代理；B站 API 使用 Python HTTP 请求。某条模型 API 通路正常，不代表平台登录页面或二维码资源也能正常访问。
