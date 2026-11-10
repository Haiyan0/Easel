<p align="left">
  <img src="assets/readme/logos/zhejiang_university_horizontal_readme.png#gh-light-mode-only" width="106" align="middle" alt="Zhejiang University">
  <img src="assets/readme/logos/zhejiang_university_horizontal_dark.png#gh-dark-mode-only" width="106" align="middle" alt="Zhejiang University">
  &nbsp;&nbsp;&nbsp;
  <img src="assets/readme/logos/peking_university_horizontal_red.png#gh-light-mode-only" width="98" align="middle" alt="Peking University">
  <img src="assets/readme/logos/peking_university_horizontal_dark.png#gh-dark-mode-only" width="98" align="middle" alt="Peking University">
</p>

<p align="center">
  <img src="assets/readme/brand.png#gh-light-mode-only" width="640" alt="Easel">
  <img src="assets/readme/brand-dark.png#gh-dark-mode-only" width="640" alt="Easel">
  <br>
  <img src="assets/readme/logos/real_lab_horizontal_readme.png" width="74" alt="REAL Lab">&thinsp;&thinsp;<img src="assets/readme/logos/opendcai_lab_horizontal_readme.png#gh-light-mode-only" width="83" alt="OpenDCAI Lab"><img src="assets/readme/logos/opendcai_lab_horizontal_readme_dark.png#gh-dark-mode-only" width="83" alt="OpenDCAI Lab">
</p>

<p align="center">
  <strong>简体中文</strong> · <a href="docs/windows-desktop-operations.zh-CN.md">部署与运维</a>
</p>

<p align="center">
  你的私人、持续进化的社媒运营助手<br>
  从一个想法开始，完成发现、策划、创作、发布与复盘。
</p>

<p align="center">
  <a href="https://zju-real.github.io/Easel/"><img src="https://img.shields.io/badge/Easel-Project_Page-F05A3C?style=flat-square&logo=googlechrome&logoColor=white" alt="Easel Project Page"></a>
  <img src="https://img.shields.io/badge/Agent-Multi_Harness-111827?style=flat-square" alt="多执行引擎">
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+">
  <a href="docs/skill-function-mapping.md"><img src="https://img.shields.io/badge/Skills-118-0F9D8A?style=flat-square" alt="118 Skills"></a>
  <a href="https://github.com/ZJU-REAL/Easel/stargazers"><img src="https://img.shields.io/github/stars/ZJU-REAL/Easel?style=flat-square&color=F6C344" alt="GitHub Stars"></a>
  <a href="https://github.com/ZJU-REAL/Easel/releases/latest"><img src="https://img.shields.io/github/v/release/ZJU-REAL/Easel?style=flat-square&color=0F9D8A&label=release" alt="Latest release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache_2.0-2EA44F?style=flat-square" alt="License: Apache 2.0"></a>
  <a href="assets/readme/wechat-group.png"><img src="https://img.shields.io/badge/微信群-加入交流-07C160?style=flat-square&logo=wechat&logoColor=white" alt="WeChat Group"></a>
</p>

<p align="center">
  <a href="https://trendshift.io/repositories/195527?utm_source=trendshift-badge&utm_medium=badge&utm_campaign=badge-trendshift-195527" target="_blank" rel="noopener noreferrer"><img src="https://trendshift.io/api/badge/trendshift/repositories/195527/daily?language=Python" alt="ZJU-REAL%2FEasel | Trendshift" width="250" height="55"/></a> <a href="https://trendshift.io/repositories/195527?utm_source=trendshift-badge&utm_medium=badge&utm_campaign=badge-trendshift-195527" target="_blank" rel="noopener noreferrer"><img src="https://trendshift.io/api/badge/trendshift/repositories/195527/weekly?language=Python" alt="ZJU-REAL%2FEasel | Trendshift" width="250" height="55"/></a>
</p>

![Easel 产品宣传海报](assets/readme/poster.png)

## 先从这里开始

