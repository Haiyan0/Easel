"""A3 R2 真实 Docker CLI 超时与 cidfile 定向清理。

显式设置 ``EASEL_RUN_CONTAINER_TESTS=1``。测试用临时 cmd 包装器让真实
``docker run -d`` 创建容器后延迟返回，从而触发 Provisioner 的 CLI 超时；
随后按 Docker 写出的 cidfile 实例 ID 清理，不按同名容器兜底删除。
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = pathlib.Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "deploy" / "multiuser" / "provisioner.py"

pytestmark = pytest.mark.skipif(
    os.environ.get("EASEL_RUN_CONTAINER_TESTS") != "1",
    reason="设置 EASEL_RUN_CONTAINER_TESTS=1 才运行真实 Docker 清理测试")


def _load_provisioner():
    spec = importlib.util.spec_from_file_location(
        "easel_provisioner_cleanup_real", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


provisioner = _load_provisioner()


def _docker(*args: str, timeout: float = 60.0,
            check: bool = True) -> subprocess.CompletedProcess:
    binary = shutil.which("docker")
    if binary is None:
        pytest.fail("找不到 docker")
    result = subprocess.run([binary, *args], capture_output=True, text=True,
                            check=False, timeout=timeout)
    if check and result.returncode != 0:
        pytest.fail(f"docker {' '.join(args[:3])} 失败："
                    f"{result.stderr[-2000:]}")
    return result


def _write_wrapper(path: pathlib.Path) -> pathlib.Path:
    """run 创建容器后延迟返回；inspect/rm 等管理命令立即透传。"""
    path.write_text(
        "@echo off\r\n"
        'if /I "%~1"=="run" (\r\n'
        "  docker %*\r\n"
        "  ping -n 8 127.0.0.1 >nul\r\n"
        "  exit /b 0\r\n"
        ")\r\n"
        "docker %*\r\n",
        encoding="ascii")
    return path


def _image_id(tag: str) -> str:
    result = _docker("image", "inspect", "--format", "{{.Id}}", tag,
                     check=False)
    image = result.stdout.strip()
    if result.returncode != 0 or re.fullmatch(
            r"sha256:[0-9a-f]{64}", image) is None:
        pytest.fail(f"缺少本地固定测试镜像 {tag}")
    return image


def _wait_for_cidfile(path: pathlib.Path, timeout: float = 10.0
                      ) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            value = path.read_text(encoding="ascii").strip()
            if re.fullmatch(r"[0-9a-f]{64}", value):
                return value
        time.sleep(0.1)
    pytest.fail("真实 docker run 超时后未写出 cidfile")


def test_real_docker_cli_timeout_keeps_container_then_cidfile_cleanup(
        tmp_path: pathlib.Path) -> None:
    workspace_id = uuid.uuid4().hex
    image = _image_id("python:3.12-slim")
    root = tmp_path / "root"
    root.mkdir()
    paths = provisioner.prepare_workspace(root, workspace_id)
    key = tmp_path / "worker.pub"
    key.write_bytes(Ed25519PrivateKey.generate().public_key()
                    .public_bytes_raw())
    token = "real-timeout-" + uuid.uuid4().hex
    env_file = provisioner.write_env_file(
        root, workspace_id, 1, token, paths)
    cidfile = tmp_path / "run.cid"
    wrapper = _write_wrapper(tmp_path / "docker.cmd")
    settings = provisioner.ProvisionerSettings(
        image=image, workspace_root=root,
        socket_path=tmp_path / "provisioner.sock",
        quota_mode="none", allow_unquotaed=True, docker=str(wrapper),
        worker_public_key=key)
    service = provisioner.Provisioner(settings)
    network = provisioner.network_name(workspace_id)
    worker_name = provisioner.container_name(workspace_id)
    instance_id = ""
    try:
        _docker("network", "create", "--internal",
                "--label", "easel.managed=true",
                "--label", f"easel.workspace-id={workspace_id}", network)
        argv = provisioner.build_run_argv(
            image=image, workspace_id=workspace_id, config_version=1,
            paths=paths, env_file=env_file, public_key_file=key,
            cidfile=cidfile)
        with pytest.raises(subprocess.TimeoutExpired):
            asyncio.run(service._docker(*argv, timeout=3.0))
        instance_id = _wait_for_cidfile(cidfile)
        assert _docker("container", "inspect", instance_id,
                       check=False).returncode == 0
        assert _docker("container", "inspect", instance_id,
                       check=False).stdout
        # CLI 超时后实例确实已创建；按 cidfile ID 清理，而不是按名。
        asyncio.run(service._cleanup_start_attempt(
            workspace_id, cidfile, None, instance_id))
        assert not cidfile.exists()
        assert _docker("container", "inspect", instance_id,
                       check=False).returncode != 0
        assert _docker("container", "inspect", worker_name,
                       check=False).returncode != 0
    finally:
        if instance_id:
            _docker("container", "rm", "-f", instance_id, check=False)
        _docker("container", "rm", "-f", worker_name, check=False)
        _docker("network", "rm", network, check=False)
