"""Task 5C2 R1 隔离容器闭环（手动执行，不进入默认 pytest）。

增量构建/复用当前源码的 control、gateway、worker 镜像，在全新标签的容器、
网络和卷里跑真实 control→gateway→worker→RunManager 链路。worker 内由固定
受控 runner 以真实子进程启动镜像内 Playwright/Chromium，读写本人 HOME 的
持久 profile、二维码和状态文件；平台站点/账号为本地夹具。

不访问真实账号/付费服务，不复用用户容器、卷或 socket。清理前逐个 inspect
归属标签，只删除本轮资源。

示例：
  python tests/multiuser/task5c2_container_chain.py --build \
    --control-image easel-control:task5c2-r1-20261101 \
    --gateway-image easel-gateway:task5c2-r1-20261101 \
    --worker-image easel-worker-execution:task5c2-r1-20261101
"""
from __future__ import annotations

import argparse
import hashlib
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

import httpx

ROOT = Path(__file__).resolve().parents[2]
LABEL = "easel.test.run-id"
RUN = f"task5c2-{secrets.token_hex(6)}"
ORIGIN = "https://easel.test"
TERMINAL = ("done", "failed", "interrupted", "cancelled")


def run(argv: list[str], *, timeout: float = 180,
        check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(argv)}\n"
            f"stdout={result.stdout[-3000:]}\nstderr={result.stderr[-3000:]}")
    return result


def labels_args(component: str) -> list[str]:
    return ["--label", f"{LABEL}={RUN}",
            "--label", f"easel.test.component={component}"]


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


def wait_http(url: str, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=2)
            if response.status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"service did not become ready: {url}")


def container_logs(name: str, limit: int = 8000) -> str:
    result = run(["docker", "logs", name], check=False)
    text = (result.stdout or "") + (result.stderr or "")
    return text[-limit:]


def top(name: str) -> str:
    result = run(["docker", "top", name], check=False)
    return result.stdout or ""


def sql(control_name: str, query: str) -> list:
    code = (
        "import json, sqlite3\n"
        "connection = sqlite3.connect('/srv/easel/control/control.sqlite3')\n"
        f"print(json.dumps(connection.execute({query!r}).fetchall()))\n")
    result = run(["docker", "exec", control_name, "python", "-c", code])
    return json.loads(result.stdout)


def active_runs(control_name: str) -> int:
    rows = sql(
        control_name,
        "SELECT COUNT(*) FROM runs WHERE status NOT IN"
        " ('done','failed','interrupted','cancelled')")
    return int(rows[0][0])


def run_state(control_name: str, run_id: str) -> str:
    rows = sql(
        control_name,
        f"SELECT status FROM runs WHERE id = '{run_id}'")
    return str(rows[0][0]) if rows else "missing"


def wait_state(control_name: str, run_id: str, states: tuple[str, ...],
               timeout: float = 30) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = run_state(control_name, run_id)
        if state in states:
            return state
        time.sleep(0.3)
    raise AssertionError(f"run {run_id} did not reach {states}: "
                         f"{run_state(control_name, run_id)}")


def wait_status(client, platform: str, states: tuple[str, ...],
                headers: dict, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/login/{platform}/status",
                              headers=headers)
        if response.status_code == 200:
            last = response.json()
            if last.get("state") in states:
                return last
        time.sleep(0.3)
    raise AssertionError(f"status {platform} did not reach {states}: {last}")


def image_hash(image: str, path: str) -> str:
    result = run(["docker", "run", "--rm", "--entrypoint", "sha256sum",
                  image, path])
    return result.stdout.split()[0]


def host_hash(relative: str) -> str:
    return hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()


