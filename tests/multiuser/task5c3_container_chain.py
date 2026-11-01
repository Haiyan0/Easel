"""Task 5C3 隔离容器闭环：真实镜像内发布子进程/Chromium、导出与清理。

真实层：增量构建的 control/gateway/worker 镜像、固定身份/签名、Gateway、
WorkspaceApi、WorkerRelay、RunManager、发布执行器、镜像内受控发布子进程与
Playwright Chromium、真实 ZIP 导出/下载。

替身层：原平台脚本（xhs_publish.py / douyin_publish.py / web_publisher.py /
weixin_mp_stats.py / biliup）不执行；由固定受控 runner 代替，不访问真实平台。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "https://127.0.0.1:7861"
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8"
    b"\xff\xff?\x00\x05\xfe\x02\xfe\xa75\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82")
VIDEO = b"\x00\x00\x00\x18ftypmp42container-video"


def run(argv: list[str], *, timeout: float = 180,
        check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed {argv[:4]} rc={result.returncode}\n"
            f"stdout={result.stdout[-3000:]}\nstderr={result.stderr[-3000:]}")
    return result


def labels_args(component: str) -> list[str]:
    return ["--label", "easel.task=5c3",
            "--label", f"easel.component={component}"]


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
    return isinstance(labels, dict) and labels.get("easel.task") == "5c3" \
        and labels.get("easel.component") in {
            "control", "gateway", "worker", "control-volume",
            "worker-volume", "home-volume", "network"}


def cleanup(containers: list[str], network: str,
            volumes: list[str]) -> list[str]:
    errors: list[str] = []
    for name in containers:
        if not name:
            continue
        if not owned("container", name):
            errors.append(f"refuse foreign container {name}")
            continue
        result = run(["docker", "rm", "-f", name], check=False)
        if result.returncode != 0:
            errors.append(f"rm {name}: {result.stderr.strip()[:200]}")
    if owned("network", network):
        result = run(["docker", "network", "rm", network], check=False)
        if result.returncode != 0:
            errors.append(f"network {network}: {result.stderr.strip()[:200]}")
    else:
        errors.append(f"refuse foreign network {network}")
    for name in volumes:
        if not owned("volume", name):
            errors.append(f"refuse foreign volume {name}")
            continue
        result = run(["docker", "volume", "rm", name], check=False)
        if result.returncode != 0:
            errors.append(f"volume {name}: {result.stderr.strip()[:200]}")
    return errors


def wait_http(url: str, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=3.0, verify=False)
            if response.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.4)
    raise RuntimeError(f"http not ready: {url}")


def container_logs(name: str, limit: int = 8000) -> str:
    result = run(["docker", "logs", name], check=False)
    text = (result.stdout or "") + (result.stderr or "")
    return text[-limit:]


def top(name: str) -> str:
    result = run(["docker", "top", name], check=False)
    return (result.stdout or "") + (result.stderr or "")


def sql(control_name: str, query: str) -> list:
    code = (
        "import json, sqlite3\n"
        f"c=sqlite3.connect('/srv/easel/control/control.sqlite3')\n"
        f"print(json.dumps(c.execute({query!r}).fetchall()))\n")
    result = run(["docker", "exec", control_name, "python", "-c", code])
    return json.loads(result.stdout)


def active_runs(control_name: str) -> int:
    rows = sql(control_name,
               "SELECT COUNT(*) FROM runs WHERE status NOT IN"
               " ('done','failed','interrupted','cancelled')")
    return int(rows[0][0])


def run_state(control_name: str, run_id: str) -> str:
    rows = sql(control_name,
               f"SELECT status FROM runs WHERE id='{run_id}'")
    return rows[0][0] if rows else "missing"


def wait_state(control_name: str, run_id: str, states: tuple[str, ...],
               timeout: float = 30) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = run_state(control_name, run_id)
        if state in states:
            return state
        time.sleep(0.2)
    raise AssertionError((run_id, state, states))


def wait_status(client, platform: str, states: tuple[str, ...],
                headers: dict, timeout: float = 40) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            response = client.get(
                f"/api/publish/{platform}/status", headers=headers)
            if response.status_code == 200:
                last = response.json()
                if last.get("state") in states:
                    return last
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise AssertionError(last)


def image_hash(image: str, path: str) -> str:
    result = run(["docker", "run", "--rm", "--entrypoint", "sha256sum",
                  image, path], timeout=120)
    return result.stdout.strip().split()[0]


def host_hash(relative: str) -> str:
    return hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()


def wait_publish_capacity(control_name: str, worker_name: str,
                          timeout: float = 15) -> None:
    """等上一条 Run 的 control 容量与 worker 会话锁都释放。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if active_runs(control_name) == 0 \
                and "task5c3_controlled_publisher" not in top(worker_name):
            time.sleep(0.5)
            return
        time.sleep(0.2)
    raise AssertionError(("capacity not released",
                          active_runs(control_name), top(worker_name)[-500:]))


