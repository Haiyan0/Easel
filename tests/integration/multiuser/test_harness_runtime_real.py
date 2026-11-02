"""A5：真实 DeepSeek Harness 在 A4 正式拓扑中的 ACP/broker/续接验收。

显式设置 ``EASEL_RUN_CONTAINER_TESTS=1`` 后运行。测试复用 A4 的正式
control/gateway/provisioner/sidecar/broker 装配，只把 ACP 夹具替换为固定
源码构建的真实 Harness。上游是受控 TLS fake provider；不是真实供应商计费。

真实 Harness 镜像可由 ``EASEL_HARNESS_WORKER_IMAGE`` 提供，否则按
``deploy/multiuser/managed/acp-profile.json`` 的固定 commit 构建 runtime。
"""
from __future__ import annotations

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

ROOT = pathlib.Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "deploy" / "multiuser" / "managed" / "acp-profile.json"
FAKE_PROVIDER = ROOT / "tests" / "fixtures" / "fake_openai_tls.py"
WSL_DISTRO = "Ubuntu-26.04"
RUN_LABEL = "easel.a5.harness"
PROVIDER_MODEL = "deepseek-chat"
CONTROL_TAG = os.environ.get(
    "EASEL_A4_CONTROL_TAG", "easel-control-test:a4")
GATEWAY_TAG = os.environ.get(
    "EASEL_A4_GATEWAY_TAG", "easel-gateway-test:g2")
HARNESS_TAG = "easel-worker-harness:a5"
SOURCE_ROOT = os.environ.get("EASEL_A4_SOURCE_ROOT", "/mnt/c/Code/Easel")

pytestmark = pytest.mark.skipif(
    os.environ.get("EASEL_RUN_CONTAINER_TESTS") != "1",
    reason="设置 EASEL_RUN_CONTAINER_TESTS=1 才运行真实 Harness 拓扑测试")