def prepare_build_context(work: Path) -> Path:
    """只把运行源码/当前 dist 放进临时上下文，避免复制 node_modules。

    仓库根 `.dockerignore` 会排除 dist；增量镜像必须带上当前前端构建，
    因此这里用独立的小上下文（不携带开发者状态/凭证）。
    """
    context = work / "build-context"
    shutil.copytree(
        ROOT / "easel", context / "easel",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    (context / "web").mkdir(parents=True)
    shutil.copy2(ROOT / "web" / "app.py", context / "web" / "app.py")
    shutil.copytree(ROOT / "web" / "frontend" / "dist",
                    context / "web" / "frontend" / "dist")
    for name in ("Dockerfile.control-task5c2-r1",
                 "Dockerfile.gateway-task5c2-r1",
                 "Dockerfile.worker-task5c2-r1"):
        shutil.copy2(ROOT / "tests" / "multiuser" / name, context / name)
    return context


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-image", default="easel-control:task5c2-r1-20261101")
    parser.add_argument("--gateway-image", default="easel-gateway:task5c2-r1-20261101")
    parser.add_argument("--worker-image",
                        default="easel-worker-execution:task5c2-r1-20261101")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    work = Path(tempfile.mkdtemp(prefix="easel-task5c2-container-"))
    if args.build:
        try:
            context = prepare_build_context(work)
            print("BUILD control", flush=True)
            run(["docker", "build", "-f",
                 str(context / "Dockerfile.control-task5c2-r1"),
                 "-t", args.control_image, str(context)], timeout=900)
            print("BUILD gateway", flush=True)
            run(["docker", "build", "-f",
                 str(context / "Dockerfile.gateway-task5c2-r1"),
                 "-t", args.gateway_image, str(context)], timeout=900)
            print("BUILD worker", flush=True)
            run(["docker", "build", "-f",
                 str(context / "Dockerfile.worker-task5c2-r1"),
                 "-t", args.worker_image, str(context)], timeout=900)
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise

    for image in (args.control_image, args.gateway_image, args.worker_image):
        inspected = run(["docker", "image", "inspect", image], check=False)
        if inspected.returncode != 0:
            print(f"缺少镜像：{image}", file=sys.stderr)
            return 2

    images = {
        name: run(["docker", "image", "inspect", "--format", "{{.Id}}",
                   image]).stdout.strip()
        for name, image in (("control", args.control_image),
                            ("gateway", args.gateway_image),
                            ("worker", args.worker_image))
    }
    print("CONTAINER_IMAGE_IDS", json.dumps(images, sort_keys=True), flush=True)

    # 镜像内源码必须与宿主当前修复一致（不只看“镜像能启动”）。
    for name, image, paths in (
            ("control", args.control_image,
             ["easel/control/workspace_accounts.py",
              "easel/control/app.py", "easel/control/policy.py",
              "easel/control/tasks.py"]),
            ("gateway", args.gateway_image, ["easel/gateway/app.py"]),
            ("worker", args.worker_image,
             ["easel/worker/runs.py", "easel/worker/browser_tasks.py",
              "web/app.py"])):
        for relative in paths:
            inside = image_hash(image, f"/opt/easel/{relative}")
            host = host_hash(relative)
            assert inside == host, (name, relative, inside, host)
            print(f"SOURCE_HASH {name} {relative} {host}", flush=True)
    dist = sorted((ROOT / "web" / "frontend" / "dist" / "assets").glob(
        "index-*.js"))[-1]
    dist_inside = image_hash(
        args.control_image, f"/opt/easel/web/frontend/dist/assets/{dist.name}")
    dist_host = hashlib.sha256(dist.read_bytes()).hexdigest()
    assert dist_inside == dist_host, (dist.name, dist_inside, dist_host)
    print(f"SOURCE_HASH control web/frontend/dist/assets/{dist.name} {dist_host}",
          flush=True)

    run_id = secrets.token_hex(8)
    workspace = secrets.token_hex(16)
    service = "service-" + secrets.token_urlsafe(24)
    container_id = secrets.token_hex(32)
    gateway_instance = f"gateway-{run_id}"
    boot = secrets.token_hex(16)
    network = f"easel-task5c2-net-{run_id}"
    control_volume = f"easel-task5c2-control-{run_id}"
    worker_volume = f"easel-task5c2-ws-{run_id}"
    home_volume = f"easel-task5c2-home-{run_id}"
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
        (seed / "outputs" / "_login").mkdir(parents=True)
        (seed / "profiles").mkdir(parents=True)
        (seed / "state").mkdir(parents=True)
        (seed / "outputs" / "_analytics").mkdir(parents=True)

        for name, source in (
                ("worker_launcher.py", WORKER_LAUNCHER),
                ("gateway_launcher.py", GATEWAY_LAUNCHER),
                ("control_launcher.py", CONTROL_LAUNCHER)):
            (work / name).write_text(source, encoding="utf-8")

        run(["docker", "network", "create", *labels_args("network"), network])
        for name, component in ((control_volume, "control-volume"),
                                (worker_volume, "worker-volume"),
                                (home_volume, "home-volume")):
            run(["docker", "volume", "create", *labels_args(component), name])

        run([
            "docker", "run", "--rm", "--user", "root",
            "-v", f"{worker_volume}:/workspace",
            "-v", f"{home_volume}:/home/easel",
            "-v", f"{seed}:/seed:ro",
            "--entrypoint", "sh", args.worker_image, "-c",
            "cp -a /seed/outputs/. /workspace/outputs/ "
            "&& cp -a /seed/profiles/. /workspace/profiles/ "
            "&& cp -a /seed/state/. /workspace/state/ "
            "&& chown -R 10001:10001 /workspace /home/easel",
        ])

        worker_name = f"easel-task5c2-worker-{run_id}"
        worker = run([
            "docker", "run", "-d", "--name", worker_name,
            *labels_args("worker"),
            "--security-opt", "seccomp=unconfined",
            "--network", network, "--network-alias", "worker",
            "-v", f"{worker_volume}:/workspace",
            "-v", f"{home_volume}:/home/easel",
            "-v", f"{public_key}:/keys/worker-public.key:ro",
            "-v", f"{work / 'worker_launcher.py'}:/run/easel/worker_launcher.py:ro",
            "-v", (ROOT / "tests" / "multiuser" /
                   "task5c2_controlled_platform.py").as_posix()
            + ":/run/easel/task5c2_controlled_platform.py:ro",
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
            args.worker_image,
            "python", "/run/easel/worker_launcher.py",
        ]).stdout.strip()
        containers.append(worker)

        gateway_name = f"easel-task5c2-gateway-{run_id}"
        gateway = run([
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
            args.gateway_image,
            "python", "/run/easel/gateway_launcher.py",
        ]).stdout.strip()
        containers.append(gateway)

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        control_name = f"easel-task5c2-control-{run_id}"
        control = run([
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
            "-e", f"PUBLIC_ORIGIN={ORIGIN}",
            "-e", "CONTROL_DIR=/srv/easel/control",
            "-e", "SIGNING_KEY=/run/easel/control-key.pem",
            "-e", "PYTHONPATH=/opt/easel",
            args.control_image,
            "python", "/run/easel/control_launcher.py",
        ]).stdout.strip()
        containers.append(control)

        base = f"http://127.0.0.1:{port}"
        wait_http(base + "/api/health")
        invite = ""
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not invite:
            logs = container_logs(control_name)
            for line in logs.splitlines():
                if line.startswith("TASK5C2_INVITE "):
                    invite = line.split(" ", 1)[1].strip()
                    break
            if not invite:
                time.sleep(0.3)
        if not invite:
            raise RuntimeError("control 未输出邀请\n" + container_logs(control_name))

        with httpx.Client(base_url=base, timeout=60.0,
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

            # 1) 一次受管登录派发：真实 RunManager + 容器内 Chromium。
            started = client.post(
                "/api/login/xiaohongshu",
                json={"requestId": "container-login-1"}, headers=headers)
            assert started.status_code == 200, started.text
            login_run = started.json()["runId"]
            print("LOGIN_START", started.json(), flush=True)
            status = wait_status(client, "xiaohongshu",
                                 ("qr_ready", "error", "expired"),
                                 headers, timeout=30)
            assert status["state"] == "qr_ready", status
            assert status["qrUrl"] == "/api/accounts/xiaohongshu/qr"
            qr = client.get(status["qrUrl"] + "?v=1", headers=headers)
            assert qr.status_code == 200, qr.text
            assert qr.content.startswith(b"\x89PNG\r\n\x1a\n")
            width = int.from_bytes(qr.content[16:20], "big")
            height = int.from_bytes(qr.content[20:24], "big")
            assert (width, height) == (240, 240), (width, height)
            print("LOGIN_QR_BYTES", len(qr.content), width, height, flush=True)
            profile = run([
                "docker", "exec", worker_name, "ls",
                "/home/easel/.easel-browser-profiles/xiaohongshu/default",
            ])
            assert profile.stdout.strip(), profile.stdout
            processes = top(worker_name)
            assert "task5c2_controlled_platform.py" in processes, processes
            print("CHROMIUM_PROCESS", True, flush=True)

            wait_status(client, "xiaohongshu", ("sms_required",),
                        headers, timeout=30)
            sms = client.post(
                "/api/login/xiaohongshu/sms",
                json={"code": "123456", "runId": login_run}, headers=headers)
            assert sms.status_code == 200, sms.text
            success = wait_status(client, "xiaohongshu", ("success",),
                                  headers, timeout=30)
            assert success["state"] == "success", success
            assert wait_state(control_name, login_run, ("done",)) == "done"
            assert active_runs(control_name) == 0
            print("LOGIN_SUCCESS", True, flush=True)

            # 2) 本人 profile/结果：whoami 与 analytics 都真实启动容器内浏览器。
            who = client.post(
                "/api/accounts/xiaohongshu/whoami",
                json={"force": True, "requestId": "container-whoami"},
                headers=headers)
            assert who.status_code == 200, who.text
            assert who.json()["name"] == "容器受控昵称", who.text
            print("WHOAMI", who.json(), flush=True)

            analytics = client.post(
                "/api/analytics/xiaohongshu", headers=headers)
            assert analytics.status_code == 200, analytics.text
            assert analytics.json()["followers"] == 12, analytics.text
            analytics_disk = run([
                "docker", "exec", worker_name, "ls",
                "/workspace/outputs/_analytics",
            ]).stdout
            assert "xiaohongshu" in analytics_disk, analytics_disk
            print("ANALYTICS", analytics.json()["followers"], flush=True)

            # 3) 凭证：秘密只经 stdin，落盘在本人 state_root。
            credentials = client.post(
                "/api/accounts/wechat-oa/credentials",
                json={"appId": "wxcontainer", "appSecret": "container-secret",
                      "name": "容器公众号", "author": "",
                      "requestId": "container-cred"},
                headers=headers)
            assert credentials.status_code == 200, credentials.text
            assert credentials.json()["ok"] is True, credentials.text
            config = run([
                "docker", "exec", worker_name, "cat",
                "/workspace/state/skills/wechat/default.yaml",
            ]).stdout
            assert "wxcontainer" in config, config
            assert "container-secret" in config  # 只允许落在本人受管配置
            accounts = client.get("/api/accounts", headers=headers)
            wechat = [item for item in accounts.json()
                      if item["platform"] == "wechat-oa"][0]
            assert wechat["credentialConfigured"] is True, wechat
            combined_logs = container_logs(worker_name) + container_logs(control_name)
            assert "container-secret" not in combined_logs
            print("CREDENTIALS", True, flush=True)

            # 4) 显式停止：取消当前登录并释放容量；进程树必须被终止。
            stopped = client.post(
                "/api/login/zhihu", json={"requestId": "container-stop"},
                headers=headers)
            assert stopped.status_code == 200, stopped.text
            stop_run = stopped.json()["runId"]
            wait_status(client, "zhihu", ("qr_ready",), headers, timeout=30)
            stopped_result = client.post(
                "/api/login/zhihu/stop", json={"runId": stop_run},
                headers=headers)
            assert stopped_result.status_code == 200, stopped_result.text
            assert stopped_result.json()["state"] in ("cancelled", "interrupted")
            wait_state(control_name, stop_run, ("cancelled", "interrupted"))
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if active_runs(control_name) == 0 and \
                        "zhihu" not in top(worker_name):
                    break
                time.sleep(0.3)
            assert active_runs(control_name) == 0
            assert "zhihu" not in top(worker_name), top(worker_name)
            print("STOP_RELEASE", True, flush=True)

            # 容量确实释放：停止后还能再派发一次登录。
            again = client.post(
                "/api/login/douyin", json={"requestId": "container-after-stop"},
                headers=headers)
            assert again.status_code == 200, again.text
            wait_status(client, "douyin", ("qr_ready",), headers, timeout=30)
            again_stop = client.post(
                "/api/login/douyin/stop",
                json={"runId": again.json()["runId"]}, headers=headers)
            assert again_stop.status_code == 200, again_stop.text
            print("CAPACITY_AFTER_STOP", True, flush=True)

            # 5) 超时释放：不提交验证码，RunManager 超时取消执行器与子进程。
            timed = client.post(
                "/api/login/kuaishou", json={"requestId": "container-timeout"},
                headers=headers)
            assert timed.status_code == 200, timed.text
            timeout_run = timed.json()["runId"]
            wait_status(client, "kuaishou", ("qr_ready",), headers, timeout=30)
            # 状态轮询同时把 worker 的真实终态同步回 control 账本。
            timeout_status = wait_status(
                client, "kuaishou", ("error", "expired"), headers,
                timeout=40)
            assert timeout_status["state"] == "error", timeout_status
            state = wait_state(control_name, timeout_run,
                               ("failed", "cancelled", "interrupted"),
                               timeout=15)
            assert state == "failed", state
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if active_runs(control_name) == 0 and \
                        "kuaishou" not in top(worker_name):
                    break
                time.sleep(0.3)
            assert active_runs(control_name) == 0
            assert "kuaishou" not in top(worker_name), top(worker_name)
            print("TIMEOUT_RELEASE", True, flush=True)

            # 6) 旧状态/跨站动作的容器侧只读探针：GET 不得起分析任务。
            before = sql(control_name, "SELECT COUNT(*) FROM runs")
            cross = client.get(
                "/api/analytics/zhihu",
                headers={"Cookie": headers["Cookie"],
                         "Sec-Fetch-Site": "cross-site",
                         "Sec-Fetch-Mode": "navigate",
                         "Sec-Fetch-Dest": "document"})
            assert cross.status_code == 404, cross.text
            assert cross.json()["code"] == "analytics_cache_missing"
            after = sql(control_name, "SELECT COUNT(*) FROM runs")
            assert before == after, (before, after)
            print("CROSS_SITE_GET_NO_TASK", True, flush=True)

        print("CONTAINER_IMAGES", json.dumps({
            "control": args.control_image,
            "gateway": args.gateway_image,
            "worker": args.worker_image,
            "ids": images,
        }, ensure_ascii=False), flush=True)
        print("CONTAINER_CHECK", json.dumps({
            "login_dispatch": True, "qr_png": True,
            "profile": True, "chromium_process": True,
            "sms_success": True, "whoami": True, "analytics": True,
            "credentials": True, "stop_release": True,
            "timeout_release": True, "capacity_after_stop": True,
            "cross_site_get_no_task": True,
        }, ensure_ascii=False), flush=True)
        print("TASK5C2_CONTAINER_OK", True, flush=True)
        return 0
    except Exception as error:  # noqa: BLE001
        print("TASK5C2_CONTAINER_OK", False)
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


WORKER_LAUNCHER = r'''
import asyncio
import json
import os
import subprocess
import sys

import uvicorn
from web import app as worker

RUNNER = "/run/easel/task5c2_controlled_platform.py"
LOGIN_DIR = worker.LOGIN_DIR


def _env():
    env = worker._proxy_env()
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return env


def _spawn_login(platform, profile_name, qr, status, code, log_name):
    LOGIN_DIR.mkdir(parents=True, exist_ok=True)
    for path in (qr, status, code):
        try:
            path.unlink()
        except OSError:
            pass
    log = (LOGIN_DIR / log_name).open("a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            [sys.executable, RUNNER, "login", "--platform", platform,
             "--profile-name", profile_name, "--qr-out", str(qr),
             "--status-file", str(status), "--code-file", str(code),
             "--timeout", "60"],
            cwd="/opt/easel", env=_env(), stdout=log,
            stderr=subprocess.STDOUT, start_new_session=True)
    finally:
        log.close()
    return proc


async def start_login(platform, restart=False):
    existing = worker.LOGIN_PROCESSES.get(platform)
    if existing is not None and existing.poll() is None:
        if not restart:
            return {"mode": "qr", **worker._login_status(platform)}
        existing.terminate()
        try:
            existing.wait(timeout=8)
        except subprocess.TimeoutExpired:
            existing.kill()
            existing.wait(timeout=5)
        worker.LOGIN_PROCESSES.pop(platform, None)
    qr = LOGIN_DIR / f"{platform}.png"
    status = LOGIN_DIR / f"{platform}.json"
    code = LOGIN_DIR / f"{platform}.code"
    worker.LOGIN_PROCESSES[platform] = _spawn_login(
        platform, platform, qr, status, code, f"{platform}.log")
    return {"mode": "qr", "state": "starting"}


async def mp_start(platform):
    key = "wechat-oa-mp"
    existing = worker.LOGIN_PROCESSES.get(key)
    if existing is not None and existing.poll() is None:
        return {"mode": "qr", **worker._mp_login_status()}
    qr = LOGIN_DIR / "wechat-oa-mp.png"
    status = LOGIN_DIR / "wechat-oa-mp.json"
    code = LOGIN_DIR / "wechat-oa-mp.code"
    worker.LOGIN_PROCESSES[key] = _spawn_login(
        "wechat-oa", key, qr, status, code, "wechat-oa-mp.log")
    return {"mode": "qr", "state": "starting"}


def _json_line(output):
    for line in reversed((output or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    raise RuntimeError("controlled runner produced no JSON")


async def whoami(platform, force=False):
    completed = await worker._run_tracked_subprocess(
        "whoami", platform,
        [sys.executable, RUNNER, "whoami", "--platform", platform,
         "--profile-name", platform], _env(), 30)
    return _json_line(completed.stdout)


async def analytics(platform):
    completed = await worker._run_tracked_subprocess(
        "analytics", platform,
        [sys.executable, RUNNER, "analytics", "--platform", platform,
         "--profile-name", platform], _env(), 30)
    result = _json_line(completed.stdout)
    target = worker.OUTPUTS_DIR / "_analytics" / f"{platform}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, target)
    return result


def verify(app_id, secret):
    completed = subprocess.run(
        [sys.executable, RUNNER, "verify"], cwd="/opt/easel", env=_env(),
        input=json.dumps({"app_id": app_id, "app_secret": secret}),
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    lines = (completed.stdout or "").strip().splitlines()
    last = lines[-1].strip() if lines else ""
    if last == "OK":
        return True, ""
    if last.startswith("ERR:"):
        return False, last[4:].strip()
    return False, "验证失败"


worker._start_login = start_login
worker.api_mp_login_start = mp_start
worker._check_account_whoami = whoami
worker._fetch_analytics = analytics
worker._wechat_verify_credentials = verify
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
import easel.control.proxy as control_proxy
from easel.control.app import ControlConfig, create_app
from easel.control.auth import AuthService
from easel.control.gateway_client import GatewayClient
from easel.control.proxy import PinnedRunConfig, WorkerRelay
from easel.control.store import Store
from easel.control.types import WorkerHandle
from easel.control.workspace_api import WorkspaceApi

# 仅容器验收：把登录 Run 时限临时缩短到 15s，便于在验收窗口内真实触发
# RunManager 超时取消；生产默认 600s 不变。超时/取消路径本身未替换。
control_proxy._BROWSER_ACTION_TIMEOUTS["login"] = 15

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
    config=ControlConfig(public_origin=os.environ["PUBLIC_ORIGIN"]),
    runtime=runtime, frontend_dir="/opt/easel/web/frontend/dist",
    workspace_factory=workspace_factory, relay_factory=relay_factory)
store.close()
print("TASK5C2_INVITE " + invite, flush=True)
uvicorn.run(app, host="0.0.0.0", port=7861, proxy_headers=False)
'''


if __name__ == "__main__":
    sys.exit(main())
