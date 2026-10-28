"""A4 R1-01：真实同 UID 容器中的 control 材料隔离。

仅输出布尔/路径结果，不打印 key 内容。测试资源使用随机名称并逐项删除。
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import uuid

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
CONTROL_TAG = "easel-control-test:a4"
GATEWAY_TAG = "easel-gateway-test:g2"
WORKER_TAG = "easel-worker-execution:a4"
LABEL = "easel.r1.material.test"

pytestmark = pytest.mark.skipif(
    os.environ.get("EASEL_RUN_CONTAINER_TESTS") != "1",
    reason="设置 EASEL_RUN_CONTAINER_TESTS=1 才运行真实材料隔离测试")


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    binary = shutil.which("docker")
    if binary is None:
        pytest.skip("本机没有 docker")
    result = subprocess.run(
        [binary, *args], capture_output=True, text=True, check=False,
        timeout=180)
    if check and result.returncode != 0:
        pytest.fail(
            f"docker {' '.join(args[:4])} 失败：{result.stderr[-2000:]}")
    return result


def _image(tag: str) -> str:
    result = _docker("image", "inspect", "--format", "{{.Id}}", tag,
                     check=False)
    if result.returncode != 0:
        pytest.fail(f"缺少固定镜像 {tag}")
    return result.stdout.strip()


def _readable(image: str, mounts: list[str],
              paths: list[str]) -> dict[str, bool]:
    script = "; ".join(
        f"if [ -r '{path}' ]; then echo 'yes:{path}'; "
        f"else echo 'no:{path}'; fi" for path in paths)
    command = [
        "run", "--rm", "--user", "10001:10001",
        "--label", f"{LABEL}=true",
        "--entrypoint", "sh",
    ]
    for mount in mounts:
        command.extend(["--mount", mount])
    command.extend([image, "-c", script])
    result = _docker(*command)
    observed: dict[str, bool] = {}
    for line in result.stdout.splitlines():
        if line.startswith(("yes:", "no:")):
            observed[line.split(":", 1)[1]] = line.startswith("yes:")
    for path in paths:
        assert path in observed, (path, result.stdout, result.stderr)
    return observed


def _create_volume(name: str) -> None:
    _docker("volume", "create", "--label", f"{LABEL}=true", name)


def _remove_volume(name: str) -> None:
    _docker("volume", "rm", "-f", name, check=False)


def _write_files(volume: str, image: str, files: dict[str, str]) -> None:
    lines = []
    for path, content in files.items():
        target = "/volume" + path
        parent = str(pathlib.PurePosixPath(target).parent)
        lines.append(f"mkdir -p '{parent}'")
        lines.append(f"printf '%s' '{content}' > '{target}'")
    _docker(
        "run", "--rm", "--user", "0:0", "--entrypoint", "sh",
        "--label", f"{LABEL}=true",
        "--mount", f"type=volume,src={volume},dst=/volume",
        image, "-c", " && ".join(lines))


def test_control_material_is_visible_only_to_control_uid():
    control = _image(CONTROL_TAG)
    gateway = _image(GATEWAY_TAG)
    worker = _image(WORKER_TAG)
    run_id = uuid.uuid4().hex[:12]
    control_volume = f"easel-r1-mat-{run_id}-control"
    key_volume = f"easel-r1-mat-{run_id}-keys"
    egress_volume = f"easel-r1-mat-{run_id}-egress"
    paths = [
        "/keys/master.key",
        "/keys/snapshot.pem",
        "/srv/easel/control/control.sqlite3",
    ]
    try:
        for volume in (control_volume, key_volume, egress_volume):
            _create_volume(volume)
        _write_files(control_volume, control, {
            "/control.sqlite3": "sentinel-control-db"})
        _write_files(key_volume, control, {
            "/master.key": "sentinel-master",
            "/snapshot.pem": "sentinel-signing"})
        _write_files(egress_volume, control, {
            "/egress-policy.json": '{"approved_hosts":[]}'})
        # 卷内是扁平文件；测试挂载路径模拟真实容器边界。
        control_seen = _readable(control, [
            f"type=volume,src={control_volume},dst=/srv/easel/control",
            f"type=volume,src={key_volume},dst=/keys,readonly",
        ], [
            "/keys/master.key",
            "/keys/snapshot.pem",
            "/srv/easel/control/control.sqlite3",
        ])
        assert all(control_seen.values()), control_seen

        egress_seen = _readable(control, [
            f"type=volume,src={egress_volume},dst=/policy,readonly",
        ], paths)
        # egress 服务也使用 control 镜像，但只挂策略卷；同 UID 仍看不到
        # key 卷与 control 数据卷。
        assert not any(egress_seen.values()), egress_seen

        for image in (gateway, worker):
            seen = _readable(image, [], paths)
            assert not any(seen.values()), (image, seen)
    finally:
        for volume in (control_volume, key_volume, egress_volume):
            _remove_volume(volume)
