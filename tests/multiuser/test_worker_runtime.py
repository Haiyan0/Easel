"""Task 6：代码根接线、受管 ACP 命令、RPC namespace 与 token 轮换续接。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import web.app as web
from easel.control.types import DomainError, WorkspacePaths
from easel.harnesses import store
from easel.harnesses.acp import ACPAdapter
from easel.harnesses.base import HarnessError, RunRequest
from easel.harnesses.commands import acp_command
from easel.harnesses.rpc import close_sessions, connection
from easel.worker.config import (WorkerConfigSnapshot, sign_worker_snapshot)
from easel.worker.guard import WorkerIdentity
from easel.worker.sessions import WorkspaceSessionStore

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "harness_rpc.py"


def workspace_roots(tmp_path: Path) -> tuple[Path, Path]:
    code = tmp_path / "code"
    workspace = tmp_path / "workspace"
    source = code / "agents" / "workspace"
    source.mkdir(parents=True)
    (source / "AGENTS.md").write_text("共享业务规则", encoding="utf-8")
    (source / "SOUL.md").write_text("共享人格", encoding="utf-8")
    for directory in (code / "skills" / "builtin",
                      code / "skills" / "shared" / "scripts",
                      code / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")):
        directory.mkdir(parents=True, exist_ok=True)
    (workspace / "outputs").mkdir(parents=True)
    (workspace / "profiles").mkdir(parents=True)
    return code, workspace


def test_worker_binding_refuses_legacy_unowned_file(tmp_path, monkeypatch):
    code, workspace = workspace_roots(tmp_path)
    paths = WorkspacePaths(
        code_root=code, workspace_root=workspace,
        state_root=tmp_path / "state", home=tmp_path / "home")
    monkeypatch.setattr(web, "_RUNTIME_PATHS", paths)
    monkeypatch.setattr(web, "_WORKER_SNAPSHOT", WorkerConfigSnapshot(
        "workspace-a", "run-a", 1, {}, {}))
    monkeypatch.setattr(web, "_WORKER_IDENTITY",
                        WorkerIdentity("workspace-a", "service-token"))
    monkeypatch.setattr(web, "SESSIONS_DIR", paths.state_root / "sessions")
    legacy = web.harness_store.binding_path(web.SESSIONS_DIR, "legacy")
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({
        "runtime": "acp", "native_id": "native-old"}), encoding="utf-8")

    with pytest.raises(web.HTTPException) as rejected:
        web._session_binding("legacy")
    assert rejected.value.status_code == 409
    assert "旧版无归属" in rejected.value.detail

    with pytest.raises(web.HTTPException) as invalid:
        web._session_binding("../escape")
    assert invalid.value.status_code == 400


def test_web_worker_startup_verifies_and_admits_snapshot_once(tmp_path, monkeypatch):
    private = Ed25519PrivateKey.generate()
    now = int(time.time())
    envelope = sign_worker_snapshot(
        private, workspace_id="workspace-a", run_id="run-a", revision=2,
        routes={"broker_token": "run-token"},
        agent={"runtime": "acp",
               "command": [sys.executable, str(FIXTURE), "acp"]},
        expires_at=now + 60, nonce="startup-nonce")
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(envelope), encoding="utf-8")
    public_path = tmp_path / "worker.pem"
    public_path.write_bytes(private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo))
    paths = WorkspacePaths(code_root=tmp_path / "code",
                           workspace_root=tmp_path / "workspace",
                           state_root=tmp_path / "state",
                           home=tmp_path / "home")
    monkeypatch.setattr(web, "_WORKER_IDENTITY",
                        WorkerIdentity("workspace-a", "service-token"))
    monkeypatch.setattr(web, "_RUNTIME_PATHS", paths)
    monkeypatch.setenv("EASEL_WORKER_SNAPSHOT_FILE", str(snapshot_path))
    monkeypatch.setenv("EASEL_WORKER_PUBLIC_KEY_FILE", str(public_path))
    monkeypatch.setenv("EASEL_RUN_ID", "run-a")

    snapshot, admission = web._load_worker_runtime()
    assert snapshot.workspace_id == "workspace-a"
    assert snapshot.agent["runtime"] == "acp"
    assert admission["duplicate"] is False
    _, duplicate = web._load_worker_runtime()
    assert duplicate["duplicate"] is True

    tampered = {**envelope, "revision": 3}
    snapshot_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(DomainError):
        web._load_worker_runtime()


def test_acp_managed_command_ignores_writable_workspace_file(tmp_path, monkeypatch):
    cwd = tmp_path / "workspace"
    store.write_json(cwd / "outputs" / "_harness" / "acp.json",
                     {"command": ["evil-agent", "acp"]})
    resolved: list[str] = []

    def which(command):
        resolved.append(command)
        return {"managed-agent": "/usr/bin/managed-agent",
                "evil-agent": "/usr/bin/evil-agent"}.get(command)

    monkeypatch.setattr("easel.harnesses.commands.shutil.which", which)
    command = acp_command(
        cwd, managed_command=("managed-agent", "acp", "x;echo ignored"))
    assert command == ("/usr/bin/managed-agent", "acp", "x;echo ignored")
    assert "evil-agent" not in resolved

    with pytest.raises(HarnessError):
        acp_command(cwd, managed_command=("managed-agent", "bad\narg"))
    with pytest.raises(HarnessError):
        acp_command(cwd, managed_command=("managed.cmd", "acp"))


def test_acp_worker_without_managed_command_does_not_read_writable_file(tmp_path, monkeypatch):
    cwd = tmp_path / "workspace"
    store.write_json(cwd / "outputs" / "_harness" / "acp.json",
                     {"command": ["evil-agent", "acp"]})

    def forbidden(*args, **kwargs):
        raise AssertionError("worker 模式不得读取可写 ACP 文件")

    monkeypatch.setattr(store, "read_json", forbidden)
    request = RunRequest("hello", cwd, {}, workspace_id="workspace-a",
                         code_root=tmp_path / "code")
    with pytest.raises(HarnessError, match="受管"):
        asyncio.run(ACPAdapter().run(
            request, lambda *event: None,
            lambda questions: (_ for _ in ()).throw(AssertionError()),
            lambda identifier: None))


def test_web_worker_uses_managed_command_and_clean_env(tmp_path, monkeypatch):
    code, workspace = workspace_roots(tmp_path)
    paths = WorkspacePaths(
        code_root=code, workspace_root=workspace,
        state_root=tmp_path / "state", home=tmp_path / "home")
    managed_command = (sys.executable, str(FIXTURE), "acp")
    snapshot = WorkerConfigSnapshot(
        "workspace-a", "run-a", 9,
        {"broker_url": "http://broker.internal",
         "broker_token": "run-token-sentinel"},
        {"runtime": "acp", "command": list(managed_command),
         "model": "managed-model", "sandbox": "read-only",
         "codex_auto_approve": False},
        nonce="managed-nonce", expires_at=int(time.time()) + 3600)
    monkeypatch.setattr(web, "_WORKER_SNAPSHOT", snapshot)
    monkeypatch.setattr(web, "_RUNTIME_PATHS", paths)
    monkeypatch.setattr(web, "SESSIONS_DIR", paths.state_root / "sessions")
    monkeypatch.setattr(web, "OUTPUTS_DIR", workspace / "outputs")
    monkeypatch.setattr(web, "_session_locks", {})
    monkeypatch.setattr(web, "_RUNNING_CHAT", {})
    monkeypatch.setattr(web, "_HARNESS_TASKS", set())
    monkeypatch.setattr(web, "_STOPPED_CHAT", set())
    monkeypatch.setenv("EASEL_AGENT_RUNTIME", "codex")
    monkeypatch.setenv("OPENAI_API_KEY", "admin-key-sentinel")
    # 可写工作区里的底座配置和恶意 ACP 文件都不能改变受管选择
    store.write_json(workspace / "outputs" / "_harness" / "config.json",
                     {"runtime": "codex", "models": {"codex": "user-model"},
                      "sandbox": "workspace-write"})
    store.write_json(workspace / "outputs" / "_harness" / "acp.json",
                     {"command": ["evil-agent", "acp"]})
    captured: dict = {}

    class Fake:
        async def run(self, request, emit, ask, bind):
            captured["request"] = request
            bind("native-session")

    monkeypatch.setattr(web.harnesses, "adapter", lambda runtime: Fake())
    config = web._harness_settings()
    assert config["runtime"] == "acp"
    assert config["models"]["acp"] == "managed-model"
    assert config["sandbox"] == "read-only"

    async def scenario():
        events = []
        await web._run_harness_chat(
            web.ChatRequest(message="hello", sessionId="worker-session"),
            "worker-session", "hello", "acp", config,
            lambda kind, text: events.append((kind, text)))
        return events

    events = asyncio.run(scenario())
    request = captured["request"]
    assert request.command == managed_command
    assert request.workspace_id == "workspace-a"
    assert request.code_root == paths.code_root.resolve()
    assert request.cwd == paths.workspace_root.resolve()
    assert request.model == "managed-model"
    assert request.sandbox == "read-only"
    assert request.env["HOME"] == str(paths.home)
    assert request.env["DSH_HOME"] == str(paths.home / ".dsh")
    assert request.env["DSH_AGENTS_HOME"] == str(paths.code_root)
    assert request.env["EASEL_BROKER_TOKEN"] == "run-token-sentinel"
    assert "admin-key-sentinel" not in " ".join(request.env.values())
    assert "OPENAI_API_KEY" not in request.env
    assert not any(kind == "error" for kind, _ in events)
    binding = WorkspaceSessionStore(
        paths.state_root, "workspace-a").binding("worker-session")
    assert binding["native_id"] == "native-session"
    assert binding["workspace_id"] == "workspace-a"
    assert binding["agent_version"] == web.NATIVE_BINDING_VERSION
    assert binding["cwd"] == str(paths.workspace_root.resolve())


def test_acp_worker_uses_managed_command_end_to_end(tmp_path):
    cwd = tmp_path / "workspace"
    store.write_json(cwd / "outputs" / "_harness" / "acp.json",
                     {"command": ["evil-agent", "acp"]})

    async def reject(questions):
        raise AssertionError("No permissions expected")

    async def scenario():
        request = RunRequest(
            "hello", cwd,
            {**os.environ, "FAKE_HARNESS_MODE": "normal",
             "FAKE_HARNESS_AUDIT": str(tmp_path / "audit.jsonl")},
            command=(sys.executable, str(FIXTURE), "acp"), timeout=10,
            session_key="managed-command", workspace_id="workspace-a")
        identifiers = []
        try:
            await ACPAdapter().run(
                request, lambda *event: None, reject, identifiers.append)
        finally:
            await close_sessions()
        assert identifiers == ["native-session"]
        methods = [json.loads(line).get("method") for line in
                   (tmp_path / "audit.jsonl").read_text().splitlines()]
        assert "initialize" in methods
        assert "session/new" in methods
        assert "session/prompt" in methods

    asyncio.run(scenario())


def test_fake_harness_receives_explicit_home_command_and_namespace(tmp_path):
    async def respond(method, params):
        raise AssertionError("No client requests expected")

    async def scenario():
        home = tmp_path / "home"
        request = RunRequest(
            "", tmp_path,
            {**os.environ, "HOME": str(home), "DSH_HOME": str(home / ".dsh"),
             "DSH_AGENTS_HOME": str(tmp_path / "code"),
             "EASEL_WORKSPACE_ID": "workspace-a",
             "FAKE_HARNESS_MODE": "normal",
             "FAKE_HARNESS_AUDIT": str(tmp_path / "audit.jsonl")},
            command=(sys.executable, str(FIXTURE), "acp"), timeout=10,
            session_key="runtime-probe", workspace_id="workspace-a")
        try:
            async with connection(request, list(request.command),
                                  lambda *args: None, respond) as rpc:
                result = await rpc.call("runtime", {})
        finally:
            await close_sessions()
        assert Path(result["cwd"]).resolve() == tmp_path.resolve()
        assert result["home"] == str(home)
        assert result["dsh_home"] == str(home / ".dsh")
        assert result["dsh_agents_home"] == str(tmp_path / "code")
        assert result["workspace"] == "workspace-a"
        assert Path(result["command"][0]).name == "harness_rpc.py"
        assert result["command"][1] == "acp"

    asyncio.run(scenario())


def test_rpc_pool_namespaces_same_session_key_and_native_id(tmp_path):
    async def respond(method, params):
        raise AssertionError("No client requests expected")

    async def scenario():
        base = RunRequest(
            "", tmp_path, {**os.environ, "FAKE_HARNESS_MODE": "normal",
                           "FAKE_HARNESS_AUDIT": str(tmp_path / "audit.jsonl")},
            command=(sys.executable, str(FIXTURE), "acp"), timeout=10,
            session_key="same-session")
        request_a = replace(base, workspace_id="workspace-a", native_id="native-x")
        request_b = replace(base, workspace_id="workspace-b", native_id="native-x")
        async with connection(request_a, list(request_a.command),
                              lambda *args: None, respond) as rpc_a:
            async with connection(request_b, list(request_b.command),
                                  lambda *args: None, respond) as rpc_b:
                assert rpc_a.process.pid != rpc_b.process.pid
                process_a, process_b = rpc_a.process, rpc_b.process
        try:
            await close_sessions("same-session", workspace_id="workspace-a")
            assert process_a.returncode is not None
            assert process_b.returncode is None
        finally:
            await close_sessions()

    asyncio.run(scenario())


def test_token_rotation_rebuilds_process_and_resumes_native_session(tmp_path):
    async def reject(questions):
        raise AssertionError("No permissions expected")

    async def scenario():
        request = RunRequest(
            "hello", tmp_path,
            {**os.environ, "FAKE_HARNESS_MODE": "resume-only",
             "FAKE_HARNESS_AUDIT": str(tmp_path / "audit.jsonl"),
             "EASEL_BROKER_TOKEN": "token-1"},
            command=(sys.executable, str(FIXTURE), "acp"), timeout=10,
            session_key="rotated-token", workspace_id="workspace-a")
        adapter = ACPAdapter()
        identifiers = []
        try:
            for token in ("token-1", "token-2"):
                await adapter.run(
                    replace(request, env={**request.env,
                                          "EASEL_BROKER_TOKEN": token},
                            native_id=identifiers[-1] if identifiers else ""),
                    lambda *event: None, reject, identifiers.append)
            assert identifiers == ["native-session", "native-session"]
            messages = [json.loads(line) for line in
                        (tmp_path / "audit.jsonl").read_text().splitlines()]
            methods = [message.get("method") for message in messages]
            assert methods.count("initialize") == 2
            assert methods.count("session/new") == 1
            assert methods.count("session/resume") == 1
            assert methods.count("session/prompt") == 2
        finally:
            await close_sessions()

    asyncio.run(scenario())
