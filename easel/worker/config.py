"""worker 受管配置：签名快照、干净运行环境与 run/nonce 接纳。

边界：

* 快照由 control 的 Ed25519 私钥签名；worker 只加载公钥，校验完整字段、
  有效期、workspace/run/revision 与 admission nonce。掌握本人 worker 服务
  token 不能替代签名。
* 子进程环境只从明确允许的运行变量构建，不复制宿主 ``os.environ``；管理员
  模型 Key、宿主 HOME、.env 与大小写变体秘密都不会进入 worker。
* 同一 run 只接纳一次；重复合法快照返回原接纳/执行状态，篡改重用返回 409。
  接纳记录落在本人 state_root，不修改 control 旧 schema。
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)

from easel.control.types import DomainError, WorkspacePaths

SNAPSHOT_ENVELOPE_VERSION = 1
SNAPSHOT_SIGNATURE_DOMAIN = b"easel-worker-snapshot-v1\0"
MAX_SNAPSHOT_BYTES = 1024 * 1024

_PAYLOAD_FIELDS = ("envelope_version", "workspace_id", "run_id", "revision",
                   "routes", "agent", "expires_at", "nonce")
_ENVELOPE_FIELDS = frozenset((*_PAYLOAD_FIELDS, "signature"))
_ALLOWED_SECRET_KEYS = frozenset({"BROKER_TOKEN", "CREDENTIAL_REF",
                                  "CREDENTIAL_REFS"})

# 只保留跨平台启动与明确受管代理/编码所需变量；其余一律丢弃。
_ENV_ALLOWLIST = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "WINDIR",
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TZ",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "EASEL_PROXY",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "PYTHONUTF8", "PYTHONIOENCODING", "PYTHONHASHSEED",
})
_SECRET_MARKERS = ("SECRET", "PASSWORD", "PRIVATE_KEY", "ACCESS_KEY")


@dataclass(frozen=True)
class WorkerConfigSnapshot:
    """已验签的 worker 配置；nonce/expires_at 来自签名 envelope。"""

    workspace_id: str
    run_id: str
    revision: int
    routes: Mapping[str, Any]
    agent: Mapping[str, Any]
    nonce: str = ""
    expires_at: int = 0


def _canonical(value: object) -> bytes:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError):
        raise DomainError("snapshot_invalid", 403) from None
    return encoded.encode("utf-8")


def _text_id(value: object, name: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 256
            or not value.isascii()
            or any(ord(character) < 32 for character in value)):
        raise DomainError("snapshot_invalid", 403)
    return value


def _validate_key_names(value: object) -> None:
    """签名包不得携带管理员原始 Key；引用与 broker token 例外。"""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise DomainError("snapshot_invalid", 403)
            upper = key.upper().replace("-", "_")
            if upper not in _ALLOWED_SECRET_KEYS and (
                    upper.endswith(("_KEY", "_SECRET", "_PASSWORD",
                                    "_TOKEN", "_CREDENTIAL"))
                    or any(marker in upper for marker in _SECRET_MARKERS)):
                raise DomainError("snapshot_secret_forbidden", 403)
            _validate_key_names(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_key_names(item)


def _validate_payload(payload: Mapping[str, Any]) -> None:
    if payload.get("envelope_version") != SNAPSHOT_ENVELOPE_VERSION:
        raise DomainError("snapshot_invalid", 403)
    _text_id(payload.get("workspace_id"), "workspace_id")
    _text_id(payload.get("run_id"), "run_id")
    revision = payload.get("revision")
    if type(revision) is not int or revision < 0:
        raise DomainError("snapshot_invalid", 403)
    for name in ("routes", "agent"):
        if not isinstance(payload.get(name), Mapping):
            raise DomainError("snapshot_invalid", 403)
    expires_at = payload.get("expires_at")
    if type(expires_at) is not int or expires_at <= 0:
        raise DomainError("snapshot_invalid", 403)
    nonce = payload.get("nonce")
    if (not isinstance(nonce, str) or not 1 <= len(nonce) <= 128
            or not nonce.isascii()
            or any(ord(character) < 33 or ord(character) > 126
                   for character in nonce)):
        raise DomainError("snapshot_invalid", 403)
    _validate_key_names(payload.get("routes"))
    _validate_key_names(payload.get("agent"))


def _payload(workspace_id: str, run_id: str, revision: int,
             routes: Mapping[str, Any], agent: Mapping[str, Any],
             expires_at: int, nonce: str) -> dict[str, Any]:
    payload = {
        "envelope_version": SNAPSHOT_ENVELOPE_VERSION,
        "workspace_id": workspace_id,
        "run_id": run_id,
        "revision": revision,
        "routes": dict(routes),
        "agent": dict(agent),
        "expires_at": expires_at,
        "nonce": nonce,
    }
    _validate_payload(payload)
    return payload


def sign_worker_snapshot(private_key: Ed25519PrivateKey, *,
                         workspace_id: str, run_id: str, revision: int,
                         routes: Mapping[str, Any], agent: Mapping[str, Any],
                         expires_at: int, nonce: str) -> dict[str, Any]:
    """control 侧签名入口；私钥只在这里使用，不进入 worker 环境。"""
    if not isinstance(private_key, Ed25519PrivateKey):
        raise DomainError("snapshot_key_invalid", 500)
    payload = _payload(workspace_id, run_id, revision, routes, agent,
                       expires_at, nonce)
    signature = private_key.sign(
        SNAPSHOT_SIGNATURE_DOMAIN + _canonical(payload))
    return {**payload,
            "signature": base64.urlsafe_b64encode(signature)
            .decode("ascii").rstrip("=")}


def load_public_key(source: Ed25519PublicKey | str | Path) -> Ed25519PublicKey:
    """只接受公钥（对象、32 字节 raw 或 PEM）；任何私钥材料都不读取。"""
    if isinstance(source, Ed25519PublicKey):
        return source
    try:
        data = Path(source).read_bytes()
    except OSError:
        raise DomainError("snapshot_key_unavailable", 503) from None
    try:
        if len(data) == 32:
            return Ed25519PublicKey.from_public_bytes(data)
        key = serialization.load_pem_public_key(data)
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError
        return key
    except (ValueError, TypeError):
        raise DomainError("snapshot_key_invalid", 503) from None


def verify_worker_snapshot(
        envelope: Mapping[str, Any],
        public_key: Ed25519PublicKey | str | Path, *,
        expected_workspace_id: str,
        expected_run_id: str,
        expected_revision: int | None = None,
        now: int | None = None) -> WorkerConfigSnapshot:
    """验签并核对归属/版本/有效期；任何字段变化都拒绝。"""
    if not isinstance(envelope, Mapping):
        raise DomainError("snapshot_invalid", 403)
    if set(envelope) != _ENVELOPE_FIELDS:
        raise DomainError("snapshot_invalid", 403)
    payload = {name: envelope[name] for name in _PAYLOAD_FIELDS}
    _validate_payload(payload)
    signature_text = envelope.get("signature")
    if (not isinstance(signature_text, str) or not signature_text
            or len(signature_text) > 256):
        raise DomainError("snapshot_invalid", 403)
    try:
        padded = signature_text + "=" * (-len(signature_text) % 4)
        signature = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, UnicodeError):
        raise DomainError("snapshot_invalid", 403) from None
    if len(signature) != 64:
        raise DomainError("snapshot_invalid", 403)
    key = load_public_key(public_key)
    try:
        key.verify(signature,
                   SNAPSHOT_SIGNATURE_DOMAIN + _canonical(payload))
    except (InvalidSignature, ValueError, TypeError):
        raise DomainError("snapshot_invalid", 403) from None

    if (payload["workspace_id"] != expected_workspace_id
            or payload["run_id"] != expected_run_id):
        raise DomainError("snapshot_invalid", 403)
    if (expected_revision is not None
            and payload["revision"] != expected_revision):
        raise DomainError("snapshot_invalid", 403)
    current = int(time.time()) if now is None else int(now)
    if int(payload["expires_at"]) <= current:
        raise DomainError("snapshot_expired", 403)
    return WorkerConfigSnapshot(
        workspace_id=payload["workspace_id"],
        run_id=payload["run_id"],
        revision=int(payload["revision"]),
        routes=dict(payload["routes"]),
        agent=dict(payload["agent"]),
        nonce=payload["nonce"],
        expires_at=int(payload["expires_at"]))


def load_worker_snapshot(
        path: str | Path,
        public_key: Ed25519PublicKey | str | Path, *,
        expected_workspace_id: str,
        expected_run_id: str,
        expected_revision: int | None = None,
        now: int | None = None) -> WorkerConfigSnapshot:
    """从只读启动文件加载 envelope；超过大小上限或非法 JSON 明确失败。"""
    try:
        data = Path(path).read_bytes()
    except OSError:
        raise DomainError("snapshot_unavailable", 503) from None
    if len(data) > MAX_SNAPSHOT_BYTES:
        raise DomainError("snapshot_invalid", 403)
    try:
        envelope = json.loads(data)
    except (ValueError, UnicodeError):
        raise DomainError("snapshot_invalid", 403) from None
    if not isinstance(envelope, Mapping):
        raise DomainError("snapshot_invalid", 403)
    return verify_worker_snapshot(
        envelope, public_key,
        expected_workspace_id=expected_workspace_id,
        expected_run_id=expected_run_id,
        expected_revision=expected_revision, now=now)


# ---------------------------------------------------------------------------
# 子进程环境
# ---------------------------------------------------------------------------


def _is_secret_name(name: str) -> bool:
    upper = name.upper().replace("-", "_")
    if upper in _ALLOWED_SECRET_KEYS:
        return False
    return (upper.endswith(("_KEY", "_SECRET", "_PASSWORD", "_TOKEN"))
            or any(marker in upper for marker in _SECRET_MARKERS))


def _route_value(routes: Mapping[str, Any], name: str) -> str | None:
    value = routes.get(name)
    if isinstance(value, str) and value:
        return value
    broker = routes.get("broker")
    if isinstance(broker, Mapping):
        short = name.split("_", 1)[-1]
        for key in (short, name):
            value = broker.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _put(env: dict[str, str], name: str, value: object) -> None:
    for key in [key for key in env if key.upper() == name.upper()]:
        env.pop(key)
    env[name] = str(value)


def build_run_env(paths: WorkspacePaths,
                  snapshot: WorkerConfigSnapshot,
                  base_env: Mapping[str, str]) -> dict[str, str]:
    """从 allowlist 构建 worker 子进程环境，并注入本人路径与 run 凭证。"""
    env: dict[str, str] = {}
    for key, value in base_env.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        upper = key.upper()
        if upper not in _ENV_ALLOWLIST or _is_secret_name(upper):
            continue
        env[key] = value

    _put(env, "HOME", paths.home)
    _put(env, "USERPROFILE", paths.home)
    _put(env, "DSH_HOME", paths.home / ".dsh")
    _put(env, "DSH_AGENTS_HOME", paths.code_root)
    _put(env, "EASEL_CODE_ROOT", paths.code_root)
    _put(env, "EASEL_WORKSPACE_ROOT", paths.workspace_root)
    _put(env, "EASEL_STATE_ROOT", paths.state_root)
    _put(env, "EASEL_ROOT", paths.workspace_root)
    _put(env, "EASEL_SERVICE_MODE", "worker")
    _put(env, "EASEL_WORKSPACE_ID", snapshot.workspace_id)
    _put(env, "EASEL_RUN_ID", snapshot.run_id)
    _put(env, "EASEL_CONFIG_REVISION", snapshot.revision)
    _put(env, "XDG_CACHE_HOME", paths.home / ".cache")
    _put(env, "XDG_CONFIG_HOME", paths.state_root / "config")
    _put(env, "XDG_STATE_HOME", paths.state_root)
    _put(env, "TMPDIR", paths.state_root / "tmp")
    _put(env, "TEMP", paths.state_root / "tmp")
    _put(env, "TMP", paths.state_root / "tmp")

    runtime = snapshot.agent.get("runtime")
    if isinstance(runtime, str) and runtime:
        _put(env, "EASEL_AGENT_RUNTIME", runtime)
    broker_url = _route_value(snapshot.routes, "broker_url")
    broker_token = _route_value(snapshot.routes, "broker_token")
    if broker_url is not None:
        _put(env, "EASEL_BROKER_URL", broker_url)
    if broker_token is not None:
        _put(env, "EASEL_BROKER_TOKEN", broker_token)
    return env


# ---------------------------------------------------------------------------
# run/nonce 接纳
# ---------------------------------------------------------------------------


def snapshot_fingerprint(snapshot: WorkerConfigSnapshot) -> str:
    payload = {
        "envelope_version": SNAPSHOT_ENVELOPE_VERSION,
        "workspace_id": snapshot.workspace_id,
        "run_id": snapshot.run_id,
        "revision": snapshot.revision,
        "routes": dict(snapshot.routes),
        "agent": dict(snapshot.agent),
        "expires_at": snapshot.expires_at,
        "nonce": snapshot.nonce,
    }
    return hashlib.sha256(_canonical(payload)).hexdigest()


class SnapshotAdmissionStore:
    """最小持久接纳记录：同一 run 只放行一次，重复只返回原状态。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS snapshot_admissions ("
                " workspace_id TEXT NOT NULL, run_id TEXT NOT NULL,"
                " nonce TEXT NOT NULL, fingerprint TEXT NOT NULL,"
                " state TEXT NOT NULL, admitted_at INTEGER NOT NULL,"
                " PRIMARY KEY (workspace_id, run_id))")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10,
                                     isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def admit(self, snapshot: WorkerConfigSnapshot, *,
              state: str = "accepted", now: int | None = None
              ) -> dict[str, Any]:
        """原子接纳；重复同 nonce+同内容返回原状态，其它重用冲突。"""
        if not snapshot.nonce:
            raise DomainError("snapshot_invalid", 403)
        fingerprint = snapshot_fingerprint(snapshot)
        admitted_at = int(time.time()) if now is None else int(now)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT nonce, fingerprint, state FROM snapshot_admissions"
                    " WHERE workspace_id = ? AND run_id = ?",
                    (snapshot.workspace_id, snapshot.run_id)).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO snapshot_admissions"
                        " (workspace_id, run_id, nonce, fingerprint, state,"
                        " admitted_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (snapshot.workspace_id, snapshot.run_id,
                         snapshot.nonce, fingerprint, state, admitted_at))
                    connection.execute("COMMIT")
                    return {"state": state, "duplicate": False,
                            "run_id": snapshot.run_id,
                            "nonce": snapshot.nonce}
                if (row["nonce"] == snapshot.nonce
                        and row["fingerprint"] == fingerprint):
                    connection.execute("COMMIT")
                    return {"state": row["state"], "duplicate": True,
                            "run_id": snapshot.run_id,
                            "nonce": snapshot.nonce}
                connection.execute("ROLLBACK")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        raise DomainError("snapshot_conflict", 409)
