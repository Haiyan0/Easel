"""G2 真实双 workspace：固定 IPv4、激活、协议、重启与反向访问。

显式设置 ``EASEL_RUN_CONTAINER_TESTS=1`` 后运行。所有资源使用随机
workspace/网络/容器名；本测试使用 A3 协议夹具，不称真实 DeepSeek Harness。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import pathlib
import random
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = pathlib.Path(__file__).resolve().parents[3]
PROVISIONER_PATH = ROOT / "deploy" / "multiuser" / "provisioner.py"
FIXTURE = ROOT / "tests" / "fixtures" / "harness_rpc.py"
CLIENT_SCRIPT = ROOT / "tests" / "fixtures" / "gateway_control_client.py"
GATEWAY_TAG = "easel-gateway-test:g2"
WORKER_TAG = "easel-worker-execution:g2"
RUN_LABEL = "easel.g2.runtime"
READY_TIMEOUT = 90.0

pytestmark = pytest.mark.skipif(
    os.environ.get("EASEL_RUN_CONTAINER_TESTS") != "1",
    reason="设置 EASEL_RUN_CONTAINER_TESTS=1 才运行真实 gateway 拓扑测试")


def _load_provisioner():
    spec = importlib.util.spec_from_file_location(
        "easel_provisioner_g2_real", PROVISIONER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


provisioner = _load_provisioner()


def _docker(*args: str, timeout: float = 120.0, check: bool = True,
            input_text: str | None = None) -> subprocess.CompletedProcess:
    binary = shutil.which("docker")
    if binary is None:
        pytest.fail("找不到 docker")
    result = subprocess.run(
        [binary, *args], input=input_text, capture_output=True, text=True,
        check=False, timeout=timeout)
    if check and result.returncode != 0:
        pytest.fail(f"docker {' '.join(args[:3])} 失败："
                    f"{result.stderr[-2000:]}")
    return result


def _image_id(tag: str) -> str:
    result = _docker("image", "inspect", "--format", "{{.Id}}", tag,
                     check=False)
    image = result.stdout.strip()
    if result.returncode != 0 or re.fullmatch(
            r"sha256:[0-9a-f]{64}", image) is None:
        pytest.fail(f"缺少固定镜像 {tag}")
    return image


@pytest.fixture(scope="module")
def images() -> tuple[str, str]:
    _docker("build", "-f", str(ROOT / "deploy/desktop/Dockerfile.gateway"),
            "-t", GATEWAY_TAG, str(ROOT), timeout=1800)
    _docker("build", "-f", str(ROOT / "deploy/multiuser/Dockerfile.worker"),
            "--target", "execution", "-t", WORKER_TAG, str(ROOT),
            timeout=1800)
    gateway = _image_id(GATEWAY_TAG)
    worker = _image_id(WORKER_TAG)
    print("GATEWAY_IMAGE", gateway)
    print("WORKER_IMAGE", worker)
    return gateway, worker


def _control_network(run_label: str) -> str:
    for number in random.sample(range(200, 250), 20):
        name = f"easel-g2-control-{uuid.uuid4().hex[:10]}"
        result = _docker(
            "network", "create", "--subnet", f"172.31.{number}.0/24",
            "--label", f"{RUN_LABEL}={run_label}", name, check=False)
        if result.returncode == 0:
            return name
    pytest.fail("无法创建不冲突的控制网络子网")


def _keypair(directory: pathlib.Path, name: str
             ) -> tuple[Ed25519PrivateKey, pathlib.Path, pathlib.Path]:
    private = Ed25519PrivateKey.generate()
    public_path = directory / f"{name}.pub"
    private_path = directory / f"{name}.key"
    public_path.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    private_path.write_bytes(private.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption()))
    return private, public_path, private_path


def _wrapper(workspace: pathlib.Path, mode: str) -> str:
    shutil.copy2(FIXTURE, workspace / "harness_rpc.py")
    target = workspace / f"fixture_{mode}.py"
    target.write_text(
        "import os, runpy\n"
        "os.environ['FAKE_HARNESS_AUDIT'] = '/workspace/fixture-audit.jsonl'\n"
        f"os.environ['FAKE_HARNESS_MODE'] = {mode!r}\n"
        "runpy.run_path('/workspace/harness_rpc.py', run_name='__main__')\n",
        encoding="utf-8")
    return f"/workspace/{target.name}"


def _snapshot(private: Ed25519PrivateKey, workspace_id: str, run_id: str,
              session_id: str, message: str,
              command: str) -> tuple[dict, dict]:
    from easel.worker.config import run_input_digest, sign_run_snapshot
    digest = run_input_digest(
        conversation_id=session_id, task_type="chat", message=message,
        attachments=[], persona=None)
    envelope = sign_run_snapshot(
        private, workspace_id=workspace_id, run_id=run_id,
        conversation_id=session_id, input_digest=digest,
        task_type="chat", revision=1, routes={},
        agent={"runtime": "acp",
               "command": ["python", command, "acp"],
               "sandbox": "workspace-write"},
        expires_at=int(time.time()) + 900, nonce="nonce-" + run_id)
    request = {
        "sessionId": session_id, "turnId": "turn-" + run_id,
        "message": message, "attachments": [], "persona": None,
        "taskType": "chat", "inputDigest": digest,
    }
    return envelope, request


@dataclass
class DirectProvisioner:
    service: object

    async def ensure(self, workspace_id: str, config_version: int):
        return await self.service.ensure(
            workspace_id, config_version, protocol_version=2)

    async def stop(self, workspace_id: str, container_id: str,
                   gateway_instance_id: str = "") -> None:
        await self.service.stop(
            workspace_id, container_id, gateway_instance_id,
            protocol_version=2)

    async def abort(self, workspace_id: str, container_id: str,
                    gateway_instance_id: str, *, worker_created: bool,
                    gateway_created: bool) -> None:
        await self.service.abort(
            workspace_id, container_id, gateway_instance_id,
            worker_created=worker_created,
            gateway_created=gateway_created)

    async def activate(self, workspace_id: str, worker_container_id: str,
                       gateway_instance_id: str, boot_generation: str,
                       config_version: int) -> dict:
        return await self.service.activate(
            workspace_id, worker_container_id, gateway_instance_id,
            boot_generation, config_version)

    async def inspect(self, workspace_id: str) -> dict:
        return await self.service.inspect(
            workspace_id, protocol_version=2)


def _client(handle, control_key: pathlib.Path, *, image: str,
            network: str, run_label: str, op: str, run_id: str = "0" * 32,
            session_id: str = "0" * 32, input_dir: pathlib.Path | None = None,
            result_dir: pathlib.Path | None = None,
            audience_workspace: str | None = None,
            audience_boot: str | None = None) -> dict:
    name = f"easel-g2-client-{uuid.uuid4().hex[:12]}"
    args = [
        "run", "--rm", "--name", name, "--network", network,
        "--label", f"{RUN_LABEL}={run_label}", "--user", "10001:10001",
        "-v", f"{CLIENT_SCRIPT}:/client.py:ro",
        "-v", f"{control_key}:/control.key:ro",
        "-e", "PYTHONPATH=/opt/easel",
    ]
    if input_dir is not None:
        args.extend(["-v", f"{input_dir}:/input:ro"])
    if result_dir is not None:
        result_dir.mkdir(parents=True, exist_ok=True)
        args.extend(["-v", f"{result_dir}:/result"])
    args.extend([
        image, "python", "/client.py",
        "--base-url", handle.gateway_upstream,
        "--key", "/control.key",
        "--workspace-id", handle.workspace_id,
        "--gateway-instance-id", handle.gateway_instance_id,
        "--boot-generation", handle.gateway_generation,
        "--worker-container-id", handle.container_id,
        "--config-version", str(handle.config_version),
        "--op", op,
        "--run-id", run_id,
        "--session-id", session_id,
    ])
    if input_dir is not None:
        args.extend(["--input-dir", "/input"])
    if result_dir is not None:
        args.extend(["--result-dir", "/result"])
    if audience_workspace is not None:
        args.extend(["--audience-workspace", audience_workspace])
    if audience_boot is not None:
        args.extend(["--audience-boot", audience_boot])
    _docker(*args)
    if result_dir is not None:
        return json.loads(
            (result_dir / "result.json").read_text(encoding="utf-8"))
    raise AssertionError("client requires result_dir")


def _write_run(input_dir: pathlib.Path, envelope: dict,
               request: dict) -> None:
    input_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / "body.json").write_text(
        json.dumps({"envelope": envelope, "request": request}),
        encoding="utf-8")


def _network_ip(container: str, network: str) -> str:
    payload = json.loads(_docker(
        "container", "inspect", "--format", "{{json .NetworkSettings.Networks}}",
        container).stdout)
    return str(payload.get(network, {}).get("IPAddress") or "")


def _can_connect(container: str, target: str, port: int) -> bool:
    code = (
        "import socket, sys\n"
        "try:\n"
        f"    socket.create_connection(({target!r}, {port}), timeout=2)\n"
        "except OSError:\n"
        "    sys.exit(1)\n"
        "sys.exit(0)\n")
    return _docker(
        "exec", container, "python", "-c", code, check=False).returncode == 0


def _cannot_connect(worker: str, target: str, port: int) -> bool:
    return not _can_connect(worker, target, port)


def test_real_two_workspace_gateway_protocol_restart_and_isolation(
        tmp_path: pathlib.Path, images: tuple[str, str]) -> None:
    gateway_image, worker_image = images
    run_label = "g2-" + uuid.uuid4().hex[:12]
    control_network = _control_network(run_label)
    root = tmp_path / "root"
    root.mkdir()
    worker_private, worker_public, _ = _keypair(tmp_path, "worker")
    _, control_public, control_private_path = _keypair(tmp_path, "control")
    settings = provisioner.ProvisionerSettings(
        image=worker_image, workspace_root=root,
        socket_path=tmp_path / "provisioner.sock",
        quota_mode="none", allow_unquotaed=True,
        worker_public_key=worker_public,
        gateway_image=gateway_image,
        gateway_control_network=control_network,
        gateway_public_key=control_public,
        gateway_start_timeout=60.0)
    service = provisioner.Provisioner(settings)
    direct = DirectProvisioner(service)
    from easel.control.runtime import RuntimeManager
    from easel.control.store import Store

    store = Store.connect(tmp_path / "control.sqlite3")
    workers: list[str] = []
    gateways: list[str] = []
    ws_a = ws_b = ""
    try:
        user_a = store.create_user("A")
        user_b = store.create_user("B")
        ws_a, ws_b = user_a.workspace_id, user_b.workspace_id
        manager_a = RuntimeManager(
            store, direct, gateway_required=True, clock=time.time,
            ready_timeout=READY_TIMEOUT, poll_interval=0.1)
        manager_b = RuntimeManager(
            store, direct, gateway_required=True, clock=time.time,
            ready_timeout=READY_TIMEOUT, poll_interval=0.1)
        async def ensure_both():
            return await asyncio.gather(
                manager_a.ensure_ready(ws_a, 1),
                manager_b.ensure_ready(ws_b, 1))
        handle_a, handle_b = asyncio.run(ensure_both())
        workers = [handle_a.container_id, handle_b.container_id]
        gateways = [handle_a.gateway_instance_id, handle_b.gateway_instance_id]
        assert handle_a.container_id != handle_b.container_id
        assert handle_a.gateway_instance_id != handle_b.gateway_instance_id
        assert handle_a.gateway_generation != handle_b.gateway_generation
        assert handle_a.gateway_upstream.startswith("http://")
        assert handle_b.gateway_upstream.startswith("http://")
        assert handle_a.gateway_state == "available"
        assert handle_b.gateway_state == "available"
        wrappers: dict[str, dict[str, str]] = {}
        for handle in (handle_a, handle_b):
            paths = provisioner.existing_runtime_paths(
                root, handle.workspace_id)
            for directory in (paths.workspace, paths.home, paths.state):
                directory.chmod(0o777)
            wrappers[handle.workspace_id] = {
                mode: _wrapper(paths.workspace, mode)
                for mode in ("normal", "permission", "hang")
            }
            report = asyncio.run(direct.inspect(handle.workspace_id))
            assert report["gateway"]["managed"] is True
            assert report["gateway"]["available"] is True
            assert report["gateway"]["worker_container_id"] == \
                handle.container_id
        print("HANDLES", json.dumps([
            {"workspace": h.workspace_id, "worker": h.container_id,
             "gateway": h.gateway_instance_id, "boot": h.gateway_generation,
             "upstream": h.gateway_upstream} for h in (handle_a, handle_b)],
            sort_keys=True))

        # 两个工作空间的健康/参数/协议路径均经 gateway 的签名断言。
        for handle in (handle_a, handle_b):
            normal = wrappers[handle.workspace_id]["normal"]
            result_dir = tmp_path / f"health-{handle.workspace_id}"
            health = _client(
                handle, control_private_path, image=gateway_image,
                network=control_network, run_label=run_label, op="health",
                result_dir=result_dir)
            assert health["status"] == 200
            assert health["payload"]["workspace_id"] == handle.workspace_id

            run_id = uuid.uuid4().hex
            session_id = uuid.uuid4().hex
            envelope, request = _snapshot(
                worker_private, handle.workspace_id, run_id, session_id,
                "hello", normal)
            body_dir = tmp_path / f"body-{run_id}"
            _write_run(body_dir, envelope, request)
            submitted = _client(
                handle, control_private_path, image=gateway_image,
                network=control_network, run_label=run_label, op="submit",
                run_id=run_id, session_id=session_id, input_dir=body_dir,
                result_dir=tmp_path / f"submit-{run_id}")
            assert submitted["status"] == 202
            assert submitted["events"]["payload"]["state"] == "done"
            assert any(event["event"] in ("token", "done")
                       for event in submitted["events"]["payload"]["events"])
            status_dir = tmp_path / f"status-{run_id}"
            status_dir.mkdir(parents=True, exist_ok=True)
            (status_dir / "status.json").write_text(json.dumps(
                {"runId": run_id, "questionIds": []}), encoding="utf-8")
            question_status = _client(
                handle, control_private_path, image=gateway_image,
                network=control_network, run_label=run_label,
                op="question_status", run_id=run_id, session_id=session_id,
                input_dir=status_dir,
                result_dir=tmp_path / f"qstatus-{run_id}")
            assert question_status["status"] == 200
            messages = _client(
                handle, control_private_path, image=gateway_image,
                network=control_network, run_label=run_label, op="messages",
                session_id=session_id,
                result_dir=tmp_path / f"messages-{run_id}")
            assert messages["status"] == 200
            assert any(item.get("role") == "assistant"
                       for item in messages["payload"]["messages"])

        # 问题回答与 stop 也保持 G1 路由。
        q_run = uuid.uuid4().hex
        q_session = uuid.uuid4().hex
        q_envelope, q_request = _snapshot(
            worker_private, ws_a, q_run, q_session, "ask",
            wrappers[ws_a]["permission"])
        q_dir = tmp_path / f"q-{q_run}"
        _write_run(q_dir, q_envelope, q_request)
        q_submit = _client(
            handle_a, control_private_path, image=gateway_image,
            network=control_network, run_label=run_label, op="submit",
            run_id=q_run, session_id=q_session, input_dir=q_dir,
            result_dir=tmp_path / f"qsubmit-{q_run}")
        assert q_submit["events"]["payload"]["state"] == "awaiting_permission"
        question_event = next(
            event for event in q_submit["events"]["payload"]["events"]
            if event["event"] == "question")
        data = question_event["data"]
        if isinstance(data, str):
            data = json.loads(data)
        question = data["questions"][0]
        (q_dir / "qid.txt").write_text(str(data["id"]), encoding="ascii")
        (q_dir / "answer.json").write_text(json.dumps({
            "runId": q_run, "questionId": question.get(
                "questionId", "permission"),
            "answers": {question.get("questionId", "permission"): [
                question["options"][0]["label"]]},
        }), encoding="utf-8")
        answered = _client(
            handle_a, control_private_path, image=gateway_image,
            network=control_network, run_label=run_label, op="answer",
            run_id=q_run, session_id=q_session, input_dir=q_dir,
            result_dir=tmp_path / f"answer-{q_run}")
        assert answered["status"] == 200

        hang_run = uuid.uuid4().hex
        hang_session = uuid.uuid4().hex
        h_envelope, h_request = _snapshot(
            worker_private, ws_a, hang_run, hang_session, "hang",
            wrappers[ws_a]["hang"])
        h_dir = tmp_path / f"hang-{hang_run}"
        _write_run(h_dir, h_envelope, h_request)
        _client(
            handle_a, control_private_path, image=gateway_image,
            network=control_network, run_label=run_label, op="submit_once",
            run_id=hang_run, session_id=hang_session, input_dir=h_dir,
            result_dir=tmp_path / f"hang-submit-{hang_run}")
        (h_dir / "stop.json").write_text(
            json.dumps({"runId": hang_run}), encoding="utf-8")
        stopped = _client(
            handle_a, control_private_path, image=gateway_image,
            network=control_network, run_label=run_label, op="stop",
            run_id=hang_run, session_id=hang_session, input_dir=h_dir,
            result_dir=tmp_path / f"hang-stop-{hang_run}")
        assert stopped["payload"]["state"] in ("cancelled", "interrupted")

        # 跨 workspace：A audience 发往 B gateway 必须拒绝；worker 网卡
        # 不能访问管理端口或另一 workspace。
        cross = _client(
            handle_b, control_private_path, image=gateway_image,
            network=control_network, run_label=run_label, op="health",
            audience_workspace=ws_a,
            result_dir=tmp_path / "cross")
        assert cross["status"] in (401, 403)
        gateway_a_ip = handle_a.gateway_upstream.split("://", 1)[1].split(
            ":", 1)[0]
        gateway_b_ip = handle_b.gateway_upstream.split("://", 1)[1].split(
            ":", 1)[0]
        worker_b_ip = _network_ip(
            handle_b.container_id, f"easel-ws-{ws_b}")
        assert worker_b_ip
        # control 网络上的 gateway 彼此可达（共享网络事实），但 worker
        # 网卡无法到达 management 端口或另一 workspace。
        assert _can_connect(handle_a.gateway_instance_id, gateway_b_ip, 8897)
        assert _cannot_connect(
            handle_a.container_id, gateway_a_ip, 8897) is True
        assert _cannot_connect(
            handle_a.container_id, worker_b_ip, 7860) is True

        # 同容器 gateway 重启：新 boot、旧断言失效、worker ID/在途 Run 不变。
        restart_run = uuid.uuid4().hex
        restart_session = uuid.uuid4().hex
        r_envelope, r_request = _snapshot(
            worker_private, ws_a, restart_run, restart_session, "hang",
            wrappers[ws_a]["hang"])
        r_dir = tmp_path / f"restart-{restart_run}"
        _write_run(r_dir, r_envelope, r_request)
        _client(
            handle_a, control_private_path, image=gateway_image,
            network=control_network, run_label=run_label, op="submit_once",
            run_id=restart_run, session_id=restart_session, input_dir=r_dir,
            result_dir=tmp_path / f"restart-submit-{restart_run}")
        old_boot = handle_a.gateway_generation
        old_worker = handle_a.container_id
        _docker("restart", handle_a.gateway_instance_id, timeout=60)
        after_restart = _client(
            handle_a, control_private_path, image=gateway_image,
            network=control_network, run_label=run_label, op="health",
            audience_boot=old_boot,
            result_dir=tmp_path / f"old-before-{restart_run}")
        assert after_restart["status"] == 503
        refreshed = asyncio.run(manager_a.ensure_ready(ws_a, 1))
        assert refreshed.container_id == old_worker
        assert refreshed.gateway_instance_id == \
            handle_a.gateway_instance_id
        assert refreshed.gateway_generation != old_boot
        old_after = _client(
            refreshed, control_private_path, image=gateway_image,
            network=control_network, run_label=run_label, op="health",
            audience_boot=old_boot,
            result_dir=tmp_path / f"old-after-{restart_run}")
        assert old_after["status"] in (401, 403)
        new_health = _client(
            refreshed, control_private_path, image=gateway_image,
            network=control_network, run_label=run_label, op="health",
            result_dir=tmp_path / f"new-health-{restart_run}")
        assert new_health["status"] == 200
        events = _client(
            refreshed, control_private_path, image=gateway_image,
            network=control_network, run_label=run_label, op="events",
            run_id=restart_run, session_id=restart_session,
            result_dir=tmp_path / f"restart-events-{restart_run}")
        assert events["payload"]["state"] in (
            "running", "awaiting_permission")
        (r_dir / "stop.json").write_text(
            json.dumps({"runId": restart_run}), encoding="utf-8")
        stopped_restart = _client(
            refreshed, control_private_path, image=gateway_image,
            network=control_network, run_label=run_label, op="stop",
            run_id=restart_run, session_id=restart_session, input_dir=r_dir,
            result_dir=tmp_path / f"restart-stop-{restart_run}")
        assert stopped_restart["payload"]["state"] in (
            "cancelled", "interrupted")

        asyncio.run(manager_a.drain(ws_a))
        asyncio.run(manager_b.drain(ws_b))
        for workspace in (ws_a, ws_b):
            lease = store.get_worker_lease(workspace)
            assert lease is not None and lease.state == "stopped"
            assert lease.gateway_instance_id == ""
        for identifier in [*gateways, *workers]:
            assert _docker("container", "inspect", identifier,
                           check=False).returncode != 0
    finally:
        labeled = _docker(
            "ps", "-aq", "--filter", f"label={RUN_LABEL}={run_label}",
            check=False).stdout.split()
        for container in labeled:
            _docker("rm", "-f", container, check=False)
        for name in gateways + [f"easel-gateway-{ws_a}", f"easel-gateway-{ws_b}"]:
            _docker("rm", "-f", name, check=False)
        for name in workers + [f"easel-worker-{ws_a}", f"easel-worker-{ws_b}",
                               f"easel-sidecar-{ws_a}", f"easel-sidecar-{ws_b}"]:
            _docker("rm", "-f", name, check=False)
        for workspace in (ws_a, ws_b):
            _docker("network", "rm", f"easel-ws-{workspace}", check=False)
        _docker("network", "rm", control_network, check=False)
        store.close()
        shutil.rmtree(root, ignore_errors=True)
