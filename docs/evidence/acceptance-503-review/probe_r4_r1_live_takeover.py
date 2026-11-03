"""只用临时目录/假 provisioner，复现延迟派发在预占接管后写入 pending。"""
import asyncio, dataclasses, pathlib, sys, tempfile, json
sys.path.insert(0, sys.argv[1])
from tests.multiuser import test_attempt_ownership_503 as h

async def main(directory):
    store, service, gateway, ws, lease, handle, original = h._seed_expired_ensure(directory)
    with store:
        await h._make_normal_ready(store, service, ws, handle)
        entered, release, late_written = asyncio.Event(), asyncio.Event(), asyncio.Event()
        attempts = {}
        class Adapter:
            inspect = staticmethod(h._ready_inspect)
            async def commit_attempt(self, *args, **kwargs):
                try:
                    return await service.commit_attempt(*args, **kwargs)
                except h.provisioner.ProvisionerError as e:
                    raise h.RuntimeUnavailable(str(e), 503, retry_after=1)
            async def abort(self, *args, **kwargs):
                return await service.abort(*args, **kwargs)
            async def ensure(self, workspace_id, config_version, *, attempt_id='', orphan_takeover=False):
                if 'a' not in attempts:
                    attempts['a'] = attempt_id
                    entered.set()
                    # 请求在 provisioner 获取工作区锁之前延迟，B 的 commit 可先抵达。
                    await release.wait()
                    candidate = dataclasses.replace(handle, gateway_instance_id='f'*64,
                        gateway_generation='e'*32, gateway_state='available', attempt_id=attempt_id)
                    service._record_attempt(ws, attempt_id, candidate, worker_created=False, gateway_created=True)
                    late_written.set()
                    raise asyncio.CancelledError()
                attempts['b'] = attempt_id
                # B 已完成 takeover+commit A 无记录+清槽，再派发新 ensure。
                release.set()
                await late_written.wait()
                try:
                    service._ensure_attempt_available(ws, attempt_id)
                except h.provisioner.ProvisionerError as e:
                    raise h.RuntimeUnavailable(str(e), 503, retry_after=1)
                raise AssertionError('B 本应被 A pending 阻挡')
        adapter = Adapter()
        a = h.RuntimeManager(store, adapter, owner_id='live-a', gateway_required=True, clock=lambda:1201)
        b = h.RuntimeManager(store, adapter, owner_id='live-b', gateway_required=True, clock=lambda:1201)
        task = asyncio.create_task(a.ensure_ready(ws,1))
        await entered.wait()
        reserved = store.get_worker_lease(ws)
        assert reserved.state == 'starting' and reserved.lease_expires_at <= 1201
        try:
            await b.ensure_ready(ws,1)
        except h.RuntimeUnavailable as e:
            print('B first error:', e.code)
        await asyncio.gather(task, return_exceptions=True)
        for i in range(3):
            try:
                await b.ensure_ready(ws,1)
            except h.RuntimeUnavailable as e:
                print('B retry',i, e.code)
            else:
                raise AssertionError('unexpected recovery')
        final=store.get_worker_lease(ws)
        record=service._load_attempt(ws)
        print(json.dumps({'attempts':attempts,'db_state':final.state,'db_attempt':final.attempt_id,
            'pending_slot':final.pending_attempt_id,'remote_attempt':record['attempt_id'],'remote_status':record['status']}))
        assert record['status']=='pending' and record['attempt_id']==attempts['a']
        assert final.attempt_id != attempts['a'] and final.pending_attempt_id == ''
        print('REPRODUCED: A pending 归属已从 DB 丢失，连续普通首屏无法恢复')
with tempfile.TemporaryDirectory(prefix='easel-r4r1-live-') as d:
    asyncio.run(main(pathlib.Path(d)))
