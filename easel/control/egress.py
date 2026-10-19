"""受限出口策略与 HTTPX 客户端装配。

生产请求只能经独立受控出口代理发出：没有显式代理或测试注入的 transport 时
明确失败，绝不静默直连。这里负责保存/签发/实际请求共用的 URL 前置校验，以及
连接阶段由出口代理复用的解析后 IP 校验（解析与固定连接由代理脚本执行）。

安全边界：

* 只允许 HTTPS；拒绝用户信息、query/fragment（作为 base URL 时）。
* 未获管理员精确批准的主机必须在连接阶段解析为公网地址；IPv4/IPv6、
  回环、私网、链路本地、保留段、云元数据地址统一拒绝。
* 管理员可通过 ``approved_hosts`` / ``approved_networks`` 登记精确例外；
  member 没有修改策略的接口。
* 默认不跟随重定向；``trust_env=False`` 防止宿主环境代理或默认认证注入。
"""
from __future__ import annotations

import ipaddress
import json
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlsplit

import httpx

from .types import DomainError

_DENIED_HOSTNAMES = frozenset({
    "localhost",
    "metadata",
    "metadata.google.internal",
    "instance-data",
    "kubernetes.default",
    "kubernetes.default.svc",
})

DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_WRITE_TIMEOUT = 60.0


def _normalize_host(host: str) -> str:
    return host.strip().lower().rstrip(".")


def _is_denied_address(address: str) -> bool:
    """判断解析后的地址是否属于禁止范围。"""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (not ip.is_global or ip.is_multicast or ip.is_unspecified
            or ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_reserved)


def _default_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    addresses: list[str] = []
    for info in infos:
        address = info[4][0]
        if address not in addresses:
            addresses.append(address)
    return addresses


