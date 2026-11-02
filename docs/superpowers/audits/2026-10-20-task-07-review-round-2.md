# Task 7 R1 复审：仅需返工 T7-R2-01

- 父提交：7e070acd753469c1850a480b189eb772e2fa4aae。
- 被审提交：bde8e3229b323b6e7786ff568ee575cb5b40c4bb。
- 结论：一项 P2 阻塞，不进入 Task 8。
- 本轮仅复审四项修复与直接相关调用分支，未修改产品代码、暂存或提交；全部探针使用临时 profile/测试文档，不访问真实平台。

## 上轮关闭情况

- T7-R1-01 通过：最终媒体类型显式传入 FileResponse，HTML/XHTML/SVG/XML/+xml 使用统一 sandbox；真实 Chromium 不能执行 XHTML 脚本。
- T7-R1-02 通过：产物树节点和元数据解析目标统一检查，越界/内部 junction 被跳过，解析路径集合切断环；普通项目保持。
- T7-R1-03 通过：脚本与 worker 锁位置统一改到 profile 外，注销争用同一锁；busy/删除失败明确非成功，公众号等待登录进程退出后再删除。
- T7-R1-04 部分通过：原知乎两个函数和常见分支的 finally 已接入，真实导航异常可重新获锁。但 web_publisher 的 keep_open=True 分支仍在异常后遗漏清理，收敛为下方唯一返工项。

## T7-R2-01 [P2] keep_open=True 的发布预检异常仍泄漏 profile 锁

位置：`skills/shared/scripts/web_publisher.py` 的 _run_browser，约第 868–870 行。

新 finally 仍由 if not a.keep_open 决定是否调用 browser.close。预检或后续步骤抛错时，keep_open=True 会直接跳过包装器关闭。外层 sync_playwright 随异常退出关闭 driver/browser，但包装器持有的锁 fd 仍未释放，同一解释器捕获异常后的下一次任务仍被阻塞。

主审独立真实 Chromium 探针：临时 profile，调用真实 _run_browser(headed=False, do_publish=True)，仅把 _resolve_steps 替换为抛出 RuntimeError 的预检钩子；未进行网页导航或真实发布。退出后用同一 ProfileLock(timeout=.3) 获取：

```text
keep_open=false error=RuntimeError lock_reacquired=true
keep_open=true  error=RuntimeError lock_reacquired=false
```

独立复审另用本地 data:text/html,login 页面和临时平台配置调用实际 _run_browser，无需注入预检异常：真实登录预检自然触发 SystemExit(6)，keep_open=True 时退出 sync_playwright 后重取锁同样超时，原锁 fd 仍被持有。该探针未联网。

没有用假浏览器/假 close 推断，也没有把整个 CLI 退出后自动释放 fd 描述为永久故障。问题范围仍是任务单明确要求的函数异常清理和同进程复用；keep_open 调试选项不应在执行失败且 Playwright 已退出后留下无主锁。

### 修复边界与验收

1. 仅修复该分支及必要的共享清理接线。keep_open 不得绕过失败/取消退出时的 context 清理；当 Playwright 所有者退出时，包装器锁也应有明确终态。保留需要的 local 调试体验，但不能留下只有进程退出才能释放的锁。
2. 先补当前提交会失败的真实 _run_browser 调用者测试，keep_open=False/True 均覆盖：预检异常后底层浏览器已关闭，同一解释器可重新取得同 profile 锁。
3. 覆盖代表性取消/提前退出与合法正常路径，确保锁在浏览器关闭完成前不会被另一个使用者取得；不通过提前 release 修复异常后的锁泄漏。
4. 保留已通过的知乎异常、跨进程互斥、注销 busy/失败、真实 Cookie 持久化与产物边界测试。不要重做已关闭的三个修复，不实现 Task 8。

## 主审独立验证

使用 C:\Users\hyh\anaconda3\envs\Agent\python.exe：

- tests/multiuser：562 passed、2 skipped，33.87 秒。
- 任务单指定十文件回归：291 passed、20 skipped，24.51 秒。
- tests/integration/multiuser/test_browser_profiles_real.py：5 passed，3.53 秒（2 个依赖弃用警告）。
- git diff 7e070ac HEAD --check：退出 0。
- 上述真实 Chromium keep_open 对照探针实际复现。

本轮未独立复跑全仓，不将执行 Agent 报告的 42 项基线失败对照标为本轮验证。真实平台登录/发布、Linux 只读挂载、真实 DSH/供应商未执行。符号链接权限限制仍明确 skipped，junction 已有真实验证。

## 可复制给执行 Agent 的指令

基于 bde8e3229b323b6e7786ff568ee575cb5b40c4bb，仅修复 T7-R2-01：web_publisher._run_browser 在 keep_open=True 的预检/执行异常及退出时不得遗留 profile 锁。先增加真实调用者红测试，最小修复并证明关闭完成后才释放，保留 local 调试和已通过的其他修复。

运行 tests/multiuser、任务单指定十文件回归和真实 Chromium 集成；检查父提交到新提交的 git diff --check。显式路径提交，不夹带审计/任务单、其他工作或真实账号文件。报告父/新 SHA、红绿证据及实际测试结果，完成后停止等待复审，不进入 Task 8。
