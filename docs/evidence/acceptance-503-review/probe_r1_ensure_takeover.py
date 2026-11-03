"""纯临时材料复核 ensure_ready 的接管与迟到清理；不访问 Docker。"""
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
        "r1_ensure_helpers", root / "tests/multiuser/test_attempt_ownership_503.py")
    helpers = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = helpers
    spec.loader.exec_module(helpers)
    with Store.open(temporary / "control.sqlite3") as store:
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
            entered = asyncio.Event()
            never_finish = asyncio.Event()

            class Adapter:
                async def commit_attempt(self, *args, **kwargs):
                    return await service.commit_attempt(*args, **kwargs)

                async def abort(self, *args, **kwargs):
                    return await service.abort(*args, **kwargs)

                async def ensure(self, *args, **kwargs):
                    # B 已越过附加 commit，但尚未交接 DB owner/generation。
                    entered.set()
                    await never_finish.wait()

            adapter = Adapter()
            first = RuntimeManager(store, adapter, owner_id="owner-a",
                                   gateway_required=True, clock=lambda: 1200)
            second = RuntimeManager(store, adapter, owner_id="owner-b",
                                    gateway_required=True, clock=lambda: 1200)
            pending_task = asyncio.create_task(second.ensure_ready(workspace, 1))
            await asyncio.wait_for(entered.wait(), 1)
            before = store.get_worker_lease(workspace)
            cleaned = None
            cleanup_error = ""
            try:
                cleaned = await first._cleanup_candidate(
                    workspace, handle, lease, original_attempt)
            except Exception as error:
                cleanup_error = getattr(error, "code", type(error).__name__)
            final = store.get_worker_lease(workspace)
            print(json.dumps({
                "before_owner": before.owner_id,
                "cleanup_reported_success": cleaned,
                "cleanup_error": cleanup_error,
                "gateway_stop_count": len(gateway.stops),
                "database_state": final.state,
                "attempt_status": service._load_attempt(workspace)["status"],
            }))
        finally:
            if pending_task is not None:
                pending_task.cancel()
                await asyncio.gather(pending_task, return_exceptions=True)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="easel-r1-ensure-review-") as temp:
        asyncio.run(scenario(Path(sys.argv[1]).resolve(), Path(temp)))
    print("TEMP_CLEANUP=ok; LIVE_RESOURCES_TOUCHED=false")
