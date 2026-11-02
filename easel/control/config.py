"""默认能力配置、个人完整路由、AES-GCM 加密与版本控制。

设计要点：

* 默认配置按版本追加保存，旧版本不可覆盖；个人覆盖按 workspace + channel
  存储，互不影响。
* 公共路由 JSON 只保存 provider/model/base_url/protocol/options 等非秘密字段；
  凭证 bundle 用 AES-GCM 加密，AAD 绑定作用域（managed 版本或 workspace +
  channel），跨作用域替换密文无法解密。
* 个人完整路由更换 provider/base_url/protocol 时必须重新提供凭证；只有凭证
  作用域不变时才允许用空 secret 复用原个人密文。
* ``managed-model`` 是显式 route_kind：只允许管理员批准过的模型和非敏感参数，
  地址、协议、凭证仍来自当前受管版本。
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .egress import EgressPolicy
from .store import Store
from .types import DomainError, EffectiveRoute, Identity

_SHARED_SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "shared" / "scripts"
if str(_SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SHARED_SCRIPTS))

from model_registry import MODEL_GROUPS, provider_metadata  # noqa: E402

MASTER_KEY_BYTES = 32
CHANNELS = ("speech", "image", "video", "music")
"""member 可保存个人覆盖的能力通道。"""

ADMIN_CHANNELS = ("speech", "image", "video", "music", "chat")
"""管理员受管配置通道；chat 只能由管理员配置，member 不能覆盖。"""

_REGISTRY_GROUP = {"speech": "voice", "image": "image", "video": "video",
                   "music": "music", "chat": "chat"}
_COMPLETE_FIELDS = frozenset({"route_kind", "provider", "model", "base_url",
                              "protocol", "secret", "options"})
_MANAGED_MODEL_FIELDS = frozenset({"route_kind", "model", "options"})

_ALLOWED_OPTION_KEYS = frozenset({
    # 非敏感能力参数；键统一小写，值只允许标量。
    "speed", "rate", "pitch", "volume", "format", "output_format",
    "sample_rate", "bitrate", "quality", "language", "voice", "voice_id",
    "speaker", "style", "emotion", "instruct", "instruction", "instructions",
    "seed", "steps", "num_inference_steps", "n", "temperature", "top_p",
    "top_k", "presence_penalty", "frequency_penalty", "max_tokens",
    "timeout", "response_format", "duration", "seconds", "fps",
    "num_frames", "width", "height", "size", "image_size", "aspect_ratio",
    "guidance", "guidance_scale", "cfg_scale", "prompt_strength",
    "negative_prompt", "audio_format", "delimiter",
})


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError):
        raise DomainError("invalid_route", 400) from None


def _scope_bytes(scope: dict) -> bytes:
    return json.dumps(scope, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def _require_channel(channel: str) -> None:
    if not isinstance(channel, str) or channel not in CHANNELS:
        raise DomainError("invalid_channel", 400)


def _require_admin_channel(channel: str) -> None:
    if not isinstance(channel, str) or channel not in ADMIN_CHANNELS:
        raise DomainError("invalid_channel", 400)


def _text_field(value: Any, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise DomainError("invalid_route", 400)
    value = value.strip()
    if not value or len(value) > max_length or any(ord(char) < 32 for char in value):
        raise DomainError("invalid_route", 400)
    return value


def _validate_base_url(value: Any) -> str:
    if not isinstance(value, str):
        raise DomainError("invalid_route", 400)
    value = value.strip()
    if (not value or len(value) > 2048 or any(char.isspace() for char in value)
            or "\\" in value):
        raise DomainError("invalid_route", 400)
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError:
        raise DomainError("invalid_route", 400) from None
    if (parsed.scheme not in ("http", "https") or not hostname
            or parsed.username is not None or parsed.password is not None):
        raise DomainError("invalid_route", 400)
    return value


def _validate_options(value: Any) -> dict:
    """只接受明确允许的非敏感标量能力参数。

    采用允许列表而不是凭证关键词黑名单：headers/auth/credentials 等结构、
    嵌套字典/数组、路由与底座参数都不是能力参数，统一 400 拒绝。这样凭证
    只能走 ``secret`` 进入加密 bundle，无法从 options 形成明文旁路。
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise DomainError("invalid_route", 400)

    normalized: dict = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or key != key.strip():
            raise DomainError("invalid_route", 400)
        name = key.lower()
        if name not in _ALLOWED_OPTION_KEYS or name in normalized:
            raise DomainError("invalid_route", 400)
        if isinstance(item, bool) or item is None:
            normalized[name] = item
        elif isinstance(item, int):
            normalized[name] = item
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise DomainError("invalid_route", 400)
            normalized[name] = item
        elif isinstance(item, str):
            if any(ord(char) < 32 for char in item):
                raise DomainError("invalid_route", 400)
            normalized[name] = item
        else:
            # 嵌套字典/数组等结构一律拒绝，避免认证头/凭证结构绕过
            raise DomainError("invalid_route", 400)

    if len(_canonical(normalized)) > 16384:
        raise DomainError("invalid_route", 400)
    return normalized


