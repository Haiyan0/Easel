"""临时假材料检查恢复与清理交错；不连接 Docker 或用户实例。"""
from __future__ import annotations

import asyncio
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
        "review_attempt_helpers", root / "tests/multiuser/test_attempt_ownership_503.py")
    helpers = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = helpers
    spec.loader.exec_module(helpers)
    store_context = Store.open(temporary / "control.sqlite3")
    store = store_context.__enter__()
    pending_task = None
    try:
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
        gateway = helpers.FakeGateway(config_version=1)
        service = helpers._service(temporary, gateway)
        service._record_attempt(workspace, original_attempt, handle,
                                worker_created=False, gateway_created=True)
        restore_entered = asyncio.Event()
        never_finish = asyncio.Event()

        class Adapter:
            async def commit_attempt(self, *args, **kwargs):
                return await service.commit_attempt(*args, **kwargs)

            async def abort(self, *args, **kwargs):
                return await service.abort(*args, **kwargs)

            async def restore_gateway(self, *args, **kwargs):
                restore_entered.set()
                await never_finish.wait()

        adapter = Adapter()
        first = RuntimeManager(store, adapter, owner_id="owner-a",
                               gateway_required=True, clock=lambda: 1200)
        second = RuntimeManager(store, adapter, owner_id="owner-b",
                                gateway_required=True, clock=lambda: 1200)
        pending_task = asyncio.create_task(second.restore_gateway(
            workspace, helpers.WORKER, 1))
        await asyncio.wait_for(restore_entered.wait(), 1)
        cleaned = await first._cleanup_candidate(
            workspace, handle, lease, original_attempt)
        final = store.get_worker_lease(workspace)
        print(json.dumps({
            "cleanup_reported_success": cleaned,
            "gateway_stop_count": len(gateway.stops),
            "database_state": final.state,
            "database_gateway_id_empty": not final.gateway_instance_id,
            "attempt_status": service._load_attempt(workspace)["status"],
        }))
    finally:
        if pending_task is not None:
            pending_task.cancel()
            await asyncio.gather(pending_task, return_exceptions=True)
        store_context.__exit__(None, None, None)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="easel-lifecycle-review-") as temp:
        asyncio.run(scenario(Path(sys.argv[1]).resolve(), Path(temp)))
    print("TEMP_CLEANUP=ok; LIVE_RESOURCES_TOUCHED=false")
