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

from .store import Store
from .types import DomainError, EffectiveRoute, Identity

_SHARED_SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "shared" / "scripts"
if str(_SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SHARED_SCRIPTS))

from model_registry import MODEL_GROUPS, provider_metadata  # noqa: E402

MASTER_KEY_BYTES = 32
CHANNELS = ("speech", "image", "video", "music")
_REGISTRY_GROUP = {"speech": "voice", "image": "image", "video": "video",
                   "music": "music"}
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

    def __init__(self, store: Store, master_key: bytes):
        if (not isinstance(master_key, (bytes, bytearray))
                or len(master_key) != MASTER_KEY_BYTES):
            raise ValueError(
                f"master_key must be exactly {MASTER_KEY_BYTES} bytes")
        self._store = store
        self._key = bytes(master_key)
        self._verify_existing_ciphertext()

    def with_store(self, store: Store) -> "ConfigService":
        """在同一 master key 下为应用线程重建服务；会重新校验已有密文。"""
        return ConfigService(store, self._key)

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

        public_defaults: dict[str, dict] = {}
        secret_bundle: dict[str, dict] = {}
        for channel, route in defaults.items():
            _require_channel(channel)
            public = _validate_complete_route(
                channel, route, allow_allowed_models=True,
                for_admin_default=True)
            metadata = provider_metadata(_REGISTRY_GROUP[channel],
                                         public["provider"])
            if metadata["broker_adapter"] is None:
                raise DomainError("adapter_unavailable", 400)
            secret = route.get("secret")
            if _secret_is_empty(secret):
                raise DomainError("invalid_secret_bundle", 400)
            secret_bundle[channel] = _normalize_secret(
                channel, public["provider"], secret,
                base_url=public["base_url"])
            public_defaults[channel] = public

        new_version = expected_version + 1
        encrypted = self._encrypt(secret_bundle,
                                  self._managed_scope(new_version))
        public_json = _canonical({"defaults": public_defaults})
        return self._store.publish_managed_revision(
            actor, public_json, encrypted, expected_version, self._now())

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
        _require_channel(channel)
        self._store.clear_workspace_override(actor, channel, self._now())

    # ------------------------------------------------------------------
    # 解析与脱敏视图
    # ------------------------------------------------------------------

    def resolve(self, actor: Identity, channel: str) -> EffectiveRoute:
        _require_channel(channel)
        identity = self._store.require_identity(actor)
        workspace_id = identity.workspace_id
        override = self._store.get_workspace_override(workspace_id, channel)

        if override is not None:
            try:
                public = json.loads(override["public_json"])
                if not isinstance(public, dict):
                    raise ValueError
            except (TypeError, ValueError):
                raise DomainError("configuration_invalid", 500) from None
            if override["route_kind"] == "complete":
                self._decrypt(
                    override["encrypted_secrets"],
                    self._workspace_scope(workspace_id, channel))
                return EffectiveRoute(
                    channel=channel,
                    provider=public["provider"],
                    model=public["model"],
                    base_url=public["base_url"],
                    protocol=public["protocol"],
                    credential_ref=(
                        f"workspace:{workspace_id}:{channel}:{override['version']}"),
                    source="user",
                    version=int(override["version"]),
                    options=dict(public.get("options") or {}),
                )
            if override["route_kind"] == "managed-model":
                defaults, version = self._load_managed_defaults(verify=True)
                managed = defaults.get(channel)
                if managed is None:
                    raise DomainError("not_configured", 404)
                model = public.get("model") or managed["model"]
                if not _model_allowed(managed, model):
                    raise DomainError("model_not_allowed", 409)
                options = {**(managed.get("options") or {}),
                           **(public.get("options") or {})}
                return EffectiveRoute(
                    channel=channel,
                    provider=managed["provider"],
                    model=model,
                    base_url=managed["base_url"],
                    protocol=managed["protocol"],
                    credential_ref=f"managed:{version}:{channel}",
                    source="user",
                    version=version,
                    options=options,
                )
            raise DomainError("configuration_invalid", 500)

        defaults, version = self._load_managed_defaults(verify=True)
        managed = defaults.get(channel)
        if managed is None:
            raise DomainError("not_configured", 404)
        return EffectiveRoute(
            channel=channel,
            provider=managed["provider"],
            model=managed["model"],
            base_url=managed["base_url"],
            protocol=managed["protocol"],
            credential_ref=f"managed:{version}:{channel}",
            source="admin",
            version=version,
            options=dict(managed.get("options") or {}),
        )

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
        for channel in CHANNELS:
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
                "override_allowed": True,
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
                entry["override_allowed"] = metadata["override_allowed"]
            channels[channel] = entry

        return {"version": latest_version, "channels": channels}
