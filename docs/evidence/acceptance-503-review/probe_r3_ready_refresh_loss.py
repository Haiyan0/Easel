"""只用临时 Store/attempt 文件，复现 ready 首屏刷新丢回复后的恢复阻断。"""
import asyncio
import dataclasses
import importlib.util
import pathlib
import sys
import tempfile

root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
spec = importlib.util.spec_from_file_location(
    "probe_helpers", root / "tests/multiuser/test_attempt_ownership_503.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


async def scenario(path):
    store, service, gateway, workspace, lease, handle, original = h._seed_expired_ensure(path)
    with store:
        # 先由真实 RuntimeManager 收口 starting，形成正常 ready 租约。
        adapter = h._lost_response_adapter(service, handle, h.GATEWAY, {"count": 0})
        manager = h.RuntimeManager(store, adapter, owner_id="owner-b",
                                   gateway_required=True, clock=lambda: 1200)
        await manager.ensure_ready(workspace, 1)
        before = store.get_worker_lease(workspace)
        assert before.state == "ready"
        replacement = "f" * 64
        lost = {"count": 1}
        adapter = h._lost_response_adapter(service, handle, replacement, lost,
            failure=h.RuntimeUnavailable("provisioner_timeout", 503, retry_after=5))

        async def inspect(workspace_id):
            # worker 身份/就绪均正确，仅 gateway 已丢失/重启，触发正常刷新。
            return {"managed": True, "workspace_id": workspace_id,
                    "config_version": 1, "instance_id": h.WORKER,
                    "labels": {"easel.managed": "true", "easel.workspace-id": workspace_id,
                               "easel.config-version": "1"},
                    "worker_ready": True, "gateway_available": False}
        adapter.inspect = inspect
        for index, stamp in enumerate((1201, 1202, 2000), 1):
            manager = h.RuntimeManager(store, adapter, owner_id="owner-b",
                                       gateway_required=True, clock=lambda: stamp)
            try:
                await manager.ensure_ready(workspace, 1)
                raise AssertionError("预期失败未出现")
            except h.RuntimeUnavailable as error:
                current = store.get_worker_lease(workspace)
                record = service._load_attempt(workspace)
                print(index, error.code, "DB", current.state,
                      current.gateway_instance_id[:8], current.attempt_id,
                      "remote", record["status"], record["gateway_instance_id"][:8],
                      record["attempt_id"])
                assert current.state == "ready"
                assert current.gateway_instance_id == h.GATEWAY
                assert record["status"] == "pending"
                assert record["gateway_instance_id"] == replacement
                assert current.attempt_id != record["attempt_id"]
                assert error.code == ("provisioner_timeout" if index == 1 else "worker_start_failed")
                if index > 1:
                    assert getattr(error.__cause__, "code", None) == "gateway_cleanup_pending"
        print("CONFIRMED: 普通 ready 首屏丢回复后，重试与跨租约期限重试均仍 503")


with tempfile.TemporaryDirectory(prefix="easel-r3-ready-probe-") as directory:
    asyncio.run(scenario(pathlib.Path(directory)))
