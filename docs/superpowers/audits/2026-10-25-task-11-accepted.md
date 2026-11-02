# Task 11 验收通过

日期：2026-10-25（Asia/Shanghai）。最终提交：a6fc17713c65cd0acced72fffdccf72586878d6c。R5 父提交：9936d87499826aa48168510fed633792f37aba06。

## 结论与范围

Task 11 的邀请登录、身份隔离前端、服务端会话目录/历史、聊天 admission 与可恢复 SSE 接入代码验收通过。首轮及 R1–R5 范围内的阻塞项均已关闭；本轮聚焦 R5 的小范围实现和相邻恢复路径，未发现新的重要可复现缺陷。

- 身份确认和重新登录使用代次；旧查询、业务 401 与旧连接不能切回旧身份。当前身份失效卸载私人树，并只清本人缓存。
- 会话选择、用户主动选择、Run/连接和发送分别有所有权边界；迟到历史、终态及 admission 不得抢占当前工作区。
- SSE 失败状态驱动可渲染的重连入口；重复失败仍可恢复，原地重连保留已应用状态，新组件按可信回放起点重建。
- 目录和消息有可继续分页，尾页精确覆盖未读区间；归档历史可读，新 Run 仍受归档限制。
- 服务端已提交消息与 LocalSend 待确认输入分离。明确失败/未知接纳时旧回答可读，同 turnId 手动重试，不自动重新 POST。
- R5 为初次历史引入与其它消息读取一致的 messageSeq：成功/失败都在正文、分页窗口与 pruneLocalSends 之前核对；旧初次快照不能回退已提交的新终态。

本验收只覆盖 Task 11 代码及本地隔离测试。非聊天业务未装配者仍 503；Task 8 的目标宿主防火墙/IPv6、XFS、Chromium 沙箱、最终 Harness/runtime 镜像、真实供应商/公网出口等未通过门槛保持原状态，仍阻断正式多人发布。

## 主审新鲜验证

固定 Python：C:/Users/hyh/anaconda3/envs/Agent/python.exe。

| 验证 | 结果 |
|---|---|
| tests/multiuser | 908 passed、3 skipped、2 个既有依赖弃用 warning，83.56 秒 |
| 全部前端 Node | 98 passed |
| 六文件兼容回归 | 210 passed、6 skipped，18.92 秒 |
| npm run build / npm run lint | 均退出 0；构建有既有大于 500 kB 提示 |
| browser_task11_r5.py | 退出 0，空初次/含旧历史两种晚到，2 项全 true |
| browser_task11_r4.py | 退出 0，失败恢复、未知接纳、同键重试及隔离，5 项全 true |
| browser_task11_r3.py | 退出 0，两种 202 顺序与目录选择交错，9 项全 true |
| browser_task11_r2.py | 退出 0，5 项全 true；保留已知 CONTROL_STOP_WARNING |
| browser_task11_r1.py | 退出 0，15 项全 true |
| 原 browser_task11.py | 退出 0，4 次登录兑换、2 次 prompt、2 次 SSE；有既有 Windows Proactor 连接重置回调警告 |
| git diff 9936d87 HEAD --check | 退出 0 |

六份浏览器脚本由主审顺序执行，不用产品默认 HOME、真实用户浏览器配置或真实供应商凭证。R2 的关停警告如实保留：不以脚本退出 0 推断所有测试线程均正常退出。它不改变本轮产品代码验收结论，也不是正式容器隔离证据。

独立审查者确认 R5 检查位置覆盖正文、分页和本地条目清理，失败分支同样拒绝过期结果；未确认这次修复引入的重要问题。主审未修改产品或提交，原有 plan/spec 修改保持未暂存。

## 下一步

允许按 [Task 12 交接单](../tasks/2026-10-25-task-12-control-settings.md)继续受管配置展示、个人覆盖与管理员设置界面。完成后停止等待审计，不自动进入 Task 13，不把本次代码验收写成 Task 8 或正式发布通过。