def _load_a4():
    path = pathlib.Path(__file__).with_name("test_desktop_chat_full.py")
    spec = importlib.util.spec_from_file_location("a4_desktop_chat", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


a4 = _load_a4()


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _harness_image() -> str:
    supplied = os.environ.get("EASEL_HARNESS_WORKER_IMAGE")
    if supplied:
        result = a4._docker("image", "inspect", "--format", "{{.Id}}", supplied)
        image = result.stdout.strip()
    else:
        commit = _manifest()["harness"]["commit"]
        a4._docker(
            "build", "-f", str(ROOT / "deploy/multiuser/Dockerfile.worker"),
            "--target", "runtime", "--build-arg", f"DSH_COMMIT={commit}",
            "-t", HARNESS_TAG, str(ROOT), timeout=3600)
        image = a4._docker(
            "image", "inspect", "--format", "{{.Id}}", HARNESS_TAG
        ).stdout.strip()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None:
        pytest.fail(f"真实 Harness 镜像未固定：{image!r}")
    return image


def _write_managed_config(directory: pathlib.Path) -> None:
    manifest = _manifest()
    (directory / "managed-agent.json").write_text(json.dumps({
        "revision": 1,
        "agent": {
            "runtime": "acp",
            "command": manifest["command"],
            "models": manifest["provider"]["models"],
            "sandbox": "workspace-write",
            "timeout": 300,
        },
        "broker": manifest["broker"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "egress-policy.json").write_text(json.dumps({
        "approved_hosts": ["fake-provider.test"],
        "approved_cidrs": [],
    }), encoding="utf-8")


def _prepare_workspace(root: pathlib.Path, workspace_id: str) -> None:
    directory = root / workspace_id / "workspace"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "workspace-marker.txt").write_text(
        workspace_id, encoding="ascii")


def _audit(path: pathlib.Path) -> list[dict]:
    return a4._audit(path)


def _provider_chat(audit: pathlib.Path) -> list[dict]:
    return [line for line in _audit(audit)
            if line.get("path") == "/v1/chat/completions"]


def _message_text(body: object) -> str:
    if not isinstance(body, dict):
        return ""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ""
    parts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(
                        block.get("text"), str):
                    parts.append(block["text"])
    return "\n".join(parts)


_HARNESS_PIDS = r'''
import json, os
pids = []
for name in os.listdir("/proc"):
    if not name.isdigit():
        continue
    try:
        raw = open(f"/proc/{name}/cmdline", "rb").read()
    except OSError:
        continue
    command = raw.replace(b"\0", b" ").decode("utf-8", "replace")
    if "/opt/dsh/apps/cli/lib/bin.js" in command and "--profile acp" in command:
        pids.append(int(name))
print(json.dumps(sorted(pids)))
'''


def _harness_pids(worker: str) -> list[int]:
    result = a4._docker("exec", worker, "python", "-c", _HARNESS_PIDS,
                        check=False, timeout=30)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or not lines:
        return []
    try:
        value = json.loads(lines[-1])
    except ValueError:
        return []
    return [int(item) for item in value if isinstance(item, int)]


def _wait_harness_pids(worker: str, *, previous: list[int] | None = None,
                       timeout: float = 60.0) -> list[int]:
    deadline = time.monotonic() + timeout
    last: list[int] = []
    while time.monotonic() < deadline:
        last = _harness_pids(worker)
        if previous is None:
            if last:
                return last
        elif last and set(last).isdisjoint(previous):
            return last
        time.sleep(0.2)
    pytest.fail(f"未观察到预期的 Harness 进程：previous={previous} last={last}")


_TOKEN_ROWS = r'''
import json, sqlite3, sys
run_ids = sys.argv[1:]
connection = sqlite3.connect("/srv/easel/control/control.sqlite3")
rows = connection.execute(
    "select run_id, revoked_at, expires_at from broker_tokens "
    "where run_id in ({}) order by run_id".format(
        ",".join("?" for _ in run_ids)), run_ids).fetchall()
print(json.dumps([{"run": row[0], "revoked": row[1] is not None}
                  for row in rows]))
'''


def _token_rows(control: str, run_ids: list[str]) -> list[dict]:
    result = a4._docker("exec", control, "python", "-c", _TOKEN_ROWS,
                        *run_ids)
    return json.loads(result.stdout.strip())


def _session_root(root: pathlib.Path, workspace_id: str) -> pathlib.Path:
    return root / workspace_id / "home" / ".dsh" / "sessions"


def _assert_assistant(messages: list[dict], text: str) -> None:
    assert any(message.get("role") == "assistant"
               and text in str(message.get("content"))
               for message in messages), messages


def _assert_no_assistant(messages: list[dict]) -> None:
    assert not any(message.get("role") == "assistant"
                   for message in messages), messages


def test_real_harness_broker_resume_and_failure_semantics(
        tmp_path: pathlib.Path) -> None:
    control_image = a4._image_id(CONTROL_TAG)
    gateway_image = a4._image_id(GATEWAY_TAG)
    worker_image = _harness_image()
    print("A5_IMAGES", json.dumps({
        "control": control_image, "gateway": gateway_image,
        "worker": worker_image}, sort_keys=True))
    run_id = uuid.uuid4().hex[:12]
    control_network = f"easel-a5-net-{run_id}"
    control_name = f"easel-a5-control-{run_id}"
    egress_name = f"easel-a5-egress-{run_id}"
    fake_name = f"easel-a5-provider-{run_id}"
    control_volume = f"easel-a5-control-data-{run_id}"
    keys_volume = f"easel-a5-keys-{run_id}"
    egress_volume = f"easel-a5-egress-policy-{run_id}"
    root = tmp_path / "workspaces"
    root.mkdir()
    keys = tmp_path / "keys"
    keys.mkdir()
    certs = tmp_path / "certs"
    audit_dir = tmp_path / "provider-audit"
    audit_dir.mkdir()
    provisioner_log = tmp_path / "provisioner.log"
    users: list[a4.User] = []
    provisioner: subprocess.Popen | None = None
    provisioner_socket = f"/tmp/easel-a5-{run_id}/provisioner.sock"

    def cleanup() -> None:
        if os.environ.get("EASEL_A5_KEEP") == "1":
            print("A5_KEEP", control_name, egress_name, fake_name,
                  control_network)
            return
        for user in users:
            workspace = user.workspace_id
            for name in (f"easel-gateway-{workspace}",
                         f"easel-worker-{workspace}",
                         f"easel-sidecar-{workspace}"):
                a4._docker("container", "rm", "-f", name, check=False)
            a4._docker("network", "rm", f"easel-ws-{workspace}", check=False)
        for name in (control_name, egress_name, fake_name):
            a4._docker("container", "rm", "-f", name, check=False)
        labeled = a4._docker(
            "ps", "-aq", "--filter", f"label={RUN_LABEL}={run_id}",
            check=False).stdout.split()
        for container in labeled:
            a4._docker("container", "rm", "-f", container, check=False)
        a4._docker("network", "rm", control_network, check=False)
        for volume in (control_volume, keys_volume, egress_volume):
            a4._docker("volume", "rm", volume, check=False)
        for user in users:
            user.client.close()
        if provisioner is not None and provisioner.poll() is None:
            provisioner.terminate()
            try:
                provisioner.wait(timeout=10)
            except subprocess.TimeoutExpired:
                provisioner.kill()
                provisioner.wait(timeout=10)
        a4._wsl("sh", "-c",
                f"rm -rf {provisioner_socket.rsplit('/', 1)[0]}",
                check=False)

    try:
        a4._write_ed25519_keys(keys)
        ca_path, _cert_path, _server_key = a4._write_tls(certs)
        shutil.copy2(ca_path, keys / "ca.pem")
        _write_managed_config(keys)
        (audit_dir / "requests.jsonl").touch()
        a4._docker("network", "create", "--label", f"{RUN_LABEL}={run_id}",
                   control_network)
        a4._control_volume(control_image, control_volume, run_id)
        a4._init_volume(control_image, keys_volume, keys, run_id)
        a4._policy_volume(control_image, egress_volume,
                          keys / "egress-policy.json", run_id)

        provisioner_command = " ".join([
            (f"cd {SOURCE_ROOT} && "
             f"export PYTHONPATH={SOURCE_ROOT} && exec python3"),
            "deploy/multiuser/provisioner.py",
            "--socket", provisioner_socket,
            "--workspace-root", a4._wsl_path(root),
            "--image", worker_image,
            "--allowed-uid", "10001",
            "--quota-mode", "none",
            "--allow-unquotaed",
            "--worker-public-key", a4._wsl_path(keys / "worker-public.key"),
            "--sidecar-network", control_network,
            "--sidecar-policy", a4._wsl_path(keys / "egress-policy.json"),
            "--broker-upstream", "control:7861",
            "--gateway-image", gateway_image,
            "--gateway-control-network", control_network,
            "--gateway-public-key", a4._wsl_path(
                keys / "gateway-public.pem"),
        ])
        with provisioner_log.open("w", encoding="utf-8") as log:
            provisioner = subprocess.Popen(
                [shutil.which("wsl"), "-d", WSL_DISTRO, "--", "bash",
                 "-lc", provisioner_command],
                stdout=log, stderr=subprocess.STDOUT)
        a4._wait_until(
            lambda: a4._wsl("test", "-S", provisioner_socket,
                            check=False).returncode == 0,
            timeout=60)

        port = a4._free_port()
        a4._wsl_docker(
            "run", "-d", "--name", control_name,
            "--label", f"{RUN_LABEL}={run_id}",
            "--user", "10001:10001", "--group-add", "1000",
            "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--network", control_network,
            "--network-alias", "control",
            "-p", f"127.0.0.1:{port}:7861",
            "-v", f"{control_volume}:/srv/easel/control",
            "-v", f"{keys_volume}:/keys:ro",
            "-v", f"{provisioner_socket}:/run/easel/provisioner.sock:ro",
            "-e", "SSL_CERT_FILE=/keys/ca.pem",
            control_image,
            "python", "-m", "easel", "serve-control",
            "--control-dir", "/srv/easel/control",
            "--public-origin", a4.ORIGIN,
            "--master-key-file", "/keys/master.key",
            "--snapshot-key-file", "/keys/snapshot.pem",
            "--managed-agent-config", "/keys/managed-agent.json",
            "--expected-harness-commit",
            _manifest()["harness"]["commit"],
            "--frontend-dir", "/opt/easel/web/frontend/dist",
            "--host", "0.0.0.0", "--port", "7861",
            "--provisioner-socket", "/run/easel/provisioner.sock",
            "--gateway-required",
            "--egress-proxy", "http://egress:8899",
            "--egress-policy", "/keys/egress-policy.json",
            "--egress-ca-file", "/keys/ca.pem")

        health_code = (
            "import urllib.request;"
            "urllib.request.urlopen('http://127.0.0.1:7861/api/health',"
            "timeout=3).read()")
        a4._wait_until(
            lambda: a4._docker("exec", control_name, "python", "-c",
                               health_code, check=False).returncode == 0,
            timeout=90)

        a4._docker(
            "run", "-d", "--name", egress_name,
            "--label", f"{RUN_LABEL}={run_id}",
            "--user", "10001:10001", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--network", control_network, "--network-alias", "egress",
            "-v", f"{egress_volume}:/policy:ro",
            control_image,
            "python", "deploy/multiuser/egress_proxy.py", "--listen",
            "0.0.0.0", "--port", "8899", "--policy",
            "/policy/egress-policy.json")

        a4._docker(
            "run", "-d", "--name", fake_name,
            "--label", f"{RUN_LABEL}={run_id}",
            "--user", "0:0",
            "--network", control_network,
            "--network-alias", "fake-provider.test",
            "-v", f"{FAKE_PROVIDER.parent}:/fixture:ro",
            "-v", f"{certs}:/certs:ro",
            "-v", f"{audit_dir}:/audit",
            control_image,
            "python", "/fixture/fake_openai_tls.py", "--port", "443",
            "--cert", "/certs/server.pem", "--key", "/certs/server.key",
            "--audit", "/audit/requests.jsonl")

        provider_health = (
            "import urllib.request;"
            "urllib.request.urlopen('https://fake-provider.test/v1/models',"
            "timeout=3).read()")
        a4._wait_until(
            lambda: a4._docker("exec", control_name, "python", "-c",
                               provider_health, check=False).returncode == 0,
            timeout=60)

        bootstrap = a4._docker(
            "exec", control_name, "python", "-m", "easel", "admin",
            "bootstrap", "--control-dir", "/srv/easel/control",
            "--name", "Admin")
        invite = next(
            (line.split("=", 1)[1] for line in bootstrap.stdout.splitlines()
             if line.startswith("invite=")), "")
        assert invite, "管理员引导未返回邀请"
        admin = a4._exchange_invite(port, invite)
        users.append(admin)
        alice = a4._create_member(admin, "Alice")
        bob = a4._create_member(admin, "Bob")
        alice_invite = a4._create_invite(admin, alice["userId"])
        bob_invite = a4._create_invite(admin, bob["userId"])
        user_a = a4._exchange_invite(port, alice_invite)
        user_b = a4._exchange_invite(port, bob_invite)
        users.extend([user_a, user_b])
        assert user_a.workspace_id != user_b.workspace_id
        a4._publish_chat(admin)

        _prepare_workspace(root, user_a.workspace_id)
        _prepare_workspace(root, user_b.workspace_id)

        session_a = a4._create_session(user_a, "A harness")
        run_a1 = a4._submit(user_a, session_a, "harness-one")
        a4._collect_until_terminal(user_a, run_a1)
        assert a4._runs(user_a, session_a)[run_a1] == "done"
        messages_a = a4._messages(user_a, session_a)
        _assert_assistant(messages_a, "fake-provider-ok")
        worker_a = f"easel-worker-{user_a.workspace_id}"
        worker_a_id = a4._container_id(worker_a)
        pids_a1 = _wait_harness_pids(worker_a)
        sessions_a = _session_root(root, user_a.workspace_id)
        assert sessions_a.is_dir()
        assert any(sessions_a.rglob("*")), "Harness 私有 session store 为空"

        provider_a = _provider_chat(audit_dir / "requests.jsonl")
        first_a = [line for line in provider_a
                   if "harness-one" in _message_text(line.get("body"))]
        assert first_a, "真实 Harness 未向 broker/provider 发出首轮请求"
        assert first_a[-1]["authorization"] == f"Bearer {a4.SENTINEL_KEY}"
        body = first_a[-1]["body"]
        assert body.get("model") == PROVIDER_MODEL
        assert body.get("stream") is True
        assert not any("harness-two" in _message_text(item.get("body"))
                       for item in provider_a)

        run_a2 = a4._submit(user_a, session_a, "harness-two")
        a4._collect_until_terminal(user_a, run_a2)
        assert a4._runs(user_a, session_a)[run_a2] == "done"
        messages_a = a4._messages(user_a, session_a)
        assert sum(1 for message in messages_a
                   if message.get("role") == "assistant"
                   and "fake-provider-ok" in str(message.get("content"))
                   ) >= 2, messages_a
        provider_a = _provider_chat(audit_dir / "requests.jsonl")
        second_a = [line for line in provider_a
                    if "harness-two" in _message_text(line.get("body"))]
        assert second_a, "真实 Harness 未发出第二轮请求"
        resumed_text = _message_text(second_a[-1]["body"])
        assert "harness-one" in resumed_text
        assert "fake-provider-ok" in resumed_text
        pids_a2 = _wait_harness_pids(worker_a, previous=pids_a1)
        assert set(pids_a2).isdisjoint(pids_a1)

        tokens = _token_rows(control_name, [run_a1, run_a2])
        assert {row["run"] for row in tokens} == {run_a1, run_a2}
        assert all(row["revoked"] for row in tokens)
        assert a4._sqlite_active_tokens(control_name, run_a1) == 0
        assert a4._sqlite_active_tokens(control_name, run_a2) == 0

        session_b = a4._create_session(user_b, "B harness")
        assert session_b != session_a
        run_b1 = a4._submit(user_b, session_b, "harness-b")
        a4._collect_until_terminal(user_b, run_b1)
        assert a4._runs(user_b, session_b)[run_b1] == "done"
        messages_b = a4._messages(user_b, session_b)
        _assert_assistant(messages_b, "fake-provider-ok")
        worker_b = f"easel-worker-{user_b.workspace_id}"
        worker_b_id = a4._container_id(worker_b)
        assert worker_b_id != worker_a_id
        _wait_harness_pids(worker_b)
        sessions_b = _session_root(root, user_b.workspace_id)
        assert sessions_b.is_dir()
        assert any(sessions_b.rglob("*")), "B Harness 私有 session store 为空"
        assert sessions_b != sessions_a

        provider_all = _provider_chat(audit_dir / "requests.jsonl")
        b_requests = [item for item in provider_all
                      if "harness-b" in _message_text(item.get("body"))]
        assert b_requests
        assert not any("harness-one" in _message_text(item.get("body"))
                       for item in b_requests)

        # 真实 Harness 工具调用：provider 返回 tool_calls（提权 echo），
        # Harness 经 ACP 请示用户，授权后执行并把工具结果回传 provider。
        tool_session = a4._create_session(user_a, "A harness tool")
        run_tool = a4._submit(user_a, tool_session, "harness-tool")
        record, question = a4._read_question(user_a, run_tool)
        labels = [item["label"] for item in question.get("options") or []]
        assert question.get("header") == "执行授权", question
        assert len(labels) == 2, question
        answered = user_a.client.post(
            "/api/chat/question/answer",
            headers=user_a.headers(write=True),
            json={"runId": run_tool, "questionId": record,
                  "answers": {question.get("questionId", "permission"):
                              [labels[0]]}})
        assert answered.status_code == 200, answered.status_code
        a4._wait_run(user_a, tool_session, run_tool, {"done"}, timeout=120)
        _assert_assistant(a4._messages(user_a, tool_session),
                          "fake-provider-ok")
        tool_requests = [
            item for item in _provider_chat(audit_dir / "requests.jsonl")
            if "harness-tool" in _message_text(item.get("body"))]
        assert len(tool_requests) == 2, len(tool_requests)
        assert all(item["authorization"] == f"Bearer {a4.SENTINEL_KEY}"
                   for item in tool_requests)
        assert tool_requests[0]["body"].get("stream") is True
        # 第二回合必须携带工具实际执行结果，证明不是只弹了授权框。
        assert "harness-tool-ok" in _message_text(
            tool_requests[1].get("body")), tool_requests[1]["body"]
        assert any(message.get("role") == "tool"
                   for message in tool_requests[1]["body"].get("messages", []))

        for marker, state in (
                ("harness-401", "failed"),
                ("harness-429", "failed"),
                ("harness-500", "failed"),
                ("harness-drop", "failed")):
            session = a4._create_session(user_a, marker)
            run = a4._submit(user_a, session, marker)
            a4._wait_run(user_a, session, run, {state, "interrupted"},
                         timeout=90)
            assert a4._runs(user_a, session)[run] in {state, "interrupted"}
            _assert_no_assistant(a4._messages(user_a, session))
            requests = [item for item in _provider_chat(
                audit_dir / "requests.jsonl")
                if marker in _message_text(item.get("body"))]
            assert len(requests) == 1, (marker, len(requests))

        print("A5_REAL_PASS", json.dumps({
            "worker_a": worker_a_id, "worker_b": worker_b_id,
            "pids_a1": pids_a1, "pids_a2": pids_a2,
            "provider_requests": len(_provider_chat(
                audit_dir / "requests.jsonl")),
            "token_rows": tokens,
        }, sort_keys=True))
    finally:
        cleanup()


def test_real_harness_fails_loud_without_broker_env(tmp_path):
    worker_image = _harness_image()
    container = f"easel-a5-negative-{uuid.uuid4().hex[:12]}"
    try:
        result = a4._docker(
            "run", "--rm", "--name", container,
            "--label", f"{RUN_LABEL}={uuid.uuid4().hex[:12]}",
            "--user", "10001:10001", "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--tmpfs", "/workspace:rw,exec,nosuid,nodev,size=128m",
            "--workdir", "/workspace",
            "-e", "HOME=/tmp",
            "-e", "DSH_HOME=/tmp/.dsh",
            "-e", "TMPDIR=/workspace/state/tmp",
            "-e", "EASEL_SERVICE_MODE=worker",
            worker_image,
            "node", "/opt/dsh/apps/cli/lib/bin.js", "--profile", "acp",
            "--patch", "/opt/easel/managed/acp.patch.yml",
            input_text="", check=False, timeout=120)
        combined = result.stdout + result.stderr
        # Harness 的 loader 把单个 entry 配置错误报告为非致命激活失败；
        # 这里要求它明确点名缺失的 broker env，且不能出现伪造的成功输出。
        assert "EASEL_BROKER_URL is missing" in combined
        assert "fake-provider-ok" not in combined
    finally:
        a4._docker("container", "rm", "-f", container, check=False)

    try:
        result = a4._docker(
            "run", "--rm", "--name", container,
            "--label", f"{RUN_LABEL}={uuid.uuid4().hex[:12]}",
            "--user", "10001:10001", "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--tmpfs", "/workspace:rw,exec,nosuid,nodev,size=128m",
            "--workdir", "/workspace",
            "-e", "HOME=/tmp",
            "-e", "DSH_HOME=/tmp/.dsh",
            "-e", "TMPDIR=/workspace/state/tmp",
            worker_image,
            "node", "/opt/dsh/apps/cli/lib/bin.js", "--profile", "acp",
            "--patch", "/opt/easel/managed/missing.patch.yml",
            input_text="", check=False, timeout=120)
        assert result.returncode != 0
    finally:
        a4._docker("container", "rm", "-f", container, check=False)
