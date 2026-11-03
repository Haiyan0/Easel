"""R2 主审：屏障前通过 runtime claim 的文件流可在零计数检查后才登记请求。

用法：Agent/python.exe probe_r2_update_late_request.py <源码根>
仅使用临时 SQLite、真实 RuntimeManager/WorkspaceApi 与受控 provisioner/上游；
不调用 Docker、WSL 或网络，临时目录离开上下文后自动删除。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
from easel.control.runtime import RuntimeManager
from easel.control.store import Store
from easel.control.workspace_api import WorkspaceApi
from tests.multiuser.test_runtime_gateway import GatewayFake


async def scenario() -> None:
    with tempfile.TemporaryDirectory(prefix="easel-r2-late-request-") as directory:
        with Store.open(Path(directory) / "control.sqlite3") as store:
            actor = store.create_user("受控探针")
            entered, release, upstream_started = (
                asyncio.Event(), asyncio.Event(), asyncio.Event())

            class Provisioner(GatewayFake):
                block_inspect = False

                async def inspect(self, workspace_id):
                    if self.block_inspect:
                        entered.set()
                        await release.wait()
                    return await super().inspect(workspace_id)

            class Response:
                status_code = 200
                headers = {}

                async def aclose(self):
                    pass

            class Gateway:
                async def request(self, *args, **kwargs):
                    assert kwargs["stream"] is True
                    upstream_started.set()
                    return Response()

            provisioner = Provisioner(store)
            runtime = RuntimeManager(
                store, provisioner, gateway_required=True, poll_interval=0.01)
            await runtime.ensure_ready(actor.workspace_id, 0)
            api = WorkspaceApi(runtime, store, Gateway(), revision=0)
            provisioner.block_inspect = True
            # 真实 GET /api/media/{path} 经 app.py 的 _workspace_media_response
            # 调用同一 open_stream；先让请求在真实 ready claim 后的 inspect 暂停。
            task = asyncio.create_task(api.open_stream(
                actor, "GET", "/api/media/output/probe.txt"))
            await asyncio.wait_for(entered.wait(), 2)
            assert not task.done()
            barrier = store.set_admission_barrier(
                "update-abcdef123456", "desktop-update", int(time.time()))
            before = store.drain_status(int(time.time()))
            print("before_update_start=" + json.dumps(before, sort_keys=True))
            assert before["activeRuns"] == before["activeRequests"] == 0
            assert before["activeLeases"][0]["state"] == "ready"
            # Update:414-415 此时会返回并进入 Start；此后释放仍在途的请求。
            release.set()
            stream = await asyncio.wait_for(task, 2)
            try:
                after = store.drain_status(int(time.time()))
                print("after_update_start=" + json.dumps(after, sort_keys=True))
                print("upstream_started_after_barrier=" + str(
                    upstream_started.is_set()).lower())
                assert upstream_started.is_set()
                assert store.admission_barrier()["id"] == barrier["id"]
                assert after["activeRequests"] == 1
            finally:
                await stream.aclose()
            assert store.drain_status(int(time.time()))["activeRequests"] == 0
    print("temporary_resources_cleaned=true")


asyncio.run(scenario())