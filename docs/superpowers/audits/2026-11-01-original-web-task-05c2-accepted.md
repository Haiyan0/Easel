# 原 Web Task 5C2 验收：账号、登录与分析 R1 通过

日期：2026-11-01。R1父基线 `a7c8ce253905918987f2a35fc269a1936a93dd6f`；修复 `b1e49cb9bd69b3ba9cdec8713cc113cdcfea4681`；证据 `eaa0c6a7436d46487efa8f9ac36cfceeaff84033`；最终tip `fcb351d532301d22afe3be0c91373b19f3505ca7`。

实施worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。

## 裁决

Task5C2通过。[首轮四项问题及证据缺口](2026-11-01-original-web-task-05c2-review-round-1.md)均已收口，独立复审未确认本轮新增阻塞。下一项仅按 [5C3发布与导出](../tasks/2026-11-01-original-web-task-05c3-publishing-export.md)实施并等待复审，Task6整体验收/部署尚未进入。阶段通过不等于真实平台或生产可发布。

## R1关闭情况

- **R1-01 当前Run归属**：有受管Run时只从该Run事件与worker终态构造登录视图，旧共享status不再结算新Run或提供短信依据。主审独立复跑原stale-login探针：新脚本启动0，返回starting，control/worker均running，active=1。普通/MP旧终态、restart和SMS归属由新增测试覆盖。
- **R1-02 显式分析动作**：GET只读已成功结果；POST经Origin/CSRF才启动刷新。主审独立正向Chromium探针：外站顶层GET仍带Lax Cookie，但404/cache missing、执行0；缺来源/错Origin POST均403；合法POST200且执行1，后续缓存GET200、额外执行0。不是依靠旧缺陷探针超时来判通过。
- **R1-03 迟到停止回应**：成功/失败/finally按弹窗序列、平台、mp、Run归属校验。主审独立正向浏览器：放行A旧stop后，B仍显示抖音、二维码1、无误停止文案，后台qr_ready/running，按钮可用且能实际停止。
- **R1-04 凭证提交边界**：验证与提交分离，重新校验交接槽和Run，提交栅栏对停止可见；注销会停止凭证Run。主审独立复跑原credentials-stop：stop200/cancelled后放行验证，原请求409，配置未写、configured=false、marker=unknown。提交先完成、取消后新校验与注销交错由新增真实链测试覆盖。未以仅判断future状态替代文件提交约束。
- **R1-05 容器与日志**：首轮.log已转为跟踪的.txt，引用目标存在且进入Git；主审独立复跑新增隔离容器链路，12项检查全true，cleanup errors为空。镜像缺PyYAML已通过worker requirements补齐。真实平台脚本未在容器链执行，固定受控runner实际启动镜像内Chromium，证据清楚区分两者。

主审探针：[生命周期](../../evidence/original-web-task-05c2-review/probe_worker_lifecycle.py)、[R1正向浏览器](../../evidence/original-web-task-05c2-review/probe_r1_positive.py)、[正向输出](../../evidence/original-web-task-05c2-review/probe_r1_positive_output.txt)。前两个旧缺陷浏览器探针的预期超时仅为辅助，未作为独立绿态断言。

## 主审本轮新运行

- R1/账号/worker/真实ASGI链/security及runs/stop_barrier/stop_recovery：88 passed。
- Store/gateway/client/relay/policy/task_leases/workspace/profile及原本地账号/分析：430 passed、1 skipped。两组文件无重叠，共518 passed、1 skipped。
- 全frontend Node157 passed；build成功，index-zM18pZTN.js；lint退出0。
- 浏览器5C2六组、5C1七组、5B五组、5A四组、Task3 33组、Task4八组，共63组全部FAILURES=[]、各脚本退出0。截图输出临时目录并清理，未覆盖作者证据。
- 原生命周期两例、正向跨站/迟到stop浏览器由主审独立复跑，退出0并清理。两位独立审阅者分别核对后端提交/登录与前端动作/归属边界，无新增阻塞意见。
- 生产/测试/部署源码diff --check通过，实施树干净。既有chunk、Playwright Win10054关闭回调与5A coroutine警告继续记录，不扩为本轮产品阻塞。

## 容器独立复跑

主审运行当前 `tests/multiuser/task5c2_container_chain.py`，复用本轮已构建镜像，不重建或替换用户实例；随机标签的新容器/网络/三卷运行后按归属清理。镜像ID与作者日志一致：

- control：sha256:1e3cf0b6684969cf1087a0ec7065380d9f51a0081c97efaf0a2eb044171532d4
- gateway：sha256:4316aa71f57465ccf77a75eb088b3b1d6e920f49dbc964fe4ec4517f765a997e
- worker：sha256:586915838d0570065709a348cd94c8f228817119118cc97f7f9c6d43d6304328

9项镜像源码/前端哈希与当前源码一致。实际主审结果：登录派发、真实PNG/profile/Chromium、短信成功、whoami、分析、凭证保存、停止释放、超时释放、停止后容量、跨站GET不派发均true；TASK5C2_CONTAINER_OK=True、CLEANUP_ERRORS=[]。没有用宿主Python的依赖代替镜像验证。

真实层包括认证/签名/账本/RunManager、镜像内子进程和Chromium；本地平台、短信值和凭证校验结果是受控替身。容器测试用seccomp=unconfined运行沙箱浏览器，未验证生产Linux默认seccomp/网络拓扑，因此本项不解除既有Linux生产门槛。

## 保留边界

真实平台扫码、短信、AppSecret、真实发布/导出外部动作、付费模型、完整Compose/生产Linux和最终上线仍未验收。未执行完整顶层pytest，不将已有平台/顺序失败表述为已修复。

用户现有容器/卷/socket/Key/Cookie/邀请未操作，未复制用户HOME或账号材料，未重放prompt、未付费/发布、未全局prune、未合并/推送/部署。主审文档保存在root，未提交到实施分支。
