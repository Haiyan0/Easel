"""Task5C1主审：真实基线到relay的增强指令与正式factory模型一致性。

只使用临时Store/workspace、真实worker基线handler、GatewayFake和HTTP捕获。
不操作Docker/用户材料，不请求真实供应商，不执行Agent。
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.repo).resolve()))
    import httpx
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from web import app as worker
    from easel import persona
    from easel.control.store import Store
    from easel.control.sessions import SessionService
    from easel.control.auth import AuthService
    from easel.control.config import ConfigService
    from easel.control.broker import BrokerService
    from easel.control.runtime import RuntimeManager
    from easel.control.cli import build_relay_factory
    from easel.control.workspace_tasks import WorkspaceTasks
    from tests.multiuser.test_runtime_gateway import GatewayFake
    from tests.multiuser.test_managed_chat_model import CATALOG, MASTER_KEY, chat_route

    class CaptureRelay:
        def __init__(self):
            self.payload = None
        async def submit_task(self, actor, task_type, payload):
            self.payload = payload
            return {'runId': 'fixture-run', 'state': 'accepted', 'duplicate': False}

    class RealBaseline:
        async def request(self, actor, method, path, *, payload, **kwargs):
            result = await worker.api_profile_baseline(
                worker.ProfileBaselineRequest(**payload))
            return httpx.Response(200, json=result)

    async def prompt_case(root):
        profiles = root / 'profiles'
        profiles.mkdir()
        previous = worker.PROFILES_DIR, persona.PROFILES_DIR
        worker.PROFILES_DIR = persona.PROFILES_DIR = profiles
        try:
            with Store.open(root / 'prompt-db') as store:
                actor = store.create_user('review-member')
                relay = CaptureRelay()
                tasks = WorkspaceTasks(store, SessionService(store), relay, RealBaseline())
                result = await tasks.build_profile(actor, {
                    'name': 'review-profile', 'form': {'direction': 'review-direction'},
                    'turnId': 'stable-profile-review'})
                prompt = relay.payload['message']
                print('BASELINE_SIX_FILES=' + str(len(list((profiles / 'review-profile').glob('*.md')))))
                print('FORM_FIELDS_REACHED_RELAY=' + str('review-direction' in prompt))
                print('EXECUTE_PROFILE_SKILL_DIRECTIVE_PRESENT=' + str('/skill-profile-builder' in prompt))
                print('TARGET_PROFILE_DIRECTORY_PRESENT=' + str('profiles/review-profile' in prompt))
                print('ENHANCE_FILES_DIRECTIVE_PRESENT=' + str('完善已存在' in prompt or '润色/补全' in prompt))
                print('PROFILE_ADMISSION_STATUS=' + result['status'])
        finally:
            worker.PROFILES_DIR, persona.PROFILES_DIR = previous

    async def model_case(root):
        with Store.open(root / 'model-db') as store:
            admin = store.create_user('Owner', 'admin')
            actor = store.create_user('review-member')
            config = ConfigService(store, MASTER_KEY, model_catalog=CATALOG)
            config.publish(admin, {'defaults': {'chat': chat_route('deepseek-flash')}}, expected_version=0)
            broker = BrokerService(store, config)
            runtime = RuntimeManager(store, GatewayFake(store), gateway_required=True)
            posts = []
            async def capture(request):
                posts.append(json.loads(await request.aread()))
                return httpx.Response(202, json={'state': 'accepted'}, request=request)
            async with httpx.AsyncClient(transport=httpx.MockTransport(capture)) as client:
                factory = build_relay_factory(
                    signing_key=Ed25519PrivateKey.generate(), revision=7,
                    agent={'runtime': 'acp', 'command': ['managed-agent', '--acp'],
                           'model': 'deepseek-chat', 'models': list(CATALOG),
                           'sandbox': 'read-only', 'timeout': 60},
                    broker_config={'mode': 'workspace-sidecar', 'channel': 'chat'},
                    gateway_required=True)
                relay = factory(runtime, SessionService(store), AuthService(store),
                                store, client, broker, None)
                for kind in ('skill', 'profile'):
                    conversation = store.create_conversation(actor)
                    result = await relay.submit_task(actor, kind, {
                        'sessionId': conversation.id, 'turnId': 'review-' + kind,
                        'message': 'fixture ' + kind})
                    envelope = posts[-1]['envelope']
                    route = broker.authorize(envelope['routes']['broker_token'],
                        actor.workspace_id, result['runId'], 'chat')
                    print(kind.upper() + '_SIGNED_AGENT_MODEL=' + str(envelope['agent'].get('model')))
                    print(kind.upper() + '_ISSUED_BROKER_MODEL=' + route.model)
                    print(kind.upper() + '_MODEL_MATCH=' + str(envelope['agent'].get('model') == route.model))
    with tempfile.TemporaryDirectory(prefix='easel-5c1-review-') as temp:
        root = Path(temp)
        asyncio.run(prompt_case(root))
        asyncio.run(model_case(root))
    print('TEMP_CLEANUP=COMPLETE')

if __name__ == '__main__':
    main()
