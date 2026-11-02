"""Task 5A 主审探针：正式 factory 的 revision 与 workspace DB 初值交错。

只用临时真实 Store/RuntimeManager、既有 GatewayFake 和 HTTP MockTransport；
不运行 Docker、不请求真实地址、不读取用户配置。
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
    from easel.control.auth import AuthService
    from easel.control.cli import build_relay_factory, build_workspace_factory
    from easel.control.runtime import RuntimeManager
    from easel.control.sessions import SessionService
    from easel.control.snapshot import load_managed_agent_config
    from easel.control.store import Store
    from easel.control.types import DomainError
    from tests.multiuser.test_runtime_gateway import GatewayFake

    async def scenario(root):
        managed = root / 'managed-agent.json'
        managed.write_text(json.dumps({
            'revision': 7,
            'agent': {'runtime': 'acp', 'command': ['fixture-agent', '--acp'],
                      'models': ['deepseek-flash'], 'sandbox': 'read-only'},
            'broker': {'mode': 'workspace-sidecar', 'channel': 'chat'},
        }), encoding='utf-8')
        revision, agent, broker_config = load_managed_agent_config(managed)
        key = Ed25519PrivateKey.generate()
        with Store.open(root / 'control.sqlite3') as store:
            actor = store.create_user('review-member')
            provisioner = GatewayFake(store)
            runtime = RuntimeManager(store, provisioner, gateway_required=True)
            async with httpx.AsyncClient(
                    transport=httpx.MockTransport(lambda request:
                        httpx.Response(200, json=[], request=request))) as client:
                relay_factory = build_relay_factory(
                    signing_key=key, revision=revision, agent=agent,
                    broker_config=broker_config, gateway_required=True)
                relay = relay_factory(runtime, SessionService(store),
                    AuthService(store), store, client, None, None)
                pinned = relay._config_provider(actor)
                factory = build_workspace_factory(
                    signing_key=key, revision=revision, gateway_required=True)
                api = factory(runtime, store, client, None)
                await runtime.ensure_ready(actor.workspace_id, pinned.revision)
                conversation = store.create_conversation(actor)
                run = store.reserve_run(actor, conversation.id,
                    'review-request', 'review-digest', pinned.revision)
                store.mark_running(actor, run.id)
                print('MANAGED_REVISION=' + str(revision))
                print('FORMAL_RELAY_REVISION=' + str(pinned.revision))
                print('FORMAL_WORKSPACE_FACTORY_REVISION=' +
                      str(api._default_revision))
                print('WORKSPACE_RECORD_VERSION=' +
                      str(store.workspace_config_versions()[actor.workspace_id]))
                print('LIVE_CHAT_WORKER_VERSION=' +
                      str(store.get_worker_lease(actor.workspace_id).config_version))
                try:
                    await api.request(actor, 'GET', '/api/personas')
                    print('ACTIVE_CHAT_FILE_REQUEST=ACCEPTED')
                except DomainError as error:
                    print('ACTIVE_CHAT_FILE_REQUEST=' + error.code)
                store.finish_run(actor, run.id, 'done')
                await api.request(actor, 'GET', '/api/personas')
                print('AFTER_FILE_WORKER_VERSION=' +
                      str(store.get_worker_lease(actor.workspace_id).config_version))
                print('PROVISIONER_STOPS=' + str(len(provisioner.stop_calls)))
                print('PROVISIONER_ENSURES=' + str(provisioner.ensure_calls))
    with tempfile.TemporaryDirectory(prefix='easel-5a-factory-review-') as temp:
        asyncio.run(scenario(Path(temp)))
    print('TEMP_CLEANUP=COMPLETE')

if __name__ == '__main__':
    main()
