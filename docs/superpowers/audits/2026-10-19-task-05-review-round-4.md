# Task 5 R3 复审：EOF 自动关闭仍绕过唯一清理

日期：2026-10-19（Asia/Shanghai）。

审计提交：06b989e7f13340703a2cee13ce1446df5547d65f；父提交：e9a36b63e31d149e4283858208484042a786373e。

结论：暂不通过，仍有一项 P2，属于[上一轮 T5-R3-01](2026-10-19-task-05-review-round-3.md)的遗漏结束路径。本轮只要求补齐 EOF 自动关闭，不重做已通过的其他 Task 5 功能，不进入 Task 6。

## 已验证

共享 _cleanup_task 与 shield 已修复原来的“心跳先开始关闭、读取方再关闭/取消”的 gate 场景，三个新增测试通过。缺口在 HTTPX 自己先开始 EOF 关闭时，首次关闭仍不在共享任务内。

主审在 PowerShell、C:\Code\Easel，使用 C:\Users\hyh\anaconda3\envs\Agent\python.exe 运行：

```text
python -m pytest tests/multiuser -q
403 passed in 19.23s

python -m pytest tests/test_settings_stale_mirror.py tests/test_web_model_config_parity.py tests/test_local_write_guard.py tests/test_web_security.py tests/test_agent_harnesses.py tests/test_harness_workspace.py -q
248 passed, 6 skipped in 24.49s

python -m pytest tests/integration/multiuser/test_egress.py -q
6 skipped in 0.07s（真实出口未执行）

git diff e9a36b6 HEAD --check
退出 0，无空白错误
```

未运行全仓、真实供应商、Linux/容器或前端门槛。所有额外探针仅临时库与 MockTransport，无联网、无产品修改。

## T5-R4-01 / P2：HTTPX 在 EOF 内部先关闭，读取取消再次截断真正清理

延续 T5-R3-01。位置：easel/control/broker.py 第 210 行（aiter_raw）、第 218 行（next_task.cancel）、第 165–168 行（cleanup 中重复 response.aclose）。

当前 chunks 将 anext(response.aiter_raw()) 放在独立 next_task。实际安装的 HTTPX 在 aiter_raw 读完 body 后自动执行 await self.aclose()；该关闭先将 response.is_closed 置真，再 await 底层 stream.aclose。

因此正常 EOF 本身就是另一个关闭发起者：

1. 正常读完 body，next_task 内 HTTPX 自动开始底层关闭，尚未创建 guard._cleanup_task。
2. 此时租约续期失败，gone 事件使 chunks 取消 next_task，首次关闭被截断。
3. guard 的唯一清理任务再调用 response.aclose，HTTPX 因 is_closed=True 直接返回；随后额度被释放。

仅把显式关闭路径合并到 _cleanup_task，并没有把这个库内部的首次关闭纳入。

### 确定复现

主审和独立审查分别复现，使用会响应取消的有限 gate：

```python
class EofCloseStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"one"  # 随后正常 EOF，无 read_gate

    async def aclose(self):
        self.close_started.set()
        try:
            await self.close_gate.wait()
            self.closed = True
        except asyncio.CancelledError:
            self.close_cancelled = True
            raise
```

复用 test_broker_forward.py 的 broker_env，lease_ttl=3。forward 成功后读取 one，再启动第二次 anext(chunks)；等 close_started 后注入 renew_broker_lease 异常，等待读取结束，再释放 close_gate。

```text
EOF 关闭开始：response.is_closed=True，guard._cleanup_task=None
读取结束：lease_lost
leases=0，stream_closed=False，close_cancelled=True
释放 close_gate 后：stream_closed 仍为 False
```

这不是永久阻塞的自定义上游，也不是额外网络故障假设；只是在正常 EOF 清理需要 await 时同步触发既有租约丢失分支。

## 修复及验收

- 让 EOF 首次关闭也受同一个清理所有者控制，例如采用不会绕过 owner 自动关闭的原始流读取方式，或明确等待被库发起的首次关闭完成。具体实现自行选择，但不能继续依赖第二次 response.aclose 或 is_closed 标志。
- 不要仅对所有 anext 加 shield：仍需能取消真正阻塞的读取/上传/响应头等待，不能为避免截断关闭而取消不了在途请求。
- 先增加上述 EOF gate 红测试：自然 body EOF → 底层 close_started → 续期失败。gate 前不得出现“关闭被取消且额度归零”；gate 放行后底层关闭完成、额度归零、读取以明确终态结束，所有读取/心跳/清理任务结束。
- 补同一 EOF 清理过程中读取方取消/主动 aclose 的竞争，保持唯一关闭及等待完成语义。保留上一轮 response/transport gate、响应头前失败、UPDATE 零行及正常 SSE 取消回归。
- 检查实际发送能力预算，不能只断言表内保留一条已经过期、不会计入容量的租约行；测试 gate 应在当前有效租约期间完成关键断言。

本轮仅处理这一条。按先红后绿完成，运行本报告两组指定回归和 diff 检查，显式路径、中文信息提交，不夹带审计文档/任务单。报告新 SHA、EOF gate 红绿证据及实际结果后停止等待复审，不进入 Task 6。