def _provider_spec(channel: str, provider_id: str) -> dict:
    group = _REGISTRY_GROUP[channel]
    for provider in MODEL_GROUPS[group]["providers"]:
        if provider["id"] == provider_id:
            return provider
    raise DomainError("unknown_provider", 400)


def _base_url_key(key: dict) -> bool:
    env = key["env"]
    return env.endswith("BASE_URL") or env.endswith("_BASE")


def _normalize_secret(channel: str, provider_id: str, secret: Any, *,
                      base_url: str) -> dict[str, str]:
    """把 route.secret 规范成 registry env 名 → 值的加密 bundle。"""
    provider = _provider_spec(channel, provider_id)
    by_env = {key["env"]: key for key in provider["keys"]}
    required = [key for key in provider["keys"] if key["required"]]
    required_secret = [key for key in required if key["secret"]]
    required_other = [key for key in required if not key["secret"]]

    if isinstance(secret, str):
        if (not secret.strip() or any(char in secret for char in "\r\n\0")):
            raise DomainError("invalid_secret_bundle", 400)
        if len(required_secret) != 1:
            raise DomainError("invalid_secret_bundle", 400)
        if any(not _base_url_key(key) for key in required_other):
            raise DomainError("invalid_secret_bundle", 400)
        return {required_secret[0]["env"]: secret}

    if not isinstance(secret, dict) or not secret:
        raise DomainError("invalid_secret_bundle", 400)
    bundle: dict[str, str] = {}
    for env_name, value in secret.items():
        if env_name not in by_env:
            raise DomainError("invalid_secret_bundle", 400)
        if (not isinstance(value, str) or not value.strip()
                or any(char in value for char in "\r\n\0")):
            raise DomainError("invalid_secret_bundle", 400)
        key = by_env[env_name]
        if not key["secret"] and _base_url_key(key):
            if value.strip().rstrip("/") != base_url.rstrip("/"):
                raise DomainError("invalid_secret_bundle", 400)
            continue
        bundle[env_name] = value

    for key in required:
        if key["secret"] or not _base_url_key(key):
            if key["env"] not in bundle:
                raise DomainError("invalid_secret_bundle", 400)
    return bundle


def _secret_is_empty(secret: Any) -> bool:
    return secret is None or secret == "" or (
        isinstance(secret, dict) and not secret)


def _validate_complete_route(channel: str, route: Any, *,
                             allow_allowed_models: bool,
                             for_admin_default: bool) -> dict:
    if not isinstance(route, dict):
        raise DomainError("invalid_route", 400)
    allowed = set(_COMPLETE_FIELDS)
    if allow_allowed_models:
        allowed.add("allowed_models")
    if set(route) - allowed:
        raise DomainError("invalid_route", 400)
    if route.get("route_kind", "complete") != "complete":
        raise DomainError("invalid_route", 400)

    provider_id = _text_field(route.get("provider"), max_length=128)
    _provider_spec(channel, provider_id)
    metadata = provider_metadata(_REGISTRY_GROUP[channel], provider_id)
    if not metadata["override_allowed"] and not for_admin_default:
        raise DomainError("override_not_allowed", 403)

    public = {
        "route_kind": "complete",
        "provider": provider_id,
        "model": _text_field(route.get("model"), max_length=256),
        "base_url": _validate_base_url(route.get("base_url")),
        "protocol": _text_field(route.get("protocol"), max_length=64),
        "options": _validate_options(route.get("options", {})),
    }
    if allow_allowed_models and "allowed_models" in route:
        allowed_models = route["allowed_models"]
        if not isinstance(allowed_models, list) or len(allowed_models) > 64:
            raise DomainError("invalid_route", 400)
        normalized: list[str] = []
        for item in allowed_models:
            model = _text_field(item, max_length=256)
            if model not in normalized:
                normalized.append(model)
        if public["model"] not in normalized:
            # 默认模型必须在允许集合内，否则省略 model 的请求会越界
            raise DomainError("invalid_route", 400)
        public["allowed_models"] = normalized
    return public


