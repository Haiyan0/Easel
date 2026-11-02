# Task 12 验收通过

日期：2026-10-26（Asia/Shanghai）。最终提交：7d49c814532fefcb31c1e340b5735487766fe9fc；R2 父基线：79fbe371179ad61c2a5058c03afd198f182c546a。

## 结论与范围

Task 12 受管配置展示、个人能力模型覆盖、管理员默认配置与成员/邀请管理代码验收通过。[首轮三项](2026-10-26-task-12-review-round-1.md)及[R2 邀请分页单项](2026-10-26-task-12-review-round-2.md)均已关闭；本轮只复审三文件定点改动及相邻邀请请求生命周期，未发现需要返工的重要可复现缺陷。

- managed-model 编辑使用个人原始 model/options；省略模型保持跟随，显式固定与参数不因原样保存被删除或改写。
- 个人完整路由按公开供应商 schema 输入必需凭证字段，向服务端提交既有 typed bundle；全空复用、部分输入拒绝和换作用域边界成立，不读回旧凭证值。
- 成员/邀请可继续分页，后页成员目标选择保持；R2 为邀请列表引入独立代次，创建/撤销刷新、整页重读及卸载使旧页失效。旧成功、失败和 finally 不得改写新列表、游标、提示或新请求加载状态。
- 管理员角色/禁用状态事务内复查、If-Match/CAS、加密凭证作用域及聊天/SSE 的既有边界保留。

本验收限于 Task 12 代码和本地隔离测试，不构成正式多人发布通过。Task 8 目标 Linux 防火墙/IPv6、XFS、真实 Chromium 沙箱、最终 Harness/runtime 镜像、真实供应商/公网出口及宿主健康链门槛保持原状态；未装配非聊天业务继续明确 503。

## 主审新鲜验证

固定 Python：C:/Users/hyh/anaconda3/envs/Agent/python.exe。

| 验证 | 结果 |
|---|---|
| tests/multiuser | 930 passed、3 skipped、2 个既有 websockets 弃用 warning，93.00 秒 |
| 全部前端 Node | 118 passed |
| 原交接单八文件兼容回归 | 272 passed、6 skipped，21.90 秒 |
| 新增四项邀请交错组件测试 | 当前组件 4 passed；只在临时测试装配中读取父提交组件时 4 failed；未回退仓库文件 |
| npm run build / npm run lint | 均退出 0；构建保留既有大于 500 kB 提示 |
| browser_task12.py | 退出 0、8/8 true、FAILURES 空、TMP_CLEANED true；Chromium 151.0.7922.34 |
| git diff 79fbe37 HEAD --check | 退出 0 |

真实 HTTPS 浏览器测试包含延迟旧 offset=50、创建邀请刷新、放行旧响应、新游标继续加载及撤销后页邀请；同时复跑个人原值、多字段凭证、A/B 隔离、管理员冲突和聊天设置导航。仅用临时 DB/key/Cookie/worker 与假供应商，不访问开发者账号或真实供应商；这不是 Docker/正式隔离证据。

独立审查者运行设置组件 20/20，通过并确认分页 success/catch/finally、刷新、整页加载和卸载均覆盖代次。本轮未改共享身份/聊天代码，按 R2 审计单条件没有重跑 Task 11 六个浏览器脚本；不把执行方历史结果写为主审本轮证据。

主审未修改产品代码、未 stage 或提交。主审既有 plan/spec、审计及任务文档保持独立。

## 下一步

允许按[原实施计划 Task 13](../plans/2026-10-16-multiuser-workspaces.md)继续备份恢复、显式迁移与运行手册；以本次最终 SHA 为新基线，依据现有实际 CLI/Store/runtime 接口实现，原计划示意代码不等于已有接口。只使用临时测试控制目录、工作区和凭证；不得迁移开发者实际数据、覆盖运行中控制目录或改动全局部署安全边界。完成后停止等待审计，不进入 Task 14，不把本次代码验收写成 Task 8 发布门槛通过。
