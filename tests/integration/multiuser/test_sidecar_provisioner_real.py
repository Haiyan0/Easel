"""Task 8 真实 Linux sidecar 生命周期：创建/复用/严格 inspect/停止。"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import shutil
import subprocess
import sys
import uuid

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
DOCKERFILE = ROOT / "deploy" / "multiuser" / "Dockerfile.worker"
MODULE_PATH = ROOT / "deploy" / "multiuser" / "provisioner.py"
BASE_TAG = "easel-worker-test:task8"
SIDECAR_TAG = "easel-sidecar-test:task8"

pytestmark = pytest.mark.skipif(
    __import__("os").environ.get("EASEL_RUN_CONTAINER_TESTS") != "1",
    reason="设置 EASEL_RUN_CONTAINER_TESTS=1 才运行真实 sidecar 测试")


def _docker(*args: str, timeout: float = 120.0,
            check: bool = True) -> subprocess.CompletedProcess:
    binary = shutil.which("docker")
    if binary is None:
        pytest.fail("缺少 docker 可执行文件")
    result = subprocess.run([binary, *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, check=False,
                            timeout=timeout)
    if check and result.returncode != 0:
        pytest.fail(f"docker {' '.join(args[:3])} 失败："
                    f"{result.stderr.strip()[-2000:]}")
    return result


def _load_provisioner():
    spec = importlib.util.spec_from_file_location(
        "easel_provisioner_sidecar_real", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


provisioner = _load_provisioner()


@pytest.fixture(scope="module")
def sidecar_image() -> str:
    _docker("build", "-f", str(DOCKERFILE), "--target", "base",
            "-t", BASE_TAG, str(ROOT), timeout=1800)
    dockerfile = ROOT / ".scratch" / "Dockerfile.sidecar-real"
    dockerfile.parent.mkdir(exist_ok=True)
    dockerfile.write_text(
        f"FROM {BASE_TAG}\n"
        "USER root\n"
        "COPY skills/shared/scripts /opt/easel/skills/shared/scripts\n"
        "COPY deploy/multiuser/worker_sidecar.py "
        "/opt/easel/deploy/multiuser/worker_sidecar.py\n"
        "COPY deploy/multiuser/egress_proxy.py "
        "/opt/easel/deploy/multiuser/egress_proxy.py\n"
        "RUN chown -R root:root /opt/easel && chmod -R a-w /opt/easel\n",
        encoding="utf-8")
    _docker("build", "-f", str(dockerfile), "-t", SIDECAR_TAG, str(ROOT),
            timeout=600)
    return _docker("image", "inspect", "--format", "{{.Id}}",
                   SIDECAR_TAG).stdout.strip()


def test_real_sidecar_created_reused_verified_and_stopped(
        sidecar_image, tmp_path):
    workspace_id = uuid.uuid4().hex
    sidecar_network = f"easel-sidecar-out-{uuid.uuid4().hex[:10]}"
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    _docker("network", "create", sidecar_network)
    settings = provisioner.ProvisionerSettings(
        image=sidecar_image, workspace_root=tmp_path / "root",
        socket_path=tmp_path / "sock", quota_mode="none",
        allow_unquotaed=True, sidecar_network=sidecar_network,
        sidecar_policy=policy, broker_upstream="broker.invalid:9")
    service = provisioner.Provisioner(settings)
    try:
        async def scenario():
            await service._ensure_network(workspace_id)
            await service._ensure_sidecar(workspace_id)
            raw = await service._inspect_name(
                provisioner.sidecar_container_name(workspace_id))
            assert provisioner.verify_sidecar_inspect(
                raw, workspace_id=workspace_id, image=sidecar_image,
                policy_file=policy.resolve(),
                sidecar_network=sidecar_network,
                broker_upstream="broker.invalid:9") is True
            first_id = raw["Id"]
            await service._ensure_sidecar(workspace_id)
            again = await service._inspect_name(
                provisioner.sidecar_container_name(workspace_id))
            assert again["Id"] == first_id, "健康 sidecar 必须复用"
            await service._stop_sidecar(workspace_id)
            assert await service._inspect_name(
                provisioner.sidecar_container_name(workspace_id)) is None
        asyncio.run(scenario())
    finally:
        _docker("rm", "-f",
                provisioner.sidecar_container_name(workspace_id),
                check=False)
        _docker("network", "rm", provisioner.network_name(workspace_id),
                check=False)
        _docker("network", "rm", sidecar_network, check=False)
