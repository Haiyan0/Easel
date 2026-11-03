"""纯临时资源：远端写 pending 后控制进程直接退出，再重开恢复。"""
import asyncio, dataclasses, importlib.util, json, os, pathlib, subprocess, sys, tempfile
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
spec = importlib.util.spec_from_file_location('probe_helpers', root / 'tests/multiuser/test_attempt_ownership_503.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
async def inspect(workspace_id):
    return {'managed': True, 'workspace_id': workspace_id, 'config_version': 1,
            'instance_id': h.WORKER, 'labels': {'easel.managed': 'true',
            'easel.workspace-id': workspace_id, 'easel.config-version': '1'},
            'worker_ready': True, 'gateway_available': False}
async def child(path):
    store, service, gateway, workspace, lease, handle, original = h._seed_expired_ensure(path)
    with store:
        manager = h.RuntimeManager(store, h._lost_response_adapter(service, handle, h.GATEWAY, {'count': 0}), owner_id='owner-b', gateway_required=True, clock=lambda: 1200)
        await manager.ensure_ready(workspace, 1)
        assert store.get_worker_lease(workspace).state == 'ready'
        (path / 'meta.json').write_text(json.dumps({'workspace': workspace, 'handle': dataclasses.asdict(handle)}))
        adapter = h._lost_response_adapter(service, handle, 'f'*64, {'count': 1})
        remote = adapter.ensure
        async def crash(*args, **kwargs):
            try:
                await remote(*args, **kwargs)
            except asyncio.CancelledError:
                # 此时远端已落盘；直接退出，不进入 runtime 的异常处理。
                os._exit(73)
        adapter.ensure = crash
        adapter.inspect = inspect
        await h.RuntimeManager(store, adapter, owner_id='owner-b', gateway_required=True, clock=lambda: 1201).ensure_ready(workspace, 1)
async def recover(path):
    meta = json.loads((path / 'meta.json').read_text())
    workspace, handle = meta['workspace'], h.WorkerHandle(**meta['handle'])
    service = h._service(path, h.FakeGateway(config_version=1))
    for stamp in (1202, 2000, 4000):
        with h.Store.open(path / 'control.sqlite3') as store:
            adapter = h._lost_response_adapter(service, handle, 'f'*64, {'count': 0})
            adapter.inspect = inspect
            try:
                await h.RuntimeManager(store, adapter, owner_id='owner-c', gateway_required=True, clock=lambda: stamp).ensure_ready(workspace, 1)
                raise AssertionError('意外恢复成功')
            except h.RuntimeUnavailable as error:
                lease = store.get_worker_lease(workspace)
                record = service._load_attempt(workspace)
                print(stamp, error.code, getattr(error.__cause__, 'code', None), lease.state, lease.attempt_id, record['status'], record['attempt_id'])
                assert lease.state == 'ready'
                assert record['status'] == 'pending'
                assert lease.attempt_id != record['attempt_id']
                assert getattr(error.__cause__, 'code', None) == 'gateway_cleanup_pending'
    print('CONFIRMED: 进程退出后重开DB，跨租约期限仍永久503')
if len(sys.argv) > 2:
    asyncio.run(child(pathlib.Path(sys.argv[2])))
else:
    with tempfile.TemporaryDirectory(prefix='easel-r4-crash-') as directory:
        result = subprocess.run([sys.executable, __file__, str(root), directory])
        assert result.returncode == 73, result.returncode
        asyncio.run(recover(pathlib.Path(directory)))
