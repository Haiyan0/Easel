"""A3：合并 worker server 的 lifecycle 路由与执行能力标记。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from easel.worker.guard import SERVICE_TOKEN_HEADER, WORKSPACE_HEADER
from easel.worker.lifecycle import LIFECYCLE_PATH, LifecycleConfig, RequestCounter
from easel.worker.server import _attach_lifecycle

WORKSPACE = "a" * 32
TOKEN = "worker-server-token"
HEADERS = {WORKSPACE_HEADER: WORKSPACE, SERVICE_TOKEN_HEADER: TOKEN}


class _Runs:
    def active_runs(self) -> int:
        return 2


def test_combined_lifecycle_reports_runmanager_and_execution_capability():
    config = LifecycleConfig(WORKSPACE, 4, "easel-worker-example", TOKEN)
    app = FastAPI()
    _attach_lifecycle(app, config, RequestCounter(), _Runs())
    with TestClient(app, base_url="http://127.0.0.1:7860") as client:
        unauthenticated = client.get(LIFECYCLE_PATH)
        response = client.get(LIFECYCLE_PATH, headers=HEADERS)
    assert unauthenticated.status_code == 401
    assert response.status_code == 200
    payload = response.json()
    assert payload["workspace_id"] == WORKSPACE
    assert payload["config_version"] == 4
    assert payload["container_id"] == "easel-worker-example"
    assert payload["active_runs"] == 2
    assert payload["execution_capable"] is True
    assert TOKEN not in str(payload)
