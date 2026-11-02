"""Task5C1主审：基线已创建、Run尚未接纳期间的删除竞态。

真实control认证/路由/Store与worker基线/删除handler；继承既有FakeRelay，
其reserve_run/mark_running使用真实临时Store。只对基线HTTP返回设置有界gate。
不运行Agent，不操作Docker或用户材料。
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
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.repo).resolve()))
    import httpx
    from fastapi.testclient import TestClient
    from web import app as worker
    from easel import persona
    from easel.control.store import Store
    from tests.multiuser.test_workspace_tasks import (
        FakeWorkspace, _app_with_tasks, _login, ORIGIN)
    ready = threading.Event()
    release = threading.Event()

    class GatedBaseline(FakeWorkspace):
        async def request(self, actor, method, path, *, query=None,
                          payload=None, **kwargs):
            if path == '/api/profile/baseline':
                data = await worker.api_profile_baseline(
                    worker.ProfileBaselineRequest(**payload))
                ready.set()
                if not await asyncio.to_thread(release.wait, 5):
                    raise AssertionError('bounded baseline gate timed out')
                return httpx.Response(200, json=data)
            if method == 'DELETE' and path.startswith('/api/persona/'):
                data = await worker.api_persona_delete(path.split('/')[-1])
                return httpx.Response(200, json=data)
            return await super().request(actor, method, path,
                query=query, payload=payload, **kwargs)

    with tempfile.TemporaryDirectory(prefix='easel-5c1-race-review-') as temp:
        root = Path(temp)
        profiles = root / 'profiles'
        profiles.mkdir()
        previous = worker.PROFILES_DIR, persona.PROFILES_DIR
        worker.PROFILES_DIR = persona.PROFILES_DIR = profiles
        store = None
        thread = None
        try:
            store, auth, admin, member, relay, app = _app_with_tasks(
                root, GatedBaseline())
            relay.finish = False
            with TestClient(app, base_url=ORIGIN) as client:
                _, headers = _login(client, auth, admin, member)
                holder = {}
                def submit():
                    holder['response'] = client.post('/api/profile/build',
                        json={'name': 'race-profile',
                              'form': {'direction': 'review-direction'},
                              'turnId': 'stable-profile-review'},
                        headers=headers)
                thread = threading.Thread(target=submit)
                thread.start()
                if not ready.wait(5):
                    raise AssertionError('baseline did not reach gate')
                deleted = client.delete('/api/persona/race-profile',
                                        headers=headers)
                print('DELETE_DURING_PREPARATION_STATUS=' + str(deleted.status_code))
                release.set()
                thread.join(10)
                if thread.is_alive():
                    raise AssertionError('profile submission did not finish')
                built = holder['response']
                print('BUILD_AFTER_DELETE_STATUS=' + str(built.status_code))
                print('BUILD_AFTER_DELETE_STATE=' + str(built.json().get('status')))
                print('PROFILE_EXISTS_AFTER_ADMISSION=' + str((profiles / 'race-profile').exists()))
                print('RELAY_PROMPT_SUBMISSIONS=' + str(len(relay.calls)))
                with Store.open(store.path) as verify:
                    run = verify.latest_run_for_conversation(
                        member, built.json()['sessionId'])
                    print('LEDGER_RUN_STATE=' + run.status)
        finally:
            release.set()
            if thread is not None and thread.is_alive():
                thread.join(10)
            if store is not None:
                store.close()
            worker.PROFILES_DIR, persona.PROFILES_DIR = previous
    print('TEMP_CLEANUP=COMPLETE')

if __name__ == '__main__':
    main()
