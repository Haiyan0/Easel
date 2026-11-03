"""只用临时材料复现 ensure 回复超时后，ready/G1 遗留 G2 pending 的窗口。

用法：Agent/python.exe probe_r2_lost_ensure_error.py <源码根>
复用被审仓库已有无 Docker 夹具；实际执行 Store、RuntimeManager、
Provisioner._with_gateway、attempt 写盘与 commit，仅远程 gateway 使用假材料。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace


def load_fixtures(source_root: Path):
    sys.path.insert(0, str(source_root))
    path = source_root / "tests/multiuser/test_attempt_ownership_503.py"
    spec = importlib.util.spec_from_file_location("review_r2_attempt_fixtures", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def run_probe(t, temporary_root: Path) -> dict:
    store, service, _, workspace, _, handle, _ = t._seed_expired_ensure(temporary_root)
    replacement = "f" * 64
    calls = {"ensure": 0, "resolve": 0}

    class Gateway(t.FakeGateway):
        async def ensure(self, **kwargs):
            calls["ensure"] += 1
            return SimpleNamespace(
                gateway_instance_id=replacement,
                boot_generation="e" * 32,
                gateway_upstream="http://172.31.0.9:8897",
                active=True,
                gateway_created=True,
            )

    gateway = Gateway(config_version=1)
    service._gateway = gateway

    class Adapter:
        def __init__(self, provisioner_service):
            self.service = provisioner_service

        async def commit_attempt(self, *args, **kwargs):
            if kwargs.get("resolve"):
                calls["resolve"] += 1
            try:
                return await self.service.commit_attempt(*args, **kwargs)
            except t.provisioner.ProvisionerError as error:
                # UnixProvisionerClient 收到 ok=false/error 时保留服务端错误码。
                raise t.RuntimeUnavailable(error.code, 503, retry_after=5) from error

        async def inspect(self, workspace_id):
            # 物理 worker 始终是同一可信实例；原 G1 已被替换，不声称可用。
            return {
                "managed": True, "workspace_id": workspace,
                "config_version": 1, "instance_id": t.WORKER,
                "worker_ready": True,
                "labels": {
                    "easel.managed": "true",
                    "easel.workspace-id": workspace,
                    "easel.config-version": "1",
                },
            }

        async def ensure(self, workspace_id, config_version, *, attempt_id=""):
            try:
                candidate = await self.service._with_gateway(
                    handle, t.provisioner.PROTOCOL_VERSION,
                    worker_created=False, attempt_id=attempt_id,
                )
            except t.provisioner.ProvisionerError as error:
                # 对齐 UnixProvisionerClient._decode_ensure 的错误映射，
                # 不让 RuntimeManager 把原服务端 code 泛化成 worker_start_failed。
                raise t.RuntimeUnavailable(error.code, 503, retry_after=5) from error
            assert candidate.gateway_instance_id == replacement
            assert self.service._load_attempt(workspace)["status"] == "pending"
            # 模拟 Unix 客户端在 provisioner 已写盘后等待回复超时。
            raise t.RuntimeUnavailable("provisioner_timeout", 503, retry_after=5)

    first_error = None
    with store:
        manager = t.RuntimeManager(
            store, Adapter(service), owner_id="owner-b", gateway_required=True,
            clock=lambda: 1200,
        )
        try:
            await manager.ensure_ready(workspace, 1)
        except t.RuntimeUnavailable as error:
            first_error = error.code
        first = store.get_worker_lease(workspace)
        first_state = {
            "state": first.state,
            "gateway_is_old": first.gateway_instance_id == t.GATEWAY,
            "pending_attempt_id": first.pending_attempt_id,
        }

    errors = []
    states = []
    for index in range(2):
        # 不沿用 manager、数据库连接或 provisioner 内存 attempt 缓存。
        service = t._service(temporary_root, gateway)
        with t.Store.open(temporary_root / "control.sqlite3") as reopened:
            manager = t.RuntimeManager(
                reopened, Adapter(service), owner_id=f"owner-retry-{index}",
                gateway_required=True, clock=lambda: 1400 + index * 200,
            )
            try:
                await manager.ensure_ready(workspace, 1)
            except t.RuntimeUnavailable as error:
                errors.append(error.code)
            else:
                errors.append(None)
            lease = reopened.get_worker_lease(workspace)
            states.append({
                "state": lease.state,
                "gateway_is_old": lease.gateway_instance_id == t.GATEWAY,
                "pending_attempt_id": lease.pending_attempt_id,
            })
    record = service._load_attempt(workspace)
    result = {
        "first_error": first_error,
        "after_failure": first_state,
        "reopened_retry_errors": errors,
        "reopened_states": states,
        "attempt_status": record["status"],
        "attempt_gateway_is_replacement": record["gateway_instance_id"] == replacement,
        "physical_gateway_ensure_calls": calls["ensure"],
        "resolve_calls": calls["resolve"],
    }
    # 断言是已复现缺陷，而非无关 inspect/身份错误或探针错误映射。
    assert first_error == "provisioner_timeout", result
    assert first_state == {"state": "ready", "gateway_is_old": True,
                           "pending_attempt_id": ""}, result
    assert errors == ["gateway_cleanup_pending", "gateway_cleanup_pending"], result
    assert all(state == first_state for state in states), result
    assert record["status"] == "pending", result
    assert record["gateway_instance_id"] == replacement, result
    assert calls == {"ensure": 1, "resolve": 0}, result
    return result


def main():
    if len(sys.argv) != 2:
        raise SystemExit("用法：probe_r2_lost_ensure_error.py <源码根>")
    fixtures = load_fixtures(Path(sys.argv[1]).resolve())
    temporary = tempfile.TemporaryDirectory(prefix="easel-r2-lost-ensure-error-")
    temporary_root = Path(temporary.name)
    try:
        result = asyncio.run(run_probe(fixtures, temporary_root))
        print(json.dumps(result, ensure_ascii=True, indent=2))
    finally:
        temporary.cleanup()
        print("temporary_resources_removed=" + str(not temporary_root.exists()).lower())


if __name__ == "__main__":
    main()
