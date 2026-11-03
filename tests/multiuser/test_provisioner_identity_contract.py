"""更新引导的身份契约：配置相同也必须能识别旧代码进程。"""
from __future__ import annotations

import hashlib
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
IDENTITY_PATH = ROOT / "deploy" / "desktop" / "provisioner_identity.py"
PROVISIONER_PATH = ROOT / "deploy" / "multiuser" / "provisioner.py"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


identity_contract = _load("provisioner_identity_contract", IDENTITY_PATH)
provisioner = _load("provisioner_identity_provisioner", PROVISIONER_PATH)


def _expected(**overrides):
    expected = {
        "provisionerSourceSha256": "a" * 64,
        "provisionerActions": [
            "ensure", "restore_gateway", "commit_attempt", "recover_abort"],
    }
    expected.update(overrides)
    return expected


def _identity(**overrides):
    observed = {
        "protocol": 2,
        "sourceSha256": "a" * 64,
        "actions": [
            "ensure", "stop", "inspect", "activate", "abort",
            "restore_gateway", "commit_attempt", "recover_abort", "identity"],
        "engine": {"context": "default", "id": "engine"},
        "images": {"worker": "sha256:" + "b" * 64,
                   "gateway": "sha256:" + "c" * 64},
        "workspaceRoot": "/mnt/c/Easel/workspaces",
        "networks": {"sidecar": "net", "gatewayControl": "net"},
        "brokerUpstream": "control:7861",
        "keys": {"workerPublicSha256": "d" * 64,
                 "gatewayPublicSha256": "e" * 64},
        "policySha256": "f" * 64,
        "socket": {"uid": 1000, "gid": 1000, "mode": "660",
                   "allowedUids": [10001]},
    }
    observed.update(overrides)
    return observed


def _full_expected():
    return _expected(
        engineContext="default", engineId="engine",
        workerImage="sha256:" + "b" * 64,
        gatewayImage="sha256:" + "c" * 64,
        workspaceRoot="/mnt/c/Easel/workspaces",
        sidecarNetwork="net", gatewayControlNetwork="net",
        brokerUpstream="control:7861",
        workerPublicSha256="d" * 64,
        gatewayPublicSha256="e" * 64,
        policySha256="f" * 64,
        socketUid=1000, socketGid=1000, socketMode="660",
        allowedUid=10001)


def test_identity_contract_detects_stale_process_code():
    """旧进程配置完全一致但源码/动作能力落后时必须报漂移。"""
    mismatches = identity_contract.compare_identity(
        _full_expected(), _identity(sourceSha256="0" * 64,
                                    actions=["ensure", "stop"]))
    assert mismatches == ["provisionerSourceSha256", "provisionerActions"]


def test_identity_contract_accepts_current_process():
    assert identity_contract.compare_identity(
        _full_expected(), _identity()) == []


def test_config_drift_is_not_treated_as_stale_only():
    observed = _identity(images={"worker": "sha256:" + "0" * 64,
                                 "gateway": "sha256:" + "c" * 64})
    mismatches = identity_contract.compare_identity(_full_expected(), observed)
    assert "workerImage" in mismatches


def test_provisioner_reports_loaded_source_and_actions():
    digest = hashlib.sha256(PROVISIONER_PATH.read_bytes()).hexdigest()
    assert provisioner.SOURCE_SHA256 == digest
    for action in ("ensure", "restore_gateway", "commit_attempt",
                   "recover_abort", "identity"):
        assert action in provisioner.ACTIONS
