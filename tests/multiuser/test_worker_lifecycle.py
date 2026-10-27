"""Task 8：worker 生命周期健康入口，不触发任务或快照接纳。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from easel.control.types import DomainError
from easel.worker.guard import (
    SERVICE_MODE_ENV,
    SERVICE_TOKEN_ENV,
    SERVICE_TOKEN_HEADER,
    WORKSPACE_HEADER,
    WORKSPACE_ID_ENV,
)
from easel.worker.lifecycle import (
    CONFIG_VERSION_ENV,
    CONTAINER_ID_ENV,
    LIFECYCLE_PATH,
    LifecycleDraining,
    RequestCounter,
    build_payload,
    create_lifecycle_app,
    load_lifecycle_config,
)

WORKSPACE = "a" * 32
TOKEN = "service-token-lifecycle"
CONTAINER = "container-abc"
HEADERS = {WORKSPACE_HEADER: WORKSPACE, SERVICE_TOKEN_HEADER: TOKEN}


def _env(**overrides):
    env = {
        SERVICE_MODE_ENV: "worker",
        WORKSPACE_ID_ENV: WORKSPACE,
        SERVICE_TOKEN_ENV: TOKEN,
        CONFIG_VERSION_ENV: "7",
        CONTAINER_ID_ENV: CONTAINER,
    }
    env.update(overrides)
    return {key: value for key, value in env.items() if value is not None}


def test_load_lifecycle_config_requires_worker_identity_and_version():
    config = load_lifecycle_config(_env())
    assert config.workspace_id == WORKSPACE
    assert config.config_version == 7
    assert config.container_id == CONTAINER
    assert TOKEN not in repr(config)

    for missing in (SERVICE_MODE_ENV, WORKSPACE_ID_ENV, SERVICE_TOKEN_ENV,
                    CONFIG_VERSION_ENV, CONTAINER_ID_ENV):
        with pytest.raises((RuntimeError, DomainError)):
            load_lifecycle_config(_env(**{missing: None}))
    with pytest.raises(DomainError):
        load_lifecycle_config(_env(**{CONFIG_VERSION_ENV: "-1"}))
    with pytest.raises(DomainError):
        load_lifecycle_config(_env(**{CONTAINER_ID_ENV: "bad\nid"}))


def test_payload_contains_identity_and_never_the_service_token():
    config = load_lifecycle_config(_env())
    counter = RequestCounter()
    payload = build_payload(config, counter)
    assert payload == {
        "workspace_id": WORKSPACE,
        "config_version": 7,
        "container_id": CONTAINER,
        "ready": True,
        "draining": False,
        "active_requests": 0,
        "active_runs": 0,
        "execution_capable": False,
    }
    assert TOKEN not in repr(payload)


def test_request_counter_blocks_new_requests_while_draining():
    counter = RequestCounter()
    with counter.track():
        assert counter.snapshot() == (1, False)
    assert counter.snapshot() == (0, False)
    counter.start_drain()
    assert counter.snapshot() == (0, True)
    with pytest.raises(LifecycleDraining), counter.track():
        pass


def test_health_endpoint_requires_internal_credentials():
    config = load_lifecycle_config(_env())
    app = create_lifecycle_app(config, RequestCounter())
    with TestClient(app, base_url="http://127.0.0.1:7860") as client:
        assert client.get(LIFECYCLE_PATH).status_code == 401
        assert client.get(LIFECYCLE_PATH, headers={
            WORKSPACE_HEADER: "b" * 32,
            SERVICE_TOKEN_HEADER: TOKEN}).status_code == 401
        assert client.get(LIFECYCLE_PATH, headers={
            WORKSPACE_HEADER: WORKSPACE,
            SERVICE_TOKEN_HEADER: "wrong"}).status_code == 401
        response = client.get(LIFECYCLE_PATH, headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {
        "workspace_id": WORKSPACE,
        "config_version": 7,
        "container_id": CONTAINER,
        "ready": True,
        "draining": False,
        "active_requests": 0,
        "active_runs": 0,
        "execution_capable": False,
    }


def test_health_endpoint_reports_draining_and_active_requests():
    config = load_lifecycle_config(_env())
    counter = RequestCounter()
    app = create_lifecycle_app(config, counter)
    with TestClient(app, base_url="http://127.0.0.1:7860") as client:
        with counter.track():
            active = client.get(LIFECYCLE_PATH, headers=HEADERS).json()
        assert active["active_requests"] == 1
        assert active["ready"] is True
        counter.start_drain()
        draining = client.get(LIFECYCLE_PATH, headers=HEADERS).json()
    assert draining["draining"] is True
    assert draining["ready"] is False


def test_payload_reports_real_active_runs_and_execution_capability():
    config = load_lifecycle_config(_env())
    counter = RequestCounter()
    payload = build_payload(
        config, counter, active_runs=2, execution_capable=True)
    assert payload["active_runs"] == 2
    assert payload["active_requests"] == 0
    assert payload["execution_capable"] is True
    assert payload["ready"] is True


def test_lifecycle_only_healthcheck_is_not_a_ready_execution_worker(monkeypatch):
    from easel.worker import lifecycle
    config = load_lifecycle_config(_env())
    payload = build_payload(config, RequestCounter())
    assert payload["execution_capable"] is False

    class _Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            import json
            return json.dumps(payload).encode()

    monkeypatch.setattr(lifecycle, "urlopen", lambda *args, **kwargs: _Response())
    assert lifecycle._healthcheck(config, 7860) == 1
