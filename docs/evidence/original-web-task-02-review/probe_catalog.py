"""主审反例：运行时目录与固定镜像 manifest 分叉仍可发布和派发。

只使用临时真实 Store、假 Key、内存 HTTP transport，不运行容器或供应商。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKTREE = ROOT / ".claude/worktrees/original-web-task1-diagnostics"
sys.path.insert(0, str(WORKTREE))

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from easel.control.auth import AuthService
from easel.control.broker import BrokerService
from easel.control.config import ConfigService
from easel.control.proxy import BrokerDestination, PinnedRunConfig, WorkerRelay
from easel.control.sessions import SessionService
from easel.control.snapshot import load_managed_agent_config
from easel.control.store import Store
from tests.multiuser.test_managed_chat_model import (
    DispatchCapture, MASTER_KEY, REVISION, StaticRuntime, make_agent, publish_chat,
)


def main() -> None:
    catalog = ("not-in-worker-image-catalog",)
    manifest = json.loads((WORKTREE / "deploy/multiuser/managed/acp-profile.json")
                          .read_text(encoding="utf-8"))
    print("MODEL_IN_IMAGE_CATALOG=" + str(catalog[0] in
                                         manifest["provider"]["models"]))
    with tempfile.TemporaryDirectory(prefix="easel-task2-review-") as directory:
        managed = Path(directory) / "managed-agent.json"
        managed.write_text(json.dumps({
            "revision": REVISION, "agent": make_agent(catalog),
            "broker": {"mode": "workspace-sidecar", "channel": "chat"},
        }), encoding="utf-8")
        managed.chmod(0o600)
        _, loaded_agent, _ = load_managed_agent_config(managed)
        catalog = tuple(loaded_agent["models"])
        print("CONFIG_LOAD_ACCEPTED=True")
        with Store.open(Path(directory) / "control.sqlite3") as store:
            admin = store.create_user("Owner", "admin")
            member = store.create_user("Member")
            conversation = store.create_conversation(member, "catalog probe")
            config = ConfigService(store, MASTER_KEY, model_catalog=catalog)
            print("PUBLISH_VERSION=" + str(publish_chat(
                config, admin, catalog[0])))
            broker = BrokerService(store, config, clock=lambda: 1000.0,
                                   model_catalog=catalog)
            broker.ensure_issuable(member, "chat")
            print("PREFLIGHT_ACCEPTED=True")
            capture = DispatchCapture()
            key = Ed25519PrivateKey.generate()

            def provider(_actor):
                return PinnedRunConfig(
                    revision=REVISION, routes={}, agent=loaded_agent,
                    broker=BrokerDestination("http://broker.internal", "chat"))

            async def scenario():
                async with httpx.AsyncClient(transport=capture) as client:
                    relay = WorkerRelay(
                        StaticRuntime(), SessionService(store), AuthService(store),
                        store=store, signing_key=key, config_provider=provider,
                        http_client=client, broker=broker, clock=lambda: 1000.0,
                        system_clock=lambda: 1000.0)
                    return await relay.submit(member, {
                        "sessionId": conversation.id, "turnId": "catalog-probe",
                        "message": "fixture only"})

            result = asyncio.run(scenario())
            print("SUBMITTED=" + str(bool(result.get("runId"))))
            print("DISPATCHES=" + str(len(capture.posts)))
            if capture.posts:
                agent = capture.posts[0]["body"]["envelope"]["agent"]
                print("SIGNED_MODEL=" + agent["model"])
    print("TEMP_CLEANUP=ok")


if __name__ == "__main__":
    main()
