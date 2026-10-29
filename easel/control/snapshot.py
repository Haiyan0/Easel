"""control 侧 worker 快照签名密钥加载。

私钥只由 control 显式传入的受保护文件加载，不进入 worker 环境、挂载或日志。
签名与验签的 envelope 定义在 :mod:`easel.worker.config`，worker 侧只加载公钥。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_MANAGED_FIELDS = frozenset({"revision", "agent", "broker"})
_AGENT_FIELDS = frozenset({
    "runtime", "command", "model", "models", "sandbox", "timeout",
    "codex_auto_approve"})
_LEGACY_BROKER_FIELDS = frozenset({"url", "channel"})
_SIDECAR_BROKER_FIELDS = frozenset({"mode", "channel"})
_RUNTIMES = ("acp", "opencode", "codex")
_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
_MAX_TIMEOUT = 86400
WORKSPACE_SIDECAR_MODE = "workspace-sidecar"


def _broker_channel(value: object) -> str:
    if (not isinstance(value, str) or not value
            or len(value) > 32
            or not all(character.islower() or character.isdigit()
                       or character == "_" for character in value)):
        raise ValueError("managed broker channel is invalid")
    return value


def _broker_destination(value: object) -> dict:
    """受管 broker 目的地：固定 URL 兼容或逐 workspace sidecar。

    两种模式都只声明非秘密目的地；逐 Run token 仍由 BrokerService.issue
    在接纳后签发。sidecar 模式不接受 URL、模板变量或客户端输入。
    """
    if not isinstance(value, dict):
        raise ValueError("managed broker fields are invalid")
    keys = set(value)
    if keys == _SIDECAR_BROKER_FIELDS:
        if value["mode"] != WORKSPACE_SIDECAR_MODE:
            raise ValueError("managed broker mode is invalid")
        return {"mode": WORKSPACE_SIDECAR_MODE,
                "channel": _broker_channel(value["channel"])}
    if keys != _LEGACY_BROKER_FIELDS:
        raise ValueError("managed broker fields are invalid")
    url = value["url"]
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise ValueError("managed broker url is invalid")
    parsed = urlsplit(url)
    if (parsed.scheme not in ("http", "https") or not parsed.netloc
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
            or parsed.path not in ("", "/")):
        raise ValueError("managed broker url is invalid")
    return {"url": url.rstrip("/"),
            "channel": _broker_channel(value["channel"])}


def load_signing_key(path: str | Path) -> Ed25519PrivateKey:
    """读取 Ed25519 私钥；缺失、类型错误或 POSIX 权限过宽明确失败。"""
    key_path = Path(path)
    data = key_path.read_bytes()
    if os.name == "posix" and key_path.stat().st_mode & 0o077:
        raise ValueError("snapshot signing key must not be group/world accessible")
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("snapshot signing key must be Ed25519")
    return key


def _command(value: object) -> list[str]:
    if (not isinstance(value, list) or not 1 <= len(value) <= 64
            or any(not isinstance(part, str) or not part
                   or len(part) > 4096
                   or any(ord(character) < 32 for character in part)
                   for part in value)):
        raise ValueError("managed agent command must be an argv array")
    return list(value)


def _model_catalog(value: object) -> list[str]:
    """受管 Harness 模型目录：有界、唯一、无控制字符的公开模型 ID。"""
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise ValueError("managed agent models must be a non-empty list")
    normalized: list[str] = []
    for item in value:
        if (not isinstance(item, str) or not item or len(item) > 256
                or any(ord(character) < 32 for character in item)):
            raise ValueError("managed agent model is invalid")
        if item in normalized:
            raise ValueError("managed agent model is duplicated")
        normalized.append(item)
    return normalized


def load_managed_agent_config(path: str | Path) -> tuple[int, dict, dict]:
    """读取受保护的受管 Agent 配置，返回 ``(revision, agent, broker)``。

    只接受固定字段，拒绝任何疑似凭证字段，避免管理员秘密被签进逐 Run
    快照下发到 worker。``broker`` 只声明目的地与 channel；逐 Run token
    由正式装配在接纳后调用 BrokerService.issue 签发。
    """
    config_path = Path(path)
    data = config_path.read_bytes()
    if os.name == "posix" and config_path.stat().st_mode & 0o077:
        raise ValueError(
            "managed agent config must not be group/world accessible")
    try:
        payload = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("managed agent config must be a JSON object") from None
    if not isinstance(payload, dict) or set(payload) != _MANAGED_FIELDS:
        raise ValueError("managed agent config fields are invalid")
    revision = payload["revision"]
    if type(revision) is not int or not 0 <= revision <= 2**31 - 1:
        raise ValueError("managed agent revision must be a non-negative int")
    agent = payload["agent"]
    if not isinstance(agent, dict) or not agent:
        raise ValueError("managed agent must be a non-empty object")
    if set(agent) - _AGENT_FIELDS:
        raise ValueError("managed agent contains unsupported fields")

    runtime = agent.get("runtime")
    if runtime not in _RUNTIMES:
        raise ValueError("managed agent runtime is unsupported")
    normalized: dict[str, object] = {"runtime": runtime}
    if "command" in agent:
        normalized["command"] = _command(agent["command"])
    elif runtime == "acp":
        raise ValueError("managed ACP agent requires an explicit command")
    if "model" in agent:
        model = agent["model"]
        if (not isinstance(model, str) or not model or len(model) > 256
                or any(ord(character) < 32 for character in model)):
            raise ValueError("managed agent model is invalid")
        normalized["model"] = model
    models = None
    if "models" in agent:
        models = _model_catalog(agent["models"])
        normalized["models"] = models
    if ("model" in normalized and models is not None
            and normalized["model"] not in models):
        raise ValueError("managed agent model is not in the catalog")
    if "sandbox" in agent:
        if agent["sandbox"] not in _SANDBOXES:
            raise ValueError("managed agent sandbox is invalid")
        normalized["sandbox"] = agent["sandbox"]
    if "timeout" in agent:
        timeout = agent["timeout"]
        if type(timeout) is not int or not 1 <= timeout <= _MAX_TIMEOUT:
            raise ValueError("managed agent timeout is invalid")
        normalized["timeout"] = timeout
    if "codex_auto_approve" in agent:
        if type(agent["codex_auto_approve"]) is not bool:
            raise ValueError("managed agent codex_auto_approve is invalid")
        normalized["codex_auto_approve"] = agent["codex_auto_approve"]
    broker = _broker_destination(payload["broker"])
    return revision, normalized, broker