def _validate_managed_model_route(route: Any) -> dict:
    if not isinstance(route, dict):
        raise DomainError("invalid_route", 400)
    if set(route) - _MANAGED_MODEL_FIELDS:
        raise DomainError("invalid_route", 400)
    if route.get("route_kind") != "managed-model":
        raise DomainError("invalid_route", 400)
    public = {"route_kind": "managed-model",
              "options": _validate_options(route.get("options", {}))}
    if route.get("model") is not None:
        public["model"] = _text_field(route["model"], max_length=256)
    return public


def _model_allowed(managed: dict, model: str) -> bool:
    allowed = managed.get("allowed_models")
    if isinstance(allowed, list) and allowed:
        return model in allowed
    return model == managed.get("model")


def load_master_key(path: Path) -> bytes:
    """从显式文件读取 32 字节 master key；缺失/长度错误/权限过宽明确失败。"""
    key_path = Path(path)
    data = key_path.read_bytes()
    if len(data) != MASTER_KEY_BYTES:
        raise ValueError(
            f"master key file must contain exactly {MASTER_KEY_BYTES} bytes")
    if os.name == "posix" and key_path.stat().st_mode & 0o077:
        raise ValueError("master key file must not be group/world accessible")
    return data


class ConfigService:
    """默认/个人能力配置；所有秘密只在服务端内存中解密。"""

    def __init__(self, store: Store, master_key: bytes,
                 egress_policy: EgressPolicy | None = None, *,
                 egress_policy_configured: bool | None = None,
                 model_catalog: tuple[str, ...] | None = None):
        if (not isinstance(master_key, (bytes, bytearray))
                or len(master_key) != MASTER_KEY_BYTES):
            raise ValueError(
                f"master_key must be exactly {MASTER_KEY_BYTES} bytes")
        if model_catalog is not None:
            if (not isinstance(model_catalog, tuple)
                    or len(model_catalog) > 64
                    or any(not isinstance(item, str) or not item
                           or len(item) > 256
                           or any(ord(character) < 32 for character in item)
                           for item in model_catalog)
                    or len(set(model_catalog)) != len(model_catalog)):
                raise ValueError("model_catalog is invalid")
        self._model_catalog = model_catalog
        if egress_policy_configured is None:
            egress_policy_configured = egress_policy is not None
        if not isinstance(egress_policy_configured, bool):
            raise TypeError("egress_policy_configured must be bool")
        if egress_policy is None:
            egress_policy = EgressPolicy()
        if not isinstance(egress_policy, EgressPolicy):
            raise TypeError("egress_policy must be EgressPolicy")
        self._store = store
        self._key = bytes(master_key)
        self.egress_policy = egress_policy
        self.egress_policy_configured = egress_policy_configured
        self._verify_existing_ciphertext()

    def with_store(self, store: Store) -> "ConfigService":
        """在同一 master key 下为应用线程重建服务；会重新校验已有密文。"""
        return ConfigService(
            store, self._key,
            egress_policy=self.egress_policy,
            egress_policy_configured=self.egress_policy_configured,
            model_catalog=self._model_catalog)

    @property
    def model_catalog(self) -> tuple[str, ...] | None:
        return self._model_catalog

    def _now(self) -> int:
        return int(time.time())

    # ------------------------------------------------------------------
    # 加解密
    # ------------------------------------------------------------------

    def _encrypt(self, bundle: dict, scope: dict) -> bytes:
        nonce = os.urandom(12)
        plaintext = _canonical(bundle).encode("utf-8")
        return nonce + AESGCM(self._key).encrypt(nonce, plaintext,
                                                 _scope_bytes(scope))

    def _decrypt(self, blob: Any, scope: dict) -> dict:
        if not isinstance(blob, (bytes, bytearray)) or len(blob) < 13:
            raise DomainError("secret_unavailable", 500)
        try:
            nonce = bytes(blob[:12])
            plaintext = AESGCM(self._key).decrypt(
                nonce, bytes(blob[12:]), _scope_bytes(scope))
            value = json.loads(plaintext)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except Exception:
            # 不回显密文、key 或底层异常，避免把秘密带进日志/响应
            raise DomainError("secret_unavailable", 500) from None

    def _managed_scope(self, version: int) -> dict:
        return {"scope": "managed", "version": int(version)}

    def _workspace_scope(self, workspace_id: str, channel: str) -> dict:
        return {"scope": "workspace", "workspace_id": workspace_id,
                "channel": channel}

    def _snapshot_scope(self, snapshot_id: str) -> dict:
        return {"scope": "broker", "snapshot_id": snapshot_id}

    @staticmethod
    def _validated_bundle(value: Any) -> dict[str, str]:
        """凭证 bundle 只允许非空字符串键值；不回显内容。"""
        if not isinstance(value, dict) or not value:
            raise DomainError("secret_unavailable", 500)
        bundle: dict[str, str] = {}
        for key, item in value.items():
            if (not isinstance(key, str) or not key or not isinstance(item, str)
                    or not item or any(char in item for char in "\r\n\0")):
                raise DomainError("secret_unavailable", 500)
            bundle[key] = item
        return bundle

    def seal_broker_snapshot(self, bundle: dict, snapshot_id: str) -> bytes:
        """把签发时的凭证 bundle 重新加密到 token 快照作用域。"""
        bundle = self._validated_bundle(bundle)
        return self._encrypt(bundle, self._snapshot_scope(snapshot_id))

    def open_broker_snapshot(self, snapshot_id: str) -> dict[str, str]:
        """仅服务器内部 adapter 使用；解密 token 快照。"""
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise DomainError("secret_unavailable", 500)
        row = self._store.get_broker_snapshot(snapshot_id)
        if row is None:
            raise DomainError("secret_unavailable", 500)
        return self._validated_bundle(
            self._decrypt(row["encrypted_secrets"],
                          self._snapshot_scope(snapshot_id)))

    def credentials_for_ref(self, credential_ref: str) -> dict[str, str]:
        """按 credential_ref 解密服务端凭证；调用者不能提交任意引用。

        支持的引用形式均来自服务端 resolve 结果：

        * ``managed:<version>:<channel>`` —— 不可变管理员版本；
        * ``workspace:<workspace_id>:<channel>:<revision>`` —— 个人完整路由，
          revision 必须与当前行一致（签发 token 时会另行保存不可变快照）；
        * ``snapshot:<snapshot_id>`` —— broker token 自己的加密快照。
        """
        if not isinstance(credential_ref, str) or not credential_ref:
            raise DomainError("secret_unavailable", 500)
        parts = credential_ref.split(":")
        if len(parts) == 2 and parts[0] == "snapshot":
            return self.open_broker_snapshot(parts[1])
        if len(parts) == 3 and parts[0] == "managed":
            version_text, channel = parts[1], parts[2]
            if not version_text.isdigit():
                raise DomainError("secret_unavailable", 500)
            version = int(version_text)
            row = self._store.get_managed_revision(version)
            if row is None:
                raise DomainError("secret_unavailable", 500)
            decrypted = self._decrypt(row["encrypted_secrets"],
                                      self._managed_scope(version))
            return self._validated_bundle(decrypted.get(channel))
        if len(parts) == 4 and parts[0] == "workspace":
            workspace_id, channel, version_text = parts[1], parts[2], parts[3]
            if not version_text.isdigit():
                raise DomainError("secret_unavailable", 500)
            row = self._store.get_workspace_override(workspace_id, channel)
            if (row is None or row["route_kind"] != "complete"
                    or int(row["version"]) != int(version_text)
                    or row["encrypted_secrets"] is None):
                raise DomainError("secret_unavailable", 500)
            return self._validated_bundle(self._decrypt(
                row["encrypted_secrets"],
                self._workspace_scope(workspace_id, channel)))
        raise DomainError("secret_unavailable", 500)

    def export_credentials(self, route: EffectiveRoute) -> dict[str, str]:
        """签发快照时复制当前生效凭证；明文只停留在服务端内存。"""
        return self.credentials_for_ref(route.credential_ref)

    def _verify_existing_ciphertext(self) -> None:
        latest = self._store.latest_managed_revision()
        if latest is not None:
            try:
                self._decrypt(latest["encrypted_secrets"],
                              self._managed_scope(latest["version"]))
            except DomainError:
                raise ValueError(
                    "master_key does not match existing ciphertext") from None
            return
        row = self._store.first_encrypted_override()
        if row is not None:
            try:
                self._decrypt(row["encrypted_secrets"],
                              self._workspace_scope(row["workspace_id"],
                                                    row["channel"]))
            except DomainError:
                raise ValueError(
                    "master_key does not match existing ciphertext") from None

    # ------------------------------------------------------------------
    # 默认配置
    # ------------------------------------------------------------------

    def publish(self, actor: Identity, settings: dict,
                expected_version: int) -> int:
        if type(expected_version) is not int or expected_version < 0:
            raise DomainError("invalid_version", 400)
        # 先按数据库复查管理员身份，再做请求体校验/加密
        identity = self._store.require_identity(actor)
        if identity.role != "admin":
            raise DomainError("forbidden", 403)
        if not isinstance(settings, dict) or set(settings) - {"defaults"}:
            raise DomainError("invalid_settings", 400)
        defaults = settings.get("defaults", {})
        if not isinstance(defaults, dict):
            raise DomainError("invalid_settings", 400)

        current_defaults, current_bundle, current_version = \
            self._load_managed_state()
        if expected_version != current_version:
            # 乐观锁在读取完整版本后立即拒绝，避免以旧完整版本覆盖他人新版本
            raise DomainError("version_conflict", 409)
        if set(current_defaults) != set(current_bundle):
            raise DomainError("configuration_invalid", 500)

        # 保存完整版本：未提交的通道沿用当前公共路由与凭证，编辑只改变
        # 请求中出现的通道。所有凭证按新版本 AAD 重新加密。
        merged_public = {channel: dict(route)
                         for channel, route in current_defaults.items()}
        merged_bundle = {channel: dict(bundle)
                         for channel, bundle in current_bundle.items()}
        for channel, route in defaults.items():
            _require_admin_channel(channel)
            public = _validate_complete_route(
                channel, route, allow_allowed_models=True,
                for_admin_default=True)
            if channel == "chat":
                self._validate_chat_catalog(public)
            # 保存时即执行与签发/实际请求一致的出口前置校验
            self.egress_policy.validate_url(public["base_url"], resolve=False)
            metadata = provider_metadata(_REGISTRY_GROUP[channel],
                                         public["provider"])
            if metadata["broker_adapter"] is None:
                raise DomainError("adapter_unavailable", 400)
            secret = route.get("secret")
            if _secret_is_empty(secret):
                # 空凭证只允许在当前受管版本的同一凭证作用域内复用；
                # 公共响应从不回传旧 Key，浏览器也无需再次提交明文。
                previous = current_defaults.get(channel)
                previous_bundle = current_bundle.get(channel)
                if previous is None or previous_bundle is None:
                    raise DomainError("invalid_secret_bundle", 400)
                if any(previous.get(key) != public.get(key)
                       for key in ("provider", "base_url", "protocol")):
                    raise DomainError("invalid_secret_bundle", 400)
                bundle = dict(previous_bundle)
            else:
                bundle = _normalize_secret(
                    channel, public["provider"], secret,
                    base_url=public["base_url"])
            merged_public[channel] = public
            merged_bundle[channel] = bundle

        new_version = expected_version + 1
        encrypted = self._encrypt(merged_bundle,
                                  self._managed_scope(new_version))
        public_json = _canonical({"defaults": merged_public})
        return self._store.publish_managed_revision(
            actor, public_json, encrypted, expected_version, self._now())

    def _validate_chat_catalog(self, public: dict) -> None:
        """受管 chat 只能使用锁定 Harness 已验证的模型目录。"""
        if self._model_catalog is None:
            return
        if not self._model_catalog:
            raise DomainError("model_catalog_unavailable", 503)
        allowed = tuple(public.get("allowed_models") or (public["model"],))
        if (public["model"] not in self._model_catalog
                or any(model not in self._model_catalog for model in allowed)):
            raise DomainError("model_not_allowed", 409)

    def _validated_managed_bundle(self, value: Any) -> dict[str, dict]:
        """受管凭证 bundle 只允许已知通道 → 非空字符串键值。"""
        if not isinstance(value, dict):
            raise DomainError("configuration_invalid", 500)
        bundle: dict[str, dict] = {}
        for channel, item in value.items():
            if channel not in ADMIN_CHANNELS:
                raise DomainError("configuration_invalid", 500)
            bundle[channel] = self._validated_bundle(item)
        return bundle

    def _load_managed_state(self) -> tuple[dict, dict[str, dict], int]:
        """读取当前完整公共版本与解密 bundle；缺失版本返回空状态。"""
        latest = self._store.latest_managed_revision()
        if latest is None:
            return {}, {}, 0
        version = int(latest["version"])
        try:
            payload = json.loads(latest["public_json"])
            if not isinstance(payload, dict):
                raise ValueError
            defaults = payload.get("defaults")
            if (not isinstance(defaults, dict)
                    or set(defaults) - set(ADMIN_CHANNELS)):
                raise ValueError
        except (TypeError, ValueError):
            raise DomainError("configuration_invalid", 500) from None
        decrypted = self._decrypt(latest["encrypted_secrets"],
                                  self._managed_scope(version))
        return defaults, self._validated_managed_bundle(decrypted), version

    def public_managed_settings(self, actor: Identity) -> dict:
        """管理员公共视图：只投影白名单字段与凭证是否已配置。"""
        identity = self._store.require_identity(actor)
        if identity.role != "admin":
            raise DomainError("forbidden", 403)
        defaults, bundle, version = self._load_managed_state()
        public: dict[str, dict] = {}
        for channel, route in defaults.items():
            entry = {
                "provider": route.get("provider"),
                "model": route.get("model"),
                "base_url": route.get("base_url"),
                "protocol": route.get("protocol"),
                "options": dict(route.get("options") or {}),
                "credential_configured": bool(bundle.get(channel)),
            }
            if "allowed_models" in route:
                entry["allowed_models"] = list(route["allowed_models"])
            public[channel] = entry
        return {"version": version, "defaults": public}

    def _load_managed_defaults(self, *, verify: bool) -> tuple[dict, int]:
        latest = self._store.latest_managed_revision()
        if latest is None:
            return {}, 0
        if verify:
            self._decrypt(latest["encrypted_secrets"],
                          self._managed_scope(latest["version"]))
        try:
            payload = json.loads(latest["public_json"])
            defaults = payload.get("defaults")
            if not isinstance(defaults, dict):
                raise ValueError
        except (TypeError, ValueError):
            raise DomainError("configuration_invalid", 500) from None
        return defaults, int(latest["version"])

    # ------------------------------------------------------------------
    # 个人覆盖
    # ------------------------------------------------------------------

    def save_override(self, actor: Identity, channel: str, route: dict) -> None:
        if channel == "chat":
            # 主聊天模型由管理员配置，member 不能加入个人覆盖
            raise DomainError("override_not_allowed", 403)
        _require_channel(channel)
        identity = self._store.require_identity(actor)
        if not isinstance(route, dict):
            raise DomainError("invalid_route", 400)
        workspace_id = identity.workspace_id
        route_kind = route.get("route_kind", "complete")

        if route_kind == "complete":
            public = _validate_complete_route(
                channel, route, allow_allowed_models=False,
                for_admin_default=False)
            self.egress_policy.validate_url(public["base_url"], resolve=False)
            secret = route.get("secret")
            if _secret_is_empty(secret):
                encrypted = None
                keep_scope = {"provider": public["provider"],
                              "base_url": public["base_url"],
                              "protocol": public["protocol"]}
            else:
                bundle = _normalize_secret(
                    channel, public["provider"], secret,
                    base_url=public["base_url"])
                encrypted = self._encrypt(
                    bundle, self._workspace_scope(workspace_id, channel))
                keep_scope = None
            self._store.save_workspace_override(
                actor, channel, "complete", _canonical(public),
                encrypted, keep_scope, self._now())
            return

        if route_kind == "managed-model":
            public = _validate_managed_model_route(route)
            defaults, _version = self._load_managed_defaults(verify=True)
            managed = defaults.get(channel)
            if managed is None:
                raise DomainError("not_configured", 404)
            if "model" in public and not _model_allowed(managed, public["model"]):
                raise DomainError("model_not_allowed", 400)
            # 未显式指定 model 时不落库；resolve 每次跟随最新受管模型
            self._store.save_workspace_override(
                actor, channel, "managed-model", _canonical(public),
                None, None, self._now())
            return

        raise DomainError("invalid_route", 400)

    def clear_override(self, actor: Identity, channel: str) -> None:
        if channel == "chat":
            raise DomainError("override_not_allowed", 403)
        _require_channel(channel)
        self._store.clear_workspace_override(actor, channel, self._now())

    # ------------------------------------------------------------------
    # 解析与脱敏视图
    # ------------------------------------------------------------------

    @staticmethod
    def _public_payload(row, *, defaults: bool) -> dict:
        try:
            payload = json.loads(row["public_json"])
            if not isinstance(payload, dict):
                raise ValueError
            if defaults:
                payload = payload.get("defaults")
                if not isinstance(payload, dict):
                    raise ValueError
            return payload
        except (TypeError, ValueError):
            raise DomainError("configuration_invalid", 500) from None

    def _managed_bundle(self, managed_row, channel: str) -> dict[str, str]:
        version = int(managed_row["version"])
        decrypted = self._decrypt(managed_row["encrypted_secrets"],
                                  self._managed_scope(version))
        return self._validated_bundle(decrypted.get(channel))

    def snapshot_from_rows(self, workspace_id: str, channel: str,
                           managed_row, override_row
                           ) -> tuple[EffectiveRoute, dict[str, str]]:
        """从同一事务读取的行同时构建路由与凭证，不重新查库。

        issue 在写事务内调用本方法；管理员发布/个人覆盖更新必须等待该事务
        提交，因此不会出现旧地址配新 Key（或反之）的快照。
        """
        if channel == "chat":
            if managed_row is None:
                raise DomainError("not_configured", 404)
            version = int(managed_row["version"])
            managed = self._public_payload(
                managed_row, defaults=True).get("chat")
            if managed is None:
                raise DomainError("not_configured", 404)
            return (
                EffectiveRoute(
                    channel="chat",
                    provider=managed["provider"],
                    model=managed["model"],
                    base_url=managed["base_url"],
                    protocol=managed["protocol"],
                    credential_ref=f"managed:{version}:chat",
                    source="admin",
                    version=version,
                    options=dict(managed.get("options") or {}),
                    allowed_models=tuple(
                        managed.get("allowed_models") or (managed["model"],)),
                ),
                self._managed_bundle(managed_row, "chat"),
            )

        _require_channel(channel)
        if override_row is not None:
            public = self._public_payload(override_row, defaults=False)
            if override_row["route_kind"] == "complete":
                bundle = self._validated_bundle(self._decrypt(
                    override_row["encrypted_secrets"],
                    self._workspace_scope(workspace_id, channel)))
                return (
                    EffectiveRoute(
                        channel=channel,
                        provider=public["provider"],
                        model=public["model"],
                        base_url=public["base_url"],
                        protocol=public["protocol"],
                        credential_ref=(
                            f"workspace:{workspace_id}:{channel}:"
                            f"{override_row['version']}"),
                        source="user",
                        version=int(override_row["version"]),
                        options=dict(public.get("options") or {}),
                        allowed_models=(public["model"],),
                    ),
                    bundle,
                )
            if override_row["route_kind"] == "managed-model":
                if managed_row is None:
                    raise DomainError("not_configured", 404)
                version = int(managed_row["version"])
                managed = self._public_payload(
                    managed_row, defaults=True).get(channel)
                if managed is None:
                    raise DomainError("not_configured", 404)
                model = public.get("model") or managed["model"]
                if not _model_allowed(managed, model):
                    raise DomainError("model_not_allowed", 409)
                options = {**(managed.get("options") or {}),
                           **(public.get("options") or {})}
                return (
                    EffectiveRoute(
                        channel=channel,
                        provider=managed["provider"],
                        model=model,
                        base_url=managed["base_url"],
                        protocol=managed["protocol"],
                        credential_ref=f"managed:{version}:{channel}",
                        source="user",
                        version=version,
                        options=options,
                        allowed_models=tuple(
                            managed.get("allowed_models") or (model,)),
                    ),
                    self._managed_bundle(managed_row, channel),
                )
            raise DomainError("configuration_invalid", 500)

        if managed_row is None:
            raise DomainError("not_configured", 404)
        version = int(managed_row["version"])
        managed = self._public_payload(
            managed_row, defaults=True).get(channel)
        if managed is None:
            raise DomainError("not_configured", 404)
        return (
            EffectiveRoute(
                channel=channel,
                provider=managed["provider"],
                model=managed["model"],
                base_url=managed["base_url"],
                protocol=managed["protocol"],
                credential_ref=f"managed:{version}:{channel}",
                source="admin",
                version=version,
                options=dict(managed.get("options") or {}),
                allowed_models=tuple(
                    managed.get("allowed_models") or (managed["model"],)),
            ),
            self._managed_bundle(managed_row, channel),
        )

    def resolve(self, actor: Identity, channel: str) -> EffectiveRoute:
        if channel != "chat":
            _require_channel(channel)
        identity = self._store.require_identity(actor)
        managed_row, override_row = self._store.read_config_rows(
            identity.workspace_id, channel)
        route, _bundle = self.snapshot_from_rows(
            identity.workspace_id, channel, managed_row, override_row)
        return route

    @staticmethod
    def _provider_schema(channel: str) -> dict[str, dict]:
        """公开能力目录的凭证字段描述；只含 env/label/必填/是否秘密/choices。"""
        group = _REGISTRY_GROUP[channel]
        schema: dict[str, dict] = {}
        for provider in MODEL_GROUPS[group]["providers"]:
            metadata = provider_metadata(group, provider["id"])
            schema[provider["id"]] = {
                "name": provider["name"],
                "keys": [
                    {
                        "env": key["env"],
                        "label": key["label"],
                        "required": bool(key["required"]),
                        "secret": bool(key["secret"]),
                        "choices": list(key.get("choices") or ()),
                    }
                    for key in provider["keys"]
                ],
                "override_allowed": metadata["override_allowed"],
                "broker_adapter": metadata["broker_adapter"],
            }
        return schema

    def public_settings(self, actor: Identity) -> dict:
        identity = self._store.require_identity(actor)
        latest = self._store.latest_managed_revision()
        latest_version = int(latest["version"]) if latest is not None else 0
        managed_defaults: dict = {}
        if latest is not None:
            try:
                payload = json.loads(latest["public_json"])
                managed_defaults = payload.get("defaults")
                if not isinstance(managed_defaults, dict):
                    raise ValueError
            except (TypeError, ValueError):
                raise DomainError("configuration_invalid", 500) from None

        overrides = {row["channel"]: row for row in
                     self._store.list_workspace_overrides(identity.workspace_id)}
        managed_verified = False
        channels: dict[str, dict] = {}
        for channel in ADMIN_CHANNELS:
            override = overrides.get(channel)
            managed = managed_defaults.get(channel)
            entry = {
                "configured": False,
                "source": "none",
                "version": 0,
                "mode": "none",
                "provider": None,
                "model": None,
                "base_url": None,
                "protocol": None,
                "options": {},
                "broker_adapter": None,
                "broker_supported": False,
                "override_allowed": channel != "chat",
            }
            if override is not None:
                try:
                    public = json.loads(override["public_json"])
                    if not isinstance(public, dict):
                        raise ValueError
                except (TypeError, ValueError):
                    raise DomainError("configuration_invalid", 500) from None
                entry["source"] = "user"
                entry["version"] = int(override["version"])
                if override["route_kind"] == "managed-model":
                    # 区分“生效摘要”与个人编辑原值：前端重开表单时只编辑
                    # 个人显式 model/options，不能把合并后的默认值当成个人覆盖。
                    entry["personal_model"] = public.get("model")
                    entry["personal_options"] = dict(
                        public.get("options") or {})
                if override["route_kind"] == "complete":
                    self._decrypt(
                        override["encrypted_secrets"],
                        self._workspace_scope(identity.workspace_id, channel))
                    entry.update({
                        "configured": True,
                        "mode": "complete",
                        "provider": public["provider"],
                        "model": public["model"],
                        "base_url": public["base_url"],
                        "protocol": public["protocol"],
                        "options": dict(public.get("options") or {}),
                    })
                elif managed is not None:
                    if not managed_verified:
                        self._decrypt(latest["encrypted_secrets"],
                                      self._managed_scope(latest_version))
                        managed_verified = True
                    model = public.get("model") or managed["model"]
                    entry.update({
                        "configured": True,
                        "mode": "managed-model",
                        "version": latest_version,
                        "provider": managed["provider"],
                        "model": model,
                        "base_url": managed["base_url"],
                        "protocol": managed["protocol"],
                        "options": {**(managed.get("options") or {}),
                                    **(public.get("options") or {})},
                    })
                    entry["allowed_models"] = managed.get("allowed_models")
                else:
                    entry["mode"] = "managed-model"
                    entry["version"] = latest_version
            elif managed is not None:
                if not managed_verified:
                    self._decrypt(latest["encrypted_secrets"],
                                  self._managed_scope(latest_version))
                    managed_verified = True
                entry.update({
                    "configured": True,
                    "source": "admin",
                    "version": latest_version,
                    "mode": "inherited",
                    "provider": managed["provider"],
                    "model": managed["model"],
                    "base_url": managed["base_url"],
                    "protocol": managed["protocol"],
                    "options": dict(managed.get("options") or {}),
                })
                entry["allowed_models"] = managed.get("allowed_models")

            if entry["provider"]:
                metadata = provider_metadata(
                    _REGISTRY_GROUP[channel], entry["provider"])
                entry["broker_adapter"] = metadata["broker_adapter"]
                entry["override_allowed"] = (
                    metadata["override_allowed"] and channel != "chat")
                # 支持矩阵必须反映实际 adapter，而不是只看 metadata 非空
                try:
                    from ..providers.openai import adapter_supported
                    entry["broker_supported"] = adapter_supported(
                        _REGISTRY_GROUP[channel], entry["provider"])
                except (ImportError, KeyError):
                    entry["broker_supported"] = False
            if channel == "chat" and self._model_catalog is not None:
                # 公共目录只暴露锁定 Harness 已验证的模型；旧版本中越界
                # 的 allowed_models 不再出现在可选项里。
                entry["model_catalog"] = list(self._model_catalog)
                allowed = entry.get("allowed_models")
                if isinstance(allowed, list):
                    entry["allowed_models"] = [
                        model for model in allowed
                        if model in self._model_catalog]
            channels[channel] = entry

        return {
            "version": latest_version,
            "channels": channels,
            "providers": {
                channel: self._provider_schema(channel)
                for channel in ADMIN_CHANNELS
            },
        }
