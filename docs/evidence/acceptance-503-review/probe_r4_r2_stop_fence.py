"""只用临时文件及 fake，复现 stop 清除精确执行围栏。"""
import asyncio
import pathlib
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, sys.argv[1])
from tests.multiuser import test_attempt_ownership_503 as h

async def main(root):
    service = h._service(root)
    old, new = 'a' * 32, 'b' * 32
    await service.commit_attempt(h.WORKSPACE, h.WORKER, h.GATEWAY, attempt_id=old)
    h._record(service, new, worker_created=False, gateway_created=True)
    await service.commit_attempt(h.WORKSPACE, h.WORKER, h.GATEWAY, attempt_id=new)
    assert service._load_attempt(h.WORKSPACE)['fenced_attempts'] == [old]
    try:
        service._ensure_attempt_available(h.WORKSPACE, old)
    except h.provisioner.ProvisionerError as exc:
        assert exc.code == 'gateway_attempt_fenced'
    else:
        raise AssertionError('缺失前置围栏')
    async def missing(*args):
        return None
    async def no_sidecar(*args):
        pass
    service._inspect_raw = missing
    service._stop_sidecar = no_sidecar
    await service.stop(h.WORKSPACE, h.WORKER, h.GATEWAY, protocol_version=2)
    assert service._load_attempt(h.WORKSPACE) is None
    print('正式 stop 后 attempt 文件及旧 A 围栏均消失')
    restarted = h._service(root)
    class CrossedSideEffectBoundary(Exception):
        pass
    def side_effect(*args):
        raise CrossedSideEffectBoundary()
    with patch.object(h.provisioner, 'prepare_workspace', side_effect):
        try:
            await restarted.ensure(h.WORKSPACE, h.CONFIG_VERSION, protocol_version=2, attempt_id=old)
        except CrossedSideEffectBoundary:
            print('重启后迟到 A 的正式 ensure 已越过 admission，抵达 prepare_workspace 副作用')
        else:
            raise AssertionError('未复现')
    original_ensure = restarted._gateway.ensure
    async def created_gateway(**kwargs):
        candidate = await original_ensure(**kwargs)
        candidate.gateway_created = True
        return candidate
    restarted._gateway.ensure = created_gateway
    await restarted._with_gateway(h._handle(), 2, worker_created=False, attempt_id=old)
    record = restarted._load_attempt(h.WORKSPACE)
    assert record['attempt_id'] == old and record['status'] == 'pending'
    assert restarted._gateway.ensure_calls == 1
    print('同一旧 A 再次获准 gateway.ensure，并写回 A/pending:', record)

with tempfile.TemporaryDirectory(prefix='easel-r4r2-stop-') as temp:
    asyncio.run(main(pathlib.Path(temp)))
