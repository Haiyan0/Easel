"""首批 broker 协议适配实现。

支持矩阵（精确 method + 相对 path，均相对配置的 base URL）：

* chat/deepseek（DeepSeek ACP 主模型，OpenAI-compatible 协议）
  - POST ``chat/completions``（JSON）
  - GET  ``models``（模型列表）
* image/openai（OpenAI-compatible）
  - POST ``images/generations``（JSON）
  - GET  ``models``
* speech/openai-compatible（TTS/ASR）
  - POST ``audio/speech``（JSON，二进制响应）
  - POST ``audio/transcriptions``（multipart）
  - GET  ``models``

未列出的 method/path 组合一律拒绝；不使用 ``/v1`` 前缀泛放行。
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from ..control.types import DomainError, EffectiveRoute
from .adapters import AdapterRoute, ProviderAdapter, safe_header_name

_SHARED_SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "shared" / "scripts"
if str(_SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SHARED_SCRIPTS))

from model_registry import MODEL_GROUPS, provider_metadata  # noqa: E402

CHANNEL_GROUP = {
    "speech": "voice",
    "image": "image",
    "video": "video",
    "music": "music",
    "chat": "chat",
}

BROKER_ROUTE_TABLE: tuple[tuple[str, str, str, str], ...] = (
    ("chat", "POST", "chat/completions", "json"),
    ("chat", "GET", "models", "none"),
    ("image", "POST", "images/generations", "json"),
    ("image", "GET", "models", "none"),
    ("speech", "POST", "audio/speech", "json"),
    ("speech", "POST", "audio/transcriptions", "multipart"),
    ("speech", "GET", "models", "none"),
)
"""control 实际注册的 broker 路由：channel → method + 相对 path + body 类型。"""


class DeepSeekChatAdapter(ProviderAdapter):
    """DeepSeek ACP 主模型：OpenAI-compatible chat completions。"""

    adapter_id = "deepseek-acp"
    protocols = frozenset({"openai"})
    auth_env = "CHAT_API_KEY"
    routes = (
        AdapterRoute("POST", "chat/completions", "json"),
        AdapterRoute("GET", "models", "none"),
    )


class OpenAIImageAdapter(ProviderAdapter):
    """image/openai：同步生图，支持自定义鉴权头与 api-version。"""

    adapter_id = "openai-compatible"
    protocols = frozenset({"openai"})
    auth_env = "IMG_API_KEY"
    routes = (
        AdapterRoute("POST", "images/generations", "json"),
        AdapterRoute("GET", "models", "none"),
    )

    def _auth_headers(self) -> dict[str, str]:
        key = self._credential(self.auth_env)
        header_name = self._credentials.get("IMG_API_KEY_HEADER", "")
        if header_name:
            safe = safe_header_name(header_name)
            if safe is None:
                raise DomainError("secret_unavailable", 500)
            if safe.lower() != "authorization":
                return {safe: key}
        return {"authorization": f"Bearer {key}"}

    def _adjust_url(self, url: str) -> str:
        version = self._credentials.get("IMG_API_VERSION", "")
        if not version:
            return url
        if any(char in version for char in "\r\n\x00#"):
            raise DomainError("secret_unavailable", 500)
        parsed = urlsplit(url)
        query = f"api-version={quote(version, safe='')}"
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query,
                           parsed.fragment))


class OpenAISpeechAdapter(ProviderAdapter):
    """speech/openai-compatible：TTS 与 ASR。"""

    adapter_id = "openai-compatible"
    protocols = frozenset({"openai"})
    auth_env = "VOICE_API_KEY"
    routes = (
        AdapterRoute("POST", "audio/speech", "json"),
        AdapterRoute("POST", "audio/transcriptions", "multipart"),
        AdapterRoute("GET", "models", "none"),
    )


_ADAPTERS: dict[tuple[str, str], type[ProviderAdapter]] = {
    ("chat", "deepseek"): DeepSeekChatAdapter,
    ("image", "openai"): OpenAIImageAdapter,
    ("voice", "openai-compatible"): OpenAISpeechAdapter,
}


def adapter_class(group: str, provider_id: str) -> type[ProviderAdapter] | None:
    """返回实际实现的 adapter；metadata 声称支持但没有实现时返回 None。"""
    candidate = _ADAPTERS.get((group, provider_id))
    if candidate is None:
        return None
    try:
        metadata = provider_metadata(group, provider_id)
    except KeyError:
        return None
    if metadata["broker_adapter"] != candidate.adapter_id:
        return None
    return candidate


def adapter_supported(group: str, provider_id: str) -> bool:
    return adapter_class(group, provider_id) is not None


def adapter_for_route(route: EffectiveRoute) -> type[ProviderAdapter]:
    group = CHANNEL_GROUP.get(route.channel)
    if group is None:
        raise DomainError("capability_unavailable", 503)
    candidate = adapter_class(group, route.provider)
    if candidate is None:
        raise DomainError("capability_unavailable", 503)
    return candidate


def support_matrix() -> list[dict]:
    """逐供应商输出实际 adapter 支持情况，未适配项明确 supported=False。"""
    matrix: list[dict] = []
    for group, spec in MODEL_GROUPS.items():
        for provider in spec["providers"]:
            candidate = adapter_class(group, provider["id"])
            matrix.append({
                "group": group,
                "provider": provider["id"],
                "broker_adapter": provider.get("broker_adapter"),
                "supported": candidate is not None,
                "protocols": sorted(candidate.protocols) if candidate else [],
                "routes": [
                    {"method": route.method, "path": route.relative_path}
                    for route in (candidate.routes if candidate else ())
                ],
                "override_allowed": bool(provider.get("override_allowed", False)),
            })
    return matrix