def worker_counter(worker_name: str) -> list[str]:
    result = run([
        "docker", "exec", worker_name, "cat",
        "/workspace/outputs/_publish/controlled-count.txt"], check=False)
    text = (result.stdout or "").strip()
    return [line for line in text.splitlines() if line.strip()]


def prepare_build_context(work: Path) -> Path:
    context = work / "build-context"
    shutil.copytree(
        ROOT / "easel", context / "easel",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    (context / "web").mkdir(parents=True)
    shutil.copy2(ROOT / "web" / "app.py", context / "web" / "app.py")
    shutil.copytree(ROOT / "web" / "frontend" / "dist",
                    context / "web" / "frontend" / "dist")
    for name in ("Dockerfile.control-task5c3",
                 "Dockerfile.gateway-task5c3",
                 "Dockerfile.worker-task5c3"):
        shutil.copy2(ROOT / "tests" / "multiuser" / name, context / name)
    return context


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-image", default="easel-control:task5c3-20261101")
    parser.add_argument("--gateway-image", default="easel-gateway:task5c3-20261101")
    parser.add_argument("--worker-image",
                        default="easel-worker-execution:task5c3-20261101")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    work = Path(tempfile.mkdtemp(prefix="easel-task5c3-container-"))
    contexts = {
        "control": "Dockerfile.control-task5c3",
        "gateway": "Dockerfile.gateway-task5c3",
        "worker": "Dockerfile.worker-task5c3",
    }
    images = {
        "control": args.control_image,
        "gateway": args.gateway_image,
        "worker": args.worker_image,
    }
    if args.build:
        try:
            context = prepare_build_context(work)
            for component, dockerfile in contexts.items():
                print(f"BUILD {component}", flush=True)
                run(["docker", "build", "-f", str(context / dockerfile),
                     "-t", images[component], str(context)], timeout=900)
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise

    for name, image in images.items():
        inspected = run(["docker", "image", "inspect", image], check=False)
        if inspected.returncode != 0:
            print(f"缺少镜像：{image}", file=sys.stderr)
            return 2
        print("CONTAINER_IMAGE", name, run([
            "docker", "image", "inspect", "--format", "{{.Id}}", image
        ]).stdout.strip(), flush=True)

    for name, image, paths in (
            ("control", images["control"],
             ["easel/control/workspace_publishing.py",
              "easel/control/app.py", "easel/control/tasks.py",
              "easel/control/proxy.py", "easel/manual_publish.py"]),
            ("gateway", images["gateway"], ["easel/gateway/app.py"]),
            ("worker", images["worker"],
             ["easel/worker/browser_tasks.py", "easel/manual_publish.py",
              "web/app.py"])):
        for relative in paths:
            inside = image_hash(image, f"/opt/easel/{relative}")
            host = host_hash(relative)
            assert inside == host, (name, relative, inside, host)
            print(f"SOURCE_HASH {name} {relative} {host}", flush=True)
    dist = sorted((ROOT / "web" / "frontend" / "dist" / "assets").glob(
        "index-*.js"))[-1]
    assert image_hash(
        images["control"],
        f"/opt/easel/web/frontend/dist/assets/{dist.name}") == \
        hashlib.sha256(dist.read_bytes()).hexdigest()
    print(f"SOURCE_HASH control web/frontend/dist/assets/{dist.name}",
          hashlib.sha256(dist.read_bytes()).hexdigest(), flush=True)

    run_id = secrets.token_hex(8)
    workspace = secrets.token_hex(16)
    service = "service-" + secrets.token_urlsafe(24)
    container_id = secrets.token_hex(32)
    gateway_instance = f"gateway-{run_id}"
    boot = secrets.token_hex(16)
    network = f"easel-task5c3-net-{run_id}"
    control_volume = f"easel-task5c3-control-{run_id}"
    worker_volume = f"easel-task5c3-ws-{run_id}"
    home_volume = f"easel-task5c3-home-{run_id}"
    containers: list[str] = []
    try:
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
        (seed / "outputs" / "project").mkdir(parents=True)
        (seed / "outputs" / "_login").mkdir(parents=True)
        (seed / "outputs" / "_publish").mkdir(parents=True)
        (seed / "outputs" / "project" / "cover.png").write_bytes(PNG)
        (seed / "outputs" / "project" / "clip.mp4").write_bytes(VIDEO)
        for platform in ("xiaohongshu", "douyin", "kuaishou"):
            (seed / "outputs" / "_login" / f"{platform}.json").write_text(
                json.dumps({"state": "success", "message": "容器受控已登录",
                            "qr": "", "ts": int(time.time())},
                           ensure_ascii=False), encoding="utf-8")
        (seed / "profiles").mkdir(parents=True)
        (seed / "state").mkdir(parents=True)

        for name, source in (
                ("worker_launcher.py", WORKER_LAUNCHER),
                ("gateway_launcher.py", GATEWAY_LAUNCHER),
                ("control_launcher.py", CONTROL_LAUNCHER)):
            (work / name).write_text(source, encoding="utf-8")

        run(["docker", "network", "create", *labels_args("network"), network])
        volumes = [control_volume, worker_volume, home_volume]
        for name, component in ((control_volume, "control-volume"),
                                (worker_volume, "worker-volume"),
                                (home_volume, "home-volume")):
            run(["docker", "volume", "create", *labels_args(component), name])
        run([
            "docker", "run", "--rm", "--user", "root",
            "-v", f"{worker_volume}:/workspace",
            "-v", f"{home_volume}:/home/easel",
            "-v", f"{seed}:/seed:ro",
            "--entrypoint", "sh", images["worker"], "-c",
            "cp -a /seed/outputs/. /workspace/outputs/ "
            "&& cp -a /seed/profiles/. /workspace/profiles/ "
            "&& cp -a /seed/state/. /workspace/state/ "
            "&& chown -R 10001:10001 /workspace /home/easel",
        ])

        worker_name = f"easel-task5c3-worker-{run_id}"
        containers.append(run([
            "docker", "run", "-d", "--name", worker_name,
            *labels_args("worker"),
            "--security-opt", "seccomp=unconfined",
            "--network", network, "--network-alias", "worker",
            "-v", f"{worker_volume}:/workspace",
            "-v", f"{home_volume}:/home/easel",
            "-v", f"{public_key}:/keys/worker-public.key:ro",
            "-v", f"{work / 'worker_launcher.py'}:/run/easel/worker_launcher.py:ro",
            "-v", (ROOT / "tests" / "multiuser" /
                   "task5c3_controlled_publisher.py").as_posix()
            + ":/run/easel/task5c3_controlled_publisher.py:ro",
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
            "-e", "PYTHONPATH=/opt/easel",
            images["worker"],
            "python", "/run/easel/worker_launcher.py",
        ]).stdout.strip())

        gateway_name = f"easel-task5c3-gateway-{run_id}"
        containers.append(run([
            "docker", "run", "-d", "--name", gateway_name,
            *labels_args("gateway"),
            "--network", network, "--network-alias", "gateway",
            "-v", f"{work / 'gateway_launcher.py'}:/run/easel/gateway_launcher.py:ro",
            "-v", f"{public_key}:/run/easel/worker-public.pem:ro",
            "-e", f"WORKSPACE_ID={workspace}",
            "-e", f"GATEWAY_INSTANCE_ID={gateway_instance}",
            "-e", f"BOOT_GENERATION={boot}",
            "-e", f"WORKER_CONTAINER_ID={container_id}",
            "-e", "WORKER_ORIGIN=http://worker:7860",
            "-e", f"WORKER_SERVICE_TOKEN={service}",
            "-e", "PYTHONPATH=/opt/easel",
            images["gateway"],
            "python", "/run/easel/gateway_launcher.py",
        ]).stdout.strip())

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        origin = f"https://127.0.0.1:{port}"
        control_name = f"easel-task5c3-control-{run_id}"
        containers.append(run([
            "docker", "run", "-d", "--name", control_name,
            *labels_args("control"),
            "--network", network, "--network-alias", "control",
            "-p", f"127.0.0.1:{port}:7861",
            "-v", f"{control_volume}:/srv/easel/control",
            "-v", f"{work / 'control_launcher.py'}:/run/easel/control_launcher.py:ro",
            "-v", f"{private_key}:/run/easel/control-key.pem:ro",
            "-e", f"WORKSPACE_ID={workspace}",
            "-e", f"WORKER_CONTAINER_ID={container_id}",
            "-e", f"GATEWAY_INSTANCE_ID={gateway_instance}",
            "-e", f"BOOT_GENERATION={boot}",
            "-e", f"WORKER_SERVICE_TOKEN={service}",
            "-e", "GATEWAY_ORIGIN=http://gateway:8897",
            "-e", "WORKER_ORIGIN=http://worker:7860",
            "-e", f"PUBLIC_ORIGIN={origin}",
            "-e", "CONTROL_DIR=/srv/easel/control",
            "-e", "SIGNING_KEY=/run/easel/control-key.pem",
            "-e", "PYTHONPATH=/opt/easel",
            images["control"],
            "python", "/run/easel/control_launcher.py",
        ]).stdout.strip())

        base = f"http://127.0.0.1:{port}"
        wait_http(base + "/api/health")
        invite = ""
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not invite:
            for line in container_logs(control_name).splitlines():
                if line.startswith("TASK5C3_INVITE "):
                    invite = line.split(" ", 1)[1].strip()
                    break
            if not invite:
                time.sleep(0.3)
        if not invite:
            raise RuntimeError("control 未输出邀请\n" + container_logs(control_name))

        with httpx.Client(base_url=base, timeout=30.0,
                          verify=False, follow_redirects=False) as client:
            exchanged = client.post(
                "/api/auth/exchange", json={"token": invite},
                headers={"Origin": origin})
            assert exchanged.status_code == 200, exchanged.text
            body = exchanged.json()
            token = exchanged.headers["set-cookie"].split(
                ";", 1)[0].split("=", 1)[1]
            headers = {
                "Cookie": f"__Host-easel_session={token}",
                "Origin": origin,
                "X-CSRF-Token": body["csrfToken"],
            }
            policy = client.get("/api/publishing-policy", headers=headers)
            assert policy.status_code == 200, policy.text
            assert policy.json()["mode"] == "automated", policy.text

            # 1) 人工导出：真实 ZIP + 下载 + 解包，published=false。
            exported = client.post(
                "/api/manual-publish/xiaohongshu/export",
                json={"title": "容器人工稿件", "body": "容器正文",
                      "tags": "工具", "media": ["project/cover.png"],
                      "reviewed": True, "requestId": "container-export"},
                headers=headers)
            assert exported.status_code == 200, exported.text
            export_body = exported.json()
            assert export_body["published"] is False, export_body
            downloaded = client.get(
                f"/api/media/{export_body['path']}", headers=headers)
            assert downloaded.status_code == 200, downloaded.text
            with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
                assert archive.read("title.txt").decode() == "容器人工稿件"
                assert archive.read("media/01_cover.png") == PNG
                manifest = json.loads(archive.read("manifest.json"))
                assert manifest["status"] == "ready_for_manual_submission"
                assert "container-secret" not in " ".join(archive.namelist())
            export_ls = run([
                "docker", "exec", worker_name, "ls", "-l",
                "/workspace/outputs/_publish/manual"]).stdout
            assert "easel-xiaohongshu-" in export_ls, export_ls
            print("EXPORT", export_body["path"], flush=True)

            # 2) 自动发布 xhs：一次提交 + 日历一次 + 镜像内受控 runner。
            published = client.post(
                "/api/publish/xiaohongshu",
                json={"title": "容器自动稿件", "body": "容器正文",
                      "tags": "工具", "media": ["project/cover.png"],
                      "persona": "", "requestId": "container-publish-1"},
                headers=headers)
            assert published.status_code == 200, published.text
            result = published.json()
            assert result["ok"] is True and result["state"] == "success", result
            run_id_1 = result["runId"]
            assert wait_state(control_name, run_id_1, ("done",)) == "done"
            assert worker_counter(worker_name) == [run_id_1]
            proof = run([
                "docker", "exec", worker_name, "cat",
                f"/workspace/outputs/_publish/controlled-proofs/{run_id_1}.json"
            ]).stdout
            proof = json.loads(proof)
            assert proof["profile_used"] is True, proof
            assert proof["media"][0]["sha256"] == hashlib.sha256(PNG).hexdigest()
            schedule = run([
                "docker", "exec", worker_name, "cat",
                "/workspace/outputs/_schedule.json"]).stdout
            assert len(json.loads(schedule)) == 1, schedule
            duplicate = client.post(
                "/api/publish/xiaohongshu",
                json={"title": "容器自动稿件", "body": "容器正文",
                      "tags": "工具", "media": ["project/cover.png"],
                      "persona": "", "requestId": "container-publish-1"},
                headers=headers)
            assert duplicate.status_code == 200, duplicate.text
            assert duplicate.json()["runId"] == run_id_1
            assert worker_counter(worker_name) == [run_id_1]
            print("PUBLISH_ONCE", run_id_1, flush=True)
            wait_publish_capacity(control_name, worker_name)

            # 3) 抖音短信：错误码不完成，正确码只绑定当前 Run。
            started = client.post(
                "/api/publish/douyin",
                json={"title": "容器抖音", "body": "容器正文",
                      "tags": "", "media": ["project/clip.mp4"],
                      "persona": "", "requestId": "container-douyin"},
                headers=headers)
            assert started.status_code == 200, started.text
            douyin_run = started.json()["runId"]
            assert started.json()["async"] is True
            status = wait_status(client, "douyin", ("sms_required",), headers)
            assert status["runId"] == douyin_run, status
            wrong = client.post(
                "/api/publish/douyin/sms",
                json={"code": "000000", "runId": douyin_run},
                headers=headers)
            assert wrong.status_code == 200, wrong.text
            wait_status(client, "douyin", ("sms_required",), headers)
            correct = client.post(
                "/api/publish/douyin/sms",
                json={"code": "123456", "runId": douyin_run},
                headers=headers)
            assert correct.status_code == 200, correct.text
            assert wait_status(
                client, "douyin", ("success",), headers)["runId"] == douyin_run
            assert len(worker_counter(worker_name)) == 2
            print("DOUYIN_SMS", douyin_run, flush=True)
            wait_publish_capacity(control_name, worker_name)

            # 4) 停止：只停止本地执行，外部结果明确未知；进程树释放。
            stopping_response = client.post(
                "/api/publish/douyin",
                json={"title": "容器停止", "body": "容器正文", "tags": "",
                      "media": ["project/clip.mp4"], "persona": "",
                      "requestId": "container-stop"},
                headers=headers)
            assert stopping_response.status_code == 200, stopping_response.text
            stop_run = stopping_response.json()["runId"]
            wait_status(client, "douyin", ("sms_required",), headers)
            stopped = client.post(
                "/api/chat/stop", json={"runId": stop_run}, headers=headers)
            assert stopped.status_code == 200, stopped.text
            assert wait_status(
                client, "douyin", ("unknown",), headers)["state"] == "unknown"
            assert wait_state(control_name, stop_run,
                              ("cancelled", "interrupted")) in (
                                  "cancelled", "interrupted")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if "task5c3_controlled_publisher" not in top(worker_name) \
                        and active_runs(control_name) == 0:
                    break
                time.sleep(0.3)
            assert "task5c3_controlled_publisher" not in top(worker_name)
            assert active_runs(control_name) == 0
            assert len(worker_counter(worker_name)) == 3
            print("STOP_RELEASE", stop_run, flush=True)
            wait_publish_capacity(control_name, worker_name)

            # 5) 执行器超时：受控 runner 超过 3s，进程树终止并释放容量。
            timed = client.post(
                "/api/publish/kuaishou",
                json={"title": "容器超时", "body": "容器正文", "tags": "",
                      "media": ["project/cover.png"], "persona": "",
                      "requestId": "container-timeout"},
                headers=headers)
            assert timed.status_code == 200, timed.text
            assert timed.json()["state"] == "unknown", timed.text
            assert timed.json()["message"].startswith("发布超时"), timed.text
            assert active_runs(control_name) == 0
            assert "task5c3_controlled_publisher" not in top(worker_name)
            assert len(worker_counter(worker_name)) == 4
            print("TIMEOUT_RELEASE", True, flush=True)
            wait_publish_capacity(control_name, worker_name)

            # 6) 查询暂时失败后可恢复，且不创建新 Run。
            before = len(worker_counter(worker_name))
            flaky_response = client.post(
                "/api/publish/douyin",
                json={"title": "容器查询恢复", "body": "容器正文", "tags": "",
                      "media": ["project/clip.mp4"], "persona": "",
                      "requestId": "container-flaky"},
                headers=headers)
            assert flaky_response.status_code == 200, flaky_response.text
            flaky_run = flaky_response.json()["runId"]
            wait_status(client, "douyin", ("sms_required",), headers)
            run(["docker", "pause", worker_name])
            try:
                failed = None
                try:
                    failed = client.get(
                        "/api/publish/douyin/status", headers=headers,
                        timeout=8.0)
                except httpx.HTTPError as error:
                    print("STATUS_TRANSIENT", type(error).__name__, flush=True)
                if failed is not None:
                    assert failed.status_code in (503, 504), failed.text
            finally:
                run(["docker", "unpause", worker_name])
            recovered = wait_status(
                client, "douyin", ("sms_required",), headers, timeout=30)
            assert recovered["runId"] == flaky_run
            accepted = client.post(
                "/api/publish/douyin/sms",
                json={"code": "123456", "runId": flaky_run},
                headers=headers)
            assert accepted.status_code == 200, accepted.text
            wait_status(client, "douyin", ("success",), headers)
            assert len(worker_counter(worker_name)) == before + 1
            print("STATUS_RECOVERY", flaky_run, flush=True)

            # 7) worker 重启后不重放已终态发布。
            before_restart = len(worker_counter(worker_name))
            runs_before = sql(control_name, "SELECT COUNT(*) FROM runs")[0][0]
            run(["docker", "restart", worker_name], timeout=120)
            time.sleep(5)
            replay = client.get(
                "/api/publish/douyin/status", headers=headers)
            assert replay.status_code == 200, replay.text
            assert replay.json()["state"] == "success", replay.text
            assert len(worker_counter(worker_name)) == before_restart
            runs_after = sql(control_name, "SELECT COUNT(*) FROM runs")[0][0]
            assert runs_after == runs_before, (runs_before, runs_after)
            print("RESTART_NO_REPLAY", True, flush=True)

        print("CONTAINER_CHECKS", json.dumps({
            "export_download_unpack": True, "publish_once": True,
            "calendar_once": True, "douyin_sms_owner": True,
            "stop_release": True, "timeout_release": True,
            "status_recovery": True, "restart_no_replay": True,
            "controlled_runner_executed": True,
            "original_platform_scripts_executed": [],
            "substituted_entries": [
                "xhs_publish.py", "douyin_publish.py", "web_publisher.py",
                "weixin_mp_stats.py", "biliup"],
        }, ensure_ascii=False), flush=True)
        print("TASK5C3_CONTAINER_OK", True, flush=True)
        return 0
    except Exception as error:  # noqa: BLE001
        print("TASK5C3_CONTAINER_OK", False)
        print("ERROR", repr(error))
        for name in containers:
            print(f"--- logs {name} ---")
            print(container_logs(name))
        return 1
    finally:
        if not args.keep:
            errors = cleanup(containers, network,
                             [control_volume, worker_volume, home_volume])
            print("CLEANUP_ERRORS", errors, flush=True)
        shutil.rmtree(work, ignore_errors=True)


WORKER_LAUNCHER = r'''
import asyncio
import os
import sys

import uvicorn
from web import app as worker

RUNNER = "/run/easel/task5c3_controlled_publisher.py"


def _env():
    env = worker._proxy_env()
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return env


async def controlled_build(platform, title, body, tags, media_paths, *,
                           status_dir=None):
    status_dir = status_dir or worker.PUBLISH_DIR
    status_dir.mkdir(parents=True, exist_ok=True)
    status_file = status_dir / "controlled.json"
    code_file = status_dir / "controlled.code"
    proof_file = (worker.OUTPUTS_DIR / "_publish" / "controlled-proofs"
                  / f"{status_dir.name}.json")
    counter_file = worker.OUTPUTS_DIR / "_publish" / "controlled-count.txt"
    cmd = [sys.executable, RUNNER, "publish", "--platform", platform,
           "--title", title, "--profile-name", platform,
           "--status-file", str(status_file),
           "--code-file", str(code_file),
           "--proof-file", str(proof_file),
           "--counter-file", str(counter_file),
           "--sleep", "0.2"]
    for path in media_paths:
        cmd += ["--media", str(path)]
    timeout = 120
    if platform == "kuaishou":
        cmd += ["--sleep", "30"]
        timeout = 3
    is_async = platform == "douyin"
    return {
        "cmd": cmd, "env": _env(), "title": title, "timeout": timeout,
        "async": is_async,
        "status_file": status_file if is_async else None,
        "code_file": code_file if is_async else None,
        "publish_type": "视频",
    }


worker._build_publish_command = controlled_build
uvicorn.run(worker.app, host="0.0.0.0", port=7860, proxy_headers=False)
'''

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
from easel.control.proxy import PinnedRunConfig, WorkerRelay
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

    async def bound_handle(self, workspace_id, container, config_version):
        if (workspace_id != handle.workspace_id
                or container != handle.container_id
                or config_version != handle.config_version):
            return None
        return handle


runtime = Runtime()


def workspace_factory(runtime, store, http_client, db_runner):
    return WorkspaceApi(
        runtime, store, GatewayClient(signing_key, http_client),
        revision=0, db_runner=db_runner, gateway_required=True)


def relay_factory(runtime, sessions, auth, store, http_client, broker,
                  db_runner):
    return WorkerRelay(
        runtime, sessions, auth, store=store,
        signing_key=signing_key,
        config_provider=lambda actor: PinnedRunConfig(
            revision=0, routes={}, agent={
                "runtime": "acp", "command": ["fixture-acp"],
                "sandbox": "read-only", "timeout": 15,
            }, broker=None),
        http_client=http_client,
        gateway_client=GatewayClient(signing_key, http_client),
        db_runner=db_runner, gateway_required=True)


app = create_app(
    store=store, auth=auth,
    config=ControlConfig(public_origin=os.environ["PUBLIC_ORIGIN"],
                         publish_mode="automated"),
    runtime=runtime, frontend_dir="/opt/easel/web/frontend/dist",
    workspace_factory=workspace_factory, relay_factory=relay_factory)
store.close()
print("TASK5C3_INVITE " + invite, flush=True)
uvicorn.run(app, host="0.0.0.0", port=7861, proxy_headers=False)
'''


if __name__ == "__main__":
    sys.exit(main())
