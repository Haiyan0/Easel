"""主审 probe_r1_lost_restore_response.py 的展示变体：只丢失第一次响应。

原探针的 Adapter 每次 restore 都固定抛 CancelledError，用于复现 bug；
修复后第一次恢复会成功，第二次正常 restore 调用仍被该 Adapter 模拟为
中断，导致原探针在打印 JSON 前以 CancelledError 退出。本变体仅把
“每次丢失”改为“只丢失第一次”，用于展示恢复终态；原探针结果另存。
"""
from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
from pathlib import Path
import sys
import tempfile


async def scenario(root: Path, temporary: Path) -> None:
    sys.path.insert(0, str(root))
    from easel.control.runtime import RuntimeManager
    from easel.control.store import Store
    from easel.control.types import WorkerHandle

    spec = importlib.util.spec_from_file_location(
        "r1_lost_helpers", root / "tests/multiuser/test_attempt_ownership_503.py")
    helpers = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = helpers
    spec.loader.exec_module(helpers)
    database = temporary / "control.sqlite3"
    service = helpers._service(temporary, helpers.FakeGateway(config_version=1))
    with Store.open(database) as store:
        workspace = store.create_user("temporary-review").workspace_id
        lease = store.claim_worker_start(workspace, 1, "owner-a", 1000, 60)[1]
        handle = WorkerHandle(
            workspace, helpers.WORKER, "http://127.0.0.1:7860", "fake-token", 1,
            gateway_instance_id=helpers.GATEWAY, gateway_generation="d" * 32,
            gateway_upstream="http://127.0.0.1:8897",
            gateway_state="staged", gateway_created=True)
        original_attempt = "a" * 32
        assert store.stage_worker_gateway_start(
            workspace, "owner-a", lease.generation, handle, original_attempt, 1001)
        service._record_attempt(workspace, original_attempt, handle,
                                worker_created=False, gateway_created=True)

        lost = {"remaining": 1}

        class Adapter:
            async def commit_attempt(self, *args, **kwargs):
                return await service.commit_attempt(*args, **kwargs)

            async def restore_gateway(self, workspace_id, worker_id, version,
                                      *, attempt_id):
                # 只第一次模拟响应丢失；后续按真实成功路径返回可用候选。
                candidate = dataclasses.replace(
                    handle, gateway_instance_id="f" * 64,
                    gateway_state="available")
                if lost["remaining"] > 0:
                    lost["remaining"] -= 1
                    service._record_attempt(
                        workspace_id, attempt_id, candidate,
                        worker_created=False, gateway_created=True)
                    raise asyncio.CancelledError()
                service._record_attempt(
                    workspace_id, attempt_id, candidate,
                    worker_created=False, gateway_created=False)
                return candidate

        adapter = Adapter()
        first = RuntimeManager(store, adapter, owner_id="owner-b",
                               gateway_required=True, clock=lambda: 1200)
        try:
            await first.restore_gateway(workspace, helpers.WORKER, 1)
        except asyncio.CancelledError:
            pass
        before = store.get_worker_lease(workspace)
        target_mismatch = (
            before.gateway_instance_id
            != service._load_attempt(workspace)["gateway_instance_id"])

    # 关闭并重开 SQLite，且换新 manager，防止内存状态掩盖重启缺口。
    errors = []
    with Store.open(database) as store:
        for index, stamp in enumerate((1500, 1800)):
            recovered = RuntimeManager(store, adapter, owner_id=f"owner-c-{index}",
                                       gateway_required=True, clock=lambda: stamp)
            try:
                await recovered.restore_gateway(workspace, helpers.WORKER, 1)
            except Exception as error:
                errors.append(getattr(error, "code", type(error).__name__))
        final = store.get_worker_lease(workspace)
        print(json.dumps({
            "lost_response_target_mismatch": target_mismatch,
            "recovery_errors": errors,
            "database_state": final.state,
            "pending_attempt_present": bool(final.pending_attempt_id),
            "attempt_status": service._load_attempt(workspace)["status"],
        }))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="easel-r1-lost-review-") as temp:
        asyncio.run(scenario(Path(sys.argv[1]).resolve(), Path(temp)))
    print("TEMP_CLEANUP=ok; LIVE_RESOURCES_TOUCHED=false")
