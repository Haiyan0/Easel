# Task 5C1 R1 证据：四项返工

日期：2026-10-30（Asia/Shanghai）。
父基线：`c9df47738c344d382d45e91aec91f6019f0fbaa0`。
实施 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。
本轮只修复主审四项 P2；不进入 5C2/5C3/6，不自动上线。

## 返工结果

| 项目 | 处理 |
|---|---|
| T5C1-R1-01 模型快照 | `easel/control/proxy.py` 对已启用的 `skill`/`profile` 与 `chat` 一样，用同次 broker 签发路由覆盖签名 Agent 的 `model`/`allowed_models`；未扩展账号/发布等后续类型。 |
| T5C1-R1-02 权限题卡 | `WorkspaceTasks` 不再把 `awaiting_permission` 折叠成 running；状态返回持久 question 和待确认标记。SkillDrawer/OnboardingWizard 复用原 QuestionCards，显示允许/拒绝并提交到既有 `/api/chat/question/answer`；画像向导增加明确“停止”入口。未知/503 保留题卡但禁用提交，401 走原身份卸载。 |
| T5C1-R1-03 画像指令 | control 用校验后的画像名和固定 profile-builder 指令组合基线摘要，包含本人 `profiles/<name>/`、完善六维、抓取失败降级/勿臆造和完成度摘要；浏览器/容器受控执行器按该指令更新本人画像文件。 |
| T5C1-R1-04 准备期互斥 | 基线准备到 Run 接纳期间持有非阻塞准备声明；同画像编辑/删除/并发构建立即 `profile_busy` 409。接纳失败释放声明并保留基线；终态后可正常保存/删除；同 turn 重试不重复派发。 |

## 定向红绿

先新增 `tests/multiuser/test_task5c1_r1.py` 并观察失败：模型快照为 `deepseek-chat`、状态折叠为 running、prompt 仅摘要、准备期删除/同名构建未被拒绝；随后逐项实现转绿。保留的定向测试：

- 模型：正式 `build_relay_factory` + 真实 `ConfigService`/`BrokerService`，skill/profile 快照与 token 模型一致；配置发布后的同 turn 请求仍返回原 Run，不重发。
- 题卡：真实 control 适配与 `QuestionRelay` 的 awaiting/question 状态；question 状态 503 时保留题卡并标记待确认。
- 指令：真实 worker baseline → `WorkspaceTasks` → relay message；断言固定执行/目标/降级/摘要语义。
- 互斥：真实 worker baseline gated 窗口内并发删除/同名构建返回 409；准备失败释放声明；终态后可保存/删除；重复 turn 不产生第二个 Run。

红测原始记录：`logs/red-before-r1.txt`（父基线 `c9df477` 临时副本，8 项全红）；
实现后同一测试 `8 passed`（`logs/pytest-multiuser-final.txt` 中包含）。

## 验证

| 项目 | 结果 |
|---|---|
| `tests/multiuser` 全量 | **1621 passed, 6 skipped** |
| `tests/multiuser/test_task5c1_r1.py` | **8 passed** |
| 顶层相关安全/趋势/路径 | **124 passed** |
| Node 全量 | **146 passed, 0 failed** |
| build | 成功，`dist/assets/index-B-SlLwSa.js` |
| lint | 退出 0 |
| Task 5C1 浏览器（R1 夹具） | **6/6，3 次 + 最终 1 次，`FAILURES=[]`** |
| Task 5B 浏览器 | **5/5，3 次，`FAILURES=[]`** |
| Task 5A 浏览器 | **4/4，3 次，`FAILURES=[]`** |
| Task 3 浏览器 | **33/33，3 次，`FAILURES=[]`** |
| Task 4 浏览器 | **8/8，3 次，`FAILURES=[]`** |
| 隔离容器链路 | **`TASK5C1_CONTAINER_OK=True`，`CLEANUP_ERRORS=[]`** |

## 主审探针复跑

只读探针位于 root `docs/evidence/original-web-task-05c1-review/`，本轮未修改：

- `logs/probe-profile-and-model.txt`：skill/profile 均 `MODEL_MATCH=True`；六文件、表单摘要、profile-builder 指令、固定目录均 True。
- `logs/probe-questions.json`：技能/画像均 `question_cards=1`、`allow_buttons=1`、`stop_buttons=1`（画像），status 为 `awaiting_permission` 且含 question。
- `logs/probe-preparation-race.txt`：准备期删除 `409`，构建仍 `200`，relay 提交 1 次。

## 浏览器夹具层

`tests/frontend/browser_original_web_task05c1.py` 使用真实 dist、真实 control/gateway/worker HTTP 路径、真实 RunManager；执行器是受控 ACP 假替身：

- 技能正常执行/刷新找回；
- 技能 ask 题卡显示、允许后继续、结果可见；
- 画像基线六维；受控执行器按完整任务指令更新本人 `memory.md`，原 ProfilePage 读回；
- 画像题卡显示、回答后出现第二题、停止后回到可用画像；
- 技能停止、深色/移动布局。

截图：`browser/01-skill-run-light.png` 至 `browser/11-profile-question-stopped.png`。

## 隔离容器

`tests/multiuser/task5c1_container_chain.py` 使用真实 control/gateway/worker 容器、真实 Ed25519 gateway、真实 Store/Run 账本；worker 执行器是夹具内假 ACP，不声称真实 Harness/provider 通过。本轮增量构建：

- control `easel-control:task5c1-r1-20261030`
  `sha256:31bfe43210bb00846fa6b3a80aa2960503bc426aff639e3353a58f49e4aa3ce3`
- worker `easel-worker-execution:task5c1-r1-20261030`
  `sha256:db8fac2a815f050487b44f6ccaa1ad6c951000f0357bb2d6c4b3fdf7a5877431`
- gateway 未改源码，复用
  `easel-gateway:task5c1-20261030`
  `sha256:30126e515dd2b256276030ba495e6652aded7e8adf4fe802be2b0edff93bdb07`

容器链路覆盖技能目录/prepare/Run/结果、画像基线/固定指令增强/本人文件标记/状态、worker 重启找回、5B 回归、匿名 401；最终临时资源无残留。

## 未执行与边界

- 真实 Harness/真实 provider、真实模型、账号登录/分析、发布/导出：**not-run**。
- 公网热点、完整 Compose、生产 Linux、最终部署未执行。
- 用户 control/gateway/worker/sidecar、原卷/socket、Key/Cookie/邀请未操作；未重放 prompt、未付费、未发布、未全局 prune。
- 5A/Task3/Task4/Task5B 历史截图已恢复；5C1 历史 `browser/01-06` 已恢复，新截图只在 `R1/browser`。
- 顶层 `pytest tests` 在全量运行时仍有本机既有的 local streaming/setup 环境失败（如 `test_chat_question_replay.py`、`test_direct_api.py`、`test_setup_*`），不是本组改动范围；本组以 `tests/multiuser`、相关顶层安全和浏览器矩阵为门禁，未将它们伪报为通过。
