# 原 Web 修复 Task 4 验收：原主题登录、设置与品牌资源通过

日期：2026-10-30（Asia/Shanghai）。
父基线：`55501036f31ff5880db87436bd35e3c5d24653d1`。
生产提交：`c0eadbeeed7c69fbfcafebe9bee4d21afa8dcfdc`。
证据 tip / 下一项父基线：`1b57e1927052d930e13779239ff2bc8907895ae5`。
worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。

## 裁决与通过范围

Task 4 验收通过，本轮未确认需返工阻塞。登录/模式与身份状态共用 AuthFrame、原 Easel 品牌和主题；个人/管理员设置复用原设置容器，角色权限保持；真实 Logo 进入 Vite public/dist，并通过 control 固定静态目录验证。

下一项仅 [Task 5A 基础工作区 API](../tasks/2026-10-30-original-web-task-05a-workspace-api.md)。Task 5 仍有工作台数据与执行/账号/发布分组，不能因为登录/主题通过就称全部原 Web 功能恢复。

## 主审新运行

- 全部 frontend Node：136 passed、0 failed/0 skipped。
- tsc + Vite build：成功，index-CK2tbBN-.js；已有 chunk 提示不作为本项阻塞。
- oxlint：退出 0。
- tests/multiuser/test_control_static.py：3 passed（0.78 秒）。
- 新 browser_original_web_login.py：8/8，FAILURES=[]、TASK4_BROWSER_OK=True，退出 0。
- 原 browser_original_web_task03.py：33/33，FAILURES=[]，退出 0。
- `git diff 55501036 1b57e19 --check`：退出 0，worktree 干净。

两套浏览器使用本轮真实 build、随机 localhost 假 API、独立 Chromium context；截图输出到临时 .pytest_cache 子目录并清理，没有覆盖作者证据。主审未使用 Docker、用户 Cookie/Key/卷/邀请或供应商。

## 主题、资源与交互核对

已查看作者桌面浅色登录、移动深色登录、成员设置截图；复用原 Logo/品牌字体、纸白/雾蓝/淡桃与原深色 token，移除独立 EASEL CONTROL/紫黑登录壳。主题在模式/身份状态、登录、工作台与设置之间连续，移动端与焦点/回车/busy/错误语义由新浏览器矩阵覆盖。

原资产/public/dist 的 PNG 均 398752 字节、800×800，SHA-256 `71c260f5a28e82aced0a513186ebdcf43782b068633e3a7ca5f159d3e3606132` 一致；品牌 Node 与真实浏览器验证无占位图片。Static 测试以真实 dist + create_app 验证 200/正确字节与 MIME、缺文件及穿越拒绝。

control Dockerfile 未改，其前端阶段已有 COPY web/frontend 后 build、再复制 dist，新增 public 文件按此路径进入产物。本轮通过构建规则检查及真实 dist 路由验证，不冒称主审新构建/运行镜像；未要求无变化 worker/Harness/gateway 重建。

## 设置与兼容

个人覆盖、全体默认/主聊天、成员/邀请三个分类仍可达；成员没有管理员写入口或 admin 请求。主聊天未配为明确引导，已配只表示配置已保存，并注明实际可用性另验。

独立设置复核：multiuserSettings 23 passed；真实构建 fresh fixture 验证成员浅/深/390px、无 admin 请求，以及管理员未配/已配状态与成员入口，全部通过。独立登录/品牌复核 13 passed；均与主审总集重叠，不相加。

数据 effect、版本冲突、凭证空值复用和身份 owner guard 未重做；未挂 local SettingsPanel，不触发 env/tools/CLI 全局副作用。Task 3 的认证、Run/SSE/题卡回归完整通过。

## 边界与后续

未恢复的工作台/技能/内容库/账号/画像接口仍按阶段明确限制；真实 DeepSeek/用户真实聊天与整体部署未验收。用户原容器未动，root 仍为原 1a881109；本轮仅更新主审文档，未 cherry-pick/发布。

后续按总计划 Task 5 的三组分别实施/验收：5A 基础工作区，5B 工作台数据，5C 执行/账号/发布。只下发第一组，已通过的 UI/身份/模型行为保持。
