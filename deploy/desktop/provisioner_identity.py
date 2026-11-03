"""Start/Status 使用的 provisioner 只读身份核验客户端。

只通过受信 Unix socket 请求脱敏配置；不读取 key 内容、不提供镜像/路径等
可变参数。比较失败只输出字段名，便于拒绝配置漂移而不泄露秘密。
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

MAX_RESPONSE_BYTES = 64 * 1024


class IdentityError(RuntimeError):
    pass


def request_identity(socket_path: str, timeout: float = 5.0) -> dict:
    try:
        probe = socket.socket(socket.AF_UNIX)
    except (AttributeError, OSError):
        raise IdentityError("unix_socket_unavailable") from None
    probe.settimeout(timeout)
    try:
        try:
            probe.connect(socket_path)
            probe.sendall(
                (json.dumps({"version": 2, "action": "identity"},
                            separators=(",", ":")) + "\n").encode("utf-8"))
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = probe.recv(4096)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise IdentityError("identity_response_too_large")
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
        except OSError:
            raise IdentityError("unix_socket_connect_failed") from None
    finally:
        probe.close()
    raw = b"".join(chunks).split(b"\n", 1)[0]
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise IdentityError("identity_response_invalid") from None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        error = payload.get("error") if isinstance(payload, dict) else ""
        raise IdentityError(str(error or "identity_rejected"))
    identity = payload.get("identity")
    if not isinstance(identity, dict):
        raise IdentityError("identity_response_invalid")
    return identity


def _nested(value: object, *path: str) -> object:
    current = value
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def compare_identity(expected: dict, identity: dict) -> list[str]:
    """返回漂移字段名；空列表才表示可复用同一 provisioner 进程。"""
    if not isinstance(expected, dict) or not isinstance(identity, dict):
        return ["identity"]
    mismatches: list[str] = []
    engine = identity.get("engine")
    if (not isinstance(engine, dict)
            or not isinstance(engine.get("context"), str)
            or not engine.get("context")
            or not isinstance(engine.get("id"), str)
            or not engine.get("id")):
        mismatches.append("engine")

    checks = (
        ("engineContext", ("engine", "context")),
        ("engineId", ("engine", "id")),
        ("workerImage", ("images", "worker")),
        ("gatewayImage", ("images", "gateway")),
        ("workspaceRoot", ("workspaceRoot",)),
        ("sidecarNetwork", ("networks", "sidecar")),
        ("gatewayControlNetwork", ("networks", "gatewayControl")),
        ("brokerUpstream", ("brokerUpstream",)),
        ("workerPublicSha256", ("keys", "workerPublicSha256")),
        ("gatewayPublicSha256", ("keys", "gatewayPublicSha256")),
        ("policySha256", ("policySha256",)),
        # 配置身份相同不代表运行的是新代码；源码摘要与所需动作能力
        # 都必须由实际进程回报。
        ("provisionerSourceSha256", ("sourceSha256",)),
    )
    for field, path in checks:
        wanted = expected.get(field)
        if wanted is None:
            continue
        observed = _nested(identity, *path)
        if not isinstance(observed, str) or observed != wanted:
            mismatches.append(field)

    socket_info = identity.get("socket")
    if not isinstance(socket_info, dict):
        mismatches.append("socket")
        return mismatches
    if "socketUid" in expected and socket_info.get("uid") != expected["socketUid"]:
        mismatches.append("socketUid")
    if "socketGid" in expected and socket_info.get("gid") != expected["socketGid"]:
        mismatches.append("socketGid")
    if ("socketMode" in expected
            and socket_info.get("mode") != expected["socketMode"]):
        mismatches.append("socketMode")
    if "allowedUid" in expected:
        allowed = socket_info.get("allowedUids")
        if (not isinstance(allowed, list)
                or expected["allowedUid"] not in allowed):
            mismatches.append("allowedUid")
    if identity.get("protocol") != 2:
        mismatches.append("protocol")
    required_actions = expected.get("provisionerActions")
    if required_actions is not None:
        observed_actions = identity.get("actions")
        if (not isinstance(required_actions, list)
                or not isinstance(observed_actions, list)
                or any(action not in observed_actions
                       for action in required_actions)):
            mismatches.append("provisionerActions")
    return mismatches


def _load_expected(path: str) -> dict:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise IdentityError("expected_invalid") from None
    if not isinstance(payload, dict):
        raise IdentityError("expected_invalid")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="easel-provisioner-identity")
    parser.add_argument("--socket", default=None)
    parser.add_argument("--timeout", type=float, default=5.0)
    sub = parser.add_subparsers(dest="command", required=True)
    for child in (sub.add_parser("show", help="打印脱敏的实际身份"),
                  sub.add_parser("check", help="比较期望配置与实际身份")):
        # 允许 --socket/--timeout 出现在子命令之后；未提供时保留顶层值。
        child.add_argument("--socket", default=argparse.SUPPRESS)
        child.add_argument("--timeout", type=float,
                           default=argparse.SUPPRESS)
    sub.choices["check"].add_argument("--expected", required=True)
    sub.choices["show"].set_defaults(command="show")
    sub.choices["check"].set_defaults(command="check")
    args = parser.parse_args(argv)
    if not args.socket:
        parser.error("--socket is required")
    try:
        identity = request_identity(args.socket, timeout=args.timeout)
        if args.command == "show":
            print(json.dumps(identity, ensure_ascii=False, sort_keys=True))
            return 0
        expected = _load_expected(args.expected)
        mismatches = compare_identity(expected, identity)
    except IdentityError as error:
        print(f"identity_unavailable={error}", file=sys.stderr)
        return 4
    if mismatches:
        print("identity_mismatch=" + ",".join(mismatches))
        return 3
    print("identity=ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
