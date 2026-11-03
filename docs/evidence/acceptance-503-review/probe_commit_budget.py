"""假 provisioner 延迟提交，核对 ready 预算；不运行容器。"""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time


async def scenario(root: Path, temp: Path) -> None:
    sys.path.insert(0, str(root))
    from easel.control.runtime import RuntimeManager
    from easel.control.store import Store
    spec = importlib.util.spec_from_file_location(
        "review_runtime_helpers", root / "tests/multiuser/test_runtime_gateway.py")
    helpers = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = helpers
    spec.loader.exec_module(helpers)
    with Store.open(temp / "control.sqlite3") as store:
        workspace = store.create_user("temporary-budget").workspace_id
        fake = helpers.GatewayFake(store)

        async def delayed_commit(*args, **kwargs):
            await asyncio.sleep(0.15)

        fake.commit_attempt = delayed_commit
        runtime = RuntimeManager(store, fake, gateway_required=True,
                                 ready_timeout=0.02, poll_interval=0.001)
        start = time.monotonic()
        await runtime.ensure_ready(workspace, 1)
        print(json.dumps({"ready_budget_seconds": 0.02,
                          "elapsed_seconds": round(time.monotonic() - start, 3),
                          "outcome": "success"}))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="easel-budget-review-") as directory:
        asyncio.run(scenario(Path(sys.argv[1]).resolve(), Path(directory)))
    print("TEMP_CLEANUP=ok; LIVE_RESOURCES_TOUCHED=false")
