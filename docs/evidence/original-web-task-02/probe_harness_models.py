"""Task 2 只读探针：真实锁定 Harness 的 ACP 模型目录与选择值形状。

在 worker 镜像内运行；只使用临时 HOME/workspace 与假 broker token，不发起
模型请求、不读取真实 Key、不接触用户容器/卷。输出只含公开模型目录与
session/config 结果，用于确定 modelId 的真实格式。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

PROVIDER = "easel-broker"
MODELS = ("deepseek-chat", "deepseek-flash", "deepseek-v4-pro")
TIMEOUT = 60.0


def _patch_text() -> str:
    source = Path("/opt/easel/managed/acp.patch.yml").read_text("utf-8")
    head, separator, _tail = source.partition("        models:\n")
    if not separator:
        raise SystemExit("patch_models_section_missing")
    rows = []
    for model in MODELS:
        rows.append(
            f"          - id: {model}\n"
            f"            name: {model} (Easel broker)\n"
            "            contextWindow: 65536\n"
            "            maxTokens: 4096\n"
            "            reasoningEfforts: false\n")
    return head + separator + "".join(rows)


async def _call(process, identifier: int, method: str, params: dict) -> dict:
    request = {"jsonrpc": "2.0", "id": identifier, "method": method,
               "params": params}
    process.stdin.write(
        (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
    await process.stdin.drain()
    while True:
        line = await asyncio.wait_for(process.stdout.readline(), TIMEOUT)
        if not line:
            raise RuntimeError(f"closed during {method}")
        message = json.loads(line)
        if not isinstance(message, dict) or message.get("id") != identifier:
            continue
        if "error" in message:
            return {"method": method, "error": message["error"]}
        return {"method": method, "result": message.get("result")}


async def _run() -> dict:
    with tempfile.TemporaryDirectory(prefix="easel-task2-probe-") as root:
        base = Path(root)
        workspace = base / "workspace"
        home = base / "home"
        workspace.mkdir()
        (home / ".dsh").mkdir(parents=True)
        (home / "state").mkdir()
        patch = base / "acp.patch.yml"
        patch.write_text(_patch_text(), encoding="utf-8")
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
            "node", "/opt/dsh/apps/cli/lib/bin.js", "--profile", "acp",
            "--patch", str(patch), cwd=str(workspace), env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=4 * 1024 * 1024)
        try:
            initialized = await _call(process, 1, "initialize", {
                "protocolVersion": 1,
                "clientInfo": {"name": "easel-task2-probe", "version": "1"},
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
            })
            created = await _call(process, 2, "session/new", {
                "cwd": str(workspace), "mcpServers": []})
            result = created.get("result") or {}
            options = result.get("configOptions") or []
            model_option = next(
                (item for item in options
                 if isinstance(item, dict) and item.get("category") == "model"),
                None)
            selections = []
            session_id = result.get("sessionId")
            choices = []
            if isinstance(model_option, dict):
                for group in model_option.get("options") or []:
                    for choice in (group or {}).get("options") or []:
                        choices.append(choice)
            for model in MODELS:
                wanted = json.dumps([PROVIDER, model], separators=(",", ":"))
                selected = next(
                    (choice for choice in choices
                     if choice.get("value") == wanted), None)
                if selected is None:
                    selections.append({"model": model, "value": wanted,
                                       "available": False})
                    continue
                outcome = await _call(process, 10 + len(selections),
                                      "session/set_config_option", {
                                          "sessionId": session_id,
                                          "configId": model_option.get("id"),
                                          "value": wanted,
                                      })
                selections.append({
                    "model": model, "value": wanted, "available": True,
                    "result": outcome})
            if session_id:
                await _call(process, 99, "session/close", {
                    "sessionId": session_id})
            return {
                "initialize": initialized,
                "modelOption": model_option,
                "choices": choices,
                "selections": selections,
            }
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            stderr = await asyncio.wait_for(process.stderr.read(), 5)
            if stderr:
                sys.stderr.write(stderr.decode("utf-8", "replace")[-2000:])


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        result = asyncio.run(_run())
    except Exception as error:  # noqa: BLE001 - 探针失败需原样输出类型
        print(json.dumps({"probe_failed": type(error).__name__,
                          "message": str(error)[:500]},
                         ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
