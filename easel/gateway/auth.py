"""control→gateway 的逐请求 Ed25519 断言。

协议固定：

* 域分隔符 ``easel.desktop.gateway.v1`` 与 canonical JSON 一起签名；
* 断言头 ``X-Easel-Gateway-Assertion`` 的值是
  ``v1.<base64url(payload)>.<base64url(signature)>``；
* payload 字段严格固定，任何未知/缺失/重复字段都拒绝；
* nonce 由有界、线程安全的缓存原子单次接纳。

本模块只依赖密码学原语与标准库。私钥只由签名工具持有；gateway 只接收
公钥和 ``GatewayConfig``。错误对象不携带断言、token、body 或期望值。
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

GATEWAY_ASSERTION_DOMAIN = "easel.desktop.gateway.v1"
GATEWAY_ASSERTION_HEADER = "X-Easel-Gateway-Assertion"
GATEWAY_ASSERTION_VERSION = "v1"
MAX_ASSERTION_TTL_SECONDS = 30
MAX_CLOCK_SKEW_SECONDS = 5
MAX_ASSERTION_BYTES = 4096
MAX_PATH_BYTES = 2048
MAX_QUERY_BYTES = 4096
MAX_TOKEN_BYTES = 4096
DEFAULT_NONCE_CAPACITY = 4096

_DOMAIN_BYTES = GATEWAY_ASSERTION_DOMAIN.encode("ascii") + b"\0"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_METHOD = re.compile(r"^[A-Z]{3,16}$")
_QUERY_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_VISIBLE_ASCII = frozenset(chr(code) for code in range(33, 127))
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_HEX = frozenset("0123456789abcdefABCDEF")
_FIELDS = frozenset({
    "workspace_id",
    "gateway_instance_id",
    "boot_generation",
    "worker_container_id",
    "config_version",
    "method",
    "path",
    "canonical_query",
    "body_sha256",
    "issued_at",
    "expires_at",
    "nonce",
    "request_id",
})


class GatewayError(Exception):
    """带稳定错误码与 HTTP 状态的 gateway 领域错误；不含秘密。"""

    def __init__(self, code: str, status: int, *, retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after = retry_after


def _unauthorized() -> GatewayError:
    return GatewayError("gateway_unauthorized", 401)


def _forbidden() -> GatewayError:
    return GatewayError("gateway_forbidden", 403)


def _invalid_argument() -> GatewayError:
    return GatewayError("gateway_invalid_request", 400)


def _require_identifier(value: object, name: str) -> str:
    if (not isinstance(value, str) or not value.isascii()
            or not _IDENTIFIER.fullmatch(value)):
        raise ValueError(f"{name} is invalid")
    return value


def _require_container_id(value: object) -> str:
    if not isinstance(value, str) or not _CONTAINER_ID.fullmatch(value):
        raise ValueError("worker_container_id must be a 64-character hex ID")
    return value


def _require_version(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("config_version must be a non-negative int")
    return value


def _visible(value: object, name: str, *, maximum: int = 128) -> str:
    if (not isinstance(value, str) or not value or len(value) > maximum
            or any(character not in _VISIBLE_ASCII for character in value)):
        raise ValueError(f"{name} must be visible ASCII")
    return value


def _validate_origin(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("worker_origin is invalid")
    if (any(character.isspace() or ord(character) < 32 for character in value)
            or "?" in value or "#" in value):
        raise ValueError("worker_origin is invalid")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("worker_origin is invalid") from None
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or (port is not None and port == 0)
            or parsed.query or parsed.fragment
            or parsed.path not in ("", "/")):
        raise ValueError("worker_origin is invalid")
    if parsed.hostname in ("0.0.0.0", "::", "::0"):
        raise ValueError("worker_origin is invalid")
    netloc = parsed.netloc
    if port is None and parsed.hostname and ":" not in parsed.hostname:
        netloc = parsed.hostname
    return f"{parsed.scheme}://{netloc}"


@dataclass(frozen=True)
class GatewayAudience:
    """一次 gateway 实例/worker 代次的固定 audience。"""

    workspace_id: str
    gateway_instance_id: str
    boot_generation: str
    worker_container_id: str
    config_version: int

    def __post_init__(self) -> None:
        _require_identifier(self.workspace_id, "workspace_id")
        _require_identifier(self.gateway_instance_id, "gateway_instance_id")
        _require_identifier(self.boot_generation, "boot_generation")
        _require_container_id(self.worker_container_id)
        _require_version(self.config_version)


@dataclass(frozen=True)
class GatewayConfig:
    """受信部署配置；worker service token 不进入默认 repr。"""

    workspace_id: str
    gateway_instance_id: str
    boot_generation: str
    worker_container_id: str
    config_version: int
    worker_origin: str
    worker_service_token: str = field(repr=False)

    def __post_init__(self) -> None:
        _require_identifier(self.workspace_id, "workspace_id")
        _require_identifier(self.gateway_instance_id, "gateway_instance_id")
        _require_identifier(self.boot_generation, "boot_generation")
        _require_container_id(self.worker_container_id)
        _require_version(self.config_version)
        object.__setattr__(self, "worker_origin", _validate_origin(
            self.worker_origin))
        token = self.worker_service_token
        if (not isinstance(token, str) or not token or len(token) > MAX_TOKEN_BYTES
                or any(character not in _VISIBLE_ASCII for character in token)):
            raise ValueError("worker_service_token is invalid")

    @property
    def audience(self) -> GatewayAudience:
        return GatewayAudience(
            self.workspace_id, self.gateway_instance_id,
            self.boot_generation, self.worker_container_id,
            self.config_version)


@dataclass(frozen=True)
class GatewayAssertion:
    """已验签的请求断言；不含原始头或服务凭证。"""

    workspace_id: str
    gateway_instance_id: str
    boot_generation: str
    worker_container_id: str
    config_version: int
    method: str
    path: str
    canonical_query: str
    body_sha256: str
    issued_at: int
    expires_at: int
    nonce: str
    request_id: str


def new_boot_generation() -> str:
    """每次进程启动生成新的随机 boot generation；不得复用 Docker 标签。"""
    return secrets.token_hex(16)


def _quote(value: str) -> str:
    return quote(value, safe="-._~", encoding="utf-8", errors="strict")


def canonical_query(pairs: Mapping[str, str] | Iterable[tuple[str, str]]
                    ) -> str:
    """把已知查询参数规范成排序、单次百分号编码的字符串。"""
    if isinstance(pairs, Mapping):
        items = list(pairs.items())
    else:
        items = list(pairs)
    seen: set[str] = set()
    normalized: list[tuple[str, str]] = []
    for key, value in items:
        if (not isinstance(key, str) or not _QUERY_KEY.fullmatch(key)
                or key in seen or not isinstance(value, str)):
            raise ValueError("query parameter is invalid")
        seen.add(key)
        if not value or any(character in "\x00\r\n" for character in value):
            raise ValueError("query value is invalid")
        normalized.append((key, _quote(value)))
    normalized.sort(key=lambda item: item[0])
    return "&".join(f"{key}={value}" for key, value in normalized)


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode_b64url(value: str) -> bytes:
    if (not value or any(character not in
                         "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
                         for character in value)
            or len(value) % 4 == 1):
        raise ValueError("invalid base64url")
    padding = "=" * ((4 - len(value) % 4) % 4)
    decoded = base64.urlsafe_b64decode(value + padding)
    if _b64url(decoded) != value:
        raise ValueError("non-canonical base64url")
    return decoded


def _validate_path(path: object, *, status: int) -> str:
    if (not isinstance(path, str) or not path or len(path) > MAX_PATH_BYTES
            or not path.isascii() or not path.startswith("/")):
        raise GatewayError("gateway_invalid_path", status)
    if (path == "/" or path.endswith("/") or "//" in path
            or "\\" in path or "?" in path or "#" in path
            or any(ord(character) < 32 or ord(character) == 127
                   for character in path)):
        raise GatewayError("gateway_invalid_path", status)
    lowered = path.lower()
    if any(marker in lowered for marker in ("%00", "%2f", "%5c", "%0a", "%0d")):
        raise GatewayError("gateway_invalid_path", status)
    index = 0
    while index < len(path):
        if path[index] == "%":
            if (index + 2 >= len(path) or path[index + 1] not in _HEX
                    or path[index + 2] not in _HEX):
                raise GatewayError("gateway_invalid_path", status)
            index += 3
            continue
        index += 1
    return path


def _validate_canonical_query(value: object, *, status: int) -> str:
    if not isinstance(value, str) or not value.isascii() \
            or len(value) > MAX_QUERY_BYTES:
        raise GatewayError("gateway_invalid_query", status)
    if not value:
        return value
    previous_key: str | None = None
    for part in value.split("&"):
        if not part or part.count("=") != 1:
            raise GatewayError("gateway_invalid_query", status)
        key, encoded = part.split("=", 1)
        if (not _QUERY_KEY.fullmatch(key) or not encoded
                or (previous_key is not None and key <= previous_key)):
            raise GatewayError("gateway_invalid_query", status)
        previous_key = key
        index = 0
        while index < len(encoded):
            character = encoded[index]
            if character == "%":
                if (index + 2 >= len(encoded)
                        or encoded[index + 1] not in "0123456789ABCDEF"
                        or encoded[index + 2] not in "0123456789ABCDEF"):
                    raise GatewayError("gateway_invalid_query", status)
                byte = int(encoded[index + 1:index + 3], 16)
                if chr(byte) in _UNRESERVED:
                    # 明确拒绝“百分号编码了 unreserved 字符”的别名形式
                    raise GatewayError("gateway_invalid_query", status)
                index += 3
                continue
            if character not in _UNRESERVED:
                raise GatewayError("gateway_invalid_query", status)
            index += 1
    return value


def _validate_assertion_payload(payload: object) -> dict:
    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        raise _unauthorized()
    try:
        _require_identifier(payload["workspace_id"], "workspace_id")
        _require_identifier(
            payload["gateway_instance_id"], "gateway_instance_id")
        _require_identifier(payload["boot_generation"], "boot_generation")
        _require_container_id(payload["worker_container_id"])
        _require_version(payload["config_version"])
        method = payload["method"]
        if not isinstance(method, str) or not _METHOD.fullmatch(method):
            raise ValueError("method is invalid")
        _validate_path(payload["path"], status=401)
        _validate_canonical_query(payload["canonical_query"], status=401)
        if (not isinstance(payload["body_sha256"], str)
                or not _SHA256.fullmatch(payload["body_sha256"])):
            raise ValueError("body_sha256 is invalid")
        issued_at = payload["issued_at"]
        expires_at = payload["expires_at"]
        if type(issued_at) is not int or type(expires_at) is not int:
            raise ValueError("times are invalid")
        if (issued_at < 0 or expires_at <= issued_at
                or expires_at - issued_at > MAX_ASSERTION_TTL_SECONDS):
            raise ValueError("lifetime is invalid")
        _visible(payload["nonce"], "nonce")
        _visible(payload["request_id"], "request_id")
    except (ValueError, GatewayError):
        raise _unauthorized() from None
    return payload


def _parse_assertion(token: object) -> tuple[dict, bytes]:
    if (not isinstance(token, str) or not token.isascii()
            or len(token) > MAX_ASSERTION_BYTES):
        raise _unauthorized()
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != GATEWAY_ASSERTION_VERSION:
        raise _unauthorized()
    try:
        payload_bytes = _decode_b64url(parts[1])
        signature = _decode_b64url(parts[2])
    except ValueError:
        raise _unauthorized() from None
    if len(signature) != 64 or len(payload_bytes) > MAX_ASSERTION_BYTES:
        raise _unauthorized()
    try:
        payload = json.loads(
            payload_bytes.decode("ascii"),
            object_pairs_hook=_no_duplicate_pairs)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _unauthorized() from None
    try:
        canonical = _canonical_json(payload)
    except (TypeError, ValueError, RecursionError):
        # Python json 接受 NaN/Infinity；canonical JSON 明确不接受。
        raise _unauthorized() from None
    if canonical != payload_bytes:
        raise _unauthorized()
    return _validate_assertion_payload(payload), signature


def _no_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _validate_signed_request(method: object, path: object,
                             canonical_query_value: object) -> tuple[str, str, str]:
    if (not isinstance(method, str) or not _METHOD.fullmatch(method)):
        raise _invalid_argument()
    path_value = _validate_path(path, status=400)
    query_value = _validate_canonical_query(canonical_query_value, status=400)
    return method, path_value, query_value


def sign_gateway_assertion(
        private_key: Ed25519PrivateKey,
        audience: GatewayAudience, *,
        method: str,
        path: str,
        query: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
        body: bytes = b"",
        issued_at: int | None = None,
        expires_at: int | None = None,
        nonce: str | None = None,
        request_id: str | None = None,
        clock: Callable[[], float] = time.time) -> str:
    """签名一次请求；调用方负责传入规范 path 与允许的 query 参数。

    默认生成 30 秒有效断言；不接受超过 30 秒的显式有效期。
    """
    if not isinstance(private_key, Ed25519PrivateKey):
        raise _invalid_argument()
    if not isinstance(audience, GatewayAudience):
        raise _invalid_argument()
    method, path, query_text = _validate_signed_request(
        method, path, canonical_query({} if query is None else query))
    if not isinstance(body, (bytes, bytearray, memoryview)):
        raise _invalid_argument()
    body_bytes = bytes(body)
    if len(body_bytes) > 64 * 1024 * 1024:
        raise _invalid_argument()
    now = int(clock())
    issued = now if issued_at is None else issued_at
    if type(issued) is not int or issued < 0:
        raise _invalid_argument()
    expires = issued + MAX_ASSERTION_TTL_SECONDS if expires_at is None \
        else expires_at
    if (type(expires) is not int or expires <= issued
            or expires - issued > MAX_ASSERTION_TTL_SECONDS):
        raise _invalid_argument()
    nonce_value = secrets.token_urlsafe(32) if nonce is None else nonce
    request_value = secrets.token_hex(16) if request_id is None else request_id
    try:
        _visible(nonce_value, "nonce")
        _visible(request_value, "request_id")
    except ValueError:
        raise _invalid_argument() from None
    payload = {
        "workspace_id": audience.workspace_id,
        "gateway_instance_id": audience.gateway_instance_id,
        "boot_generation": audience.boot_generation,
        "worker_container_id": audience.worker_container_id,
        "config_version": audience.config_version,
        "method": method,
        "path": path,
        "canonical_query": query_text,
        "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
        "issued_at": issued,
        "expires_at": expires,
        "nonce": nonce_value,
        "request_id": request_value,
    }
    payload_bytes = _canonical_json(payload)
    signature = private_key.sign(_DOMAIN_BYTES + payload_bytes)
    return (f"{GATEWAY_ASSERTION_VERSION}.{_b64url(payload_bytes)}."
            f"{_b64url(signature)}")


def verify_gateway_assertion(
        public_key: Ed25519PublicKey,
        token: str,
        *,
        audience: GatewayAudience,
        method: str,
        path: str,
        canonical_query: str,
        body_sha256: str,
        now: int) -> GatewayAssertion:
    """验签并绑定完整请求；任何 audience/请求字段不一致都拒绝。"""
    if not isinstance(public_key, Ed25519PublicKey) \
            or not isinstance(audience, GatewayAudience):
        raise _unauthorized()
    if type(now) is not int:
        raise _unauthorized()
    method, path, query_text = _validate_signed_request(
        method, path, canonical_query)
    if (not isinstance(body_sha256, str)
            or not _SHA256.fullmatch(body_sha256)):
        raise _invalid_argument()
    payload, signature = _parse_assertion(token)
    try:
        public_key.verify(signature, _DOMAIN_BYTES + _canonical_json(payload))
    except InvalidSignature:
        raise _unauthorized() from None
    if (
        payload["workspace_id"] != audience.workspace_id
        or payload["gateway_instance_id"] != audience.gateway_instance_id
        or payload["boot_generation"] != audience.boot_generation
        or payload["worker_container_id"] != audience.worker_container_id
        or payload["config_version"] != audience.config_version
        or payload["method"] != method
        or payload["path"] != path
        or payload["canonical_query"] != query_text
        or payload["body_sha256"] != body_sha256
    ):
        raise _forbidden()
    issued_at = payload["issued_at"]
    expires_at = payload["expires_at"]
    if expires_at <= now or issued_at > now + MAX_CLOCK_SKEW_SECONDS:
        raise _unauthorized()
    return GatewayAssertion(
        workspace_id=payload["workspace_id"],
        gateway_instance_id=payload["gateway_instance_id"],
        boot_generation=payload["boot_generation"],
        worker_container_id=payload["worker_container_id"],
        config_version=payload["config_version"],
        method=payload["method"],
        path=payload["path"],
        canonical_query=payload["canonical_query"],
        body_sha256=payload["body_sha256"],
        issued_at=issued_at,
        expires_at=expires_at,
        nonce=payload["nonce"],
        request_id=payload["request_id"],
    )


class GatewayNonceCache:
    """有界 nonce 缓存；线程锁保证 check+add 原子，满容量失败关闭。"""

    def __init__(self, capacity: int = DEFAULT_NONCE_CAPACITY) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("capacity must be a positive int")
        self._capacity = capacity
        self._entries: dict[str, int] = {}
        self._lock = threading.Lock()

    def accept(self, nonce: str, *, expires_at: int, now: int) -> None:
        if (not isinstance(nonce, str) or not nonce or len(nonce) > 128
                or type(expires_at) is not int or type(now) is not int):
            raise _unauthorized()
        with self._lock:
            expired = [key for key, expiry in self._entries.items()
                       if expiry <= now]
            for key in expired:
                del self._entries[key]
            if nonce in self._entries:
                raise GatewayError("gateway_replay", 401)
            if len(self._entries) >= self._capacity:
                raise GatewayError("gateway_nonce_capacity", 503)
            self._entries[nonce] = expires_at

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._entries)
