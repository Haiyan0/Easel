# Task 3 复审通过

日期：2026-10-19（Asia/Shanghai）。

审计提交：dd3da720d2dfa7e10b95f155f0cbfd79798d852d。
父提交：472872f7a8ec8764cc3f7877026ca6f8e7543153。

结论：Task 3 通过，[R1 两项阻塞](2026-10-19-task-03-review-round-1.md)关闭。本次仅复审两项修复及其引入的回归，未发现新的 P1/P2。允许进入 Task 4，不代表完整多用户部署或发布验收通过。

## 核验结果

| 问题 | 核验依据 | 结论 |
|---|---|---|
| T3-R1-01 方法错配 | 仅 POST question/status 注册 TASK；GET 默认拒绝。真实 worker POST 返回 harness_ 问题状态；control 合法写请求分类后为 503，缺 CSRF 拒绝 | 关闭 |
| T3-R1-02 客户端默认状态绕过清洗 | build_request 合并后清洗最终头并重建固定身份；send 显式覆盖默认 auth，禁止自动跟随重定向。未临时修改共享 client 状态 | 关闭 |

主审独立 MockTransport 探针使用带混合大小写默认凭证头、Cookie 头、cookie jar、Basic auth 和 follow_redirects=True 的同一 client，并发发送 12 个不同固定 workspace/token 的请求。全部实际请求不含禁用头，内部身份单独匹配，请求体/参数保留，client 默认状态未改变。另一个 307 请求直接返回，未联系第二目的地。

独立审查另覆盖重复 Cookie、Digest auth、URL 用户凭证等，未发现本轮范围内的 P1/P2。探针均为本地假凭证和 MockTransport，没有真实网络调用。

## 主审验证

环境：PowerShell，C:\Code\Easel，C:\Users\hyh\anaconda3\envs\Agent\python.exe。

```text
python -m pytest tests/multiuser -q
265 passed in 9.66s

python -m pytest tests/test_local_write_guard.py tests/test_web_security.py tests/test_agent_harnesses.py tests/test_harness_workspace.py -q
186 passed, 6 skipped in 15.42s

git diff 472872f dd3da72 --check
退出 0，无输出
```

未运行全仓测试、前端构建、真实供应商或 Linux 容器门槛；没有把未执行项记为通过。没有修改产品或测试文件，审计文档不混入产品提交。

## 后续

按[原计划](../plans/2026-10-16-multiuser-workspaces.md)进入[Task 4 交接单](../tasks/2026-10-19-task-04-config.md)。保留 Task 1/2/3 的认证、路由、事务、线程及 local 兼容规则。
