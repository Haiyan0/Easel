"""G2 gateway 进程入口：可信本地配置、状态与激活握手。

进程启动只生成新的 boot generation 并监听固定管理地址；在受信配置到来前，
所有管理请求返回 503 ``gateway_inactive``。配置/激活只能通过容器内
0600 Unix socket 的固定命令完成，HTTP 请求不能提交镜像、命令、挂载、密钥、
worker 地址或服务 token。

本模块不复制 G1 的验签/转发代码，配置完成后直接实例化
``create_gateway_app``。状态输出不包含 worker service token 或断言。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import os
import re
import socket
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from easel.gateway.app import GatewayController, GatewayLimits, create_gateway_app
from easel.gateway.auth import (
    GatewayConfig,
    new_boot_generation,
)

CONTROL_MAX_BYTES = 64 * 1024
CONTROL_TIMEOUT_SECONDS = 5.0
DEFAULT_MANAGEMENT_PORT = 8897
DEFAULT_CONTROL_SOCKET = "/run/easel/gateway-control.sock"

_WORKSPACE = re.compile(r"^[0-9a-f]{32}$")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_BOOT = re.compile(r"^[0-9a-f]{32}$")

_CONFIG_KEYS = frozenset({
    "workspace_id", "gateway_instance_id", "expected_boot_generation",
    "worker_container_id", "config_version", "worker_origin",
    "worker_service_token",
})
_ACTIVATE_KEYS = frozenset({
    "workspace_id", "gateway_instance_id", "boot_generation",
    "worker_container_id", "config_version",
})


class GatewayControlError(Exception):
    """可信控制通道错误；只含稳定错误码，不回显配置值。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def load_public_key(path: str | Path) -> Ed25519PublicKey:
    """加载管理员固定的 Ed25519 公钥；PEM 或 32 字节裸公钥。"""
    try:
        data = Path(path).read_bytes()
    except OSError:
        raise GatewayControlError("gateway_public_key_unavailable") from None
    if not data or len(data) > 32 * 1024:
        raise GatewayControlError("gateway_public_key_invalid")
    try:
        if len(data) == 32:
            return Ed25519PublicKey.from_public_bytes(data)
        key = serialization.load_pem_public_key(data)
    except (ValueError, TypeError):
        raise GatewayControlError("gateway_public_key_invalid") from None
    if not isinstance(key, Ed25519PublicKey):
        raise GatewayControlError("gateway_public_key_invalid")
    return key


def _exact(payload: object, keys: frozenset[str]) -> dict:
    if not isinstance(payload, dict) or set(payload) != keys:
        raise GatewayControlError("gateway_config_invalid")
    return payload


def _text(payload: dict, name: str, pattern: re.Pattern[str]) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise GatewayControlError("gateway_config_invalid")
    return value


def _version(payload: dict) -> int:
    value = payload.get("config_version")
    if type(value) is not int or not 0 <= value < 2 ** 31:
        raise GatewayControlError("gateway_config_invalid")
    return value


def _origin(value: object) -> str:
    if (not isinstance(value, str) or not value or len(value) > 2048
            or any(character.isspace() or ord(character) < 32
                   for character in value)
            or "?" in value or "#" in value):
        raise GatewayControlError("gateway_config_invalid")
    try:
        config = GatewayConfig(
            workspace_id="0" * 32, gateway_instance_id="0" * 64,
            boot_generation="0" * 32, worker_container_id="0" * 64,
            config_version=0, worker_origin=value,
            worker_service_token="x")
    except ValueError:
        raise GatewayControlError("gateway_config_invalid") from None
    return config.worker_origin


