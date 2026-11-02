"""5C2 主审：旧登录状态提前终结新Run、凭证验证迟到写入。

真实control/gateway/worker/RunManager，平台仅用现有Chain假执行器；
只写TemporaryDirectory，不读真实材料、不启动Docker或平台浏览器。
"""
import argparse
import asyncio
from pathlib import Path
import sys
import tempfile
import threading


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', required=True)
    parser.add_argument('--case', choices=('stale-login', 'credentials-stop', 'all'), default='all')
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.repo).resolve()))
    import httpx
    import pytest
    from easel.control.store import Store
    from web import app as worker
    from tests.multiuser.test_task5c2_chain import Chain, ORIGIN, PLATFORM

    async def authenticate(chain, client):
        invite = chain.auth.create_invite(chain.admin, chain.member.user_id)
        response = await client.post('/api/auth/exchange',
            json={'token': invite}, headers={'Origin': ORIGIN})
        assert response.status_code == 200
        return {'Origin': ORIGIN, 'X-CSRF-Token': response.json()['csrfToken']}

    async def stale_login(root):
        patch = pytest.MonkeyPatch()
        chain = Chain(root, patch)
        gate = asyncio.Event()
        entered = asyncio.Event()
        original = chain.manager._browser_executor
        async def paused(*arguments):
            entered.set()
            await gate.wait()
            return await original(*arguments)
        chain.manager._browser_executor = paused
        chain.fake._write_status(PLATFORM, 'success', 'PREVIOUS_LOGIN_SUCCESS')
        try:
            async with chain.app.router.lifespan_context(chain.app):
                async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=chain.app), base_url=ORIGIN) as client:
                    headers = await authenticate(chain, client)
                    response = await client.post('/api/login/' + PLATFORM,
                        json={'requestId': 'new-request'}, headers=headers)
                    result = response.json()
                    run_id = result['runId']
                    await asyncio.wait_for(entered.wait(), 2)
                    with Store.open(chain.store.path) as check:
                        state = check.get_run(chain.member, run_id).status
                        active = check.count_active_runs_for_workspace(chain.member.workspace_id)
                    print('LOGIN_RESPONSE_STATUS=' + str(response.status_code))
                    print('LOGIN_RESPONSE_STATE=' + result['state'])
                    print('NEW_LOGIN_SCRIPT_STARTS=' + str(len(chain.fake.login_starts)))
                    print('CONTROL_RUN_STATE=' + state)
                    print('WORKER_RUN_STATE=' + str(chain.manager.state(run_id)))
                    print('CONTROL_ACTIVE_RUNS=' + str(active))
                    await chain.manager.stop(run_id)
            await chain.close()
        finally:
            gate.set()
            chain.store.close()
            patch.undo()

    async def credentials_stop(root):
        patch = pytest.MonkeyPatch()
        chain = Chain(root, patch)
        gate = threading.Event()
        entered = threading.Event()
        def verify(app_id, secret):
            entered.set()
            if not gate.wait(5):
                raise AssertionError('bounded verification gate timed out')
            return True, ''
        patch.setattr(worker, '_wechat_verify_credentials', verify)
        try:
            async with chain.app.router.lifespan_context(chain.app):
                async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=chain.app), base_url=ORIGIN) as client:
                    headers = await authenticate(chain, client)
                    pending = asyncio.create_task(client.post(
                        '/api/accounts/wechat-oa/credentials',
                        json={'appId': 'wx-review-app', 'appSecret': 'FAKE-SECRET',
                              'name': 'review'}, headers=headers))
                    if not await asyncio.to_thread(entered.wait, 3):
                        raise AssertionError('verification did not start')
                    with Store.open(chain.store.path) as check:
                        run_id = check._connection.execute(
                            'SELECT id FROM runs ORDER BY created_at DESC LIMIT 1').fetchone()[0]
                    stopped = await client.post('/api/chat/stop',
                        json={'runId': run_id}, headers=headers)
                    print('STOP_HTTP_STATUS=' + str(stopped.status_code))
                    print('STOP_STATE=' + str(stopped.json().get('state')))
                    print('WORKER_STATE_BEFORE_VERIFY_RELEASE=' + str(chain.manager.state(run_id)))
                    print('CONFIG_BEFORE_VERIFY_RELEASE=' + str((root / 'wechat.yaml').exists()))
                    gate.set()
                    response = await asyncio.wait_for(pending, 4)
                    print('CREDENTIAL_POST_AFTER_STOP_HTTP=' + str(response.status_code))
                    print('CONFIG_SAVED_AFTER_STOP=' + str((root / 'wechat.yaml').is_file()))
                    print('CREDENTIALS_CONFIGURED_AFTER_STOP=' + str(worker._wechat_has_credentials()))
                    print('LOGIN_MARKER_AFTER_STOP=' + str(worker._login_status('wechat-oa')['state']))
            await chain.close()
        finally:
            gate.set()
            chain.store.close()
            patch.undo()

    cases = [('stale-login', stale_login), ('credentials-stop', credentials_stop)]
    for name, scenario in cases:
        if args.case not in (name, 'all'):
            continue
        print('CASE=' + name)
        with tempfile.TemporaryDirectory(prefix='easel-5c2-review-') as temporary:
            asyncio.run(scenario(Path(temporary)))
        print('TEMP_CLEANUP=COMPLETE')

if __name__ == '__main__':
    main()