@dataclass(frozen=True)
class EgressPolicy:
    """管理员批准范围 + 公网目的地址策略。"""

    approved_hosts: frozenset[str] = frozenset()
    approved_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    resolver: Callable[[str, int], Iterable[str]] | None = field(
        default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        hosts: set[str] = set()
        for host in self.approved_hosts:
            if not isinstance(host, str) or not host.strip():
                raise ValueError("approved_hosts 必须是非空字符串")
            hosts.add(_normalize_host(host))
        object.__setattr__(self, "approved_hosts", frozenset(hosts))

        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for network in self.approved_networks:
            if isinstance(network, str):
                try:
                    network = ipaddress.ip_network(network, strict=False)
                except ValueError as error:
                    raise ValueError("approved_networks 含非法网段") from error
            if not isinstance(network, (ipaddress.IPv4Network,
                                        ipaddress.IPv6Network)):
                raise ValueError("approved_networks 含非法网段")
            networks.append(network)
        object.__setattr__(self, "approved_networks", tuple(networks))

        if self.resolver is not None and not callable(self.resolver):
            raise ValueError("resolver 必须可调用")

    def _host_approved(self, host: str) -> bool:
        return _normalize_host(host) in self.approved_hosts

    def _address_approved(self, address: str) -> bool:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        return any(ip in network for network in self.approved_networks)

    def validate_url(self, url: object, *, resolve: bool = False,
                     allow_query: bool = False) -> None:
        """校验 base URL / 上游 URL；不解析 DNS 时不产生网络副作用。"""
        if not isinstance(url, str) or not url:
            raise DomainError("invalid_destination", 400)
        if any(char.isspace() for char in url) or "\\" in url:
            raise DomainError("invalid_destination", 400)
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            port = parsed.port or 443
        except ValueError:
            raise DomainError("invalid_destination", 400) from None
        if parsed.scheme.lower() != "https":
            raise DomainError("destination_denied", 403)
        if not host:
            raise DomainError("invalid_destination", 400)
        if parsed.username is not None or parsed.password is not None:
            raise DomainError("invalid_destination", 400)
        if parsed.fragment:
            raise DomainError("invalid_destination", 400)
        if parsed.query and not allow_query:
            raise DomainError("invalid_destination", 400)

        normalized = _normalize_host(host)
        approved = self._host_approved(normalized)
        try:
            literal = ipaddress.ip_address(normalized)
        except ValueError:
            literal = None

        if literal is not None:
            if not approved and not self._address_approved(str(literal)):
                if _is_denied_address(str(literal)):
                    raise DomainError("destination_denied", 403)
        elif not approved:
            if normalized in _DENIED_HOSTNAMES or normalized.endswith(".localhost"):
                raise DomainError("destination_denied", 403)

        if resolve:
            self.resolve_and_validate(normalized, port)

    def resolve_and_validate(self, host: str, port: int) -> str:
        """在连接阶段解析并校验全部地址，返回固定连接的地址。

        出口代理必须连接到本方法返回的地址，不能再解析一次，避免校验后
        DNS 重绑定切到私网地址。
        """
        if not isinstance(host, str) or not host:
            raise DomainError("invalid_destination", 400)
        normalized = _normalize_host(host)
        try:
            literal = ipaddress.ip_address(normalized)
        except ValueError:
            literal = None

        if literal is not None:
            addresses = [str(literal)]
        else:
            resolver = self.resolver or _default_resolver
            try:
                addresses = [str(address) for address in resolver(normalized, port)]
            except OSError:
                raise DomainError("destination_unresolved", 502) from None
        if not addresses:
            raise DomainError("destination_unresolved", 502)

        approved_host = self._host_approved(normalized)
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                raise DomainError("destination_unresolved", 502) from None
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
                ip = ip.ipv4_mapped
            if approved_host or self._address_approved(str(ip)):
                continue
            if _is_denied_address(str(ip)):
                raise DomainError("destination_denied", 403)
        return addresses[0]


def _validate_proxy_url(proxy_url: object) -> str | None:
    if proxy_url is None:
        return None
    if not isinstance(proxy_url, str) or not proxy_url:
        raise ValueError("egress proxy 必须显式配置")
    try:
        parsed = urlsplit(proxy_url)
        port = parsed.port
    except ValueError as error:
        raise ValueError("egress proxy URL 非法") from error
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError("egress proxy 必须是 http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("egress proxy 不得包含用户信息")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("egress proxy 不得包含路径或参数")
    if port is None:
        raise ValueError("egress proxy 必须显式指定端口")
    return proxy_url


class EgressGateway:
    """按显式配置创建 HTTPX 客户端；绝不回退到直连。"""

    def __init__(self, policy: EgressPolicy | None = None, *,
                 proxy_url: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.policy = policy if policy is not None else EgressPolicy()
        self.proxy_url = _validate_proxy_url(proxy_url)
        self.transport = transport

    def with_transport(self, transport: httpx.AsyncBaseTransport) -> "EgressGateway":
        return EgressGateway(self.policy, proxy_url=self.proxy_url,
                             transport=transport)

    def client(self) -> httpx.AsyncClient:
        timeout = httpx.Timeout(connect=DEFAULT_CONNECT_TIMEOUT,
                                read=None,
                                write=DEFAULT_WRITE_TIMEOUT,
                                pool=DEFAULT_CONNECT_TIMEOUT)
        # 测试的 fake transport 必须显式注入；生产没有 silent direct fallback。
        if self.transport is not None:
            return httpx.AsyncClient(transport=self.transport, trust_env=False,
                                     follow_redirects=False, timeout=timeout)
        if self.proxy_url is not None:
            return httpx.AsyncClient(proxy=self.proxy_url, trust_env=False,
                                     follow_redirects=False, timeout=timeout)
        raise DomainError("egress_unavailable", 503)


def parse_proxy_authority(target: str) -> tuple[str, int]:
    """解析 CONNECT 的 host:port（含 IPv6 方括号形式）。"""
    if not isinstance(target, str) or not target:
        raise ValueError("missing authority")
    if target.startswith("["):
        end = target.find("]")
        if end < 0 or not target[end + 1:].startswith(":"):
            raise ValueError("invalid IPv6 authority")
        host = target[1:end]
        port_text = target[end + 2:]
    else:
        host, separator, port_text = target.rpartition(":")
        if not separator:
            raise ValueError("missing port")
    if not host or not port_text.isdigit():
        raise ValueError("invalid authority")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError("invalid port")
    return _normalize_host(host), port


def load_policy(path: Path | str) -> EgressPolicy:
    """读取管理员出口策略文件；缺失或结构非法时明确失败。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("egress policy 文件不可读或不是合法 JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("egress policy 必须是 JSON 对象")
    unknown = set(payload) - {"approved_hosts", "approved_cidrs"}
    if unknown:
        raise ValueError("egress policy 含未知字段")
    hosts = payload.get("approved_hosts", [])
    cidrs = payload.get("approved_cidrs", [])
    if not isinstance(hosts, list) or not all(isinstance(item, str) for item in hosts):
        raise ValueError("approved_hosts 必须是字符串数组")
    if not isinstance(cidrs, list) or not all(isinstance(item, str) for item in cidrs):
        raise ValueError("approved_cidrs 必须是字符串数组")
    try:
        networks = tuple(ipaddress.ip_network(item, strict=False) for item in cidrs)
    except ValueError as error:
        raise ValueError("approved_cidrs 含非法网段") from error
    return EgressPolicy(approved_hosts=frozenset(hosts),
                        approved_networks=networks)