def _valid_bind(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise GatewayControlError("gateway_bind_invalid") from None
    if address.version != 4 or address.is_unspecified:
        raise GatewayControlError("gateway_bind_invalid")
    return str(address)


def _inactive_response() -> bytes:
    return b'{"error":"gateway_inactive"}'


class GatewayService:
    """单进程 gateway 状态机；configure 成功后替换为 G1 app。"""

    def __init__(self, public_key: Ed25519PublicKey, *,
                 management_bind: str, management_port: int = DEFAULT_MANAGEMENT_PORT,
                 boot_generation: str | None = None,
                 clock: Callable[[], float] | None = None,
                 limits: GatewayLimits | None = None,
                 transport=None, http_client=None) -> None:
        if not isinstance(public_key, Ed25519PublicKey):
            raise TypeError("public_key must be Ed25519PublicKey")
        self.public_key = public_key
        self.management_bind = _valid_bind(management_bind)
        if type(management_port) is not int or not 1 <= management_port <= 65535:
            raise ValueError("management_port is invalid")
        self.management_port = management_port
        self.boot_generation = boot_generation or new_boot_generation()
        if _BOOT.fullmatch(self.boot_generation) is None:
            raise ValueError("boot_generation is invalid")
        self._clock = clock
        self._limits = limits
        self._transport = transport
        self._http_client = http_client
        self._config: GatewayConfig | None = None
        self._controller: GatewayController | None = None
        self._app = None
        self._lock = asyncio.Lock()

    @property
    def public_key_sha256(self) -> str:
        return hashlib.sha256(
            self.public_key.public_bytes_raw()).hexdigest()

    @property
    def configured(self) -> bool:
        return self._config is not None

    @property
    def active(self) -> bool:
        return bool(self._controller is not None and self._controller.active)

    @property
    def available(self) -> bool:
        return bool(
            self._controller is not None and self._controller.active
            and not self._controller.isolated)

    def status(self) -> dict:
        """可信本地状态；不含 worker token、断言或请求内容。"""
        config = self._config
        return {
            "gateway_instance_id": (
                config.gateway_instance_id if config else ""),
            "boot_generation": self.boot_generation,
            "workspace_id": config.workspace_id if config else "",
            "worker_container_id": (
                config.worker_container_id if config else ""),
            "config_version": config.config_version if config else 0,
            "worker_origin": config.worker_origin if config else "",
            "management_bind": self.management_bind,
            "management_port": self.management_port,
            "public_key_sha256": self.public_key_sha256,
            "configured": config is not None,
            "active": self.active,
            "available": self.available,
        }

    async def configure(self, payload: object) -> dict:
        """绑定实际实例/worker 目标；不激活，激活必须单独精确提交。"""
        data = _exact(payload, _CONFIG_KEYS)
        expected_boot = _text(
            data, "expected_boot_generation", _BOOT)
        if expected_boot != self.boot_generation:
            raise GatewayControlError("gateway_config_mismatch")
        workspace_id = _text(data, "workspace_id", _WORKSPACE)
        gateway_instance_id = _text(
            data, "gateway_instance_id", _CONTAINER_ID)
        worker_container_id = _text(
            data, "worker_container_id", _CONTAINER_ID)
        config_version = _version(data)
        worker_origin = _origin(data.get("worker_origin"))
        token = data.get("worker_service_token")
        if (not isinstance(token, str) or not token
                or len(token.encode("utf-8")) > 4096):
            raise GatewayControlError("gateway_config_invalid")
        try:
            config = GatewayConfig(
                workspace_id=workspace_id,
                gateway_instance_id=gateway_instance_id,
                boot_generation=self.boot_generation,
                worker_container_id=worker_container_id,
                config_version=config_version,
                worker_origin=worker_origin,
                worker_service_token=token)
        except ValueError:
            raise GatewayControlError("gateway_config_invalid") from None

        async with self._lock:
            if self.active:
                raise GatewayControlError("gateway_already_active")
            old = self._controller
            app = create_gateway_app(
                config, self.public_key, clock=self._clock,
                limits=self._limits, transport=self._transport,
                http_client=self._http_client, activated=False)
            self._config = config
            self._controller = app.state.gateway
            self._app = app
            if old is not None:
                await old.aclose()
        return self.status()

    async def activate(self, payload: object) -> dict:
        """只接受当前已配置的精确实例/boot/worker/config_version。"""
        data = _exact(payload, _ACTIVATE_KEYS)
        config = self._config
        controller = self._controller
        if config is None or controller is None:
            raise GatewayControlError("gateway_not_configured")
        try:
            workspace_id = _text(data, "workspace_id", _WORKSPACE)
            gateway_instance_id = _text(
                data, "gateway_instance_id", _CONTAINER_ID)
            boot_generation = _text(data, "boot_generation", _BOOT)
            worker_container_id = _text(
                data, "worker_container_id", _CONTAINER_ID)
            config_version = _version(data)
        except GatewayControlError:
            raise GatewayControlError(
                "gateway_activation_mismatch") from None
        if (
            workspace_id != config.workspace_id
            or gateway_instance_id != config.gateway_instance_id
            or boot_generation != self.boot_generation
            or worker_container_id != config.worker_container_id
            or config_version != config.config_version
        ):
            raise GatewayControlError("gateway_activation_mismatch")
        controller.activate()
        return self.status()

    async def aclose(self) -> None:
        controller = self._controller
        self._controller = None
        self._app = None
        self._config = None
        if controller is not None:
            await controller.aclose()

    async def __call__(self, scope, receive, send) -> None:
        """Uvicorn 只把带异步 ``__call__`` 的对象识别为 ASGI3。"""
        await self.asgi(scope, receive, send)

    async def asgi(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            raise RuntimeError("gateway only supports http")
        if self._app is None:
            await self._send_inactive(send)
            return
        await self._app(scope, receive, send)

    async def _lifespan(self, receive, send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await self.aclose()
                await send({"type": "lifespan.shutdown.complete"})
                return

    @staticmethod
    async def _send_inactive(send) -> None:
        body = _inactive_response()
        await send({
            "type": "http.response.start",
            "status": 503,
            "headers": [
                (b"content-type", b"application/json"),
                (b"cache-control", b"no-store"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    # -- 可信本地 Unix socket ---------------------------------------------

    async def _handle_control(self, reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter) -> None:
        try:
            peer = _peer_uid(writer)
            if peer is not None and hasattr(os, "geteuid") \
                    and peer != os.geteuid():
                await _send_control(writer, {
                    "ok": False, "error": "gateway_control_unauthorized"})
                return
            line = await asyncio.wait_for(
                reader.readline(), timeout=CONTROL_TIMEOUT_SECONDS)
            if len(line) > CONTROL_MAX_BYTES:
                await _send_control(writer, {
                    "ok": False, "error": "gateway_control_too_large"})
                return
            try:
                request = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                await _send_control(writer, {
                    "ok": False, "error": "gateway_control_invalid"})
                return
            if not isinstance(request, dict):
                await _send_control(writer, {
                    "ok": False, "error": "gateway_control_invalid"})
                return
            action = request.get("action")
            if action == "configure":
                status = await self.configure(request.get("payload"))
            elif action == "activate":
                status = await self.activate(request.get("payload"))
            elif action == "status":
                if set(request) != {"action"}:
                    raise GatewayControlError("gateway_control_invalid")
                status = self.status()
            else:
                raise GatewayControlError("gateway_control_invalid")
            await _send_control(writer, {"ok": True, "status": status})
        except GatewayControlError as error:
            await _send_control(writer, {"ok": False, "error": error.code})
        except Exception:  # noqa: BLE001 - 不回显内部异常
            await _send_control(writer, {
                "ok": False, "error": "gateway_control_failed"})
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def start_control(self, path: str | Path) -> asyncio.AbstractServer:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            if not stat.S_ISSOCK(target.stat().st_mode):
                raise GatewayControlError("gateway_control_conflict")
            target.unlink()
        server = await asyncio.start_unix_server(
            self._handle_control, path=str(target))
        os.chmod(target, 0o600)
        return server


async def _send_control(writer: asyncio.StreamWriter, payload: dict) -> None:
    data = (json.dumps(payload, ensure_ascii=False,
                       separators=(",", ":")) + "\n").encode("utf-8")
    writer.write(data)
    try:
        await writer.drain()
    except OSError:
        pass


def _peer_uid(writer: asyncio.StreamWriter) -> int | None:
    raw = writer.get_extra_info("socket")
    option = getattr(socket, "SO_PEERCRED", None)
    if raw is None or option is None:
        return None
    try:
        import struct
        value = raw.getsockopt(socket.SOL_SOCKET, option,
                               struct.calcsize("3i"))
        return int(struct.unpack("3i", value)[1])
    except OSError:
        return None


async def _control_call(path: str, payload: dict) -> dict:
    try:
        reader, writer = await asyncio.open_unix_connection(path)
    except (OSError, AttributeError, NotImplementedError):
        raise GatewayControlError("gateway_control_unavailable") from None
    try:
        writer.write((json.dumps(payload, separators=(",", ":")) + "\n")
                     .encode("utf-8"))
        await writer.drain()
        line = await asyncio.wait_for(
            reader.readline(), timeout=CONTROL_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        raise GatewayControlError("gateway_control_timeout") from None
    except OSError:
        raise GatewayControlError("gateway_control_unavailable") from None
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
    try:
        response = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GatewayControlError("gateway_control_invalid") from None
    if not isinstance(response, dict):
        raise GatewayControlError("gateway_control_invalid")
    return response


async def _serve(args) -> int:
    try:
        public_key = load_public_key(args.public_key)
        service = GatewayService(
            public_key, management_bind=args.bind,
            management_port=args.port)
        control = await service.start_control(args.control_socket)
    except GatewayControlError as error:
        print(error.code, file=sys.stderr)
        return 2
    config = uvicorn.Config(
        service, host=service.management_bind,
        port=service.management_port, proxy_headers=False,
        access_log=False, log_level="info", lifespan="on")
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        control.close()
        await control.wait_closed()
        await service.aclose()
    return 0 if server.started else 2


def _read_stdin_json() -> object:
    raw = sys.stdin.buffer.read(CONTROL_MAX_BYTES + 1)
    if len(raw) > CONTROL_MAX_BYTES:
        raise GatewayControlError("gateway_control_too_large")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GatewayControlError("gateway_control_invalid") from None


async def _cli(args) -> int:
    if args.command == "configure":
        payload = {"action": "configure", "payload": _read_stdin_json()}
    elif args.command == "activate":
        payload = {"action": "activate", "payload": _read_stdin_json()}
    else:
        payload = {"action": "status"}
    try:
        response = await _control_call(args.control_socket, payload)
    except GatewayControlError as error:
        print(json.dumps({"ok": False, "error": error.code},
                         separators=(",", ":")))
        return 1
    print(json.dumps(response, separators=(",", ":")))
    return 0 if response.get("ok") is True else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="easel-gateway")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve")
    serve.add_argument("--bind", required=True)
    serve.add_argument("--port", type=int, default=DEFAULT_MANAGEMENT_PORT)
    serve.add_argument("--public-key", required=True)
    serve.add_argument("--control-socket", default=DEFAULT_CONTROL_SOCKET)
    for name in ("configure", "activate", "status"):
        command = subparsers.add_parser(name)
        command.add_argument("--control-socket", default=DEFAULT_CONTROL_SOCKET)
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            return asyncio.run(_serve(args))
        return asyncio.run(_cli(args))
    except GatewayControlError as error:
        print(json.dumps({"ok": False, "error": error.code},
                         separators=(",", ":")), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