本仓库基于 [ZJU-REAL/Easel](https://github.com/ZJU-REAL/Easel) 迭代，加入多人登录、独立工作区、受管模型代理与 Docker Desktop 装配。当前多人功能分支是 **`feat/multiuser-task1-store`**，不要默认 `main` 或上游 release 包含这些改动。上方原项目演示与社区链接保留用于介绍来源。

| 你想做什么 | 从哪里开始 |
|---|---|
| Windows 上部署多人实例 | [Windows Docker Desktop 手册](docs/windows-desktop-operations.zh-CN.md)：第 0～6 节 |
| 已部署，想启停、查日志、备份或更新 | 同一手册第 7～10 节，更新在原源码目录完成 |
| Linux 单人 Docker / 多人服务器准备 | [Linux Docker 手册](docs/linux-docker-operations.zh-CN.md)，注意两种模式的区别 |
| 本机单人使用或更换执行引擎 | 下方快速开始与 [Agent 底座说明](docs/agent-harnesses.md) |
| 验收多人功能 | [用户验收单](docs/windows-multiuser-acceptance.zh-CN.md) |

**当前状态（2026-11-03）：** Windows Docker Desktop 的多人登录、真实 Harness 聊天、工作区隔离、故障恢复和运维流程已按组验证，验收修复已合并，用户确认本机聊天正常。原生 Linux 多人生命周期装配、目标宿主防火墙/IPv6、XFS 配额、容器 Chromium 沙箱等仍有未关闭门槛；不据此宣称生产环境可直接发布。技能和平台操作还取决于相应服务、凭证及授权，接口存在不代表所有外部平台都已实测。

## 🎨 Easel 是什么

Easel 是一个面向社交媒体创作者的开源内容工作台。它把 Agent 执行引擎、账号画像、内容技能和媒体工具接在一起，让 Agent 不只回答“应该怎么做”，而是直接把内容做出来并归档，且可实现直接/按需发布。

你可以把它理解成一个会记住你的内容搭档：它了解账号定位、受众、风格、平台限制和历史表现，从热点发现一直陪你做到发布，再把结果带回下一次创作。

Easel 宣传演示：

https://github.com/user-attachments/assets/4dd060dc-53dd-4bb2-99a3-e65ab6f65166

Easel 围绕五个连续工作流展开：**发现**适合账号的热点与机会，**策划**选题、标题、脚本和排期，
**创作**图文、音频与视频内容，**发布**经过检查和平台适配的成品直接到对应平台，再通过**归因**分析表现并把有效经验沉淀回账号画像。

#### 📌 使用提示与研究愿景

> - **推荐使用 Web 前端**：前端提供完整的会话、素材、账号、画像、内容库和发布管理能力，体验和功能比单独使用 CLI 更全面，也是我们推荐的使用方式；部分终端专有的显示问题见[已知问题](docs/known-issues.md)。
> - **谨慎自动发布到小红书**：小红书平台可能检测自动化操作，存在验证、限流或账号风控风险；建议使用预览与发布前检查，并由用户确认后手动发布。其他平台也应先在本人授权账号上验证。
> - **从研究走向真实生活**：Easel 是我们将研究成果应用到真实社媒创作场景的一次实践。后续我们会继续研究 AI 在社媒场景中的社交智能，让 Agent 更好地理解创作者、受众与真实互动。

## ✨ 为什么是 Easel

- **一个 Agent 贯穿完整链路**：发现热点、评估选题、规划日历、生成文案与视觉、制作视频、发布和归因在同一个工作流中完成。
- **画像驱动，而不是一次性生成**：每个账号有独立的定位、风格、受众、平台、偏好和记忆，输出会越来越贴合真实账号。
- **技能是真执行，不是功能清单**：图片、卡片、配音、字幕、剪辑、短剧和发布技能都配有可运行脚本，成品写入 `outputs/`。
- **一份素材，多种平台形态**：同一主题可以改写成小红书卡片、短视频、知乎长文或短帖，并遵循不同平台的格式和字数要求。
- **项目化保存产物**：内容、素材、中间文件和元数据按项目归档，后续修改、重试和发布不会散落在聊天记录里。
- **真实发布与复盘闭环**：目前支持小红书、抖音、快手、知乎、B 站、微信视频号、微信公众号、推特 X、掘金、今日头条十个平台的登录、适配和发布，并可回收账号数据。

## 🧭 五层内容工作流

1. **发现**：聚合热榜、行业新闻、竞品动态和用户讨论，筛选真正适合账号的机会。
2. **策划**：把机会变成选题、标题、脚本和内容矩阵，写入内容日历。
3. **创作**：生成文案、卡片、海报、信息图、音频、视频、短剧和论文解读等可发布素材。
4. **发布**：按平台适配标题、正文、画幅和媒体要求，执行发布前检查并发送到已登录账号。
5. **归因**：读取播放、互动、评论和内容表现，把有效结构和偏好沉淀回账号画像。

## 🧰 已经落地的能力

| 能力层 | 已实现功能 |
|---|---|
| **发现** | 全网热搜聚合、垂类趋势研究、内容缺口分析、节日与事件日历、平台算法动态、竞品分析、行业资讯、跨平台差异、RSS 聚合、UGC 发现 |
| **策划** | 账号定位分析、受众画像、人设与声音构建、账号诊断、选题矩阵、选题评分、热点结合、系列内容规划、标题与 Hook、文章大纲、分镜脚本、内容日历、跨平台复用、直播策划、营销活动与商单方案 |
| **文字与视觉** | 社媒文案、短视频与中长视频脚本、小红书笔记、长文、小说、去 AI 感改写、风格迁移、论文解读、金句卡、小红书知识卡、海报、信息图、数据图表、思维导图、对比图、电商详情图、Meme、AI 生图、图片增强、去背景与批处理 |
| **音频与视频** | 文字转语音、多角色配音、声音克隆、AI 音乐、降噪、混音、语音转文字、音频可视化、AI 视频、AI 短剧、字幕与翻译、视频剪辑、长视频切片、直播高光、横竖版转换、片头片尾、相册视频、音乐卡点、绿幕换背景、视频转图文与章节目录 |
| **发布与归因** | 发布质量门禁、敏感与版权风险检查、平台搜索优化、发布 Checklist、多平台格式适配；小红书、抖音、快手、知乎、B 站、微信视频号、微信公众号、推特 X、掘金、今日头条登录与发布；内容日历回写、账号数据、评论洞察（掘金走公开 JSON 接口、X 走网页 DOM 采集）、内容复盘、ROI 与画像记忆 |

## 🖥️ 工作台功能示例

下面只展示 Easel 工作台中的四个代表性功能，并不是完整功能清单。更多发现、策划、创作、发布和归因能力，
可以在工作台的技能库或[能力地图](docs/skill-function-mapping.md)中查看。

<table>
  <tr>
    <td width="50%" valign="top">
      <strong>🧬 账号画像</strong><br>
      <sub>通过基础信息、社媒链接、运营意图和偏好红线建立账号上下文；一份画像可以跨多个平台和会话持续使用。</sub><br><br>
      <img src="assets/readme/features/profile.png" width="100%" alt="Easel 账号画像配置">
    </td>
    <td width="50%" valign="top">
      <strong>🔥 热点雷达</strong><br>
      <sub>聚合现有平台热榜，并通过标准协议接入你自己的 Python 采集脚本、HTTP 服务或 RSS/Atom 订阅，从更多来源寻找适合账号的选题。</sub><br><br>
      <img src="assets/readme/features/discover.png" width="100%" alt="Easel 多平台热点雷达">
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <strong>📅 内容日历</strong><br>
      <sub>统一管理选题、草稿、待发、已发和平台活动，结合节日节点规划内容，并记录每一天的发布安排。</sub><br><br>
      <img src="assets/readme/features/calendar.png" width="100%" alt="Easel 内容日历">
    </td>
    <td width="50%" valign="top">
      <strong>📣 发布中心</strong><br>
      <sub>从一份母版内容生成多平台版本，集中完成格式适配、媒体附件、发布前检查、预览和真实发布。</sub><br><br>
      <img src="assets/readme/features/publish.png" width="100%" alt="Easel 多平台发布中心">
    </td>
  </tr>
</table>

## 🖼️ 真实产物

这些文件来自 Easel 的实际工作流，主页中也有同一批案例的分类展示。
README 的品牌图、海报、案例图片和视频统一保存在 `assets/readme/`；产品主页使用独立的
`web/static/showcase/` 素材，其中视频保持为 6 秒轻量预览。

### 📚 论文解读与知识卡片

<p align="center">
  <img src="assets/readme/showcase/spatialevo-cards-strip.jpg" width="49%" alt="SpatialEvo 论文解读卡片">
  <img src="assets/readme/showcase/spatialladder-cards-strip.jpg" width="49%" alt="SpatialLadder 论文解读卡片">
</p>
<p align="center"><sub>SpatialEvo · 论文解读　/　SpatialLadder · VLM 视觉叙事</sub></p>

<p align="center">
  <img src="assets/readme/showcase/culture-mt-cards-strip.jpg" width="49%" alt="CULTURE-MT 研究卡片">
  <img src="assets/readme/showcase/knowu-cards-strip.jpg" width="49%" alt="KnowU-Bench 论文解读卡片">
</p>
<p align="center"><sub>CULTURE-MT · 文化翻译　/　KnowU-Bench · 侦探漫画</sub></p>

<p align="center">
  <img src="assets/readme/showcase/agentg2-cards-strip.jpg" width="49%" alt="AGENT G2 论文解读卡片">
  <img src="assets/readme/showcase/pause-cards-strip.jpg" width="49%" alt="Pause or Fabricate 论文解读卡片">
</p>
<p align="center"><sub>AGENT G2 · 暗夜英雄　/　Pause or Fabricate · 角色化科普</sub></p>

### 📖 小说与故事

<p align="center">
  <img src="assets/readme/showcase/novel-xiuxian.png" width="24%" alt="外门弟子整顿修仙界">
  <img src="assets/readme/showcase/novel-horror.png" width="24%" alt="中元节那晚">
  <img src="assets/readme/showcase/novel-romance.png" width="24%" alt="我和相亲对象互演正常人">
  <img src="assets/readme/showcase/novel-seventh.png" width="24%" alt="第七格">
</p>
<p align="center"><sub>搞笑修仙　/　恐怖短篇　/　爱情喜剧　/　悬疑故事</sub></p>

### 🌿 生活分享与梗内容

<p align="center">
  <img src="assets/readme/showcase/life-color-cards-strip.jpg" width="49%" alt="丰富多彩的生活">
  <img src="assets/readme/showcase/life-mountain-cards-strip.jpg" width="49%" alt="大山大水">
</p>
<p align="center"><sub>生活方式　/　自然记录</sub></p>

<p align="center">
  <img src="assets/readme/showcase/life-chengdu-cards-strip.jpg" width="49%" alt="成都乡土生活">
  <img src="assets/readme/showcase/jokes-cards-strip-wide.jpg" width="49%" alt="开学趣事梗卡">
</p>
<p align="center"><sub>城市生活　/　梗内容</sub></p>

### 🎬 视频成片

为便于快速浏览，这里使用可点击的视频封面。点击封面查看最长 1 分钟的轻量预览，点击封面下方的标题
查看完整成片。

**论文解读视频**

<p align="center">
  <a href="assets/readme/videos/preview/spatialladder-explainer.mp4"><img src="assets/readme/videos/thumbnails/spatialladder-explainer.jpg" width="49%" alt="播放 SpatialLadder 论文解读预览"></a>
  <a href="assets/readme/videos/preview/culture-mt-explainer.mp4"><img src="assets/readme/videos/thumbnails/culture-mt-explainer.jpg" width="49%" alt="播放 CULTURE-MT 论文解读预览"></a>
</p>
<p align="center"><sub>▶ 点击封面播放预览 · 查看完整视频：<a href="assets/readme/videos/full/spatialladder-explainer.mp4">SpatialLadder</a>　/　<a href="assets/readme/videos/full/culture-mt-explainer.mp4">CULTURE-MT</a></sub></p>

**口播、连续剧与 AI 角色短视频**

<p align="center">
  <a href="assets/readme/videos/preview/ordinary-person.mp4"><img src="assets/readme/videos/thumbnails/ordinary-person.jpg" width="24%" alt="播放普通人口播剧预览"></a>
  <a href="assets/readme/videos/preview/hanako-change.mp4"><img src="assets/readme/videos/thumbnails/hanako-change.jpg" width="24%" alt="播放花子的转变预览"></a>
  <a href="assets/readme/videos/preview/cyber-cultivation.mp4"><img src="assets/readme/videos/thumbnails/cyber-cultivation.jpg" width="24%" alt="播放赛博修仙预览"></a>
  <a href="assets/readme/videos/preview/cyber-turtle.mp4"><img src="assets/readme/videos/thumbnails/cyber-turtle.jpg" width="24%" alt="播放赛博吃播甲鱼预览"></a>
</p>
<p align="center"><sub>▶ 点击封面播放预览 · 查看完整视频：<a href="assets/readme/videos/full/ordinary-person.mp4">普通人</a>　/　<a href="assets/readme/videos/full/hanako-change.mp4">花子的转变</a>　/　<a href="assets/readme/videos/full/cyber-cultivation.mp4">赛博修仙</a>　/　<a href="assets/readme/videos/full/cyber-turtle.mp4">赛博吃播甲鱼</a></sub></p>

**氛围影像与横版内容**

<p align="center">
  <a href="assets/readme/videos/preview/jilong-news.mp4"><img src="assets/readme/videos/thumbnails/jilong-news.jpg" width="32%" alt="播放西藏吉隆泥石流预览"></a>
  <a href="assets/readme/videos/preview/daomu-book.mp4"><img src="assets/readme/videos/thumbnails/daomu-book.jpg" width="32%" alt="播放盗墓笔记小说介绍预览"></a>
  <a href="assets/readme/videos/preview/zju-intro.mp4"><img src="assets/readme/videos/thumbnails/zju-intro.jpg" width="32%" alt="播放浙江大学介绍预览"></a>
</p>
<p align="center"><sub>▶ 点击封面播放预览 · 查看完整视频：<a href="assets/readme/videos/full/jilong-news.mp4">西藏吉隆泥石流</a>　/　<a href="assets/readme/videos/full/daomu-book.mp4">盗墓笔记小说介绍</a>　/　<a href="assets/readme/videos/full/zju-intro.mp4">浙江大学</a></sub></p>

## 🚀 快速开始

### 1. 获取当前代码：只保留一个仓库目录

新电脑的 PowerShell：

```powershell
git clone --branch feat/multiuser-task1-store --single-branch https://github.com/Haiyan0/Easel.git C:\Code\Easel
Set-Location C:\Code\Easel
```

已有 `C:\Code\Easel` 就直接进入，不再 clone，也不创建 `Easel-acceptance` / `Easel-next`。Linux 使用手册中的 `/srv/easel/src`。用 `git rev-parse --short HEAD` 记录实际版本。

### 2. 多人使用：Docker Desktop 路线

准备 Windows 11、Docker Desktop（Linux containers）、WSL2、Git 和 ngrok。按 [Windows 手册](docs/windows-desktop-operations.zh-CN.md) 顺序完成：

1. 核对 Windows/WSL 使用同一个 Docker Engine。
2. 在当前仓库构建 control、worker、gateway 三个镜像，记录固定镜像 ID。
3. 初始化独立数据目录和专用卷，创建 master key / 签名 key。
4. 启动服务，使用 ngrok HTTPS 地址登录。
5. 创建首个管理员，由管理员配置模型并邀请成员。
6. 验证一条新聊天、历史、停止操作与两用户隔离。

Docker 镜像包含 Python、前端和受管 Harness；这条路线**不运行 `setup.ps1`，不需要项目内 `.venv`**。配置、数据和 Key 放在仓库外，不能提交到 GitHub。完整命令与参数以手册为准，不直接运行参考 Compose 代替初始化和身份校验。

### 3. 单人使用：本机原生路线

这是原有 local 模式，默认在 `http://localhost:7860` 使用，没有多人登录隔离。不要把无认证的 local 服务直接暴露公网。

Windows PowerShell：

```powershell
Set-Location C:\Code\Easel
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
.\.venv\Scripts\easel.exe doctor
.\.venv\Scripts\easel.exe web
```

Linux / macOS（已获取本仓库代码）：

```bash
cd /path/to/Easel
bash setup.sh
source .venv/bin/activate
easel doctor
easel web
```

原生安装器会准备项目虚拟环境、依赖与前端，默认装配独立 OpenClaw profile；媒体和浏览器技能另依赖 FFmpeg、Playwright/Chromium。缺失或不可用的能力按安装诊断排查。Codex、OpenCode、通用 ACP 与仅聊天模式的选择见 [Agent 底座说明](docs/agent-harnesses.md)。它们与 Docker 多人部署中管理员固定的受管 Harness 配置不同。

### 4. 以后怎样更新

**先正常停止和备份校验，再拉取代码**。运行中的 provisioner 使用源码目录，不能一边运行一边热改文件。确认没有未保存修改后，在同一仓库执行：

```powershell
Set-Location C:\Code\Easel
git status --short
git pull --ff-only origin feat/multiuser-task1-store
```

这两条 Git 命令只更新代码，**不会构建镜像、迁移数据或重启服务**。接着按 [Windows 手册第 10 节](docs/windows-desktop-operations.zh-CN.md#10-以后怎样更新停机--备份--拉取--构建--启动) 构建新版并用匹配备份启动。更新失败不要删卷、重新初始化 Key 或强行清屏障；回退代码也不等于回滚数据库。

## ⚙️ 配置说明

### 多人 control 模式

- 管理员在 Web 设置中发布全体默认模型，按能力允许成员使用个人覆盖；普通成员不能修改管理员配置。
- Key 加密保存在 control 中；worker 通过本 workspace / 本 Run 的短期 broker token 调用模型，不接收供应商真实 Key。
- 执行引擎、命令和模型目录由受管配置与固定 worker 镜像共同约束，不自动读取开发电脑的 `.env`、HOME 或 Harness 登录态。
- `manual` / `automated` 发布策略是显式部署参数；先采用手册中的人工审核路线，平台登录与发布需本人授权。
- `.env` 和已有 local 历史不会自动导入多人实例。显式迁移见 [多人部署与迁移说明](docs/multiuser-deployment.zh-CN.md)。

### 单人 local 模式

以下 `.env`、OpenClaw profile 和向量索引配置仅适用于本机 local 路线，不能用它们替代多人管理员配置。


最小配置只需要在项目根目录 `.env` 中提供一个可用的 LLM：

```bash
ANTHROPIC_API_KEY=你的_API_Key
CLAUDE_MODEL=anthropic/claude-sonnet-4-6
```

也可以使用 OpenAI 或 OpenAI-compatible 服务：

```bash
OPENAI_API_KEY=你的_API_Key
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o
```

如果聊天 API 不提供向量模型，请单独配置 Embedding API；否则 OpenClaw 会默认请求 `text-embedding-3-small`，可能得到“模型不可用”。不配置独立向量 API 时，Easel 会显式使用关键词记忆检索，不会反复请求聊天端点的 embedding 模型：

```dotenv
EASEL_EMBEDDING_API_KEY=你的向量_API_Key
EASEL_EMBEDDING_BASE_URL=https://your-embedding-provider.example/v1
EASEL_EMBEDDING_MODEL=你的向量模型名
```

修改向量 provider 或模型后，使用对应 profile 重建一次索引：

```bash
openclaw --profile easel memory index --force
```

或者使用其他 Anthropic-compatible 服务：

```bash
EASEL_LLM_API_KEY=你的_API_Key
EASEL_LLM_BASE_URL=https://你的服务地址/v1
CLAUDE_MODEL=你的模型名
```

安装器会把这些标准配置同步到 OpenClaw。OpenClaw 支持但 Easel 没有预设环境变量映射的其他 provider，
可以按 OpenClaw 自身的 provider/auth 配置方式配置；Easel 不会覆盖这些自定义配置。

在 Web 设置的聊天通道中，自定义供应商可勾选“该模型会思考”，保存后会写入模型的
`reasoning: true`。OpenAI 兼容协议还可选择思考格式（OpenAI、DeepSeek 等），需与上游
接口实际支持的格式一致；原生 Anthropic 协议使用其自身的思考格式，无需另选。
新增行默认关闭，避免对非推理模型发送不支持的参数；已有声明会回显。取消勾选会写入
`reasoning: false`，仅移除该模型的思考相关兼容项，不影响工具等其它能力声明。
保存 API 不传 `thinking` 时保持原有声明；启用时保留已有努力档位映射，不推测上游支持的档位。
思考格式会写入 OpenClaw 的 `compat.thinkingFormat`，实际发送哪些参数由其版本和模型适配器决定；
例如 OpenClaw 2026.9.8 对普通自定义模型仍可能只发送 `reasoning_effort`，不是任意方言转换器。
这只是能力声明，不保证网关支持推理，亦不覆盖 `EASEL_THINKING_LEVEL=off`；
API 直连模式不提供此 OpenClaw 专用开关。
对显式声明 `reasoning: false` 的当前模型，Web 调用 OpenClaw CLI 时会使用 `--thinking off`，
避免禁用后仍请求不支持的 `medium` 档位。

`.env.example` 还列出了视频、音乐、语音等可选模型配置。只需要配置实际使用的能力，也可以在 Web
工作台的“技能库”中填写；没有配置的媒体 Skill 不会影响聊天、策划和文本创作。常见可选项包括：

| 能力 | 配置入口 | 额外依赖 |
|---|---|---|
| AI 视频 | `VIDEO_PROVIDER` 及对应服务的 Key、URL、模型 | 相应视频生成服务 |
| AI 音乐 | `MUSIC_PROVIDER` 及对应服务配置 | 相应音乐生成服务 |
| 云端配音 | `VOICE_PROVIDER` 及对应服务配置 | 相应语音生成服务 |
| 图片与音视频处理 | 无额外模型时也可使用本地工具 | FFmpeg |
| 浏览器发布 | 在 Web“账号”页面登录目标平台 | Playwright Chromium、有效平台账号 |

## 🧩 使用 Easel

发布中心提供“自动发布 / 人工审核发布”两条可选路径，默认保留原有自动发布流程。人工路径采用“AI 排版 → 人工审核 → 复制/导出 → 官方发布页亲自提交”，修改稿件或素材后需重新审核，导出和排期不等于已发布；公众号支持富文本复制和 HTML 预览。页面切换不更改全局发布权限；如需在 Web 后端禁用自动提交，可配置 `EASEL_PUBLISH_MODE=manual`。配置边界、登录状态和导出说明见[人工发布工作流](docs/manual-publishing.md)。

常用入口：

| 命令 | 作用 |
|---|---|
| `easel web [--port 7860]` | 启动 Web 工作台 |
| `easel chat` | 在终端开启多轮对话并选择账号画像 |
| `easel skill <name> -i "..." [-p <画像>]` | 直接运行指定 Skill；输入也可以是文件路径 |
| `easel doctor` | 检查 Python、Node.js、OpenClaw 和关键配置 |
| `easel ping` | 检查 gateway 与 Agent 连通性 |
| `easel gateway start\|stop\|restart\|status\|logs` | 管理 OpenClaw gateway |

所有技能都通过 Easel 的 Agent 执行。Agent 会读取对应 `SKILL.md`，调用脚本和工具，并把产物保存到 `outputs/`。

```bash
easel skill quality-gate -i "帮我检查这条小红书文案"
easel skill social-content -i "写一条介绍空间智能的微博"
easel skill quality-gate -i "这是一段待发布文案" -p 科技数码达人
```

Web 工作台提供同样的能力，并额外管理会话、素材、账号、画像、内容库和发布状态。拖入对话框的素材会作为结构化附件传给 Agent，用户消息只显示实际输入的文字。

## 🧬 账号画像

local 模式下，一个画像对应 `profiles/<名字>/` 目录，包含六个维度：定位、风格、受众、平台、偏好与红线、长期记忆。

```bash
cp -r profiles/_template "profiles/我的账号"
```

在 Web 的“画像”页面创建和编辑；同一画像可以跨多个已登录平台使用。多人模式的画像、内容与浏览器状态落在本人 workspace，不使用开发者仓库的个人目录。

## 🏗️ 项目结构

```text
Easel/
├── easel/control/           登录、权限、配置、会话、任务、broker 与 relay
├── easel/worker/            隔离执行、持久历史、任务与浏览器状态
├── easel/gateway/           control 到 worker 的验签与精确路由
├── easel/harnesses/         OpenClaw / Codex / OpenCode / ACP 等适配
├── web/                    后端与 React/TypeScript 工作台
├── deploy/desktop/         Docker Desktop 初始化、启停、备份、恢复与更新
├── deploy/multiuser/       worker 镜像、provisioner、sidecar 与出口策略
├── skills/                 内置技能、共享脚本与参考资料
├── tests/                  Python、前端与隔离集成验证
├── docs/                   运维手册、设计、验收与开发参考
├── profiles/               local 画像模板及本机画像
└── outputs/                local 产物；多人产物存于外部 workspace
```

多人请求链路：浏览器 → HTTPS → control → gateway → worker；模型调用由 worker → sidecar → control broker → 供应商完成。Docker Desktop 上只有 control 发布回环端口，worker/gateway 不对宿主发布端口。这里的隔离 gateway 与 local OpenClaw gateway 是不同组件。

源码与持久数据分开。数据库、master key、签名私钥、模型 Key、Cookie、邀请和浏览器 profile 不入 Git；备份包含敏感材料，需私密保存。不要用 `docker system prune --volumes` 作为重装方式。

## 📑 文档

- [热点数据源接入：Python 脚本 / HTTP 服务 / RSS 订阅](docs/trend-collectors.md)
- [渠道平台接入指南](docs/channel-platforms.md)
- [能力地图](docs/skill-function-mapping.md)
- [SKILL 接口规范](docs/SKILL-SPEC.md)
- [提示词与架构分层](docs/prompt-stack.md)
- [新手手册：Windows 本机与 Docker Desktop](docs/windows-desktop-operations.zh-CN.md)
- [新手手册：Linux Docker 部署与运维](docs/linux-docker-operations.zh-CN.md)
- [多人部署、备份恢复与显式迁移](docs/multiuser-deployment.zh-CN.md)
- [已知问题](docs/known-issues.md)
- [完整致谢](docs/ACKNOWLEDGMENTS.md)

## 🗺️ 当前进展与后续工作

- [x] 复用原工作台，接入邀请登录、身份切换与 workspace 隔离。
- [x] 接通受管 Harness、会话历史、可恢复事件、权限回答及任务停止。
- [x] Docker Desktop 的 control / gateway / worker / sidecar 装配与恢复。
- [x] 完整备份校验、恢复、受控更新和中文运维文档。
- [ ] 原生 Linux 多人启停与升级入口的等价验收。
- [ ] 目标 Linux 的配额、防火墙、IPv6、Chromium 沙箱等生产门槛。
- [ ] 按明确授权继续验证真实供应商与各平台登录/发布，并完善兼容性。

开发前先看 [Agent 适配](docs/agent-harnesses.md)、[提示词分层](docs/prompt-stack.md) 和 [技能接口](docs/SKILL-SPEC.md)。历史故障材料位于 docs 的 evidence / 审计目录，不作为日常部署命令使用。

## 🙏 致谢

Easel 的技能体系和工作流受益于许多优秀的开源项目、工具与内容方法论。感谢所有原作者和社区贡献者；
具体项目、用途和许可信息请查看[完整致谢](docs/ACKNOWLEDGMENTS.md)。

欢迎参与贡献与讨论。有想法、问题或改进建议，欢迎提交 Issue 或 PR；也欢迎扫码加入我们的微信交流群：

<p align="center">
  <img src="assets/readme/wechat-group.png" width="240" alt="Easel 微信交流群二维码">
</p>
<p align="center"><sub>Easel 交流群 · 二维码定期更新，若已失效请通过 Issue 联系我们</sub></p>

### 🤝 Contributors

<table>
  <tr>
    <td align="center" width="84"><a href="https://github.com/lidingm"><img src="https://github.com/lidingm.png?size=96" width="72" height="72" alt="lidingm"><br><strong>lidingm</strong></a></td>
    <td align="center" width="84"><a href="https://github.com/qywMichelle"><img src="https://github.com/qywMichelle.png?size=96" width="72" height="72" alt="qywMichelle"><br><strong>qywMichelle</strong></a></td>
    <td align="center" width="84"><a href="https://github.com/wulinjuan"><img src="https://github.com/wulinjuan.png?size=96" width="72" height="72" alt="wulinjuan"><br><strong>wulinjuan</strong></a></td>
    <td align="center" width="84"><a href="https://github.com/arctanxarc"><img src="https://github.com/arctanxarc.png?size=96" width="72" height="72" alt="arctanxarc"><br><strong>arctanxarc</strong></a></td>
    <td align="center" width="84"><a href="https://github.com/hongxingli319"><img src="https://github.com/hongxingli319.png?size=96" width="72" height="72" alt="hongxingli319"><br><strong>hongxingli319</strong></a></td>
    <td align="center" width="84"><a href="https://github.com/mengyuyuan"><img src="https://github.com/mengyuyuan.png?size=96" width="72" height="72" alt="mengyuyuan"><br><strong>mengyuyuan</strong></a></td>
    <td align="center" width="84"><a href="https://github.com/Dear47"><img src="https://github.com/Dear47.png?size=96" width="72" height="72" alt="Dear47"><br><strong>Dear47</strong></a></td>
  </tr>
</table>

## ⚖️ 许可证

[Apache 2.0](LICENSE)

## 🌟 Star 趋势

<p align="center">
  <a href="https://star-history.com/#ZJU-REAL/Easel&Date">
    <img src="https://api.star-history.com/svg?repos=ZJU-REAL/Easel&type=Date" width="720" alt="Easel Star History Chart">
  </a>
</p>
