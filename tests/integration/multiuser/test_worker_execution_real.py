"""A3 真实 worker 容器协议验收：签名快照、RunManager、ACP 夹具、重启历史。

显式设置 ``EASEL_RUN_CONTAINER_TESTS=1`` 后运行。测试只创建随机命名/标签的临时
worker、客户端容器和网络；不发布 worker 端口，不访问开发者资源。夹具是协议
验收，不代表真实 DeepSeek Harness。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
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
FIXTURE = ROOT / "tests" / "fixtures" / "harness_rpc.py"
EXECUTION_TAG = "easel-worker-execution-test:a3"
CLIENT_IMAGE = "python:3.12-slim"
RUN_LABEL = "easel.a3.execution"
READY_DEADLINE = 60.0

pytestmark = pytest.mark.skipif(
    os.environ.get("EASEL_RUN_CONTAINER_TESTS") != "1",
    reason="设置 EASEL_RUN_CONTAINER_TESTS=1 才运行真实 worker 协议测试")


def _load_provisioner():
    spec = importlib.util.spec_from_file_location(
        "easel_provisioner_a3_real", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


provisioner = _load_provisioner()


def _docker(*args: str, timeout: float = 120.0,
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


def _image_id(tag: str) -> str:
    _docker("build", "-f",
            str(ROOT / "deploy/multiuser/Dockerfile.worker"),
            "--target", "execution", "-t", tag, str(ROOT),
            timeout=1800)
    result = _docker("image", "inspect", "--format", "{{.Id}}", tag)
    image = result.stdout.strip()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None:
        pytest.fail(f"执行镜像未固定：{image!r}")
    return image


def _write_key(path: pathlib.Path) -> pathlib.Path:
    path.write_bytes(Ed25519PrivateKey.generate().public_key()
                     .public_bytes_raw())
    return path


def _wrapper(workspace: pathlib.Path, mode: str, *,
             delay_seconds: float | None = None) -> str:
    shutil.copy2(FIXTURE, workspace / "harness_rpc.py")
    suffix = "" if delay_seconds is None else f"-{delay_seconds:g}"
    target = workspace / f"fixture_{mode}{suffix}.py"
    lines = [
        "import os, runpy\n",
        "os.environ['FAKE_HARNESS_AUDIT'] = '/workspace/fixture-audit.jsonl'\n",
        f"os.environ['FAKE_HARNESS_MODE'] = {mode!r}\n",
    ]
    if delay_seconds is not None:
        lines.append(
            "os.environ['FAKE_HARNESS_DELAY_SECONDS'] = "
            f"{str(delay_seconds)!r}\n")
    lines.append(
        "runpy.run_path('/workspace/harness_rpc.py', run_name='__main__')\n")
    target.write_text("".join(lines), encoding="utf-8")
    return f"/workspace/{target.name}"


def _snapshot(private, workspace_id: str, run_id: str, session_id: str,
              message: str, command: str) -> tuple[dict, dict]:
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


@pytest.fixture(scope="module")
def execution_image() -> str:
    image = _image_id(EXECUTION_TAG)
    print("EXECUTION_IMAGE", image)
    return image


def _inspect_state(worker: str) -> dict:
    result = _docker("container", "inspect", "--format",
                     "{{json .State}}", worker)
    return json.loads(result.stdout)


def _wait_not_running(worker: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = _inspect_state(worker)
        if last.get("Running") is False:
            return last
        time.sleep(0.2)
    pytest.fail(f"worker 未在 {timeout:g}s 内退出：{last}")


_HEALTH_PROBE = r'''
import json, os, urllib.error, urllib.request
request = urllib.request.Request(
    "http://127.0.0.1:7860/internal/lifecycle/health",
    headers={"X-Easel-Workspace-Id": os.environ["EASEL_WORKSPACE_ID"],
             "X-Easel-Service-Token": os.environ["EASEL_SERVICE_TOKEN"]})
try:
    with urllib.request.urlopen(request, timeout=2) as response:
        print(json.dumps([response.status, json.loads(response.read())]))
except urllib.error.HTTPError as error:
    print(json.dumps([error.code, json.loads(error.read())]))
'''


def _health(network: str, name: str, run_label: str, image: str,
            worker: str, token: str, workspace_id: str,
            tmp_path: pathlib.Path) -> tuple[int, dict]:
    """在 worker 内做固定回环探测，避免每次启动临时客户端拖慢时序。"""
    result = _docker("exec", worker, "python", "-c", _HEALTH_PROBE,
                     check=False, timeout=10)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or not lines:
        raise RuntimeError("worker health 不可用")
    status, payload = json.loads(lines[-1])
    return int(status), payload


def _wait_health_state(network: str, name: str, run_label: str, image: str,
                       worker: str, token: str, workspace_id: str,
                       tmp_path: pathlib.Path, *, ready: bool,
                       active_runs: int | None = None,
                       timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        try:
            status, payload = _health(
                network, name, run_label, image, worker, token,
                workspace_id, tmp_path)
        except Exception as error:  # noqa: BLE001 - 停机边界继续轮询
            logs = _docker("logs", "--tail", "40", worker,
                           check=False).stdout
            state = _docker("container", "inspect", "--format",
                            "{{json .State}}", worker,
                            check=False).stdout.strip()
            last = {"error": type(error).__name__, "state": state,
                    "logs": logs[-3000:]}
            time.sleep(0.2)
            continue
        last = payload
        if (status == 200 and payload.get("ready") is ready
                and (active_runs is None
                     or payload.get("active_runs") == active_runs)):
            return payload
        time.sleep(0.2)
    pytest.fail(f"未等到 health ready={ready} active_runs={active_runs}：{last}")


def _wait_health(worker: str) -> None:
    deadline = time.monotonic() + READY_DEADLINE
    last = ""
    while time.monotonic() < deadline:
        result = _docker(
            "exec", worker, "python", "-m", "easel.worker.server",
            "healthcheck", "--port", "7860", check=False, timeout=15)
        if result.returncode == 0:
            return
        last = result.stderr[-500:]
        time.sleep(0.5)
    logs = _docker("logs", "--tail", "80", worker, check=False).stdout
    pytest.fail(f"worker 未就绪：{last}\n{logs[-4000:]}")


_CLIENT = r'''
import http.client, json, sys, time
target, port, token, workspace, op, run_id, session = (
    sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4],
    sys.argv[5], sys.argv[6], sys.argv[7])
headers = {"X-Easel-Workspace-Id": workspace,
           "X-Easel-Service-Token": token}
def request(method, path, body=None):
    connection = http.client.HTTPConnection(target, port, timeout=5)
    data = None if body is None else json.dumps(body).encode()
    hdrs = dict(headers)
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    connection.request(method, path, body=data, headers=hdrs)
    response = connection.getresponse()
    raw = response.read(1024 * 1024).decode("utf-8", "replace")
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {"raw": raw}
    connection.close()
    return response.status, payload
def events(after=0):
    return request("GET", f"/internal/runs/{run_id}/events?after={after}")
def write(value):
    with open("/result/result.json", "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
def submit_once(loop):
    body = {
        "envelope": json.load(open("/input/envelope.json")),
        "request": json.load(open("/input/request.json")),
    }
    status, payload = request("POST", f"/internal/runs/{run_id}", body)
    result = {"status": status, "payload": payload, "events": None}
    if loop:
        deadline = time.time() + 20
        while time.time() < deadline:
            es, ep = events(0)
            result["events"] = ep
            state = ep.get("state") if isinstance(ep, dict) else None
            if state in ("done", "failed", "interrupted", "cancelled",
                         "awaiting_permission"):
                break
            time.sleep(0.1)
    write(result)
if op == "submit":
    submit_once(True)
elif op == "submit-once":
    submit_once(False)
elif op == "duplicate":
    submit_once(False)
elif op == "events":
    write({"events": events(0)})
elif op == "answer":
    answers = json.load(open("/input/answers.json"))
    question_id = open("/input/question-id.txt", encoding="ascii").read().strip()
    write(request(
        "POST", f"/internal/runs/{run_id}/questions/{question_id}/answer",
        {"answers": answers}))
elif op == "messages":
    write(request(
        "GET", f"/internal/sessions/{session}/messages?limit=100&offset=0"))
elif op == "health":
    write(request("GET", "/internal/lifecycle/health"))
elif op == "stop":
    write(request("POST", f"/internal/runs/{run_id}/stop",
                  {"runId": run_id, "sessionId": session}))
else:
    raise SystemExit("unknown op")
'''


def _client(network: str, name: str, run_label: str, image: str,
            worker: str, token: str, workspace_id: str, op: str,
            run_id: str, session: str, input_dir: pathlib.Path,
            result_dir: pathlib.Path) -> dict:
    input_dir.mkdir(exist_ok=True)
    result_dir.mkdir(exist_ok=True)
    for directory in (input_dir, result_dir):
        directory.chmod(0o777)
    client = result_dir.parent / "_a3_client.py"
    client.write_text(_CLIENT, encoding="utf-8")
    result_dir.joinpath("result.json").unlink(missing_ok=True)
    _docker(
        "run", "--rm", "--name", name, "--network", network,
        "--label", f"{RUN_LABEL}={run_label}", "--user", "10001:10001",
        "-v", f"{client}:/client.py:ro",
        "-v", f"{input_dir}:/input:ro",
        "-v", f"{result_dir}:/result",
        CLIENT_IMAGE, "python", "/client.py", worker, "7860", token,
        workspace_id, op, run_id, session)
    return json.loads(result_dir.joinpath("result.json").read_text(
        encoding="utf-8"))


def _start_worker(image: str, workspace_id: str, paths, env_file,
                  key: pathlib.Path, token: str) -> str:
    worker = provisioner.container_name(workspace_id)
    argv = provisioner.build_run_argv(
        image=image, workspace_id=workspace_id, config_version=1,
        paths=paths, env_file=env_file, public_key_file=key)
    _docker(*argv)
    _wait_health(worker)
    return worker


def test_real_worker_protocol_run_question_stop_and_restart(
        tmp_path: pathlib.Path, execution_image: str) -> None:
    image = execution_image
    run_label = "a3-" + uuid.uuid4().hex[:12]
    workspace_id = uuid.uuid4().hex
    root = tmp_path / "root"
    root.mkdir()
    paths = provisioner.prepare_workspace(root, workspace_id)
    for directory in (paths.workspace, paths.home, paths.state):
        directory.chmod(0o777)
    private = Ed25519PrivateKey.generate()
    key = root.parent / "worker.pub"
    key.write_bytes(private.public_key().public_bytes_raw())
    token = "token-" + uuid.uuid4().hex
    env_file = provisioner.write_env_file(
        root, workspace_id, 1, token, paths)
    network = provisioner.network_name(workspace_id)
    worker = provisioner.container_name(workspace_id)
    clients: list[str] = []
    try:
        _docker("network", "create", "--internal",
                "--label", f"{RUN_LABEL}={run_label}", network)
        normal = _wrapper(paths.workspace, "normal")
        permission = _wrapper(paths.workspace, "permission")
        hang = _wrapper(paths.workspace, "hang")
        _start_worker(image, workspace_id, paths, env_file, key, token)

        # 普通 Run：提交、事件、幂等重复、历史。
        run_id = "run-normal-" + uuid.uuid4().hex[:12]
        envelope, request = _snapshot(
            private, workspace_id, run_id, "session-normal",
            "hello", normal)
        inp = tmp_path / "normal-input"
        out = tmp_path / "normal-result"
        inp.mkdir()
        inp.joinpath("envelope.json").write_text(json.dumps(envelope))
        inp.joinpath("request.json").write_text(json.dumps(request))
        client_name = f"easel-a3-client-{run_id}"
        clients.append(client_name)
        result = _client(network, client_name, run_label, image, worker,
                         token, workspace_id, "submit", run_id,
                         "session-normal", inp, out)
        assert result["status"] == 202
        assert result["events"]["state"] == "done"
        assert any(event["event"] in ("token", "done")
                   for event in result["events"]["events"])
        client_name2 = client_name + "-dup"
        clients.append(client_name2)
        duplicate = _client(network, client_name2, run_label, image, worker,
                            token, workspace_id, "duplicate", run_id,
                            "session-normal", inp, out)
        assert duplicate["payload"].get("duplicate") is True

        clients.append(client_name + "-msg")
        messages = _client(network, client_name + "-msg", run_label, image,
                           worker, token, workspace_id, "messages", run_id,
                           "session-normal", inp, tmp_path / "msg-result")
        assert any(item.get("role") == "assistant"
                   for item in messages[1]["messages"])

        # 同 workspace 重建容器，历史/状态仍可读。
        _docker("stop", worker)
        _docker("rm", "-f", worker)
        _start_worker(image, workspace_id, paths, env_file, key, token)
        clients.append(client_name + "-msg2")
        messages2 = _client(network, client_name + "-msg2", run_label, image,
                            worker, token, workspace_id, "messages", run_id,
                            "session-normal", inp,
                            tmp_path / "msg-result-2")
        assert messages2[1]["total"] >= messages[1]["total"]

        # 问题回答：夹具发起 permission 请求，客户端回答后继续到 done。
        question_run = "run-question-" + uuid.uuid4().hex[:12]
        q_envelope, q_request = _snapshot(
            private, workspace_id, question_run, "session-question",
            "ask", permission)
        qin = tmp_path / "question-input"
        qin.mkdir()
        qin.joinpath("envelope.json").write_text(json.dumps(q_envelope))
        qin.joinpath("request.json").write_text(json.dumps(q_request))
        qclient = f"easel-a3-client-{question_run}"
        clients.append(qclient)
        qresult = _client(network, qclient, run_label, image, worker, token,
                          workspace_id, "submit", question_run,
                          "session-question", qin,
                          tmp_path / "question-result")
        assert qresult["events"]["state"] == "awaiting_permission"
        question_event = next(
            event for event in qresult["events"]["events"]
            if event["event"] == "question")
        data = question_event["data"]
        if isinstance(data, str):
            data = json.loads(data)
        question = data["questions"][0]
        qin.joinpath("answers.json").write_text(json.dumps(
            {question.get("questionId", "permission"):
             [question["options"][0]["label"]]}))
        qin.joinpath("question-id.txt").write_text(str(data["id"]),
                                                   encoding="ascii")
        clients.append(qclient + "-answer")
        answer = _client(network, qclient + "-answer", run_label, image,
                         worker, token, workspace_id, "answer", question_run,
                         "session-question", qin,
                         tmp_path / "question-answer")
        assert answer[1]["ok"] is True
        clients.append(qclient + "-after")
        after = _client(network, qclient + "-after", run_label, image,
                        worker, token, workspace_id, "events", question_run,
                        "session-question", qin,
                        tmp_path / "question-after")
        assert after["events"][1]["state"] in ("done", "running")

        # 停止：hang 夹具保持运行，显式 stop 后为终态。
        stop_run = "run-stop-" + uuid.uuid4().hex[:12]
        s_envelope, s_request = _snapshot(
            private, workspace_id, stop_run, "session-stop",
            "stop", hang)
        sin = tmp_path / "stop-input"
        sin.mkdir()
        sin.joinpath("envelope.json").write_text(json.dumps(s_envelope))
        sin.joinpath("request.json").write_text(json.dumps(s_request))
        sclient = f"easel-a3-client-{stop_run}"
        clients.append(sclient)
        _client(network, sclient, run_label, image, worker, token,
                workspace_id, "submit-once", stop_run, "session-stop",
                sin, tmp_path / "stop-submit")
        clients.append(sclient + "-stop")
        stopped = _client(network, sclient + "-stop", run_label, image,
                          worker, token, workspace_id, "stop", stop_run,
                          "session-stop", sin, tmp_path / "stop-result")
        assert stopped[1]["state"] in ("cancelled", "interrupted")
    finally:
        for name in clients:
            _docker("rm", "-f", name, check=False)
        _docker("rm", "-f", worker, check=False)
        _docker("network", "rm", network, check=False)


def test_real_sigterm_grace_drain_and_persistent_recovery(
        tmp_path: pathlib.Path, execution_image: str) -> None:
    """真实 Linux worker + 真实 SIGTERM：先 drain，短 Run 完成，挂起 Run 有限退出。"""
    run_label = "a3-sigterm-" + uuid.uuid4().hex[:12]
    workspace_id = uuid.uuid4().hex
    root = tmp_path / "root"
    root.mkdir()
    paths = provisioner.prepare_workspace(root, workspace_id)
    for directory in (paths.workspace, paths.home, paths.state):
        directory.chmod(0o777)
    private = Ed25519PrivateKey.generate()
    key = root.parent / "worker.pub"
    key.write_bytes(private.public_key().public_bytes_raw())
    settings = provisioner.ProvisionerSettings(
        image=execution_image, workspace_root=root,
        socket_path=tmp_path / "provisioner.sock",
        quota_mode="none", allow_unquotaed=True,
        worker_public_key=key)
    service = provisioner.Provisioner(settings)
    network = provisioner.network_name(workspace_id)
    worker = provisioner.container_name(workspace_id)
    clients: list[str] = []
    try:
        short_wrapper = _wrapper(paths.workspace, "delayed",
                                 delay_seconds=8.0)
        hang_wrapper = _wrapper(paths.workspace, "hang")
        handle = asyncio.run(service.ensure(workspace_id, 1))
        token = handle.service_token
        token_path = root / ".credentials" / f"{workspace_id}.token"
        original_token = token_path.read_text(encoding="ascii")
        assert original_token == token

        # 真实 inspect/ensure 必须使用同一 token；错误/缺失 token 不得改写
        # 运行实例，也不得自动创建新凭据。
        assert handle.container_id == json.loads(_docker(
            "container", "inspect", "--format", "{{json .Id}}",
            worker).stdout)
        token_path.write_text("wrong-token", encoding="ascii")
        wrong = asyncio.run(service.inspect(workspace_id))
        assert wrong["ready"] is False
        assert _inspect_state(worker)["Running"] is True
        token_path.unlink()
        missing = asyncio.run(service.inspect(workspace_id))
        assert missing["ready"] is False
        assert not token_path.exists()
        with pytest.raises(provisioner.ProvisionerError) as missing_ensure:
            asyncio.run(service.ensure(workspace_id, 1))
        assert missing_ensure.value.code == "worker_credentials_missing"
        assert _inspect_state(worker)["Running"] is True
        token_path.write_text(original_token, encoding="ascii")
        assert asyncio.run(service.inspect(workspace_id))["ready"] is True
        assert _inspect_state(worker)["Running"] is True

        # 短 Run：收到 SIGTERM 后仍在宽限期内完成。
        short_run = "run-sig-short-" + uuid.uuid4().hex[:12]
        short_envelope, short_request = _snapshot(
            private, workspace_id, short_run, "session-sig-short",
            "short", short_wrapper)
        short_in = tmp_path / "short-input"
        short_in.mkdir()
        short_in.joinpath("envelope.json").write_text(
            json.dumps(short_envelope))
        short_in.joinpath("request.json").write_text(
            json.dumps(short_request))
        short_client = f"easel-a3-sig-short-{short_run}"
        clients.append(short_client)
        submitted = _client(
            network, short_client, run_label, execution_image, worker, token,
            workspace_id, "submit-once", short_run, "session-sig-short",
            short_in, tmp_path / "short-result")
        assert submitted["status"] == 202
        _wait_health_state(
            network, "short-health", run_label, execution_image, worker,
            token, workspace_id, tmp_path, ready=True, active_runs=1)
        signal_started = time.monotonic()
        _docker("kill", "--signal=TERM", worker)
        draining = _wait_health_state(
            network, "short-drain", run_label, execution_image, worker,
            token, workspace_id, tmp_path, ready=False, active_runs=1,
            timeout=8.0)
        drain_elapsed = time.monotonic() - signal_started
        assert draining["execution_capable"] is True
        rejected_run = "run-sig-rejected-" + uuid.uuid4().hex[:12]
        rejected_envelope, rejected_request = _snapshot(
            private, workspace_id, rejected_run, "session-sig-rejected",
            "rejected", short_wrapper)
        rejected_in = tmp_path / "rejected-input"
        rejected_in.mkdir()
        rejected_in.joinpath("envelope.json").write_text(
            json.dumps(rejected_envelope))
        rejected_in.joinpath("request.json").write_text(
            json.dumps(rejected_request))
        rejected_client = f"easel-a3-sig-rejected-{rejected_run}"
        clients.append(rejected_client)
        rejected = _client(
            network, rejected_client, run_label, execution_image, worker,
            token, workspace_id, "submit-once", rejected_run,
            "session-sig-rejected", rejected_in,
            tmp_path / "rejected-result")
        assert rejected["status"] == 503
        assert "worker_draining" in json.dumps(rejected["payload"],
                                               ensure_ascii=False)
        state = _wait_not_running(worker, 20.0)
        print("SIGTERM_SHORT", json.dumps({
            "drain_ms": round(drain_elapsed * 1000),
            "exit_ms": round((time.monotonic() - signal_started) * 1000),
            "active_runs_at_drain": draining["active_runs"],
        }, sort_keys=True))
        assert state.get("Status") == "exited"
        assert time.monotonic() - signal_started < 18.0
        handle = asyncio.run(service.ensure(workspace_id, 1))
        _wait_health(worker)
        short_events = _client(
            network, f"{short_client}-after", run_label, execution_image,
            worker, handle.service_token, workspace_id, "events", short_run,
            "session-sig-short", short_in, tmp_path / "short-after")
        assert short_events["events"][1]["state"] == "done"

        # 挂起 Run：宽限期耗尽后必须有限退出，重开后可读取非 done 终态。
        hang_run = "run-sig-hang-" + uuid.uuid4().hex[:12]
        hang_envelope, hang_request = _snapshot(
            private, workspace_id, hang_run, "session-sig-hang", "hang",
            hang_wrapper)
        hang_in = tmp_path / "hang-input"
        hang_in.mkdir()
        hang_in.joinpath("envelope.json").write_text(
            json.dumps(hang_envelope))
        hang_in.joinpath("request.json").write_text(
            json.dumps(hang_request))
        hang_client = f"easel-a3-sig-hang-{hang_run}"
        clients.append(hang_client)
        submitted = _client(
            network, hang_client, run_label, execution_image, worker,
            handle.service_token, workspace_id, "submit-once", hang_run,
            "session-sig-hang", hang_in, tmp_path / "hang-result")
        assert submitted["status"] == 202
        _wait_health_state(
            network, "hang-health", run_label, execution_image, worker,
            handle.service_token, workspace_id, tmp_path, ready=True,
            active_runs=1)
        signal_started = time.monotonic()
        _docker("kill", "--signal=TERM", worker)
        _wait_health_state(
            network, "hang-drain", run_label, execution_image, worker,
            handle.service_token, workspace_id, tmp_path, ready=False,
            active_runs=1, timeout=8.0)
        hang_drain_elapsed = time.monotonic() - signal_started
        state = _wait_not_running(worker, 25.0)
        print("SIGTERM_HANG", json.dumps({
            "drain_ms": round(hang_drain_elapsed * 1000),
            "exit_ms": round((time.monotonic() - signal_started) * 1000),
        }, sort_keys=True))
        assert state.get("Status") == "exited"
        assert time.monotonic() - signal_started < 25.0
        handle = asyncio.run(service.ensure(workspace_id, 1))
        _wait_health(worker)
        hang_events = _client(
            network, f"{hang_client}-after", run_label, execution_image,
            worker, handle.service_token, workspace_id, "events", hang_run,
            "session-sig-hang", hang_in, tmp_path / "hang-after")
        assert hang_events["events"][1]["state"] in (
            "cancelled", "interrupted", "failed")
        assert hang_events["events"][1]["state"] != "done"
    finally:
        for name in clients:
            _docker("rm", "-f", name, check=False)
        _docker("rm", "-f", worker, check=False)
        _docker("network", "rm", network, check=False)
