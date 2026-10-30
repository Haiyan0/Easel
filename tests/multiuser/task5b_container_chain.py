"""Task 5B 隔离容器文件链路（手动执行，不进入默认 pytest）。

用新建的 control/gateway/worker 容器验证：真实文件、真实 Ed25519 gateway
验签、真实 HTTP、上传/画像/内容/媒体 Range/HEAD。所有资源带随机 run-id
标签；清理前 inspect 校验归属，绝不触碰用户容器/卷/网络。

先构建镜像（示例）：
  docker build -f deploy/desktop/Dockerfile.control -t easel-control:task5b .
  docker build -f deploy/desktop/Dockerfile.gateway -t easel-gateway:task5b .
  docker build -f tests/multiuser/Dockerfile.worker-task5a \
    -t easel-worker-execution:task5b .

运行：
  python tests/multiuser/task5b_container_chain.py \
    --control-image easel-control:task5b \
    --gateway-image easel-gateway:task5b \
    --worker-image easel-worker-execution:task5b
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import httpx

ROOT = Path(__file__).resolve().parents[2]
LABEL = "easel.test.run-id"
RUN = f"task5b-{secrets.token_hex(6)}"
ORIGIN = "https://easel.test"


def run(argv: list[str], *, timeout: float = 120,
        check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(argv)}\n"
            f"stdout={result.stdout[-3000:]}\nstderr={result.stderr[-3000:]}")
    return result


def labels_args() -> list[str]:
    return ["--label", f"{LABEL}={RUN}",
            "--label", "easel.test.component=task5b"]


def owned(kind: str, name: str) -> bool:
    result = run(["docker", "inspect", "--type", kind, name], check=False)
    if result.returncode != 0:
        return False
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return False
    if not isinstance(payload, list) or not payload:
        return False
    entry = payload[0]
    labels = (entry.get("Config", {}).get("Labels")
              if kind == "container" else entry.get("Labels"))
    return isinstance(labels, dict) and labels.get(LABEL) == RUN


def cleanup(containers: list[str], network: str,
            volumes: list[str]) -> list[str]:
    errors: list[str] = []
    for name in containers:
        if not owned("container", name):
            errors.append(f"container ownership mismatch: {name}")
            continue
        result = run(["docker", "rm", "-f", name], check=False)
        if result.returncode != 0 and "No such container" not in result.stderr:
            errors.append(f"remove container {name}: {result.stderr[-300:]}")
    if owned("network", network):
        result = run(["docker", "network", "rm", network], check=False)
        if result.returncode != 0 and "not found" not in result.stderr.lower():
            errors.append(f"remove network {network}: {result.stderr[-300:]}")
    for name in volumes:
        if not owned("volume", name):
            errors.append(f"volume ownership mismatch: {name}")
            continue
        result = run(["docker", "volume", "rm", name], check=False)
        if result.returncode != 0 and "no such volume" not in result.stderr.lower():
            errors.append(f"remove volume {name}: {result.stderr[-300:]}")
    return errors


def wait_http(url: str, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=2, verify=False)
            if response.status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"service did not become ready: {url}")


def container_logs(name: str, limit: int = 6000) -> str:
    result = run(["docker", "logs", name], check=False)
    text = (result.stdout or "") + (result.stderr or "")
    return text[-limit:]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-image", required=True)
    parser.add_argument("--gateway-image", required=True)
    parser.add_argument("--worker-image", required=True)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    for image in (args.control_image, args.gateway_image, args.worker_image):
        inspected = run(["docker", "image", "inspect", image], check=False)
        if inspected.returncode != 0:
            print(f"缺少镜像：{image}", file=sys.stderr)
            return 2

    run_id = secrets.token_hex(8)
    workspace = secrets.token_hex(16)
    service = "service-" + secrets.token_urlsafe(24)
    container_id = secrets.token_hex(32)
    gateway_instance = f"gateway-{run_id}"
    boot = secrets.token_hex(16)
    network = f"easel-task5b-{run_id}"
    control_volume = f"easel-task5b-control-{run_id}"
    worker_volume = f"easel-task5b-ws-{run_id}"
    home_volume = f"easel-task5b-home-{run_id}"
    containers: list[str] = []
    work = Path(tempfile.mkdtemp(prefix="easel-task5b-container-"))
    try:
        # 临时密钥/启动脚本/种子文件，全部只在本次临时目录。
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        key = Ed25519PrivateKey.generate()
        private_key = work / "control-key.pem"
        public_key = work / "worker-public.pem"
        private_key.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()))
        public_key.write_bytes(key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo))
        seed = work / "seed"
        (seed / "outputs" / "项目").mkdir(parents=True)
        (seed / "profiles" / "测试画像").mkdir(parents=True)
        (seed / "outputs" / "项目" / "文案.txt").write_text(
            "容器私有正文", encoding="utf-8")
        shutil.copyfile(ROOT / "web" / "static" /
                        "easel-icon-transparent.png",
                        seed / "outputs" / "项目" / "封面.png")
        (seed / "profiles" / "测试画像" / "identity.md").write_text(
            "# 容器画像\n\n容器定位", encoding="utf-8")
        (seed / "profiles" / "测试画像" / "style.md").write_text(
            "容器风格", encoding="utf-8")
        (seed / "outputs" / "_ideas.json").write_text(
            json.dumps([{
                "id": "a1b2c3d4e5f6", "title": "容器初始选题",
                "note": "", "source": "manual", "status": "pending",
                "created": 1,
            }], ensure_ascii=False), encoding="utf-8")
        (seed / "outputs" / "_schedule.json").write_text(
            json.dumps([{
                "id": "b1b2c3d4e5f6", "title": "容器初始排期",
                "date": "2026-11-04", "platform": "微博", "time": "10:00",
                "status": "scheduled", "note": "", "kind": "content",
                "url": "", "source": "manual", "event_type": "",
                "end_date": "",
            }], ensure_ascii=False), encoding="utf-8")

        gateway_launcher = work / "gateway_launcher.py"
        gateway_launcher.write_text(GATEWAY_LAUNCHER, encoding="utf-8")
        control_launcher = work / "control_launcher.py"
        control_launcher.write_text(CONTROL_LAUNCHER, encoding="utf-8")

        for name in (network,):
            run(["docker", "network", "create", *labels_args(), name])
        for name in (control_volume, worker_volume, home_volume):
            run(["docker", "volume", "create", *labels_args(), name])

        # 先用同一镜像的 root 助手把临时种子写进本次 workspace 卷，
        # 再启动 worker，避免启动期与 cp/chown 竞争。
        run([
            "docker", "run", "--rm", "--user", "root",
            "-v", f"{worker_volume}:/workspace",
            "-v", f"{seed}:/seed:ro",
            "--entrypoint", "sh", args.worker_image, "-c",
            "mkdir -p /workspace/outputs /workspace/profiles "
            "&& cp -a /seed/outputs/. /workspace/outputs/ "
            "&& cp -a /seed/profiles/. /workspace/profiles/ "
            "&& mkdir -p /workspace/state "
            "&& chown -R 10001:10001 /workspace",
        ])

        worker_name = f"easel-task5b-worker-{run_id}"
        worker = run([
            "docker", "run", "-d", "--name", worker_name,
            *labels_args(),
            "--network", network, "--network-alias", "worker",
            "-v", f"{worker_volume}:/workspace",
            "-v", f"{home_volume}:/home/easel",
            "-v", f"{public_key}:/keys/worker-public.key:ro",
            "-e", "EASEL_SERVICE_MODE=worker",
            "-e", f"EASEL_WORKSPACE_ID={workspace}",
            "-e", f"EASEL_SERVICE_TOKEN={service}",
            "-e", "EASEL_CODE_ROOT=/opt/easel",
            "-e", "EASEL_WORKSPACE_ROOT=/workspace",
            "-e", "EASEL_STATE_ROOT=/workspace/state",
            "-e", "HOME=/home/easel",
            "-e", "EASEL_CONFIG_VERSION=0",
            "-e", f"EASEL_CONTAINER_ID={container_id}",
            "-e", "EASEL_WORKER_PUBLIC_KEY_FILE=/keys/worker-public.key",
            args.worker_image,
            "python", "-m", "easel.worker.server", "serve",
            "--host", "0.0.0.0", "--port", "7860",
        ]).stdout.strip()
        containers.append(worker)

        gateway_name = f"easel-task5b-gateway-{run_id}"
        gateway = run([
            "docker", "run", "-d", "--name", gateway_name,
            *labels_args(),
            "--network", network, "--network-alias", "gateway",
            "-v", f"{gateway_launcher}:/run/easel/gateway_launcher.py:ro",
            "-v", f"{public_key}:/run/easel/worker-public.pem:ro",
            "-e", f"WORKSPACE_ID={workspace}",
            "-e", f"GATEWAY_INSTANCE_ID={gateway_instance}",
            "-e", f"BOOT_GENERATION={boot}",
            "-e", f"WORKER_CONTAINER_ID={container_id}",
            "-e", "WORKER_ORIGIN=http://worker:7860",
            "-e", f"WORKER_SERVICE_TOKEN={service}",
            "-e", "PYTHONPATH=/opt/easel",
            args.gateway_image,
            "python", "/run/easel/gateway_launcher.py",
        ]).stdout.strip()
        containers.append(gateway)

        port = 0
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        control_name = f"easel-task5b-control-{run_id}"
        control = run([
            "docker", "run", "-d", "--name", control_name,
            *labels_args(),
            "--network", network, "--network-alias", "control",
            "-p", f"127.0.0.1:{port}:7861",
            "-v", f"{control_volume}:/srv/easel/control",
            "-v", f"{control_launcher}:/run/easel/control_launcher.py:ro",
            "-v", f"{private_key}:/run/easel/control-key.pem:ro",
            "-e", f"WORKSPACE_ID={workspace}",
            "-e", f"WORKER_CONTAINER_ID={container_id}",
            "-e", f"GATEWAY_INSTANCE_ID={gateway_instance}",
            "-e", f"BOOT_GENERATION={boot}",
            "-e", f"WORKER_SERVICE_TOKEN={service}",
            "-e", "GATEWAY_ORIGIN=http://gateway:8897",
            "-e", "WORKER_ORIGIN=http://worker:7860",
            "-e", "PUBLIC_ORIGIN=" + ORIGIN,
            "-e", "CONTROL_DIR=/srv/easel/control",
            "-e", "SIGNING_KEY=/run/easel/control-key.pem",
            "-e", "PYTHONPATH=/opt/easel",
            args.control_image,
            "python", "/run/easel/control_launcher.py",
        ]).stdout.strip()
        containers.append(control)

        base = f"http://127.0.0.1:{port}"
        wait_http(base + "/api/health")
        # 等 launcher 打印一次性邀请。
        invite = ""
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not invite:
            logs = container_logs(control_name)
            for line in logs.splitlines():
                if line.startswith("TASK5B_INVITE "):
                    invite = line.split(" ", 1)[1].strip()
                    break
            if not invite:
                time.sleep(0.3)
        if not invite:
            raise RuntimeError("control 未输出邀请\n" + container_logs(control_name))

        with httpx.Client(base_url=base, timeout=30.0,
                          follow_redirects=False) as client:
            exchanged = client.post(
                "/api/auth/exchange", json={"token": invite},
                headers={"Origin": ORIGIN})
            assert exchanged.status_code == 200, exchanged.text
            body = exchanged.json()
            cookie = exchanged.headers.get("set-cookie", "")
            token = cookie.split(";", 1)[0].split("=", 1)[1]
            headers = {
                "Cookie": f"__Host-easel_session={token}",
                "Origin": ORIGIN,
                "X-CSRF-Token": body["csrfToken"],
            }

            status = client.get("/api/status", headers=headers)
            assert status.status_code == 200, status.text
            assert status.json()["state"] == "ready", status.text
            assert [item["name"] for item in status.json()["personas"]] == [
                "测试画像"]

            # 首次画像读取经 control → gateway → worker。
            personas = client.get("/api/personas", headers=headers)
            assert personas.status_code == 200, personas.text
            assert [item["name"] for item in personas.json()] == ["测试画像"]

            outputs = client.get("/api/outputs", headers=headers)
            assert outputs.status_code == 200, outputs.text
            assert outputs.json()[0]["name"] == "项目"
            text_path = "/api/output/" + quote("项目/文案.txt", safe="")
            text = client.get(text_path, headers=headers)
            assert text.status_code == 200, text.text
            assert text.json()["content"] == "容器私有正文"

            media = "/api/media/" + quote("项目/封面.png", safe="")
            head = client.head(media, headers=headers)
            assert head.status_code == 200, head.text
            assert head.headers["content-length"] == "398752"
            ranged = client.get(media, headers={
                **headers, "Range": "bytes=0-9"})
            assert ranged.status_code == 206, ranged.text
            assert ranged.content == (ROOT / "web" / "static" /
                                      "easel-icon-transparent.png").read_bytes()[:10]
            assert ranged.headers["content-range"] == "bytes 0-9/398752"
            assert ranged.headers["cache-control"].startswith("private")
            assert ranged.headers["etag"].startswith('"easel-')

            missing_media = client.get(
                "/api/media/" + quote("项目/不存在.png", safe=""),
                headers=headers)
            assert missing_media.status_code == 404, missing_media.text
            assert missing_media.json()["code"] == "not_found"
            after_missing = client.get(media, headers={
                **headers, "Range": "bytes=0-9"})
            assert after_missing.status_code == 206, after_missing.text

            upload = client.post(
                "/api/upload",
                files={"files": ("附件.txt", b"container-attachment", "text/plain")},
                data={"sessionId": "session-container"},
                headers=headers)
            assert upload.status_code == 200, upload.text
            saved = upload.json()["files"][0]
            assert saved["path"].startswith("_inbox/")
            saved_cat = run([
                "docker", "exec", worker_name, "cat",
                f"/workspace/outputs/{saved['path']}",
            ]).stdout
            assert saved_cat == "container-attachment"

            save = client.put(
                "/api/persona/" + quote("测试画像", safe="") + "/file",
                json={"filename": "style.md", "content": "容器保存风格"},
                headers=headers)
            assert save.status_code == 200, save.text
            files = client.get(
                "/api/persona/" + quote("测试画像", safe="") + "/files",
                headers=headers)
            assert files.status_code == 200, files.text
            contents = {item["filename"]: item["content"]
                        for item in files.json()["files"]}
            assert contents["style.md"] == "容器保存风格"

            deleted = client.delete(text_path, headers=headers)
            assert deleted.status_code == 200, deleted.text
            missing = client.get(text_path, headers=headers)
            assert missing.status_code == 404, missing.text

            ideas = client.get("/api/ideas", headers=headers)
            assert ideas.status_code == 200, ideas.text
            assert [item["title"] for item in ideas.json()] == [
                "容器初始选题"]
            created_idea = client.post(
                "/api/ideas",
                json={"title": "容器新选题", "note": "角度",
                      "source": "微博热搜", "status": "pending"},
                headers=headers)
            assert created_idea.status_code == 200, created_idea.text
            idea_id = created_idea.json()["id"]
            updated_idea = client.put(
                f"/api/ideas/{idea_id}",
                json={"title": "容器改选题", "note": "",
                      "source": "", "status": "doing"},
                headers=headers)
            assert updated_idea.status_code == 200, updated_idea.text

            schedule = client.get("/api/schedule", headers=headers)
            assert schedule.status_code == 200, schedule.text
            assert [item["title"] for item in schedule.json()] == [
                "容器初始排期"]
            created_schedule = client.post(
                "/api/schedule",
                json={"title": "容器新排期", "date": "2026-11-05",
                      "platform": "微博", "time": "10:00",
                      "status": "scheduled", "note": ""},
                headers=headers)
            assert created_schedule.status_code == 200, created_schedule.text
            schedule_id = created_schedule.json()["id"]
            updated_schedule = client.put(
                f"/api/schedule/{schedule_id}",
                json={"title": "容器改排期", "date": "2026-11-06",
                      "platform": "微博", "time": "10:00",
                      "status": "scheduled", "note": ""},
                headers=headers)
            assert updated_schedule.status_code == 200, updated_schedule.text
            context = client.get("/api/schedule/context?days=14",
                                 headers=headers)
            assert context.status_code == 200, context.text
            assert isinstance(context.json(), dict), context.text
            assert "suggestions" in context.json(), context.text

            unknown = client.delete(
                "/api/ideas/ffffffffffff", headers=headers)
            assert unknown.status_code == 404, unknown.text

            ideas_disk = json.loads(run([
                "docker", "exec", worker_name, "cat",
                "/workspace/outputs/_ideas.json",
            ]).stdout)
            assert {item["title"] for item in ideas_disk} == {
                "容器初始选题", "容器改选题"}, ideas_disk
            schedule_disk = json.loads(run([
                "docker", "exec", worker_name, "cat",
                "/workspace/outputs/_schedule.json",
            ]).stdout)
            assert {item["title"] for item in schedule_disk} == {
                "容器初始排期", "容器改排期"}, schedule_disk

            # 重启 worker 后仍必须从本人卷读回记录；会话/租约不重建。
            run(["docker", "restart", worker_name], timeout=120)
            deadline = time.monotonic() + 90
            restored = None
            while time.monotonic() < deadline:
                response = client.get("/api/ideas", headers=headers)
                if response.status_code == 200:
                    restored = response.json()
                    break
                time.sleep(1.0)
            assert restored is not None, "worker 重启后选题不可读"
            assert {item["title"] for item in restored} == {
                "容器初始选题", "容器改选题"}, restored
            restored_context = client.get(
                "/api/schedule/context?days=14", headers=headers)
            assert restored_context.status_code == 200, restored_context.text

            deleted_idea = client.delete(
                f"/api/ideas/{idea_id}", headers=headers)
            assert deleted_idea.status_code == 200, deleted_idea.text
            deleted_schedule = client.delete(
                f"/api/schedule/{schedule_id}", headers=headers)
            assert deleted_schedule.status_code == 200, deleted_schedule.text

            with httpx.Client(base_url=base, timeout=10.0) as anonymous:
                missing_auth = anonymous.get(media)
            assert missing_auth.status_code == 401, missing_auth.text

        print("CONTAINER_IMAGES", {
            "control": args.control_image,
            "gateway": args.gateway_image,
            "worker": args.worker_image,
        })
        print("CONTAINER_CHECK", {
            "status": True, "personas": True, "outputs": True, "text": True,
            "media_head_range": True, "media_404_typed": True,
            "upload": True, "persona_write": True,
            "delete": True, "anonymous_401": True,
            "ideas_crud": True, "schedule_crud": True,
            "schedule_context": True, "restart_persistence": True,
            "trends_public": "not-run",
        })
        print("TASK5B_CONTAINER_OK", True)
        return 0
    except Exception as error:  # noqa: BLE001
        print("TASK5B_CONTAINER_OK", False)
        print("ERROR", repr(error))
        for name in containers:
            print(f"--- logs {name} ---")
            print(container_logs(name))
        return 1
    finally:
        if not args.keep:
            errors = cleanup(containers, network,
                             [control_volume, worker_volume, home_volume])
            print("CLEANUP_ERRORS", errors)
        shutil.rmtree(work, ignore_errors=True)


GATEWAY_LAUNCHER = r'''
import os
import uvicorn
from cryptography.hazmat.primitives import serialization
from easel.gateway.app import GatewayConfig, create_gateway_app

with open("/run/easel/worker-public.pem", "rb") as stream:
    public_key = serialization.load_pem_public_key(stream.read())
config = GatewayConfig(
    workspace_id=os.environ["WORKSPACE_ID"],
    gateway_instance_id=os.environ["GATEWAY_INSTANCE_ID"],
    boot_generation=os.environ["BOOT_GENERATION"],
    worker_container_id=os.environ["WORKER_CONTAINER_ID"],
    config_version=0,
    worker_origin=os.environ["WORKER_ORIGIN"],
    worker_service_token=os.environ["WORKER_SERVICE_TOKEN"],
)
print("GATEWAY_CONFIG", {
    "workspace": config.workspace_id,
    "gateway": config.gateway_instance_id,
    "boot": config.boot_generation,
    "worker_container": config.worker_container_id,
    "revision": config.config_version,
}, flush=True)
app = create_gateway_app(config, public_key, activated=True)
uvicorn.run(app, host="0.0.0.0", port=8897, proxy_headers=False)
'''

CONTROL_LAUNCHER = r'''
import os
import time
from pathlib import Path

import uvicorn
from cryptography.hazmat.primitives import serialization
from easel.control.app import ControlConfig, create_app
from easel.control.auth import AuthService
from easel.control.gateway_client import GatewayClient
from easel.control.store import Store
from easel.control.types import WorkerHandle
from easel.control.workspace_api import WorkspaceApi

control_dir = Path(os.environ["CONTROL_DIR"])
store = Store.connect(control_dir / "control.sqlite3")
admin = store.create_user("ContainerOwner", "admin")
member = store.create_user("ContainerMember")
auth = AuthService(store)
invite = auth.create_invite(admin, member.user_id)
workspace = os.environ["WORKSPACE_ID"]
# 测试夹具固定 workspace id 以便 gateway/worker 用同一 audience；
# 生产由 Store.create_user 自行生成，不经过此路径。
store._connection.execute(
    "UPDATE workspaces SET id = ? WHERE id = ?",
    (workspace, member.workspace_id))
store._connection.commit()
handle = WorkerHandle(
    workspace, os.environ["WORKER_CONTAINER_ID"],
    os.environ["WORKER_ORIGIN"], os.environ["WORKER_SERVICE_TOKEN"], 0,
    gateway_instance_id=os.environ["GATEWAY_INSTANCE_ID"],
    gateway_generation=os.environ["BOOT_GENERATION"],
    gateway_upstream=os.environ["GATEWAY_ORIGIN"],
    gateway_state="available",
)
print("CONTROL_HANDLE", {
    "workspace": handle.workspace_id,
    "gateway": handle.gateway_instance_id,
    "boot": handle.gateway_generation,
    "worker_container": handle.container_id,
    "revision": handle.config_version,
}, flush=True)
now = int(time.time())
outcome, lease = store.claim_worker_start(
    workspace, 0, "container-seed", now, 3600)
assert outcome == "started", outcome
assert store.complete_worker_start(
    workspace, "container-seed", lease.generation, handle, now, 3600)
with open(os.environ["SIGNING_KEY"], "rb") as stream:
    signing_key = serialization.load_pem_private_key(
        stream.read(), password=None)

class Runtime:
    async def ensure_ready(self, workspace_id, config_version):
        assert workspace_id == handle.workspace_id
        assert config_version == handle.config_version
        return handle

runtime = Runtime()
def workspace_factory(runtime, store, http_client, db_runner):
    return WorkspaceApi(
        runtime, store, GatewayClient(signing_key, http_client),
        revision=0, db_runner=db_runner, gateway_required=True)

app = create_app(
    store=store, auth=auth,
    config=ControlConfig(public_origin=os.environ["PUBLIC_ORIGIN"]),
    runtime=runtime, frontend_dir="/opt/easel/web/frontend/dist",
    workspace_factory=workspace_factory,
)
store.close()
print("TASK5B_INVITE " + invite, flush=True)
uvicorn.run(app, host="0.0.0.0", port=7861, proxy_headers=False)
'''


if __name__ == "__main__":
    sys.exit(main())
