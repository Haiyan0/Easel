"""复审：同一请求标识在运行中改稿是否被拒绝；仅临时受控发布链。"""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
spec = importlib.util.spec_from_file_location("review_publish_chain", root / "tests/multiuser/test_task5c3_chain.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
from pytest import MonkeyPatch
from fastapi.testclient import TestClient

with tempfile.TemporaryDirectory(prefix="easel-review-request-") as directory:
    patch = MonkeyPatch()
    chain = fixture.Chain(Path(directory), patch)
    chain.media()
    chain.fake.mode = "slow"
    try:
        with TestClient(chain.app, base_url=fixture.ORIGIN) as client:
            csrf = chain.login(client)
            original = fixture._publish_body(request_id="review-same-request")
            first = client.post("/api/publish/xiaohongshu", json=original, headers=fixture._headers(csrf))
            changed = {**original, "title": "修改后的标题", "body": "修改后的正文"}
            second = client.post("/api/publish/xiaohongshu", json=changed, headers=fixture._headers(csrf))
            result = {"first_status": first.status_code, "changed_content_status": second.status_code,
                      "same_run": first.json().get("runId") == second.json().get("runId")}
            stopped = client.post("/api/chat/stop", json={"runId": first.json()["runId"]}, headers=fixture._headers(csrf))
            result["cleanup_stop_status"] = stopped.status_code
    finally:
        asyncio.run(chain.close())
        chain.store.close()
        patch.undo()
result["temporary_directory_cleaned"] = not Path(directory).exists()
print(json.dumps(result, ensure_ascii=False, indent=2))
