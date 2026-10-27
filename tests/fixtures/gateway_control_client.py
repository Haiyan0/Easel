"""G2 真实拓扑测试客户端：只经 gateway 的签名断言调用 worker 路由。

该脚本运行在 control 网络的临时容器内，私钥文件只读挂载。它不访问
Docker socket、worker 网络或控制数据库；所有目标 URL 由测试参数固定。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from easel.gateway.auth import (
    GATEWAY_ASSERTION_HEADER,
    GatewayAudience,
    sign_gateway_assertion,
)


def _private_key(path: str) -> Ed25519PrivateKey:
    data = Path(path).read_bytes()
    if len(data) == 32:
        return Ed25519PrivateKey.from_private_bytes(data)
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError("not Ed25519")
    return key


def _audience(args) -> GatewayAudience:
    return GatewayAudience(
        args.audience_workspace or args.workspace_id,
        args.audience_gateway or args.gateway_instance_id,
        args.audience_boot or args.boot_generation,
        args.audience_worker or args.worker_container_id,
        args.audience_config_version
        if args.audience_config_version is not None
        else args.config_version,
    )


def _request(client: httpx.Client, args, method: str, path: str, *,
             query: dict[str, str] | None = None, body: bytes = b""):
    token = sign_gateway_assertion(
        args.private_key, _audience(args), method=method, path=path,
        query=query, body=body)
    response = client.request(
        method, args.base_url.rstrip("/") + path, content=body,
        params=query, headers={GATEWAY_ASSERTION_HEADER: token})
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text}
    return response.status_code, payload


def _events(client, args, run_id: str) -> tuple[int, dict]:
    return _request(
        client, args, "GET", f"/internal/runs/{run_id}/events",
        query={"after": "0"})


def _poll(client, args, run_id: str) -> dict:
    deadline = time.monotonic() + 25.0
    last: dict = {}
    while time.monotonic() < deadline:
        status, payload = _events(client, args, run_id)
        last = {"status": status, "payload": payload}
        if status == 200 and payload.get("state") in (
                "done", "failed", "interrupted", "cancelled",
                "awaiting_permission"):
            return last
        time.sleep(0.2)
    return last


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--gateway-instance-id", required=True)
    parser.add_argument("--boot-generation", required=True)
    parser.add_argument("--worker-container-id", required=True)
    parser.add_argument("--config-version", type=int, required=True)
    parser.add_argument("--audience-workspace")
    parser.add_argument("--audience-gateway")
    parser.add_argument("--audience-boot")
    parser.add_argument("--audience-worker")
    parser.add_argument("--audience-config-version", type=int)
    parser.add_argument("--op", required=True)
    parser.add_argument("--run-id", default="0" * 32)
    parser.add_argument("--session-id", default="0" * 32)
    parser.add_argument("--input-dir")
    parser.add_argument("--result-dir")
    args = parser.parse_args()
    args.private_key = _private_key(args.key)
    input_dir = Path(args.input_dir) if args.input_dir else None
    result_dir = Path(args.result_dir) if args.result_dir else None
    result: dict = {}
    with httpx.Client(timeout=10.0, trust_env=False) as client:
        if args.op == "health":
            status, payload = _request(
                client, args, "GET", "/internal/lifecycle/health")
            result = {"status": status, "payload": payload}
        elif args.op in ("submit", "submit_once"):
            body = (input_dir / "body.json").read_bytes()
            status, payload = _request(
                client, args, "POST", f"/internal/runs/{args.run_id}",
                body=body)
            result = {"status": status, "payload": payload}
            if args.op == "submit" and status >= 200 and status < 300:
                result["events"] = _poll(client, args, args.run_id)
        elif args.op == "events":
            status, payload = _events(client, args, args.run_id)
            result = {"status": status, "payload": payload}
        elif args.op == "answer":
            question_id = (input_dir / "qid.txt").read_text(
                encoding="ascii").strip()
            body = (input_dir / "answer.json").read_bytes()
            status, payload = _request(
                client, args, "POST",
                f"/internal/runs/{args.run_id}/questions/{question_id}/answer",
                body=body)
            result = {"status": status, "payload": payload}
            result["events"] = _poll(client, args, args.run_id)
        elif args.op == "question_status":
            body = (input_dir / "status.json").read_bytes()
            status, payload = _request(
                client, args, "POST",
                f"/internal/runs/{args.run_id}/questions/status", body=body)
            result = {"status": status, "payload": payload}
        elif args.op == "stop":
            body = (input_dir / "stop.json").read_bytes()
            status, payload = _request(
                client, args, "POST",
                f"/internal/runs/{args.run_id}/stop", body=body)
            result = {"status": status, "payload": payload}
        elif args.op == "messages":
            query = {"limit": "100", "offset": "0"}
            status, payload = _request(
                client, args, "GET",
                f"/internal/sessions/{args.session_id}/messages",
                query=query)
            result = {"status": status, "payload": payload}
        else:
            raise SystemExit("unknown op")
    if result_dir is not None:
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
