"""R4-R3 诊断：Stop 前后持久状态与重启后副作用计数。

不修改主审探针；用真实 Provisioner 方法与假远程，仅临时目录。
"""
import asyncio
import json
import pathlib
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, sys.argv[1])
from tests.multiuser import test_attempt_ownership_503 as h


async def main(root):
    service = h._service(root)
    old, new, fresh = "a" * 32, "b" * 32, "c" * 32
    await service.commit_attempt(
        h.WORKSPACE, h.WORKER, h.GATEWAY, attempt_id=old)
    h._record(service, new, worker_created=False, gateway_created=True)
    await service.commit_attempt(
        h.WORKSPACE, h.WORKER, h.GATEWAY, attempt_id=new)
    before = service._load_attempt(h.WORKSPACE)

    async def missing(*args):
        return None

    async def no_sidecar(*args):
        pass

    service._inspect_raw = missing
    service._stop_sidecar = no_sidecar
    await service.stop(
        h.WORKSPACE, h.WORKER, h.GATEWAY, protocol_version=2)
    after = service._load_attempt(h.WORKSPACE)

    restarted = h._service(root)
    gateway = restarted._gateway
    hit = {"prepare": False}

    def prepare(*args, **kwargs):
        hit["prepare"] = True
        raise AssertionError("prepare_workspace reachable")

    with patch.object(h.provisioner, "prepare_workspace", prepare):
        try:
            await restarted.ensure(
                h.WORKSPACE, h.CONFIG_VERSION, protocol_version=2,
                attempt_id=old)
        except h.provisioner.ProvisionerError as error:
            late_ensure = error.code
        else:
            late_ensure = "CROSSED"
    try:
        await restarted._with_gateway(
            h._handle(), 2, worker_created=False, attempt_id=old)
    except h.provisioner.ProvisionerError as error:
        late_gateway = error.code
    else:
        late_gateway = "CROSSED"
    gateway_ensure_calls = gateway.ensure_calls

    restarted._ensure_attempt_available(h.WORKSPACE, fresh)
    await restarted._with_gateway(
        h._handle(), 2, worker_created=False, attempt_id=fresh)
    final = restarted._load_attempt(h.WORKSPACE)

    print(json.dumps({
        "before_stop": {
            "attempt_id": before["attempt_id"],
            "status": before["status"],
            "fenced_attempts": before["fenced_attempts"],
        },
        "after_stop": {
            "attempt_id": after["attempt_id"],
            "status": after["status"],
            "scope": after["scope"],
            "worker_created": after["worker_created"],
            "gateway_created": after["gateway_created"],
            "fenced_attempts": after["fenced_attempts"],
        },
        "late_ensure": late_ensure,
        "late_gateway": late_gateway,
        "prepare_workspace_called": hit["prepare"],
        "gateway_ensure_calls_before_fresh": gateway_ensure_calls,
        "fresh_attempt": fresh,
        "fresh_record": {
            "attempt_id": final["attempt_id"],
            "status": final["status"],
            "fenced_attempts": final["fenced_attempts"],
        },
        "gateway_ensure_calls_after_fresh": gateway.ensure_calls,
    }, sort_keys=True))
    print("R4R3_DIAGNOSTIC_OK")


with tempfile.TemporaryDirectory(prefix="easel-r4r3-diag-") as directory:
    asyncio.run(main(pathlib.Path(directory)))
