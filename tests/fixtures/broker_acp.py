"""A4 真实链路用的受管 ACP 协议夹具（仅测试）。

它在 worker 的真实 ACP 子进程内运行，从环境读取受管 broker URL/token，
直接经 sidecar 调用 control broker，并把结果写进审计文件。它不是真实
DeepSeek Harness；仅用于验证 control→gateway→worker→sidecar→broker 接线。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

MODE = "acp"
AUDIT = "/workspace/broker-audit.jsonl"
ARGS = sys.argv[1:]
if "--audit" in ARGS:
    index = ARGS.index("--audit")
    if index + 1 < len(ARGS):
        AUDIT = ARGS[index + 1]


def _audit(record: dict) -> None:
    with open(AUDIT, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False,
                                separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _send(message: dict) -> None:
    message = {"jsonrpc": "2.0", **message}
    print(json.dumps(message, ensure_ascii=False), flush=True)


def _receive() -> dict:
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    return json.loads(line)


def _reply(request: dict, result: object) -> None:
    _send({"id": request["id"], "result": result})


def _http_json(method: str, url: str, headers: dict[str, str],
               payload: object | None) -> tuple[int, object]:
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read(65536)
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = body.decode("utf-8", "replace")
            return response.status, parsed
    except urllib.error.HTTPError as error:
        body = error.read(4096)
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = body.decode("utf-8", "replace")
        return error.code, parsed
    except OSError as error:
        return 0, f"{type(error).__name__}"


def _broker(prompt: str) -> str:
    base = (os.environ.get("EASEL_BROKER_URL") or "").rstrip("/")
    token = os.environ.get("EASEL_BROKER_TOKEN") or ""
    workspace = os.environ.get("EASEL_WORKSPACE_ID") or ""
    run_id = os.environ.get("EASEL_RUN_ID") or ""
    record = {"event": "broker", "pid": os.getpid(), "base": base,
              "workspace": workspace, "run": run_id}
    if not base or not token or not workspace or not run_id:
        record["status"] = 0
        record["error"] = "missing broker environment"
        _audit(record)
        return "broker-env-missing"
    headers = {
        "Content-Type": "application/json",
        "X-Easel-Broker-Token": token,
        "X-Easel-Workspace-Id": workspace,
        "X-Easel-Run-Id": run_id,
    }
    status, payload = _http_json(
        "POST", base + "/internal/broker/chat/chat/completions", headers,
        {"messages": [{"role": "user", "content": prompt}]})
    record["status"] = status
    if isinstance(payload, dict):
        code = payload.get("code")
        if isinstance(code, str) and len(code) <= 64:
            record["error_code"] = code
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(
                choices[0], dict) else None
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    record["content"] = content[:200]
    else:
        record["content"] = str(payload)[:200]
    _audit(record)

    peer_file = "/workspace/peer-workspace.txt"
    if os.path.isfile(peer_file):
        with open(peer_file, encoding="ascii") as stream:
            peer = stream.read().strip()
        if peer:
            cross_status, _cross = _http_json(
                "POST", base + "/internal/broker/chat/chat/completions",
                {**headers, "X-Easel-Workspace-Id": peer},
                {"messages": [{"role": "user", "content": "cross"}]})
            _audit({"event": "broker_cross_workspace", "pid": os.getpid(),
                    "status": cross_status})
            wrong_status, _wrong = _http_json(
                "POST", base + "/internal/broker/image/images/generations",
                headers, {"prompt": "wrong channel"})
            _audit({"event": "broker_wrong_channel", "pid": os.getpid(),
                    "status": wrong_status})
    if status != 200:
        return f"broker-http-{status}"
    if isinstance(payload, dict):
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message")
            if isinstance(message, dict) and isinstance(
                    message.get("content"), str):
                return message["content"]
    return "broker-ok"


def _permission() -> None:
    _send({"id": "permission-request",
           "method": "session/request_permission",
           "params": {
               "sessionId": SESSION or "fixture-session",
               "toolCall": {"title": "read fixture file"},
               "options": [
                   {"optionId": "always", "name": "Always",
                    "kind": "allow_always"},
                   {"optionId": "once", "name": "Allow once",
                    "kind": "allow_once"},
                   {"optionId": "deny", "name": "Reject",
                    "kind": "reject_once"},
               ]}})
    while True:
        message = _receive()
        if message.get("id") == "permission-request":
            _audit({"event": "permission_reply",
                    "outcome": message.get("result")})
            return


SESSION = ""
PENDING = None


while True:
    message = _receive()
    method = message.get("method")
    if method == "initialize":
        _audit({"event": "initialize", "pid": os.getpid()})
        _reply(message, {"protocolVersion": 1, "agentCapabilities": {}})
    elif method == "initialized":
        pass
    elif method in ("session/new", "session/load", "session/resume"):
        if method == "session/new":
            SESSION = "fixture-session"
        else:
            SESSION = (message.get("params") or {}).get(
                "sessionId", "fixture-session")
        _reply(message, {"sessionId": SESSION})
    elif method == "session/set_config_option":
        _reply(message, {})
    elif method in ("turn/interrupt", "session/cancel"):
        if "id" in message:
            _reply(message, {})
        if PENDING is not None:
            _reply(PENDING, {"stopReason": "cancelled"})
            PENDING = None
    elif method in ("turn/start", "session/prompt"):
        PENDING = message
        _audit({"event": "prompt", "pid": os.getpid(),
                "run": os.environ.get("EASEL_RUN_ID") or ""})
        params = message.get("params") or {}
        prompt = params.get("prompt") or params.get("input") or ""
        if isinstance(prompt, list):
            prompt = " ".join(str(item) for item in prompt)
        prompt = str(prompt)
        if "permission" in prompt:
            _permission()
            text = "permission-ok"
        elif "hang" in prompt:
            # 保持 ACP 子进程存活，等待 stop/answer；不结束 turn。
            continue
        else:
            text = _broker(prompt)
        for kind, content in (("agent_thought_chunk", "thinking"),
                              ("agent_message_chunk", text)):
            _send({"method": "session/update", "params": {
                "sessionId": SESSION,
                "update": {"sessionUpdate": kind,
                           "content": {"type": "text", "text": content}}}})
        _reply(message, {"stopReason": "end_turn"})
        PENDING = None
    elif "id" in message:
        _reply(message, {})
