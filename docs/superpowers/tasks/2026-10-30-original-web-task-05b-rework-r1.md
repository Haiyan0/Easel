# 原 Web Task 5B R1：首次存储边界与热点旧缓存语义

父基线：`38955b4c8707e07e51d6153abe6794dbb9a67ed1`。
继续原worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。
依据 [首轮审计](../audits/2026-10-30-original-web-task-05b-review-round-1.md)，仅两项P2；保留 [5B原任务](2026-10-30-original-web-task-05b-workbench-data.md)已通过范围。

## T5B-R1-01：缺失文件也验证实际存储根

- 先红测临时真实outputs目录链接/junction指向根外空目录，_ideas.json和_schedule.json均不存在时，GET/POST不得把根外目录当合法新工作台，也不得创建根外文件/临时文件。
- _workbench_file不能在FileNotFoundError直接返回未经父目录验证的路径。统一读取、首次创建、原子临时文件及摘要的数据根边界，不仅验证已存在的最终文件。
- 正常本人outputs目录不存在/数据文件缺失的合法首次使用仍可创建；已有普通数据CRUD、并发、重启持久化、损坏JSON拒绝保持。
- Windows目录junction可真实验证，不必为此请求真实用户目录或软链接权限。保留测试材料根外sentinel不变断言，清理只限本轮临时目标。

## T5B-R1-02：旧热点不能冒充刚刷新

- 先红测过期缓存+上游失败：实际返回和原组件不能同时声称旧条目为当前刷新成功数据。
- 最小选择：明确返回刷新失败；或继续展示旧数据，但带实际缓存时间及可见的旧数据/刷新失败说明。正常新数据、有效期内缓存、无缓存失败与多平台部分失败都须语义一致。
- 如新增字段，同步control/local适配的必要类型、WorkspaceData解析及原TrendsPage/概览，确保显示生效；不要求重做缓存系统或新增后台服务。
- 用受控来源生成实际响应，驱动原页面或主审原组件探针验证；不要用手写新响应代替后端行为。更新时间不得把本次请求时间冒充实际取数成功时间。

## 验证与交付

主审探针保存在root docs/evidence下original-web-task-05b-review，保持只读。前三个Python/Node探针的输入及复跑方式见脚本；前端探针默认首轮旧形状，R1应传实际响应。

先两项红绿，再跑相关5B后端/gateway/存储/热点回归、Node/build/lint、新5B浏览器与既有5A/Task3/Task4矩阵。若因主审启动参数导致Windows子进程编码不一致，应在测试启动环境使父子编码一致并如实记录，不据此改无关产品代码。历史截图不覆盖。

需要隔离容器补证时只增量构建受影响部分，假材料/随机标签，清理核归属。无需无缓存重建Harness/Chromium或全栈；不触用户实例/卷/socket/Key/Cookie/邀请，不公网复跑、不付费或真实发布、不全局prune。

显式文件提交，列父/新SHA、两项红绿、相关回归、探针、not-run及清理。生产改动只限必要边界/缓存语义和前端展示；主审audit/task/plan/probe只读。完成停止等复审，不进入5C/6、不自动上线。
