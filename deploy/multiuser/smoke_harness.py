#!/usr/bin/env python3
"""A5 构建期真实 Harness ACP 冒烟。

以最终镜像内的固定 CLI、公开 ``acp`` profile 和受管 overlay 启动真实
DeepSeek Harness 子进程，完成 ``initialize`` 与 ``session/new``，并确认
默认模型路由确实是本空间 broker 的 ``easel-broker/deepseek-chat``。

这里不发起模型请求，也不读取任何供应商 Key；broker URL/token 只是显式
测试值，会话与 profile 写在临时 ``DSH_HOME``，不进入镜像或持久目录。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

DEFAULT_TIMEOUT = 180.0


class SmokeError(RuntimeError):
    """真实 Harness 未能按 A5 契约启动 ACP。"""


def _current_model_value(result: object) -> str | None:
    options = result.get("configOptions") if isinstance(result, dict) else None
    if not isinstance(options, list):
        return None
    option = next(
        (item for item in options
         if isinstance(item, dict) and item.get("category") == "model"), None)
    return option.get("currentValue") if isinstance(option, dict) else None


async def _call(process, identifier: int, method: str, params: dict,
                timeout: float) -> dict:
    request = {"jsonrpc": "2.0", "id": identifier, "method": method,
               "params": params}
    process.stdin.write(
        (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
    await process.stdin.drain()
    while True:
        line = await asyncio.wait_for(process.stdout.readline(), timeout)
        if not line:
            raise SmokeError(f"ACP process closed during {method}")
        try:
            message = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            raise SmokeError("ACP process wrote invalid JSON") from None
        if not isinstance(message, dict) or message.get("id") != identifier:
            # initialize/session/new 阶段不接受反向客户端请求；忽略通知即可。
            continue
        if "error" in message:
            error = message.get("error")
            code = error.get("code") if isinstance(error, dict) else "?"
            raise SmokeError(f"ACP {method} failed: {code}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise SmokeError(f"ACP {method} returned no object")
        return result


def _model_value(provider: str, model: str) -> str:
    return json.dumps([provider, model], separators=(",", ":"))


async def _run(command: list[str], workspace: Path, home: Path,
                provider: str, default_model: str, models: list[str],
                timeout: float) -> dict:
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "DSH_HOME": str(home / ".dsh"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "EASEL_SERVICE_MODE": "worker",
        "EASEL_CODE_ROOT": "/opt/easel",
        "EASEL_WORKSPACE_ROOT": str(workspace),
        "EASEL_STATE_ROOT": str(home / "state"),
        "EASEL_BROKER_URL": "http://127.0.0.1:9",
        "EASEL_BROKER_TOKEN": "smoke-broker-token-not-a-secret",
        "EASEL_WORKSPACE_ID": "a" * 32,
        "EASEL_RUN_ID": "smoke-run",
    }
    process = await asyncio.create_subprocess_exec(
        *command, cwd=str(workspace), env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=4 * 1024 * 1024)
    try:
        initialized = await _call(
            process, 1, "initialize", {
                "protocolVersion": 1,
                "clientInfo": {"name": "easel-a5-smoke", "version": "1"},
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
            }, timeout)
        if initialized.get("protocolVersion") != 1:
            raise SmokeError("Harness did not negotiate ACP v1")
        capabilities = initialized.get("agentCapabilities")
        if not isinstance(capabilities, dict):
            raise SmokeError("Harness returned no agentCapabilities")
        sessions = capabilities.get("sessionCapabilities")
        if not isinstance(sessions, dict) or "resume" not in sessions:
            raise SmokeError("Harness does not advertise native session resume")

        created = await _call(
            process, 2, "session/new",
            {"cwd": str(workspace), "mcpServers": []}, timeout)
        session_id = created.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise SmokeError("Harness returned no native session id")
        options = created.get("configOptions")
        if not isinstance(options, list):
            raise SmokeError("Harness returned no session config options")
        model = next(
            (option for option in options
             if isinstance(option, dict)
             and option.get("category") == "model"), None)
        if model is None:
            raise SmokeError("Harness returned no model config option")
        expected = _model_value(provider, default_model)
        if model.get("currentValue") != expected:
            raise SmokeError(
                "managed ACP profile did not select the Easel broker route")
        choices: dict[str, object] = {}
        for group in model.get("options") or []:
            if not isinstance(group, dict):
                continue
            for choice in group.get("options") or []:
                if (isinstance(choice, dict)
                        and isinstance(choice.get("value"), str)):
                    choices[choice["value"]] = choice
        selected: list[str] = []
        for index, candidate in enumerate(models):
            value = _model_value(provider, candidate)
            if value not in choices:
                raise SmokeError(
                    f"managed ACP profile did not expose {candidate}")
            result = await _call(
                process, 10 + index, "session/set_config_option", {
                    "sessionId": session_id, "configId": model.get("id"),
                    "value": value}, timeout)
            if _current_model_value(result) != value:
                raise SmokeError(
                    f"managed ACP profile could not select {candidate}")
            selected.append(f"{provider}/{candidate}")

        await _call(process, 3, "session/close", {"sessionId": session_id},
                    timeout)
        return {"session": session_id,
                "model": f"{provider}/{default_model}",
                "models": selected}
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 10)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        stderr = b""
        if process.stderr is not None:
            try:
                stderr = await asyncio.wait_for(process.stderr.read(), 1)
            except asyncio.TimeoutError:
                pass
        if process.returncode not in (0, -15, -9):
            text = stderr.decode("utf-8", "replace")[-4000:]
            print(f"HARNESS_ACP_SMOKE_STDERR:\n{text}", file=sys.stderr)


def run_smoke(manifest_path: str | Path, *,
              timeout: float = DEFAULT_TIMEOUT) -> dict:
    """按 Easel manifest 启动真实 Harness，完成 initialize/session/new。"""
    path = Path(manifest_path)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        raise SmokeError("managed ACP manifest is unreadable") from None
    command = manifest.get("command") if isinstance(manifest, dict) else None
    if (not isinstance(command, list) or not command
            or any(not isinstance(part, str) or not part for part in command)):
        raise SmokeError("managed ACP command is invalid")
    provider = manifest.get("provider") if isinstance(manifest, dict) else None
    if not isinstance(provider, dict):
        raise SmokeError("managed ACP provider is invalid")
    provider_id = provider.get("id")
    default_model = provider.get("model")
    models = provider.get("models")
    if (not isinstance(provider_id, str) or not provider_id
            or not isinstance(default_model, str) or not default_model
            or not isinstance(models, list) or not models
            or any(not isinstance(item, str) or not item for item in models)
            or default_model not in models):
        raise SmokeError("managed ACP model catalog is invalid")
    executable = shutil.which(command[0])
    if executable is None:
        raise SmokeError("managed ACP executable is not on PATH")
    argv = [executable, *command[1:]]
    patch = next((Path(part) for index, part in enumerate(command)
                  if index > 0 and command[index - 1] == "--patch"), None)
    if patch is None or not patch.is_file():
        raise SmokeError("managed ACP patch is missing")

    with tempfile.TemporaryDirectory(prefix="easel-harness-smoke-") as root:
        base = Path(root)
        workspace = base / "workspace"
        home = base / "home"
        workspace.mkdir()
        home.mkdir()
        (home / ".dsh").mkdir()
        (home / "state").mkdir()
        details = asyncio.run(_run(argv, workspace, home, provider_id,
                                   default_model, models, timeout))
    return details


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="easel-harness-smoke")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)
    try:
        details = run_smoke(args.manifest, timeout=args.timeout)
    except SmokeError as error:
        print(f"harness_acp_smoke_failed: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - build gate 必须失败关闭
        print(f"harness_acp_smoke_failed: {type(error).__name__}",
              file=sys.stderr)
        return 1
    print("harness_acp_smoke=ok model=" + details["model"]
          + " models=" + ",".join(details["models"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
